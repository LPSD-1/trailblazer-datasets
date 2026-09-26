#!/usr/bin/env python3
"""Wet-weather restraint: surface, crossed with the rain that actually fell.

Spec 9.6 C. Two halves, and the split between them is the whole design:

    # STATIC. Which lane is soft, and which gauge speaks for it. Rebuilt only
    # when the ways rebuild, and written into the container.
    python tools/build_wet.py assign --container containers/ways-midlands.tbmap \
        --stations cache/ea/stations-rainfall.json --keep out.tbmap

    # LIVE. How much rain that gauge has had. Rebuilt on the fast clock, and
    # published as its own small file.
    python tools/build_wet.py feed --region midlands \
        --stations cache/ea/stations-rainfall.json \
        --readings cache/ea/readings-rainfall.json --out published/wet

    python tools/build_wet.py --selftest

WHY THE SPLIT, AND WHY IT IS NOT A DETAIL.
Rain changes every 15 minutes; the containers are ~190 MB and a rider
re-downloads one whenever its bytes move. Writing a wet verdict into the
container would re-publish a fifth of a gigabyte every hour to say that it had
drizzled in Shropshire - the same fault `poi_staleness.py` exists to prevent,
with a faster clock behind it. So the container carries only what does not
move (which lane is soft, which gauge is nearest, how far away it is) and the
rain arrives as a file measured in kilobytes.

WHAT WE MAY AND MAY NOT CLAIM (spec 9.6 C, narrowed 2026-09-23).
Find Green Lanes already ships a rainfall overlay and a "recent rain in mm"
figure per lane. **We are not first to put rain on a map and must not say we
are.** What is ours is the join: rain tied to THIS lane's surface, offline, as
voluntary restraint. The honest sentence is "this lane is soft and it has
rained 14 mm here since Tuesday - consider leaving it", not "rainfall data".

ADVICE, NEVER A CLOSURE (spec 9.6 C, last line). Nothing here produces a
verdict that may be drawn like a traffic order, and the field is deliberately
not named anything a UI would colour red. A lane that is legally open and very
wet is legally open.

UNKNOWN SURFACE IS TREATED AS SOFT, ON PURPOSE.
`surface` is NULL on most of the network. Under WAYS-SCHEMA's rule that
"unknown is not no", an absent value must never read as a prohibition - and it
does not here, because nothing this file writes is a prohibition. It is advice,
and for advice the cautious direction is the one that protects the thing the
app sells: damage is the single most common argument for closing byways to
motors (spec 9.6 C). So an unknown surface is advised on as if soft, and
`basis` is carried so the app can say WHY - "surface not recorded" is a very
different sentence from "this lane is mud", and the app owes the rider the
difference.

CALIBRATION, STATED PLAINLY.
`BANDS_MM` below are NOT measured. They are a stated convention, and the feed
says `"calibrated": false` so the app cannot quietly present them as science.
`calibrate` is the subcommand that would ground them - it samples stations,
reads 28 days, and reports where the bands actually sit in the distribution of
48-hour totals - and it has not been run against the live API. Until it has,
the feed carries the raw millimetres beside the band so the app can show the
measurement rather than only the opinion.
"""
import argparse
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ea_flood as EA               # noqa: E402
import stable_ids                   # noqa: E402
from text_clean import clean_text   # noqa: E402

#: The soak window. 48 hours and not 24 because what makes a byway cut up is
#: saturated ground rather than a shower: the top of a soft track sheds a
#: morning's rain and the ground under it does not. Both are carried in the
#: feed so the app is free to disagree.
WINDOW_H = 48

#: Millimetres in WINDOW_H at which a lane of each class becomes worth a word.
#: (damp, wet). None means rain does not move this class enough to say anything
#: - advising restraint on a tarmac'd byway after rain would train riders to
#: ignore the whole feature.
#:
#: NOT MEASURED. See CALIBRATION in the module docstring. `calibrate` exists to
#: replace these with percentiles of the real distribution, and the feed
#: carries `calibrated: false` until it has.
BANDS_MM = {
    "hard": None,
    "firm": (12.0, 30.0),
    "soft": (5.0, 15.0),
    # Deliberately the soft numbers: see the module docstring.
    "unknown": (5.0, 15.0),
}

#: OSM `surface` values, classed by what rain does to them.
#:
#: The three classes are about DAMAGE UNDER A WHEEL, not grip. A wet limestone
#: track is slippery and recovers; a wet peat track is rutted for a season, and
#: the rut is what gets the lane closed.
SURFACE_CLASS = {
    # Sealed or engineered: rain runs off.
    "asphalt": "hard", "paved": "hard", "concrete": "hard",
    "concrete:plates": "hard", "concrete:lanes": "hard", "chipseal": "hard",
    "paving_stones": "hard", "sett": "hard", "cobblestone": "hard",
    "unhewn_cobblestone": "hard", "metal": "hard", "wood": "hard",
    "bricks": "hard", "grass_paver": "hard",
    # Loose but drained: rain moves fines, not the formation.
    "gravel": "firm", "fine_gravel": "firm", "compacted": "firm",
    "pebblestone": "firm", "limestone": "firm", "hardcore": "firm",
    "rock": "firm", "stone": "firm", "shells": "firm",
    # Soil, and what a wheel does to it wet.
    "dirt": "soft", "earth": "soft", "ground": "soft", "mud": "soft",
    "grass": "soft", "sand": "soft", "clay": "soft", "soil": "soft",
    "woodchips": "soft", "salt": "soft",
    # `unpaved` says only "not sealed", which spans gravel to peat. Calling it
    # firm would be inventing drainage the tag never claimed.
    "unpaved": "unknown",
}

#: `tracktype` is the fallback and is nearly a drainage scale by construction:
#: grade1 solid, grade5 soil or sand. It is read only where `surface` is
#: absent or unrecognised, because where both exist `surface` is the more
#: specific statement.
TRACKTYPE_CLASS = {"grade1": "hard", "grade2": "firm", "grade3": "soft",
                   "grade4": "soft", "grade5": "soft"}

#: How long a feed may be shown before the app must say it is old. Six hours
#: matches spec 5.6's promise that "a rider is never more than six hours behind
#: any change" - a wet feed that outlives that is making a claim about weather
#: it has not checked.
STALE_AFTER_H = 6

# MEASURED, on containers/bicycle-midlands-shropshire-and-25-more.tbmap
# (12,597 records, 14.1 MB), 2026-09-24. Three shapes of the same data:
#
#   way_uid TEXT PRIMARY KEY, station id repeated per row  1,073,152 B  85 B/way
#   rowid PRIMARY KEY, station id repeated per row           593,920 B  47 B/way
#   rowid PRIMARY KEY, gauge interned to an integer          413,696 B  33 B/way
#
# The first shape stores every way's uid twice - once in the table and once in
# the index SQLite builds for a TEXT primary key - and the gauge notation once
# per way, for ~3,200 distinct values. Keying on the rowid costs nothing, since
# `ways_bbox.id` is already the rowid and every reader joins on it; interning
# the gauge costs one small table. 33 B/way is ~2 MB over the whole network
# against a 1.5 GB download budget (spec 8), which is the reason this is a
# container table rather than a seventh published file.
SCHEMA = """
CREATE TABLE IF NOT EXISTS wet_gauges (
  id         INTEGER PRIMARY KEY,  -- local to this container
  station_id TEXT NOT NULL,        -- EA notation: the key into the live feed
  label      TEXT,
  lat        REAL,
  lon        REAL
);
CREATE TABLE IF NOT EXISTS way_wetness (
  id             INTEGER PRIMARY KEY,  -- = the record's rowid, as *_bbox.id is
  susceptibility TEXT NOT NULL,        -- hard | firm | soft | unknown
  basis          TEXT NOT NULL,        -- surface | tracktype | none
  basis_value    TEXT,                 -- the OSM value read, verbatim
  gauge          INTEGER,              -- wet_gauges.id; NULL when none near
  gauge_m        REAL                  -- metres to it; NULL when none near
);
"""

CLASSES = ("hard", "firm", "soft", "unknown")


# ------------------------------------------------------------ the verdict

def susceptibility(surface, tracktype=None):
    """(class, basis, value read).

    `surface` first, `tracktype` second, `unknown` last. An unrecognised
    surface string falls through to tracktype rather than to `unknown`,
    because OSM's long tail of surface values is mostly spellings of things
    tracktype already grades.
    """
    if surface:
        known = SURFACE_CLASS.get(surface.strip().lower())
        if known is not None and known != "unknown":
            return known, "surface", surface
        if known == "unknown":
            # `unpaved` is a real answer that does not narrow far enough; if
            # tracktype can narrow it, take that instead.
            if tracktype and tracktype.strip().lower() in TRACKTYPE_CLASS:
                return (TRACKTYPE_CLASS[tracktype.strip().lower()],
                        "tracktype", tracktype)
            return "unknown", "surface", surface
    if tracktype:
        known = TRACKTYPE_CLASS.get(tracktype.strip().lower())
        if known is not None:
            return known, "tracktype", tracktype
    return "unknown", "none", None


def verdict(klass, mm, bands=None):
    """'dry' | 'damp' | 'wet' | 'unknown', from a class and a millimetre total.

    `mm is None` is 'unknown' and never 'dry'. A gauge that stopped reporting
    must not read as a week without rain - which is the whole reason
    `ea_flood.accumulate` refuses to default a missing total to zero.

    A class with no band ('hard') is 'dry' in the sense that matters: there is
    nothing to advise about. It is returned as 'na' rather than 'dry' so the
    app never shows a tarmac byway a reassurance it did not earn.
    """
    bands = BANDS_MM if bands is None else bands
    band = bands.get(klass)
    if band is None:
        return "na"
    if mm is None:
        return "unknown"
    damp, wet = band
    if mm >= wet:
        return "wet"
    if mm >= damp:
        return "damp"
    return "dry"


# ------------------------------------------------------------- containers

def record_table(db):
    """`ways` after the pivot, `lanes` before it. Published containers are
    still `lanes` (checked 2026-09-24), so a tool that only knew `ways` would
    be untestable against anything that exists."""
    names = set(row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))
    for table, uid in (("ways", "way_uid"), ("lanes", "lane_uid")):
        if table in names:
            return table, uid, names
    raise SystemExit("no ways or lanes table in this container")


def read_ways(db):
    """(rowid, uid, surface, tracktype, lat, lon) per record.

    The position is the centre of the record's r-tree box rather than a decoded
    geometry midpoint. Rain varies over kilometres and a byway is ~1 km, so the
    two differ by far less than the gauge spacing - and reading the r-tree
    avoids decoding 10,000 varint geometries four times a day for an answer
    that would not change.

    SQLite's rtree stores 32-bit floats, so a coordinate read back here is
    about a metre off the one written. That is irrelevant to a gauge 12 km
    away and would not be to anything drawn on the map, which is why nothing
    downstream of this treats the pair as geometry.
    """
    table, uid, names = record_table(db)
    bbox = "%s_bbox" % table
    if bbox not in names:
        raise SystemExit("%s has no %s, so records cannot be placed" %
                         (table, bbox))
    columns = set(row[1] for row in db.execute("PRAGMA table_info(%s)" % table))
    surface = "r.surface" if "surface" in columns else "NULL"
    tracktype = "r.tracktype" if "tracktype" in columns else "NULL"
    sql = ("SELECT r.rowid, r.%s, %s, %s,"
           " (b.min_lat + b.max_lat) / 2.0, (b.min_lon + b.max_lon) / 2.0"
           " FROM %s r JOIN %s b ON b.id = r.rowid ORDER BY r.rowid"
           % (uid, surface, tracktype, table, bbox))
    return list(db.execute(sql))


def assign(rows, stations, log=None, previous=None):
    """(way_wetness rows, wet_gauges rows, counts).

    Only the gauges actually chosen are interned. A region sees a few hundred
    of the ~3,200 national stations, and carrying the rest would put gauges in
    Kent into the Welsh container.

    A GAUGE KEEPS ITS NUMBER. `previous` is the region's PUBLISHED container;
    a station it carries keeps its `wet_gauges.id` there, and a new one
    continues above the highest id published (stable_ids.py). Numbered in
    first-seen order instead, a new way whose hash rowid sorted early and saw
    a new gauge first renumbered every gauge after it, and `gauge` moved in
    every `way_wetness` row that pointed at one: a changeset carrying the
    whole table to say nothing had changed. With no published container the
    numbering is first-seen, exactly as before.
    """
    index = EA.StationIndex(stations)
    out = []
    gauges = {}
    counts = {name: 0 for name in CLASSES}
    basis_counts = {"surface": 0, "tracktype": 0, "none": 0}
    ungauged = 0
    for rowid, _uid, surface, tracktype, lat, lon in rows:
        klass, basis, value = susceptibility(surface, tracktype)
        station, metres = index.nearest(lat, lon)
        if station is None:
            ungauged += 1
            gauge_id = None
        else:
            if station["id"] not in gauges:
                # Numbered in first-seen order over rowid-sorted records, so
                # two builds of the same container give one file.
                gauges[station["id"]] = (len(gauges) + 1, station)
            gauge_id = gauges[station["id"]][0]
        counts[klass] += 1
        basis_counts[basis] += 1
        out.append({"id": rowid, "susceptibility": klass, "basis": basis,
                    "basis_value": value, "gauge": gauge_id,
                    "gauge_m": round(metres, 1) if metres is not None
                    else None})
        if log and len(out) % 20000 == 0:
            log("    %d placed" % len(out))
    held = stable_ids.previous_numbers(previous, "wet_gauges", "station_id",
                                       "id")
    if held:
        order = [sid for sid, _ in sorted(gauges.items(),
                                          key=lambda kv: kv[1][0])]
        stable = stable_ids.number(order, held)
        remap = dict((gid, stable[sid]) for sid, (gid, _) in gauges.items())
        gauges = dict((sid, (stable[sid], station))
                      for sid, (_, station) in gauges.items())
        for row in out:
            if row["gauge"] is not None:
                row["gauge"] = remap[row["gauge"]]
    # Cleaned here as well as in ea_flood.parse_station, because the station
    # list is cached across runs (text_clean.py).
    gauge_rows = [{"id": gid, "station_id": sid,
                   "label": clean_text(station.get("label")),
                   "lat": station.get("lat"), "lon": station.get("lon")}
                  for sid, (gid, station) in sorted(gauges.items(),
                                                    key=lambda kv: kv[1][0])]
    return out, gauge_rows, {"by_class": counts, "by_basis": basis_counts,
                             "ungauged": ungauged, "total": len(out),
                             "gauges": len(gauge_rows)}


def write_wetness(db_path, rows, gauges):
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        db.executemany(
            "INSERT OR REPLACE INTO wet_gauges (id, station_id, label, lat,"
            " lon) VALUES (?,?,?,?,?)",
            [(g["id"], g["station_id"], g["label"], g["lat"], g["lon"])
             for g in gauges])
        db.executemany(
            "INSERT OR REPLACE INTO way_wetness (id, susceptibility, basis,"
            " basis_value, gauge, gauge_m) VALUES (?,?,?,?,?,?)",
            [(r["id"], r["susceptibility"], r["basis"], r["basis_value"],
              r["gauge"], r["gauge_m"]) for r in rows])
        db.commit()
    finally:
        db.close()
    return len(rows)


def verify_written(db_path):
    """Refuse a container whose wetness table the app could not use.

    THE DEFECT THIS EXISTS FOR IS NOT A CRASH. It is a table that is present,
    populated, the right size, and joins to nothing - the shape this codebase
    names as its most common: "a complete feature nothing calls". A checksum
    proves the bytes arrived; six published imagery packs once passed one and
    could not be opened at all.

    Called by `do_assign` on the file it just wrote, so the build fails where
    the mistake was made rather than on a phone.
    """
    db = sqlite3.connect(db_path)
    try:
        table, _uid, names = record_table(db)
        problems = []
        if "way_wetness" not in names:
            return ["way_wetness was not written"]
        records = db.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        rows = db.execute("SELECT COUNT(*) FROM way_wetness").fetchone()[0]
        if rows != records:
            problems.append(
                "%d way_wetness rows against %d records in %s - a way with no "
                "row reads as no advice, which is indistinguishable from "
                "'this lane is fine'" % (rows, records, table))
        orphan = db.execute(
            "SELECT COUNT(*) FROM way_wetness w WHERE NOT EXISTS"
            " (SELECT 1 FROM %s r WHERE r.rowid = w.id)" % table).fetchone()[0]
        if orphan:
            problems.append("%d way_wetness rows point at no record, so "
                            "nothing on the map can reach them" % orphan)
        dangling = db.execute(
            "SELECT COUNT(*) FROM way_wetness w WHERE w.gauge IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM wet_gauges g WHERE g.id = w.gauge)"
        ).fetchone()[0] if "wet_gauges" in names else rows
        if dangling:
            problems.append("%d ways name a gauge that is not in wet_gauges, "
                            "so their rain can never be looked up" % dangling)
        # A distance without a gauge, or a gauge without a distance, would let
        # the app show a reading it cannot qualify - the one thing spec 9.6 C
        # and G both forbid.
        mismatched = db.execute(
            "SELECT COUNT(*) FROM way_wetness WHERE (gauge IS NULL)"
            " != (gauge_m IS NULL)").fetchone()[0]
        if mismatched:
            problems.append("%d rows carry a gauge without its distance or a "
                            "distance without its gauge" % mismatched)
        bad = [row[0] for row in db.execute(
            "SELECT DISTINCT susceptibility FROM way_wetness")
            if row[0] not in CLASSES]
        if bad:
            problems.append("susceptibility values the app has no band for: %s"
                            % ", ".join(sorted(bad)))
        return problems
    finally:
        db.close()


# -------------------------------------------------------------- the feed

def clip(stations, bbox, pad_deg=0.35):
    """The stations that can speak for a region.

    Padded, because a region's edge ways are legitimately served by a gauge
    just outside it - clipping hard to the box would leave a border lane
    ungauged while a gauge sat two miles over the line.
    """
    west, south, east, north = bbox
    return [s for s in stations
            if s["lat"] is not None
            and south - pad_deg <= s["lat"] <= north + pad_deg
            and west - pad_deg <= s["lon"] <= east + pad_deg]


def feed_body(region, stations, readings, as_of, calibrated=False):
    """The published wet feed. Small on purpose: ids and numbers, no geometry.

    Stations with no usable reading are OMITTED rather than written with a
    zero. The app joining `way_wetness.station_id` against this must read a
    miss as "unknown", which is what `verdict` does with `mm=None`.
    """
    body = {}
    for station in stations:
        entry = readings.get(station["id"])
        if entry is None:
            continue
        mm48 = entry.get("mm_48h")
        mm24 = entry.get("mm_24h")
        if mm48 is None and mm24 is None:
            continue
        body[station["id"]] = {
            "mm_24h": mm24, "mm_48h": mm48,
            "at": entry.get("latest_at"), "n": entry.get("readings"),
        }
    return {
        "schema": 1,
        "region": region,
        "as_of": as_of,
        "window_h": WINDOW_H,
        "stale_after_h": STALE_AFTER_H,
        # The thresholds travel WITH the data so the app and this file cannot
        # drift apart, and so a recalibration reaches riders without an app
        # release.
        "bands_mm": {k: (list(v) if v else None)
                     for k, v in sorted(BANDS_MM.items())},
        "calibrated": bool(calibrated),
        "licence": "Environment Agency flood-monitoring data, OGL v3",
        "stations": body,
    }


# ------------------------------------------------------------- calibrate

def rolling_totals(samples, window_h=WINDOW_H, step_h=6):
    """Every `window_h` total over a series of (datetime, mm) samples.

    Written as its own function because it is the only part of `calibrate`
    that can be tested without the network, and it is the part that decides
    the answer. Windows step by `step_h` so overlapping windows are counted -
    a 48-hour total sampled once every 48 hours would miss the storm that
    straddled a boundary, which is exactly the window a rider cares about.
    """
    if not samples:
        return []
    ordered = sorted(samples)
    start = ordered[0][0]
    end = ordered[-1][0]
    window = datetime.timedelta(hours=window_h)
    step = datetime.timedelta(hours=step_h)
    out = []
    edge = start + window
    while edge <= end:
        low = edge - window
        out.append(sum(v for t, v in ordered if low <= t <= edge))
        edge += step
    return out


def percentiles(values, points=(50, 75, 90, 95, 99)):
    """Nearest-rank percentiles. Empty in, empty out - never a zero."""
    if not values:
        return {}
    ordered = sorted(values)
    out = {}
    for point in points:
        rank = max(1, int(round(point / 100.0 * len(ordered))))
        out[str(point)] = ordered[min(rank, len(ordered)) - 1]
    return out


def band_position(bands, distribution):
    """Where each band edge sits in the measured distribution.

    This is the output that would let someone say the bands are right or wrong:
    if `soft`'s "wet" edge turns out to be the 40th percentile, the app would
    be advising restraint two days in five and riders would stop reading it.
    """
    if not distribution:
        return {}
    ordered = sorted(distribution)
    out = {}
    for name, band in sorted(bands.items()):
        if band is None:
            continue
        out[name] = {}
        for label, edge in zip(("damp", "wet"), band):
            below = sum(1 for v in ordered if v < edge)
            out[name][label] = {
                "mm": edge,
                "percentile": round(100.0 * below / len(ordered), 1),
            }
    return out


def do_calibrate(args, log=print, opener=None, now=None):
    """Sample stations, read `--days` of rain, and say where the bands land.

    A SAMPLE and not the whole network: 28 days of 15-minute rainfall for 3,200
    stations is ~8 million readings, and the question - "is 15 mm in 48 hours
    unusual?" - is answered as well by 40 stations spread across the country as
    by all of them. The sample is taken at a fixed stride over the id-sorted
    list so two runs pick the same stations.
    """
    with open(args.stations, encoding="utf-8") as handle:
        stations = json.load(handle)["stations"]
    if not stations:
        raise SystemExit("no stations in %s" % args.stations)
    stride = max(1, len(stations) // max(1, args.sample))
    sample = stations[::stride][:args.sample]
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(days=args.days)
    every = []
    per_station = {}
    for station in sample:
        url = ("%s/id/stations/%s/readings?parameter=rainfall&since=%s"
               "&_limit=%d" % (EA.BASE, station["id"], EA._iso(since), EA.PAGE))
        payload = EA._get(url, opener=opener)
        samples = []
        for item in payload.get("items", []):
            when = EA.parse_when(item.get("dateTime"))
            value = EA._number(item.get("value"))
            if when is None or value is None:
                continue
            if value < 0.0 or value > EA.MAX_SANE_15MIN_MM:
                continue
            samples.append((when, value))
        windows = rolling_totals(samples, window_h=args.window)
        per_station[station["id"]] = len(windows)
        every.extend(windows)
        log("    %-10s %5d readings -> %4d windows"
            % (station["id"], len(samples), len(windows)))
    result = {"days": args.days, "window_h": args.window,
              "stations": len(sample), "windows": len(every),
              "percentiles_mm": percentiles(every),
              "bands": band_position(BANDS_MM, every)}
    log(json.dumps(result, indent=2, sort_keys=True))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
        log("    -> %s" % args.out)
    return 0


# ------------------------------------------------------------------- cli

def load_stations(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["stations"]


def do_assign(args, log=print):
    stations = load_stations(args.stations)
    db = sqlite3.connect(args.container)
    try:
        rows = read_ways(db)
    finally:
        db.close()
    assigned, gauges, counts = assign(rows, stations, log=log,
                                      previous=getattr(args, "previous", None))

    work = args.container
    if not args.in_place:
        handle, work = tempfile.mkstemp(suffix=".tbmap")
        os.close(handle)
        shutil.copyfile(args.container, work)
    before = os.path.getsize(work)
    try:
        write_wetness(work, assigned, gauges)
        after = os.path.getsize(work)
        # Checked on the file that was just written, so a table nothing can
        # join fails the build rather than reaching a phone.
        problems = verify_written(work)
    finally:
        if not args.in_place:
            if args.keep:
                shutil.move(work, args.keep)
            else:
                os.remove(work)

    if problems:
        for problem in problems:
            log("REFUSED: %s" % problem)
        raise SystemExit("way_wetness is not usable by the app; see above")

    log("")
    log("wetness for %s" % os.path.basename(args.container))
    for name in CLASSES:
        log("    %-8s %7d" % (name, counts["by_class"][name]))
    log("    %-8s %7d from surface, %d from tracktype, %d from nothing"
        % ("basis", counts["by_basis"]["surface"],
           counts["by_basis"]["tracktype"], counts["by_basis"]["none"]))
    log("    %-8s %7d ways with no gauge within %.0f km"
        % ("ungauged", counts["ungauged"], EA.StationIndex.MAX_M / 1000.0))
    log("    %-8s %7d distinct gauges carried" % ("gauges", counts["gauges"]))
    log("    %-8s %7d -> %d bytes (+%d, %.1f per way)"
        % ("bytes", before, after, after - before,
           (after - before) / float(max(1, counts["total"]))))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(dict(counts, container=os.path.basename(args.container),
                           plain_before=before, plain_after=after),
                      handle, indent=2, sort_keys=True)
        log("    report -> %s" % args.report)
    return 0


def do_feed(args, log=print, now=None):
    import build_pois as PO
    if args.region not in PO.REGIONS:
        raise SystemExit("unknown region %s; one of %s"
                         % (args.region, ", ".join(sorted(PO.REGIONS))))
    stations = clip(load_stations(args.stations), PO.REGIONS[args.region])
    with open(args.readings, encoding="utf-8") as handle:
        blob = json.load(handle)
    as_of = blob.get("as_of") or EA._iso(
        now or datetime.datetime.now(datetime.timezone.utc))
    body = feed_body(args.region, stations, blob.get("stations", {}), as_of,
                     calibrated=args.calibrated)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "%s.json" % args.region)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(body, handle, indent=1, sort_keys=True)
    log("%s: %d of %d gauges reporting, as of %s -> %s (%d bytes)"
        % (args.region, len(body["stations"]), len(stations), as_of, path,
           os.path.getsize(path)))
    return 0


def selftest(log=print):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s%s" % (name, (": " + detail) if detail else ""))

    check("a soft surface classes soft",
          susceptibility("mud")[0] == "soft")
    check("tarmac classes hard", susceptibility("asphalt")[0] == "hard")
    check("no surface and no tracktype is unknown, not hard",
          susceptibility(None, None) == ("unknown", "none", None))
    check("tracktype answers when surface does not",
          susceptibility(None, "grade5") == ("soft", "tracktype", "grade5"))
    check("`unpaved` does not become firm",
          susceptibility("unpaved")[0] == "unknown")

    check("no rain total is unknown, not dry",
          verdict("soft", None) == "unknown")
    check("a hard lane is never advised on", verdict("hard", 400.0) == "na")
    check("an unknown surface is advised as if soft",
          verdict("unknown", 20.0) == verdict("soft", 20.0) == "wet")
    check("zero rain on a soft lane is dry", verdict("soft", 0.0) == "dry")

    samples = [(datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
                + datetime.timedelta(hours=h), 1.0) for h in range(0, 96)]
    windows = rolling_totals(samples, window_h=48, step_h=24)
    check("a steady 1 mm/h gives 48-ish mm windows",
          windows and all(46 <= w <= 50 for w in windows), repr(windows))
    check("no samples gives no windows, not a zero",
          rolling_totals([]) == [])
    check("no values gives no percentiles, not zeros", percentiles([]) == {})

    for failure in failures:
        log("  FAIL " + failure)
    log("selftest: %s" % ("FAILED %d" % len(failures) if failures else "ok"))
    return 1 if failures else 0


def parse_args(argv):
    parser = argparse.ArgumentParser(description="wet-weather restraint")
    parser.add_argument("--selftest", action="store_true")
    sub = parser.add_subparsers(dest="command")

    a = sub.add_parser("assign", help="write way_wetness into a container")
    a.add_argument("--container", required=True)
    a.add_argument("--stations", required=True)
    a.add_argument("--in-place", action="store_true")
    a.add_argument("--keep", help="keep the modified copy here")
    a.add_argument("--report", help="write the counts as JSON")
    a.add_argument("--previous",
                   help="the PUBLISHED container: gauges keep the ids riders "
                        "already hold (see stable_ids.py)")

    f = sub.add_parser("feed", help="publish the live rainfall feed")
    f.add_argument("--region", required=True)
    f.add_argument("--stations", required=True)
    f.add_argument("--readings", required=True)
    f.add_argument("--out", default="published/wet")
    f.add_argument("--calibrated", action="store_true",
                   help="only pass this once `calibrate` has grounded BANDS_MM")

    c = sub.add_parser("calibrate", help="where the bands sit in the real "
                                         "distribution")
    c.add_argument("--stations", required=True)
    c.add_argument("--days", type=int, default=28)
    c.add_argument("--window", type=int, default=WINDOW_H)
    c.add_argument("--sample", type=int, default=40)
    c.add_argument("--out")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.selftest:
        return selftest()
    if args.command == "assign":
        return do_assign(args)
    if args.command == "feed":
        return do_feed(args)
    if args.command == "calibrate":
        return do_calibrate(args)
    raise SystemExit("nothing to do; --selftest, assign, feed or calibrate")


if __name__ == "__main__":
    sys.exit(main())

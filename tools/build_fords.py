#!/usr/bin/env python3
"""Fords on our lanes, each tied to the nearest Environment Agency gauge.

Spec 9.6 G. Three steps, and the middle one is the honest part:

    # 1. every ford OSM knows about, into a reproducible cache
    python tools/build_fords.py fetch --region midlands --cache cache/fords

    # 2. the ones that are ON one of our ways, tied to a gauge, into the
    #    container - the static half, which does not move between builds
    python tools/build_fords.py build --region midlands --cache cache/fords \
        --container containers/ways-midlands.tbmap \
        --stations cache/ea/stations-level.json --keep out.tbmap

    # 3. what those gauges read right now - the live half, kilobytes
    python tools/build_fords.py feed --region midlands \
        --stations cache/ea/stations-level.json \
        --readings cache/ea/readings-level.json --out published/rivers

    python tools/build_fords.py --selftest

THE DISTANCE IS THE FEATURE.
Spec 9.6 G: "always with the gauge, its distance and its timestamp - a gauge
ten miles downstream says less than one above the ford". EA gauges are sparse
and essentially never at the ford, so `gauge_m` is written on every row and no
row is ever written without it. A reading 12 km away is a hint; the app can
only say so if this file tells it how far.

AND SO IS THE ABSENCE.
A ford with no gauge within `ea_flood.StationIndex.MAX_M` gets `gauge = NULL`,
not the nearest thing in England. Attaching a gauge 80 km away would let the
app announce a river is high about a catchment it has never touched. NULL here
must read to the app as "we do not know", never as "the ford is fine" - the
same rule `build_wet.verdict` applies to rain.

WHAT "ABOVE NORMAL" MEANS, AND WHEN IT MEANS NOTHING.
`stageScale.typicalRangeHigh` is what turns 1.9 mAOD - meaningless to a rider -
into "0.8 m above normal". It is ABSENT on many stations, and where it is
absent this file writes NULL and `level_state` returns 'unknown'. An invented
normal would have the app calling every level-only site in England a flood.

NOT A CLOSURE. A ford is a hazard, not a traffic order, and nothing here may be
drawn like one. It is advisory in exactly the sense spec 9.6 G uses the word.
"""
import argparse
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_map_container as BMC   # noqa: E402  (read-only: GEOMETRY_SCALE)
import build_pois as PO             # noqa: E402  (Overpass fetch, REGIONS)
import ea_flood as EA               # noqa: E402
import osm_attributes as OA         # noqa: E402  (point_segment, to_metres)
import stable_ids                   # noqa: E402

#: OSM tags that mean "you cross water here". `ford=yes` is the common one;
#: the rest are kept VERBATIM rather than flattened to a boolean, because
#: `stepping_stones` is a footpath crossing and `boat` is not a crossing at all
#: for a motorcycle, and the app owes a rider that difference. Nothing here
#: decides what a value means - it records what OSM said.
FORD_SELECTORS = [("ford", "yes"), ("ford", "stepping_stones"),
                  ("ford", "boat"), ("ford", "intermittent"),
                  ("ford", "seasonal")]

#: How close a ford must be to one of our ways to be called that way's ford.
#:
#: 25 m, matching the tolerance the traffic-order matcher already uses (spec
#: 5.1, `matchLanesAndOrders`) rather than `osm_attributes.TOL_M` of 20 m. A
#: ford is a POINT against a LINE, so the only error is positional: OSM node
#: placement and the definitive-map line disagree by a few metres routinely and
#: by more where the legal line and the track diverge - which spec 9.6 H says
#: happens by "tens of metres". Tightening this drops real fords; loosening it
#: starts attaching a ford to the lane in the next field.
FORD_TOL_M = 25.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS ford_gauges (
  id          INTEGER PRIMARY KEY,  -- local to this container
  station_id  TEXT NOT NULL,        -- EA notation: the key into the live feed
  label       TEXT,
  river       TEXT,
  lat         REAL,
  lon         REAL,
  typical_low_m  REAL,              -- NULL where the EA never said
  typical_high_m REAL
);
CREATE TABLE IF NOT EXISTS fords (
  ford_uid    TEXT PRIMARY KEY,     -- osm:n123 / osm:w123, stable across builds
  way_id      INTEGER,              -- the record's rowid, as *_bbox.id is
  ford_tag    TEXT NOT NULL,        -- the OSM value, verbatim
  name        TEXT,
  lat         REAL NOT NULL,
  lon         REAL NOT NULL,
  way_m       REAL,                 -- metres from the ford to that way's line
  gauge       INTEGER,              -- ford_gauges.id; NULL when none in range
  gauge_m     REAL,                 -- metres to it; NULL when none in range
  source_date TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fords_bbox
  USING rtree(id, min_lon, max_lon, min_lat, max_lat);
"""


# --------------------------------------------------------------- geometry

def _varint_read(buf, i):
    shift = 0
    value = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def _unzigzag(value):
    return (value >> 1) if not value & 1 else -((value + 1) >> 1)


def unpack_geometry(blob):
    """The inverse of `build_map_container.pack_geometry`. [[(lon, lat)]].

    Written here rather than imported because `build_map_container` only ever
    packs - nothing in the pipeline had needed to read a container's geometry
    back until a ford had to be tested against the LINE rather than the
    bounding box. `test_build_fords.py` round-trips this against the real
    packer, so the two cannot drift.
    """
    if not blob:
        return []
    buf = bytes(blob)
    i = 0
    count, i = _varint_read(buf, i)
    lines = []
    for _ in range(count):
        points, i = _varint_read(buf, i)
        lon = lat = 0
        line = []
        for _p in range(points):
            dlon, i = _varint_read(buf, i)
            dlat, i = _varint_read(buf, i)
            lon += _unzigzag(dlon)
            lat += _unzigzag(dlat)
            line.append((lon / float(BMC.GEOMETRY_SCALE),
                         lat / float(BMC.GEOMETRY_SCALE)))
        lines.append(line)
    return lines


def distance_to_lines(lat, lon, lines):
    """Metres from a point to the nearest segment of any line, or None.

    Projected to metres about the point's own latitude before measuring.
    Comparing degrees would make a ford look 1.6x further east-west than
    north-south at 53N, which at a 25 m tolerance is the difference between
    finding a ford and losing it.
    """
    best = None
    for line in lines:
        if len(line) < 2:
            if len(line) == 1:
                metres = EA.haversine_m(lat, lon, line[0][1], line[0][0])
                best = metres if best is None else min(best, metres)
            continue
        # `osm_attributes.to_metres` takes (LAT, LON) pairs; container geometry
        # is (LON, LAT). Passing the container's order straight through applies
        # the cos(lat) longitude scaling to the latitude and vice versa, which
        # at 53N reports an east-west offset 1.65x too far and a north-south
        # one 0.6x too near - and both look plausible. Written out because the
        # two orders are one swap apart and the wrong one does not crash.
        pts = OA.to_metres([(y, x) for x, y in line], lat)
        px, py = OA.to_metres([(lat, lon)], lat)[0]
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            metres, _bearing = OA.point_segment(px, py, ax, ay, bx, by)
            best = metres if best is None else min(best, metres)
    return best


# ------------------------------------------------------------- the fords

def ford_of(element, source_date):
    """One ford, or None. Keeps the tag value verbatim - see FORD_SELECTORS."""
    tags = element.get("tags") or {}
    value = tags.get("ford")
    if not value:
        return None
    uid = PO.element_uid(element)
    point = PO.element_point(element)
    if uid is None or point is None:
        return None
    lat, lon = point
    return {"ford_uid": uid, "ford_tag": value, "name": tags.get("name"),
            "lat": round(lat, PO.COORD_DP), "lon": round(lon, PO.COORD_DP),
            "source_date": source_date}


def load_cached(cache, region, bbox):
    """Every ford the cache holds for a region, deduped, sorted, clipped."""
    path = cache_path(cache, region)
    if not os.path.exists(path):
        raise SystemExit("no ford cache for %s - run `fetch` first" % region)
    with open(path, encoding="utf-8") as handle:
        blob = json.load(handle)
    west, south, east, north = bbox
    fords = {}
    for element in blob["elements"]:
        ford = ford_of(element, blob["fetched_at"])
        if ford is None:
            continue
        if not (west <= ford["lon"] <= east and south <= ford["lat"] <= north):
            continue
        fords.setdefault(ford["ford_uid"], ford)
    return [fords[uid] for uid in sorted(fords)], blob["fetched_at"]


def cache_path(cache, region):
    return os.path.join(cache, "%s.json" % region)


# ------------------------------------------------------------- containers

def record_table(db):
    names = set(row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))
    for table, uid in (("ways", "way_uid"), ("lanes", "lane_uid")):
        if table in names:
            return table, uid, names
    raise SystemExit("no ways or lanes table in this container")


def nearest_way(db, table, bbox_table, lat, lon, tol_m=FORD_TOL_M):
    """(rowid, metres) of the nearest way whose LINE passes within `tol_m`.

    The r-tree narrows the candidates and the geometry decides. Using the
    r-tree box alone would attach a ford to any way whose bounding box happens
    to contain it, and a 3 km byway's box can be 3 km across - it would collect
    every ford in the valley.
    """
    # A degree of latitude is ~110.5 km; the longitude degree shrinks with
    # cos(lat), so the box is widened east-west to stay a circle on the ground.
    pad_lat = tol_m / 110540.0
    pad_lon = pad_lat / max(0.05, math.cos(math.radians(lat)))
    rows = db.execute(
        "SELECT r.rowid, r.geometry FROM %s b JOIN %s r ON r.rowid = b.id"
        " WHERE b.max_lon >= ? AND b.min_lon <= ?"
        " AND b.max_lat >= ? AND b.min_lat <= ?"
        % (bbox_table, table),
        (lon - pad_lon, lon + pad_lon, lat - pad_lat, lat + pad_lat)).fetchall()
    best, best_m = None, None
    for rowid, blob in rows:
        metres = distance_to_lines(lat, lon, unpack_geometry(blob))
        if metres is None:
            continue
        if best_m is None or metres < best_m:
            best, best_m = rowid, metres
    if best_m is None or best_m > tol_m:
        return None, None
    return best, best_m


def build_rows(db, fords, stations, tol_m=FORD_TOL_M, log=None):
    """(ford rows, gauge rows, counts)."""
    table, _uid, names = record_table(db)
    bbox_table = "%s_bbox" % table
    if bbox_table not in names:
        raise SystemExit("%s has no %s" % (table, bbox_table))
    index = EA.StationIndex(stations)
    out = []
    gauges = {}
    counts = {"fords": len(fords), "on_a_way": 0, "off_our_network": 0,
              "ungauged": 0, "by_tag": {}}
    for ford in fords:
        counts["by_tag"][ford["ford_tag"]] = (
            counts["by_tag"].get(ford["ford_tag"], 0) + 1)
        way_id, way_m = nearest_way(db, table, bbox_table, ford["lat"],
                                    ford["lon"], tol_m)
        if way_id is None:
            counts["off_our_network"] += 1
        else:
            counts["on_a_way"] += 1
        station, metres = index.nearest(ford["lat"], ford["lon"])
        if station is None:
            counts["ungauged"] += 1
            gauge_id = None
        else:
            if station["id"] not in gauges:
                gauges[station["id"]] = (len(gauges) + 1, station)
            gauge_id = gauges[station["id"]][0]
        out.append(dict(ford, way_id=way_id,
                        way_m=round(way_m, 1) if way_m is not None else None,
                        gauge=gauge_id,
                        gauge_m=round(metres, 1) if metres is not None
                        else None))
        if log and len(out) % 2000 == 0:
            log("    %d fords placed" % len(out))
    gauge_rows = [{"id": gid, "station_id": sid, "label": s.get("label"),
                   "river": s.get("river"), "lat": s.get("lat"),
                   "lon": s.get("lon"),
                   "typical_low_m": s.get("typical_low_m"),
                   "typical_high_m": s.get("typical_high_m")}
                  for sid, (gid, s) in sorted(gauges.items(),
                                              key=lambda kv: kv[1][0])]
    counts["gauges"] = len(gauge_rows)
    return out, gauge_rows, counts


def write_fords(db_path, fords, gauges, keep_off_network=False,
                previous=None):
    """Write the two tables and the r-tree.

    `keep_off_network` decides whether a ford we could not tie to one of our
    ways is carried. It is FALSE by default: this container is a green-laning
    dataset, and a ford on a B-road is weight the rider downloads for nothing.
    The count is reported either way, because that number going up is how we
    would learn the tolerance had drifted.

    THE GAUGE TABLE IS TRIMMED TO WHAT THE WRITTEN FORDS REFERENCE. `build_rows`
    interns a gauge for EVERY ford it sees, including the ones on roads we do
    not carry. Measured on the real Midlands build, 2026-09-24: 2,709 fords in
    the box, 230 of them on one of our ways, and 501 gauges interned - more
    than twice what the written rows point at. Every unreferenced gauge is a
    row a rider downloads and an id the live feed is then asked to carry.

    THE NUMBERS COME FROM WHAT RIDERS HOLD. `previous` is the region's
    PUBLISHED container. A ford keeps the rowid it was published under and a
    gauge keeps its id, by `ford_uid` and `station_id`; new ones continue
    above the highest number published, and removed ones leave gaps - see
    stable_ids.py. Numbered 1..N instead, one new ford whose uid sorted early
    moved every later ford's rowid and r-tree row, and one gauge first seen
    earlier renumbered `gauge` in every ford after it: all "changed" to a
    changeset, not one of them different on the ground. With no published
    container the numbering is exactly what it always was.
    """
    rows = [f for f in fords if keep_off_network or f["way_id"] is not None]
    used = set(f["gauge"] for f in rows if f["gauge"] is not None)
    held_gauges = stable_ids.previous_numbers(previous, "ford_gauges",
                                              "station_id", "id")
    if held_gauges:
        # First-seen order among the gauges actually written, which is the
        # order `build_rows` interned them in - so a new gauge's number does
        # not depend on how many off-network fords were seen before it.
        station = dict((g["id"], g["station_id"]) for g in gauges)
        renumber = stable_ids.number(
            [station[gid] for gid in sorted(used)], held_gauges)
        remap = dict((gid, renumber[station[gid]]) for gid in used)
        gauges = [dict(g, id=remap[g["id"]]) for g in gauges
                  if g["id"] in used]
        used = set(remap.values())
        rows = [dict(f, gauge=remap[f["gauge"]]) if f["gauge"] is not None
                else f for f in rows]
    numbers = stable_ids.number(
        [f["ford_uid"] for f in rows],
        stable_ids.previous_numbers(previous, "fords", "ford_uid"))
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        db.execute("DELETE FROM ford_gauges")
        db.executemany(
            "INSERT OR REPLACE INTO ford_gauges (id, station_id, label, river,"
            " lat, lon, typical_low_m, typical_high_m)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(g["id"], g["station_id"], g["label"], g["river"], g["lat"],
              g["lon"], g["typical_low_m"], g["typical_high_m"])
             for g in sorted(gauges, key=lambda g: g["id"])
             if g["id"] in used])
        db.execute("DELETE FROM fords_bbox")
        # In rowid order, as a 1..N build writes them.
        for rowid, ford in sorted(((numbers[f["ford_uid"]], f) for f in rows),
                                  key=lambda pair: pair[0]):
            db.execute(
                "INSERT OR REPLACE INTO fords (rowid, ford_uid, way_id,"
                " ford_tag, name, lat, lon, way_m, gauge, gauge_m,"
                " source_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rowid, ford["ford_uid"], ford["way_id"], ford["ford_tag"],
                 ford["name"], ford["lat"], ford["lon"], ford["way_m"],
                 ford["gauge"], ford["gauge_m"], ford["source_date"]))
            db.execute("INSERT INTO fords_bbox VALUES (?,?,?,?,?)",
                       (rowid, ford["lon"], ford["lon"], ford["lat"],
                        ford["lat"]))
        db.commit()
    finally:
        db.close()
    return len(rows)


def verify_written(db_path):
    """Refuse a container whose ford tables the app could not use.

    Same reasoning as `build_wet.verify_written`: the failure that matters here
    is not a crash but a table that is present, populated and joins to nothing.
    A ford with no r-tree row is invisible to every map query while sitting
    perfectly well in the file - the exact fault `validate_container.py` was
    written after.
    """
    db = sqlite3.connect(db_path)
    try:
        table, _uid, names = record_table(db)
        if "fords" not in names:
            return ["fords was not written"]
        problems = []
        orphan = db.execute(
            "SELECT COUNT(*) FROM fords f WHERE f.way_id IS NOT NULL AND NOT"
            " EXISTS (SELECT 1 FROM %s r WHERE r.rowid = f.way_id)" % table
        ).fetchone()[0]
        if orphan:
            problems.append("%d fords name a way that is not in this "
                            "container" % orphan)
        dangling = db.execute(
            "SELECT COUNT(*) FROM fords f WHERE f.gauge IS NOT NULL AND NOT"
            " EXISTS (SELECT 1 FROM ford_gauges g WHERE g.id = f.gauge)"
        ).fetchone()[0] if "ford_gauges" in names else db.execute(
            "SELECT COUNT(*) FROM fords WHERE gauge IS NOT NULL").fetchone()[0]
        if dangling:
            problems.append("%d fords name a gauge that is not in ford_gauges,"
                            " so their river level can never be looked up"
                            % dangling)
        if "fords_bbox" not in names:
            problems.append("no fords_bbox, so no map query will ever find a "
                            "ford")
        else:
            fords = db.execute("SELECT COUNT(*) FROM fords").fetchone()[0]
            boxes = db.execute("SELECT COUNT(*) FROM fords_bbox").fetchone()[0]
            if fords != boxes:
                problems.append(
                    "%d fords against %d fords_bbox rows - a ford with no box "
                    "is in the file and invisible to every map query"
                    % (fords, boxes))
            missing = db.execute(
                "SELECT COUNT(*) FROM fords f WHERE NOT EXISTS"
                " (SELECT 1 FROM fords_bbox b WHERE b.id = f.rowid)"
            ).fetchone()[0]
            if missing:
                problems.append("%d fords have no r-tree row of their own"
                                % missing)
        mismatched = db.execute(
            "SELECT COUNT(*) FROM fords WHERE (gauge IS NULL)"
            " != (gauge_m IS NULL)").fetchone()[0]
        if mismatched:
            problems.append("%d fords carry a gauge without its distance or a "
                            "distance without its gauge - spec 9.6 G requires "
                            "both or neither" % mismatched)
        return problems
    finally:
        db.close()


def gauge_count(db_path):
    db = sqlite3.connect(db_path)
    try:
        return db.execute("SELECT COUNT(*) FROM ford_gauges").fetchone()[0]
    finally:
        db.close()


# ------------------------------------------------------------- the levels

def level_state(level_m, typical_low_m, typical_high_m):
    """('unknown'|'below'|'normal'|'above', metres above the typical high).

    THE THREE UNKNOWNS ARE ALL UNKNOWN, and none of them is 'normal':

      * no reading                    - the gauge is silent
      * no typical range              - the EA never said what normal is
      * a reading but no typical high - same

    Returning 'normal' for any of these would have the app reassure a rider
    about a crossing nobody has measured, which is the one direction spec 9.6 G
    forbids.

    The metres are ALWAYS measured against the typical HIGH, whichever side the
    reading falls, so the number means one thing wherever it is read. The state
    says which side it is on; a field that silently changed its reference point
    between 'above' and 'below' would be read wrong by the first person to use
    it and never be noticed, because both answers look plausible.
    """
    if level_m is None or typical_high_m is None:
        return "unknown", None
    delta = round(level_m - typical_high_m, 3)
    if level_m > typical_high_m:
        return "above", delta
    if typical_low_m is not None and level_m < typical_low_m:
        return "below", delta
    return "normal", delta


def feed_body(region, stations, readings, as_of):
    """The published river feed: ids, levels, and how each compares to normal.

    A station with no reading is OMITTED, so the app's join misses and
    `level_state(None, ...)` gives 'unknown'. Writing it with a zero level
    would put every silent gauge at the bottom of its range - "the river is
    low, cross it".
    """
    body = {}
    for station in stations:
        entry = readings.get(station["id"])
        if entry is None:
            continue
        level = entry.get("latest")
        if level is None:
            continue
        state, delta = level_state(level, station.get("typical_low_m"),
                                   station.get("typical_high_m"))
        body[station["id"]] = {
            "m": level, "at": entry.get("latest_at"), "state": state,
            "vs_typical_high_m": delta,
            "typical_low_m": station.get("typical_low_m"),
            "typical_high_m": station.get("typical_high_m"),
        }
    return {
        "schema": 1,
        "region": region,
        "as_of": as_of,
        "stale_after_h": 6,
        "licence": "Environment Agency flood-monitoring data, OGL v3",
        "stations": body,
    }


# ------------------------------------------------------------------- cli

def do_fetch(args, log=print, opener=None, pause=10.0):
    bbox = PO.REGIONS[args.region] if not args.bbox else tuple(
        float(p) for p in args.bbox.split(","))
    os.makedirs(args.cache, exist_ok=True)
    path = cache_path(args.cache, args.region)
    if os.path.exists(path) and not args.refresh:
        log("    cached -> %s" % path)
        return 0
    elements = PO.fetch_category("ford", FORD_SELECTORS, bbox, log=log,
                                 opener=opener, pause=pause)
    seen = {}
    for element in elements:
        uid = PO.element_uid(element)
        if uid is not None:
            seen[uid] = element
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"region": args.region, "bbox": list(bbox),
                   "fetched_at": datetime.now(timezone.utc).date().isoformat(),
                   "elements": [seen[uid] for uid in sorted(seen)]}, handle)
    log("    %d fords -> %s" % (len(seen), path))
    return 0


def do_build(args, log=print):
    bbox = PO.REGIONS[args.region]
    fords, fetched_at = load_cached(args.cache, args.region, bbox)
    with open(args.stations, encoding="utf-8") as handle:
        stations = json.load(handle)["stations"]

    work = args.container
    if not args.in_place:
        handle, work = tempfile.mkstemp(suffix=".tbmap")
        os.close(handle)
        shutil.copyfile(args.container, work)
    before = os.path.getsize(work)
    db = sqlite3.connect(work)
    try:
        rows, gauges, counts = build_rows(db, fords, stations, log=log)
    finally:
        db.close()
    try:
        written = write_fords(work, rows, gauges,
                              keep_off_network=args.keep_off_network,
                              previous=getattr(args, "previous", None))
        counts["gauges_written"] = gauge_count(work)
        after = os.path.getsize(work)
        # Checked on the file that was just written, so a ford nothing can
        # reach fails the build rather than reaching a phone.
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
        raise SystemExit("the ford tables are not usable by the app; see above")

    log("")
    log("fords in %s (OSM read %s)" % (args.region, fetched_at))
    for tag, count in sorted(counts["by_tag"].items()):
        log("    %-18s %6d" % (tag, count))
    log("    %-18s %6d on one of our ways, %d elsewhere"
        % ("matched", counts["on_a_way"], counts["off_our_network"]))
    log("    %-18s %6d with no gauge within %.0f km"
        % ("ungauged", counts["ungauged"], EA.StationIndex.MAX_M / 1000.0))
    log("    %-18s %6d written, %d gauges carried (of %d interned)"
        % ("written", written, counts["gauges_written"], counts["gauges"]))
    log("    %-18s %6d -> %d bytes (+%d)"
        % ("bytes", before, after, after - before))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(dict(counts, region=args.region, written=written,
                           source_date=fetched_at, plain_before=before,
                           plain_after=after), handle, indent=2,
                      sort_keys=True)
        log("    report -> %s" % args.report)
    return 0


def gauges_in_use(container):
    """The station ids a container's fords actually reference.

    MEASURED, 2026-09-24: an unfiltered level feed is ~190 kB per region and
    1.1 MB over the six, because England has ~3,600 level sites and a region
    sees 400 to 1,600 of them. Almost none of those speak for a ford. The feed
    is fetched on the fast clock, so carrying gauges no ford points at is data
    a rider pays for four times a day and never reads.

    Returns None - not an empty set - when the container has no ford_gauges
    table, so the caller can tell "this container has no fords yet" from "this
    container's fords use no gauges" and publish the whole feed rather than an
    empty one.
    """
    db = sqlite3.connect(container)
    try:
        names = set(row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        if "ford_gauges" not in names:
            return None
        return set(row[0] for row in
                   db.execute("SELECT station_id FROM ford_gauges"))
    finally:
        db.close()


def do_feed(args, log=print):
    if args.region not in PO.REGIONS:
        raise SystemExit("unknown region %s" % args.region)
    with open(args.stations, encoding="utf-8") as handle:
        stations = json.load(handle)["stations"]
    import build_wet as W
    stations = W.clip(stations, PO.REGIONS[args.region])
    if args.container:
        used = gauges_in_use(args.container)
        if used is None:
            log("    %s has no ford_gauges - publishing every gauge in the box"
                % os.path.basename(args.container))
        else:
            before = len(stations)
            stations = [s for s in stations if s["id"] in used]
            log("    %d of %d gauges are referenced by a ford"
                % (len(stations), before))
    with open(args.readings, encoding="utf-8") as handle:
        blob = json.load(handle)
    as_of = blob.get("as_of") or EA._iso(datetime.now(timezone.utc))
    body = feed_body(args.region, stations, blob.get("stations", {}), as_of)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "%s.json" % args.region)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(body, handle, indent=1, sort_keys=True)
    above = sum(1 for v in body["stations"].values() if v["state"] == "above")
    unknown = sum(1 for v in body["stations"].values()
                  if v["state"] == "unknown")
    log("%s: %d gauges, %d above normal, %d with no typical range -> %s"
        % (args.region, len(body["stations"]), above, unknown, path))
    return 0


def selftest(log=print):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s%s" % (name, (": " + detail) if detail else ""))

    blob = BMC.pack_geometry([[(-1.6, 53.0), (-1.59, 53.01)]])
    back = unpack_geometry(blob)
    check("geometry round-trips through the real packer",
          len(back) == 1 and len(back[0]) == 2
          and abs(back[0][0][0] + 1.6) < 1e-7
          and abs(back[0][1][1] - 53.01) < 1e-7, repr(back))

    lines = [[(-1.600, 53.000), (-1.590, 53.000)]]
    near = distance_to_lines(53.0001, -1.595, lines)
    far = distance_to_lines(53.010, -1.595, lines)
    check("a point on the line is metres away", near is not None and near < 20,
          repr(near))
    check("a point a kilometre off is not", far is not None and far > 500,
          repr(far))

    # The lat/lon swap, which does not crash and reports both axes wrong by
    # reciprocal factors. Measured as a disagreement between two offsets that
    # are the same distance on the ground.
    east = distance_to_lines(53.005, -1.5997, [[(-1.60, 53.00), (-1.60, 53.01)]])
    north = distance_to_lines(53.00018, -1.595,
                              [[(-1.60, 53.00), (-1.59, 53.00)]])
    check("20 m east and 20 m north measure the same",
          abs(east - north) < 6.0, "%.1f vs %.1f" % (east, north))
    check("and both are about 20 m", 15 < east < 26 and 15 < north < 26,
          "%.1f, %.1f" % (east, north))

    check("no level is unknown, not normal",
          level_state(None, 0.2, 1.0)[0] == "unknown")
    check("no typical range is unknown, not normal",
          level_state(1.5, None, None)[0] == "unknown")
    check("above normal is above", level_state(1.8, 0.2, 1.0)[0] == "above")
    check("and says by how much",
          abs(level_state(1.8, 0.2, 1.0)[1] - 0.8) < 1e-9)
    check("inside the range is normal",
          level_state(0.5, 0.2, 1.0)[0] == "normal")
    check("below the range is below",
          level_state(0.1, 0.2, 1.0)[0] == "below")

    check("a ford element with no ford tag is not a ford",
          ford_of({"type": "node", "id": 1, "lat": 53.0, "lon": -1.6,
                   "tags": {"highway": "track"}}, "2026-01-01") is None)
    check("stepping stones keep their own value",
          ford_of({"type": "node", "id": 1, "lat": 53.0, "lon": -1.6,
                   "tags": {"ford": "stepping_stones"}},
                  "2026-01-01")["ford_tag"] == "stepping_stones")

    for failure in failures:
        log("  FAIL " + failure)
    log("selftest: %s" % ("FAILED %d" % len(failures) if failures else "ok"))
    return 1 if failures else 0


def parse_args(argv):
    parser = argparse.ArgumentParser(description="fords and river levels")
    parser.add_argument("--selftest", action="store_true")
    sub = parser.add_subparsers(dest="command")

    f = sub.add_parser("fetch", help="read fords from OSM into a cache")
    f.add_argument("--region", required=True)
    f.add_argument("--bbox")
    f.add_argument("--cache", default="cache/fords")
    f.add_argument("--refresh", action="store_true")

    b = sub.add_parser("build", help="write fords into a region container")
    b.add_argument("--region", required=True)
    b.add_argument("--cache", default="cache/fords")
    b.add_argument("--container", required=True)
    b.add_argument("--stations", required=True,
                   help="ea_flood stations file for parameter=level")
    b.add_argument("--in-place", action="store_true")
    b.add_argument("--keep")
    b.add_argument("--keep-off-network", action="store_true",
                   help="carry fords not on one of our ways")
    b.add_argument("--report")
    b.add_argument("--previous",
                   help="the region's PUBLISHED container: fords and gauges "
                        "keep the numbers riders already hold "
                        "(see stable_ids.py)")

    d = sub.add_parser("feed", help="publish the live river-level feed")
    d.add_argument("--region", required=True)
    d.add_argument("--stations", required=True)
    d.add_argument("--readings", required=True)
    d.add_argument("--out", default="published/rivers")
    d.add_argument("--container",
                   help="restrict the feed to the gauges this container's "
                        "fords reference")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.selftest:
        return selftest()
    if args.command == "fetch":
        return do_fetch(args)
    if args.command == "build":
        return do_build(args)
    if args.command == "feed":
        return do_feed(args)
    raise SystemExit("nothing to do; --selftest, fetch, build or feed")


if __name__ == "__main__":
    sys.exit(main())

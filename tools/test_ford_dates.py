#!/usr/bin/env python3
"""A ford's date moves when the ford does, and not when we look at it again.

THE COST. `source_date` on a ford was the day Overpass was read
(`build_fords.load_cached` hands every row the cache's `fetched_at`), so
`build_fords.py fetch --refresh` re-dated every ford in the region, and every
refresh changeset carried the whole `fords` table for no change on the ground.
Stable rowids (stable_ids.number) did not save it: a changeset matches rows on
the rowid and then compares values, and the date is a value. The same fault
test_poi_dates.py pins for POIs.

THE RULE. A ford whose every other column - tag, name, position, the way it
is on and how far, the gauge and how far - equals the published row keeps the
published date; a new or changed ford takes the day it was read.

Each "keeps its date" check below is red with stable_ids.keep_dates removed
from build_fords.write_fords, because the refresh is always read on a later
day than the published build. Each "changed" check is red with every date
kept regardless of content.
Each check in test_a_moved_renamed_or_regauged_ford_is_a_changed_one
changes one column alone, and is red with that column left out of
keep_dates' comparison.

Run: python tools/test_ford_dates.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_map_container as B   # noqa: E402
import build_fords as F           # noqa: E402
import build_changeset as X       # noqa: E402

_passed = 0
_failed = []

PUBLISHED_DAY = "2026-08-24"
REFRESH_DAY = "2026-09-24"


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": %r" % (detail,)) if detail else ""))


def _way(uid, lon, lat):
    return {"type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [(lon, lat), (lon + 0.004, lat + 0.003)]},
            "properties": {"lane_uid": uid, "class": "boat",
                           "authority": "Derbyshire", "source_date": "2026-03-04",
                           "motorbike_ok": 1, "fourxfour_ok": 1,
                           "access_reason": "x", "access_evidence": "statutory",
                           "lengthKm": 0.4}}


# One way, (-1.700, 52.500) -> (-1.696, 52.503), inside the midlands box.
WAYS = [_way("W1", -1.70, 52.50)]

#: Points ON that line (t = 0.25, 0.5, 0.75 along it), so every ford is on
#: our network and is written.
ON_LINE = [(52.50075, -1.6990), (52.50150, -1.6980), (52.50225, -1.6970)]


def _container(path, stamp="2026-09-01T00:00:00Z"):
    B.write_container(path, WAYS, "area", (11, 11), stamp)


def _gauge(sid, lat, lon):
    return {"id": sid, "label": sid, "lat": lat, "lon": lon, "river": "Dove",
            "catchment": None, "typical_low_m": 0.2, "typical_high_m": 1.0,
            "measures": ["%s-level" % sid]}


def _node(osm_id, lat, lon, **tags):
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon,
            "tags": tags}


STATIONS = [_gauge("E1", 52.5010, -1.6985), _gauge("E2", 52.5100, -1.6900)]

ELEMENTS = [_node(10, ON_LINE[0][0], ON_LINE[0][1], ford="yes"),
            _node(20, ON_LINE[1][0], ON_LINE[1][1], ford="yes",
                  name="Watersplash"),
            _node(30, ON_LINE[2][0], ON_LINE[2][1], ford="intermittent")]


def _dates(path):
    db = sqlite3.connect(path)
    try:
        return dict(db.execute("SELECT ford_uid, source_date FROM fords"))
    finally:
        db.close()


def _set_meta(path, key, value):
    db = sqlite3.connect(path)
    try:
        db.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))
        db.commit()
    finally:
        db.close()


def _placed(path, elements, day, stations=STATIONS):
    """What build_rows makes of `elements` read on `day` against `path`."""
    fords = [F.ford_of(e, day) for e in elements]
    db = sqlite3.connect(path)
    try:
        return F.build_rows(db, [f for f in fords if f is not None], stations)
    finally:
        db.close()


# ----------------------------------------------------------------- the rule

def test_an_unchanged_ford_keeps_its_published_date():
    tmp = tempfile.mkdtemp()
    try:
        published = os.path.join(tmp, "published.tbmap")
        _container(published)
        rows, gauges, _ = _placed(published, ELEMENTS, PUBLISHED_DAY)
        F.write_fords(published, rows, gauges)

        nxt = os.path.join(tmp, "next.tbmap")
        _container(nxt)
        refreshed = [
            ELEMENTS[0],                                   # unchanged
            ELEMENTS[1],                                   # unchanged, named
            _node(30, ON_LINE[2][0], ON_LINE[2][1], ford="seasonal"),
            _node(5, 52.50100, -1.69867, ford="yes"),      # new
        ]
        rows, gauges, _ = _placed(nxt, refreshed, REFRESH_DAY)
        F.write_fords(nxt, rows, gauges, previous=published)
        got = _dates(nxt)

        check("PREMISE: the published build carries all three fords, dated "
              "the day they were read",
              _dates(published) == {"osm:n10": PUBLISHED_DAY,
                                    "osm:n20": PUBLISHED_DAY,
                                    "osm:n30": PUBLISHED_DAY},
              _dates(published))
        check("PREMISE: the refresh built every ford it was handed",
              set(got) == {"osm:n5", "osm:n10", "osm:n20", "osm:n30"}, got)
        check("an unchanged unnamed ford keeps the published date",
              got.get("osm:n10") == PUBLISHED_DAY, got)
        check("an unchanged named ford keeps it too",
              got.get("osm:n20") == PUBLISHED_DAY, got)
        check("a ford whose tag changed takes the day it was read",
              got.get("osm:n30") == REFRESH_DAY, got)
        check("a new ford takes the day it was read",
              got.get("osm:n5") == REFRESH_DAY, got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rows(path):
    """{ford_uid: every column but the date}, as the file holds them."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return dict((r["ford_uid"], dict((k, r[k]) for k in r.keys()
                                         if k != "source_date"))
                    for r in db.execute("SELECT * FROM fords"))
    finally:
        db.close()


def _refresh(tmp, refreshed, stations=STATIONS, ways=None):
    """Publish ELEMENTS on WAYS against STATIONS, then build `refreshed` a
    month later on `ways` (WAYS if None) against `stations`, with the publish
    as `previous`. ({ford_uid: source_date}, {ford_uid: the columns that
    differ from the published row}) for the refresh."""
    published = os.path.join(tmp, "published.tbmap")
    _container(published)
    rows, gauges, _ = _placed(published, ELEMENTS, PUBLISHED_DAY)
    F.write_fords(published, rows, gauges)
    nxt = os.path.join(tmp, "next.tbmap")
    if ways is None:
        _container(nxt)
    else:
        B.write_container(nxt, ways, "area", (11, 11), "2026-09-01T00:00:00Z")
    rows, gauges, _ = _placed(nxt, refreshed, REFRESH_DAY, stations=stations)
    F.write_fords(nxt, rows, gauges, previous=published)
    was, now = _rows(published), _rows(nxt)
    moved = dict((uid, set(k for k in row if row[k] != was[uid].get(k)))
                 for uid, row in now.items() if uid in was)
    return _dates(nxt), moved


def _shifted(dlat, dlon):
    """The whole scene - ways, fords and gauges - moved by (dlat, dlon), so a
    ford's position changes and its distance to the way and to the gauge
    does not."""
    return {"refreshed": [_node(e["id"], e["lat"] + dlat, e["lon"] + dlon,
                                **e["tags"]) for e in ELEMENTS],
            "ways": [_way("W1", -1.70 + dlon, 52.50 + dlat)],
            "stations": [_gauge(s["id"], s["lat"] + dlat, s["lon"] + dlon)
                         for s in STATIONS]}


ALL = ("osm:n10", "osm:n20", "osm:n30")


def _one_change(label, changed, columns, refreshed=ELEMENTS, **kw):
    """Refresh; the fords in `changed` differ from the publish in exactly
    `columns` and take the refresh day, and every other ford differs in
    nothing and keeps the published day.

    The PREMISE on `columns` is what lets each check stand on its own: with
    every ford regauged at once, a check on a rename passes on the regauge,
    and nothing would notice `name` left out of keep_dates' comparison."""
    tmp = tempfile.mkdtemp()
    try:
        got, moved = _refresh(tmp, refreshed, **kw)
        check("PREMISE %s: all three fords written" % label,
              set(got) == set(ALL) and set(moved) == set(ALL), (got, moved))
        check("PREMISE %s: %s differ in %s and nothing else"
              % (label, list(changed), sorted(columns)),
              all(moved.get(uid) == set(columns) for uid in changed), moved)
        still = [uid for uid in ALL if uid not in changed]
        check("PREMISE %s: the fords beside them did not change and kept "
              "their date" % label,
              all(moved.get(uid) == set() and got.get(uid) == PUBLISHED_DAY
                  for uid in still), (got, moved))
        check("%s: changed" % label,
              all(got.get(uid) == REFRESH_DAY for uid in changed), got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_moved_renamed_or_regauged_ford_is_a_changed_one():
    """Each column of the comparison on its own. Every check here is red with
    that one column left out of stable_ids.keep_dates' comparison."""
    # The realistic shapes, which move more than one column.
    _one_change("moved by one stored digit (PO.COORD_DP is 6)",
                ["osm:n10"], {"lat", "way_m", "gauge_m"},
                refreshed=[_node(10, ON_LINE[0][0] + 0.000001, ON_LINE[0][1],
                                 ford="yes"), ELEMENTS[1], ELEMENTS[2]])
    _one_change("E1 withdrawn, every ford reads the more distant gauge",
                ALL, {"gauge", "gauge_m"}, stations=[STATIONS[1]])
    # One column each.
    _one_change("named", ["osm:n30"], {"name"},
                refreshed=[ELEMENTS[0], ELEMENTS[1],
                           _node(30, ON_LINE[2][0], ON_LINE[2][1],
                                 ford="intermittent", name="Dove Splash")])
    _one_change("renamed", ["osm:n20"], {"name"},
                refreshed=[ELEMENTS[0],
                           _node(20, ON_LINE[1][0], ON_LINE[1][1],
                                 ford="yes", name="The Watersplash"),
                           ELEMENTS[2]])
    # A value taken away, not just changed: a comparison that skipped the
    # published columns the refresh leaves empty would call these unchanged.
    _one_change("unnamed", ["osm:n20"], {"name"},
                refreshed=[ELEMENTS[0],
                           _node(20, ON_LINE[1][0], ON_LINE[1][1], ford="yes"),
                           ELEMENTS[2]])
    _one_change("every gauge withdrawn", ALL, {"gauge", "gauge_m"},
                stations=[])
    _one_change("retagged", ["osm:n30"], {"ford_tag"},
                refreshed=[ELEMENTS[0], ELEMENTS[1],
                           _node(30, ON_LINE[2][0], ON_LINE[2][1],
                                 ford="seasonal")])
    _one_change("another gauge at the same distance", ALL, {"gauge"},
                stations=[_gauge("E3", STATIONS[0]["lat"],
                                 STATIONS[0]["lon"]), STATIONS[1]])
    _one_change("the same gauge, moved", ALL, {"gauge_m"},
                stations=[_gauge("E1", STATIONS[0]["lat"] + 0.0001,
                                 STATIONS[0]["lon"]), STATIONS[1]])
    _one_change("the same line under another lane uid", ALL, {"way_id"},
                ways=[_way("W2", -1.70, 52.50)])
    _one_change("the way redrawn a metre north of the fords", ALL, {"way_m"},
                ways=[_way("W1", -1.70, 52.50001)])
    _one_change("everything a stored digit north", ALL, {"lat"},
                **_shifted(0.000001, 0.0))
    _one_change("everything a stored digit east", ALL, {"lon"},
                **_shifted(0.0, 0.000001))


def test_a_gauge_renumbered_by_the_build_is_not_a_changed_gauge():
    """build_rows numbers gauges in the order it first meets them, over EVERY
    ford in the box, including the ones on roads we do not carry. A new
    off-network ford whose uid sorts first, nearest E2, makes this build call
    E2 gauge 1 and E1 gauge 2, where the published file calls E1 gauge 1.
    Compared before write_fords puts the published numbers back, every
    unchanged ford reads as regauged and is re-dated."""
    tmp = tempfile.mkdtemp()
    try:
        published = os.path.join(tmp, "published.tbmap")
        _container(published)
        rows, gauges, _ = _placed(published, ELEMENTS, PUBLISHED_DAY)
        F.write_fords(published, rows, gauges)

        nxt = os.path.join(tmp, "next.tbmap")
        _container(nxt)
        # ~700 m north-east of the way's far end: off our network, nearest E2.
        stray = _node(1, 52.5095, -1.6905, ford="yes")
        rows, gauges, _ = _placed(nxt, [stray] + ELEMENTS, REFRESH_DAY)
        by_uid = dict((r["ford_uid"], r) for r in rows)
        station = dict((g["id"], g["station_id"]) for g in gauges)
        check("PREMISE: the stray ford is on none of our ways",
              by_uid["osm:n1"]["way_id"] is None, by_uid["osm:n1"])
        check("PREMISE: this build numbered E1 differently from the publish",
              station.get(by_uid["osm:n10"]["gauge"]) == "E1"
              and by_uid["osm:n10"]["gauge"] != 1, (by_uid, station))
        F.write_fords(nxt, rows, gauges, previous=published)
        got = _dates(nxt)
        check("PREMISE: the stray was not written", "osm:n1" not in got, got)
        check("every unchanged ford keeps the published date",
              got == {"osm:n10": PUBLISHED_DAY, "osm:n20": PUBLISHED_DAY,
                      "osm:n30": PUBLISHED_DAY}, got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_without_a_published_file_every_ford_is_dated_as_read():
    """A region's first build, or a local run, is what it always was."""
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "first.tbmap")
        _container(path)
        rows, gauges, _ = _placed(path, ELEMENTS, REFRESH_DAY)
        F.write_fords(path, rows, gauges,
                      previous=os.path.join(tmp, "not-there.tbmap"))
        got = _dates(path)
        check("PREMISE: all three fords written", len(got) == 3, got)
        check("dated as read", set(got.values()) == {REFRESH_DAY}, got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------- through the builder

def _cache(tmp, fetched_at, elements):
    """A build_fords cache for `midlands` read on `fetched_at` - the shape
    `fetch --refresh` leaves behind."""
    cache = os.path.join(tmp, "cache-%s" % fetched_at)
    os.makedirs(cache)
    with open(F.cache_path(cache, "midlands"), "w", encoding="utf-8") as fh:
        json.dump({"region": "midlands", "fetched_at": fetched_at,
                   "elements": elements}, fh)
    return cache


def _build(path, cache, stations, previous=None):
    """`build_fords.py build` through its own parser, with the flags
    refresh-data.yml passes."""
    argv = ["build", "--region", "midlands", "--cache", cache,
            "--container", path, "--stations", stations, "--in-place"]
    if previous:
        argv += ["--previous", previous]
    return F.do_build(F.parse_args(argv), log=lambda *a: None)


def test_a_refresh_of_unchanged_osm_changes_no_ford_row():
    """THE COST, END TO END: `build_fords.py build --previous` as CI runs it,
    then build_changeset between the published build and the refresh. The
    refresh read the same OSM a month later; the changeset must carry no ford
    row, no r-tree row and no gauge row."""
    tmp = tempfile.mkdtemp()
    try:
        stations = os.path.join(tmp, "stations-level.json")
        with open(stations, "w", encoding="utf-8") as fh:
            json.dump({"stations": STATIONS}, fh)

        published = os.path.join(tmp, "published.tbmap")
        _container(published)
        _build(published, _cache(tmp, PUBLISHED_DAY, ELEMENTS), stations)

        refreshed = os.path.join(tmp, "refreshed.tbmap")
        _container(refreshed)
        _build(refreshed, _cache(tmp, REFRESH_DAY, ELEMENTS), stations,
               previous=published)
        # stamp_build.py's rule 3: the ways did not change, something else
        # may have, so the build gets a stamp of its own.
        _set_meta(refreshed, "built_at", "2026-09-24T06:00:00Z")

        check("PREMISE: the published build carries all three fords",
              len(_dates(published)) == 3, _dates(published))
        check("the refresh kept every ford's published date",
              set(_dates(refreshed).values()) == {PUBLISHED_DAY},
              _dates(refreshed))

        out = os.path.join(tmp, "refresh.tbchange")
        X.build_changeset(published, refreshed, out)
        db = sqlite3.connect(out)
        try:
            names = set(r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))
            ford_rows = db.execute("SELECT count(*) FROM fords").fetchone()[0]
            box_rows = db.execute(
                "SELECT count(*) FROM fords_bbox").fetchone()[0]
            gauge_rows = db.execute(
                "SELECT count(*) FROM ford_gauges").fetchone()[0]
            removed = db.execute(
                "SELECT count(*) FROM removed_rows WHERE tbl IN"
                " ('fords', 'fords_bbox', 'ford_gauges')").fetchone()[0]
        finally:
            db.close()
        check("PREMISE: the changeset has a fords table to carry rows in",
              "fords" in names, sorted(names))
        check("the changeset carries no ford row", ford_rows == 0, ford_rows)
        check("no r-tree row", box_rows == 0, box_rows)
        check("no gauge row", gauge_rows == 0, gauge_rows)
        check("and removes nothing", removed == 0, removed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("%d passed, %d failed" % (_passed, len(_failed)))
    for failure in _failed:
        print("  FAIL %s" % failure)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

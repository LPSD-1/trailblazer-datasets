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


def test_a_moved_renamed_or_regauged_ford_is_a_changed_one():
    tmp = tempfile.mkdtemp()
    try:
        published = os.path.join(tmp, "published.tbmap")
        _container(published)
        rows, gauges, _ = _placed(published, ELEMENTS, PUBLISHED_DAY)
        F.write_fords(published, rows, gauges)

        nxt = os.path.join(tmp, "next.tbmap")
        _container(nxt)
        refreshed = [
            # Moved by one stored digit (PO.COORD_DP is 6).
            _node(10, ON_LINE[0][0] + 0.000001, ON_LINE[0][1], ford="yes"),
            _node(20, ON_LINE[1][0], ON_LINE[1][1], ford="yes",
                  name="The Watersplash"),
            ELEMENTS[2],
        ]
        # E1 withdrawn: every ford now reads the more distant gauge.
        rows, gauges, _ = _placed(nxt, refreshed, REFRESH_DAY,
                                  stations=[STATIONS[1]])
        F.write_fords(nxt, rows, gauges, previous=published)
        got = _dates(nxt)
        check("moved by one stored digit: changed",
              got.get("osm:n10") == REFRESH_DAY, got)
        check("renamed: changed", got.get("osm:n20") == REFRESH_DAY, got)
        check("same ford, another gauge: changed",
              got.get("osm:n30") == REFRESH_DAY, got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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

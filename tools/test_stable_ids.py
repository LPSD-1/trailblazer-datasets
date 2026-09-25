#!/usr/bin/env python3
"""Row numbers that stay put: stable_ids.py, and every builder that uses it.

THE COST. Numbered 1..N in uid order on every build, one new POI whose uid
sorted early moved every later POI's rowid, and a changeset - which matches
rows on that number - carried the whole table: measured on the published
ways-east-anglia.tbmap, 5,869,568 bytes (62% of the container) for ONE POI.
Stable numbering brought the same edit to 15,872 bytes.

Every builder test here builds a "published" container, then the next build
WITH it as `previous`, and asks that no kept row moved. Each one is red with
the `previous` argument ignored, because the edit is always a new key that
sorts (or is first seen) BEFORE the existing ones.

Run: python tools/test_stable_ids.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stable_ids as S          # noqa: E402
import build_map_container as B  # noqa: E402
import build_pois as PO          # noqa: E402
import build_fords as F          # noqa: E402
import build_wet as W            # noqa: E402

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


# ------------------------------------------------------------- the rule

def test_with_nothing_published_it_is_the_old_numbering():
    """A first build must be byte-for-byte what it always was."""
    got = S.number(["a", "b", "c"])
    check("1..N in the order given", got == {"a": 1, "b": 2, "c": 3}, got)
    check("and an empty previous is the same thing",
          S.number(["a", "b"], {}) == {"a": 1, "b": 2})


def test_a_kept_key_keeps_its_number_and_a_new_one_goes_on_the_end():
    previous = {"b": 1, "c": 2, "d": 3}
    got = S.number(["a", "b", "c", "d"], previous)
    check("b, c and d are where they were",
          (got["b"], got["c"], got["d"]) == (1, 2, 3), got)
    check("a, which sorts first, is numbered after them", got["a"] == 4, got)


def test_a_removed_key_leaves_a_gap_that_is_never_refilled_at_once():
    """Reusing a removed key's number in the same build would make one
    changeset row mean "that POI is now a different POI"."""
    previous = {"a": 1, "b": 2, "c": 3}
    got = S.number(["a", "c", "z"], previous)
    check("a and c keep 1 and 3", (got["a"], got["c"]) == (1, 3), got)
    check("z is not given b's 2", got["z"] == 4, got)
    check("numbers are unique", len(set(got.values())) == len(got), got)


def test_new_keys_continue_above_the_highest_published_even_if_removed():
    previous = {"a": 1, "b": 9}
    got = S.number(["a", "x", "y"], previous)
    check("above 9, where b was", (got["x"], got["y"]) == (10, 11), got)


def test_previous_numbers_reads_nothing_it_cannot_trust():
    tmp = tempfile.mkdtemp()
    try:
        check("no path", S.previous_numbers(None, "pois", "poi_uid") == {})
        check("no file", S.previous_numbers(os.path.join(tmp, "x.tbmap"),
                                            "pois", "poi_uid") == {})
        path = os.path.join(tmp, "p.tbmap")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.commit()
        db.close()
        check("no table", S.previous_numbers(path, "pois", "poi_uid") == {})
        PO.write_pois(path, [_poi("osm:n5"), _poi("osm:n7")])
        got = S.previous_numbers(path, "pois", "poi_uid")
        check("and reads the uid -> rowid the file holds",
              got == {"osm:n5": 1, "osm:n7": 2}, got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- builders

def _poi(uid, lat=52.5, lon=-1.7):
    return {"poi_uid": uid, "category": "fuel", "name": None, "lat": lat,
            "lon": lon, "opening_hours": None, "source_date": "2026-09-01"}


def _rows(path, sql):
    db = sqlite3.connect(path)
    try:
        return dict(db.execute(sql))
    finally:
        db.close()


def _way(uid, lon, lat):
    return {"type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [(lon, lat), (lon + 0.004, lat + 0.003)]},
            "properties": {"lane_uid": uid, "class": "boat",
                           "authority": "Derbyshire", "source_date": "2026-03-04",
                           "motorbike_ok": 1, "fourxfour_ok": 1,
                           "access_reason": "x", "access_evidence": "statutory",
                           "lengthKm": 0.4}}


def _station(sid, lat, lon):
    return {"id": sid, "label": sid, "lat": lat, "lon": lon, "river": "Wye",
            "typical_low_m": 0.2, "typical_high_m": 1.4}


def _ford(uid, lat, lon):
    return {"ford_uid": uid, "ford_tag": "yes", "name": None, "lat": lat,
            "lon": lon, "source_date": "2026-09-01"}


def _container(path, ways):
    B.write_container(path, ways, "area", (11, 11), "2026-09-01T00:00:00Z")


def test_a_new_poi_that_sorts_first_moves_no_other_poi():
    tmp = tempfile.mkdtemp()
    try:
        published = os.path.join(tmp, "published.tbmap")
        _container(published, [_way("W1", -1.70, 52.50)])
        PO.write_pois(published, [_poi("osm:n200"), _poi("osm:n300"),
                                  _poi("osm:n400")])
        nxt = os.path.join(tmp, "next.tbmap")
        _container(nxt, [_way("W1", -1.70, 52.50)])
        PO.write_pois(nxt, [_poi("osm:n100"), _poi("osm:n200"),
                            _poi("osm:n400")], previous=published)
        was = _rows(published, "SELECT poi_uid, rowid FROM pois")
        now = _rows(nxt, "SELECT poi_uid, rowid FROM pois")
        check("the kept POIs are where riders have them",
              now["osm:n200"] == was["osm:n200"]
              and now["osm:n400"] == was["osm:n400"], (was, now))
        check("the new one is above everything published",
              now["osm:n100"] == 4, now)
        boxes = _rows(nxt, "SELECT id, min_lon FROM pois_bbox")
        check("and every r-tree id is its record's rowid",
              sorted(boxes) == sorted(now.values()), (boxes, now))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_new_ford_and_a_new_gauge_move_no_other_ford_or_gauge():
    tmp = tempfile.mkdtemp()
    ways = [_way("W1", -1.700, 52.500), _way("W2", -1.600, 52.600)]
    level = [_station("L2", 52.6015, -1.598), _station("L3", 52.5015, -1.699)]
    try:
        published = os.path.join(tmp, "published.tbmap")
        _container(published, ways)
        db = sqlite3.connect(published)
        rows, gauges, _ = F.build_rows(
            db, [_ford("osm:n900", 52.5015, -1.698),
                 _ford("osm:n950", 52.6015, -1.598)], level)
        db.close()
        F.write_fords(published, rows, gauges)

        # A ford whose uid sorts FIRST, on W1, with a new gauge beside it
        # that is seen first - both renumber everything under 1..N.
        nxt = os.path.join(tmp, "next.tbmap")
        _container(nxt, ways)
        db = sqlite3.connect(nxt)
        rows, gauges, _ = F.build_rows(
            db, [_ford("osm:n100", 52.50225, -1.697),
                 _ford("osm:n900", 52.5015, -1.698),
                 _ford("osm:n950", 52.6015, -1.598)],
            level + [_station("L1", 52.50225, -1.697)])
        db.close()
        F.write_fords(nxt, rows, gauges, previous=published)

        was = _rows(published, "SELECT ford_uid, rowid FROM fords")
        now = _rows(nxt, "SELECT ford_uid, rowid FROM fords")
        check("kept fords keep their rowid",
              all(now[u] == was[u] for u in was), (was, now))
        check("the new ford is numbered after them",
              now["osm:n100"] == max(was.values()) + 1, now)
        g_was = _rows(published, "SELECT station_id, id FROM ford_gauges")
        g_now = _rows(nxt, "SELECT station_id, id FROM ford_gauges")
        check("kept gauges keep their id",
              all(g_now[s] == g_was[s] for s in g_was), (g_was, g_now))
        check("the new gauge is numbered after them",
              g_now.get("L1") == max(g_was.values()) + 1, g_now)
        pointed = _rows(nxt, "SELECT f.ford_uid, g.station_id FROM fords f "
                             "JOIN ford_gauges g ON g.id = f.gauge")
        check("and every ford still points at its own gauge",
              pointed == {"osm:n100": "L1", "osm:n900": "L3",
                          "osm:n950": "L2"}, pointed)
        check("the result passes the ford writer's own check",
              not F.verify_written(nxt), F.verify_written(nxt))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_without_a_published_file_fords_are_numbered_as_before():
    """The no-previous path is the old one to the byte: gauge ids keep the
    first-seen numbering INCLUDING the gaps left by trimmed gauges."""
    tmp = tempfile.mkdtemp()
    ways = [_way("W1", -1.700, 52.500)]
    try:
        path = os.path.join(tmp, "a.tbmap")
        _container(path, ways)
        db = sqlite3.connect(path)
        # osm:n050 is off our network and interns gauge 1, which is trimmed.
        rows, gauges, _ = F.build_rows(
            db, [_ford("osm:n050", 53.5, -2.5), _ford("osm:n900", 52.5015,
                                                      -1.698)],
            [_station("FAR", 53.5, -2.5), _station("L3", 52.5015, -1.699)])
        db.close()
        F.write_fords(path, rows, gauges)
        got = _rows(path, "SELECT station_id, id FROM ford_gauges")
        check("the gap is where it always was", got == {"L3": 2}, got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_new_rain_gauge_seen_first_moves_no_wetness_row():
    tmp = tempfile.mkdtemp()
    try:
        published = os.path.join(tmp, "published.tbmap")
        ways = [_way("W1", -1.70, 52.50), _way("W2", -1.60, 52.60)]
        rain = [_station("R1", 52.50, -1.70), _station("R2", 52.60, -1.60)]
        _container(published, ways)
        db = sqlite3.connect(published)
        rows = W.read_ways(db)
        db.close()
        wet, gauges, _ = W.assign(rows, rain)
        W.write_wetness(published, wet, gauges)

        # A station opened right on top of the way the rowid order visits
        # FIRST, so first-seen numbering would give it id 1.
        first = sorted(rows)[0]
        nxt = os.path.join(tmp, "next.tbmap")
        _container(nxt, ways)
        db = sqlite3.connect(nxt)
        rows = W.read_ways(db)
        db.close()
        wet, gauges, _ = W.assign(
            rows, rain + [_station("R0", first[4], first[5])],
            previous=published)
        W.write_wetness(nxt, wet, gauges)

        g_was = _rows(published, "SELECT station_id, id FROM wet_gauges")
        g_now = _rows(nxt, "SELECT station_id, id FROM wet_gauges")
        check("the premise: R0 is used", "R0" in g_now, g_now)
        check("kept gauges keep their id",
              all(g_now.get(s, g_was[s]) == g_was[s] for s in g_was),
              (g_was, g_now))
        check("R0 goes above them", g_now.get("R0") == max(g_was.values()) + 1,
              g_now)
        pointed = _rows(nxt, "SELECT w.id, g.station_id FROM way_wetness w "
                             "JOIN wet_gauges g ON g.id = w.gauge")
        check("and the way beside it points at R0", pointed[first[0]] == "R0",
              pointed)
        check("the result passes the wetness writer's own check",
              not W.verify_written(nxt), W.verify_written(nxt))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d checks" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

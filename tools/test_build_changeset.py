"""Tests for the changeset builder.

THE ONE THAT MATTERS is `test_applying_gets_the_new_build`: it applies the
changeset to a copy of the old container and checks the result is row-for-row
the new one. A changeset that merely looks small is worthless; a changeset that
reconstructs the build it claims to is the whole promise.

Run: python tools/test_build_changeset.py
"""

import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_changeset  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


CONTAINER_SCHEMA = """
CREATE TABLE tiles (
  zoom_level INTEGER NOT NULL, tile_column INTEGER NOT NULL,
  tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL,
  PRIMARY KEY (zoom_level, tile_column, tile_row)) WITHOUT ROWID;
CREATE TABLE lanes (
  rowid INTEGER PRIMARY KEY, lane_uid TEXT UNIQUE NOT NULL,
  lane_class TEXT NOT NULL, county TEXT, name TEXT, designation TEXT,
  description TEXT, authority TEXT, vehicle_access INTEGER, length_m REAL,
  geometry BLOB NOT NULL);
CREATE VIRTUAL TABLE lanes_bbox USING rtree(id, min_lon, max_lon, min_lat, max_lat);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def make_container(path, lanes, tiles, built_at):
    """A container with the given lanes and tiles.

    `lanes` is [(rowid, uid, cls, geometry_bytes)], `tiles` is {(z,x,y): bytes}.
    """
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript(CONTAINER_SCHEMA)
    for rowid, uid, cls, geom in lanes:
        db.execute("INSERT INTO lanes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (rowid, uid, cls, "Derbyshire", None, None, None, None,
                    31, 100.0, geom))
        db.execute("INSERT INTO lanes_bbox VALUES (?,?,?,?,?)",
                   (rowid, -1.8, -1.7, 53.0, 53.1))
    for (z, x, y), blob in tiles.items():
        db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, y, blob))
    for k, v in (("format_version", "1"), ("kind", "area"),
                 ("built_at", built_at), ("bounds", "-1.9,52.9,-1.5,53.2"),
                 ("min_zoom", "11"), ("max_zoom", "14"),
                 ("lane_count", str(len(lanes)))):
        db.execute("INSERT INTO meta VALUES (?,?)", (k, v))
    db.commit()
    db.close()


def apply_changeset(container, changeset):
    """What the app will do, in one transaction.

    Written here as well as in Dart on purpose: this test is what says the
    FORMAT can reconstruct a build, independently of whether the app's
    implementation of it is right.
    """
    db = sqlite3.connect(container)
    db.execute("ATTACH DATABASE ? AS cs", (changeset,))
    db.execute("BEGIN")
    try:
        db.execute("DELETE FROM lanes WHERE lane_uid IN "
                   "(SELECT uid FROM cs.removed_records)")
        db.execute("DELETE FROM lanes_bbox WHERE id NOT IN "
                   "(SELECT rowid FROM lanes)")
        db.execute("DELETE FROM tiles WHERE (zoom_level, tile_column, tile_row) "
                   "IN (SELECT zoom_level, tile_column, tile_row FROM cs.removed_tiles)")
        db.execute("INSERT OR REPLACE INTO tiles "
                   "SELECT zoom_level, tile_column, tile_row, tile_data FROM cs.tiles")
        db.execute("INSERT OR REPLACE INTO lanes SELECT * FROM cs.lanes")
        db.execute("DELETE FROM lanes_bbox WHERE id IN "
                   "(SELECT id FROM cs.bbox_rows)")
        db.execute("INSERT INTO lanes_bbox SELECT * FROM cs.bbox_rows")
        db.execute("UPDATE meta SET value = (SELECT value FROM cs.meta "
                   "WHERE key='to_build') WHERE key='built_at'")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    db.execute("DETACH DATABASE cs")
    db.close()


def snapshot(path):
    db = sqlite3.connect(path)
    lanes = sorted(db.execute(
        "SELECT lane_uid, lane_class, geometry FROM lanes").fetchall())
    tiles = sorted(db.execute(
        "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles").fetchall())
    built = db.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()[0]
    db.close()
    return lanes, tiles, built


def test_applying_gets_the_new_build():
    print("applying a changeset reconstructs the new build exactly")
    with tempfile.TemporaryDirectory() as tmp:
        old = os.path.join(tmp, "old.tbmap")
        new = os.path.join(tmp, "new.tbmap")
        cs = os.path.join(tmp, "delta.tbchange")

        # A realistic shape of change: one lane amended, one added, one
        # removed, the rest untouched.
        base = [(i, "UID-%03d" % i, "full-access", bytes([i]) * 40)
                for i in range(1, 51)]
        after = list(base)
        after[10] = (11, "UID-011", "restricted", b"\x99" * 40)   # amended
        after.append((51, "UID-051", "full-access", b"\x51" * 40))  # added
        after = [r for r in after if r[1] != "UID-020"]            # removed

        old_tiles = {(14, 8000 + i, 5000): bytes([i]) * 200 for i in range(20)}
        new_tiles = dict(old_tiles)
        new_tiles[(14, 8005, 5000)] = b"\xAA" * 200   # changed
        new_tiles[(14, 8100, 5000)] = b"\xBB" * 200   # added
        del new_tiles[(14, 8010, 5000)]               # removed

        make_container(old, base, old_tiles, "2026-09-01T00:00:00Z")
        make_container(new, after, new_tiles, "2026-10-01T00:00:00Z")

        stats = build_changeset.build_changeset(old, new, cs)
        check("one record amended", stats["records_changed"] == 1, stats)
        check("one record added", stats["records_added"] == 1, stats)
        check("one record removed", stats["records_removed"] == 1, stats)
        check("one tile changed", stats["tiles_changed"] == 1, stats)
        check("one tile added", stats["tiles_added"] == 1, stats)
        check("one tile removed", stats["tiles_removed"] == 1, stats)

        # THE ASSERTION THIS FILE EXISTS FOR.
        working = os.path.join(tmp, "working.tbmap")
        shutil.copyfile(old, working)
        apply_changeset(working, cs)
        check("the result is the new build, row for row",
              snapshot(working) == snapshot(new))


def test_a_removed_right_of_way_really_goes():
    print("a right of way a council removed does not survive the update")
    with tempfile.TemporaryDirectory() as tmp:
        old = os.path.join(tmp, "old.tbmap")
        new = os.path.join(tmp, "new.tbmap")
        cs = os.path.join(tmp, "d.tbchange")
        base = [(1, "STAYS", "full-access", b"\x01" * 10),
                (2, "GOES", "full-access", b"\x02" * 10)]
        make_container(old, base, {(11, 1, 1): b"t"}, "2026-09-01T00:00:00Z")
        make_container(new, base[:1], {(11, 1, 1): b"t"}, "2026-10-01T00:00:00Z")
        build_changeset.build_changeset(old, new, cs)

        working = os.path.join(tmp, "w.tbmap")
        shutil.copyfile(old, working)
        apply_changeset(working, cs)
        db = sqlite3.connect(working)
        left = [r[0] for r in db.execute("SELECT lane_uid FROM lanes")]
        orphans = db.execute(
            "SELECT COUNT(*) FROM lanes_bbox WHERE id NOT IN "
            "(SELECT rowid FROM lanes)").fetchone()[0]
        db.close()
        check("the removed lane is gone", left == ["STAYS"], left)
        check("and its bbox row with it", orphans == 0, orphans)


def test_an_unchanged_build_is_almost_nothing():
    print("nothing changed means almost nothing to download")
    with tempfile.TemporaryDirectory() as tmp:
        old = os.path.join(tmp, "old.tbmap")
        new = os.path.join(tmp, "new.tbmap")
        cs = os.path.join(tmp, "d.tbchange")
        lanes = [(i, "U%03d" % i, "full-access", bytes([i % 251]) * 400)
                 for i in range(1, 200)]
        tiles = {(14, 8000 + i, 5000): bytes([i % 251]) * 900 for i in range(200)}
        make_container(old, lanes, tiles, "2026-09-01T00:00:00Z")
        make_container(new, lanes, tiles, "2026-10-01T00:00:00Z")
        stats = build_changeset.build_changeset(old, new, cs)
        check("no rows", stats["records_changed"] == 0 and stats["tiles_changed"] == 0,
              stats)
        whole = os.path.getsize(new)
        delta = os.path.getsize(cs)
        check("and the changeset is a fraction of the build",
              delta < whole * 0.2, "%d vs %d bytes" % (delta, whole))


def test_two_builds_that_claim_to_be_the_same_are_refused():
    print("a pair the app could not tell apart is refused")
    with tempfile.TemporaryDirectory() as tmp:
        old = os.path.join(tmp, "old.tbmap")
        new = os.path.join(tmp, "new.tbmap")
        lanes = [(1, "U", "full-access", b"\x01" * 10)]
        make_container(old, lanes, {(11, 1, 1): b"a"}, "2026-09-01T00:00:00Z")
        make_container(new, lanes, {(11, 1, 1): b"b"}, "2026-09-01T00:00:00Z")
        try:
            build_changeset.build_changeset(old, new, os.path.join(tmp, "d"))
            check("it refuses", False, "it built one anyway")
        except SystemExit:
            check("it refuses", True)


def test_same_size_different_bytes_is_caught():
    print("a lane that moved without changing size is still a change")
    # The trap in comparing tiles by length. A lane shifting a few metres
    # changes the bytes and not the count, and a changeset that missed it
    # would leave a rider with geometry the publisher has corrected.
    with tempfile.TemporaryDirectory() as tmp:
        old = os.path.join(tmp, "old.tbmap")
        new = os.path.join(tmp, "new.tbmap")
        cs = os.path.join(tmp, "d.tbchange")
        lanes = [(1, "U", "full-access", b"\x01" * 10)]
        make_container(old, lanes, {(14, 1, 1): b"\x01" * 500}, "2026-09-01T00:00:00Z")
        make_container(new, lanes, {(14, 1, 1): b"\x02" * 500}, "2026-10-01T00:00:00Z")
        stats = build_changeset.build_changeset(old, new, cs)
        check("the tile is in the changeset", stats["tiles_changed"] == 1, stats)

        working = os.path.join(tmp, "w.tbmap")
        shutil.copyfile(old, working)
        apply_changeset(working, cs)
        check("and applying it lands the new bytes",
              snapshot(working)[1] == snapshot(new)[1])


# --------------------------------------------------------------------------
# THE LIVE SHAPE: every table a published region container carries
# --------------------------------------------------------------------------
#
# Everything above is the `lanes` shape of 2025, and every assertion in it
# still holds - but it is not what the pipeline publishes. A region container
# since step 1.10 is `ways` + `ways_bbox` + `pois` + `pois_bbox` + `fords` +
# `fords_bbox` + `ford_gauges` + `way_wetness` + `wet_gauges` + `tiles` +
# `meta`, with `evidence_age`, `context_note`, `context_scope` and
# `schema_version` in the meta. A changeset that carried only the first two
# was refused by the app every time, so every region update was a whole
# download; applied, it would have left last build's pois, fords and wetness
# under this build's stamp.
#
# So these containers are written by THE PIPELINE'S OWN WRITERS, in the order
# refresh-data.yml runs them: build_map_container.write_container, then
# build_pois.write_pois (inside build_containers), then build_wet, then
# build_fords, then evidence_age. A fixture built by hand here would be the
# shape this file believed in, which is exactly how the lanes-only tests above
# stayed green while the live set went unapplied.

import datetime  # noqa: E402
import json  # noqa: E402

import build_fords as F  # noqa: E402
import build_map_container as B  # noqa: E402
import build_pois as PO  # noqa: E402
import build_wet as W  # noqa: E402
import evidence_age as EA  # noqa: E402

#: Every table a published region container holds, bar tiles, meta and the
#: r-trees' shadow tables. Read off containers/ways-east-anglia.tbmap
#: (2026-09-24); test_make_changeset_fixture.py says when the published set
#: stops being exactly these, and gates its own fixture on the file itself.
LIVE_TABLES = {
    "ways": "rowid", "ways_bbox": "id",
    "pois": "rowid", "pois_bbox": "id",
    "fords": "rowid", "fords_bbox": "id",
    "ford_gauges": "id", "way_wetness": "id", "wet_gauges": "id",
}


def _way(uid, lon, lat, name=None, surface=None, fourxfour=1):
    return {
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [(lon, lat), (lon + 0.004, lat + 0.003)]},
        "properties": {
            "lane_uid": uid, "class": "boat", "county": "Derbyshire",
            "name": name or "Lane %s" % uid, "designation": "BOAT",
            "authority": "Derbyshire", "legal_tier": "statutory",
            "source": "rowmaps:derbyshire", "source_date": "2026-03-04",
            "surface": surface, "motorbike_ok": 1, "fourxfour_ok": fourxfour,
            "access_reason": "the definitive map says so",
            "access_evidence": "statutory", "lengthKm": 0.4,
        },
    }


def _poi(uid, lat, lon, category="fuel", name=None):
    return {"poi_uid": uid, "category": category, "name": name,
            "lat": lat, "lon": lon, "opening_hours": None,
            "source_date": "2026-09-01"}


def _ford(uid, lat, lon, tag="yes"):
    return {"ford_uid": uid, "ford_tag": tag, "name": None,
            "lat": lat, "lon": lon, "source_date": "2026-09-01"}


def _station(sid, lat, lon):
    return {"id": sid, "label": "Station %s" % sid, "lat": lat, "lon": lon,
            "river": "Wye", "typical_low_m": 0.2, "typical_high_m": 1.4}


def write_live_shape(path, ways, pois, fords, rain, level, built_at, as_of,
                     note="Only byways are on this map."):
    """A region container, written the way refresh-data.yml writes one."""
    B.write_container(path, ways, "area", (11, 12), built_at,
                      context_scope="none", context_note=note)
    PO.write_pois(path, pois)
    db = sqlite3.connect(path)
    try:
        rows = W.read_ways(db)
    finally:
        db.close()
    wet, wet_gauges, _ = W.assign(rows, rain)
    W.write_wetness(path, wet, wet_gauges)
    db = sqlite3.connect(path)
    try:
        ford_rows, ford_gauges, _ = F.build_rows(db, fords, level)
    finally:
        db.close()
    F.write_fords(path, ford_rows, ford_gauges)
    EA.write_meta(path, EA.read_container(path, as_of))


# Five ways a few hundred metres apart. W5 is out on its own with its own
# rain gauge and its own ford and river gauge, so removing it removes a row
# from EVERY table: the way, its box, its wetness, the gauge only it used,
# the ford only it had, and that ford's river gauge.
_WAYS = [
    _way("W1", -1.700, 52.500, surface="asphalt"),
    _way("W2", -1.690, 52.502),
    _way("W3", -1.680, 52.504, surface="gravel"),
    _way("W4", -1.670, 52.506),
    _way("W5", -1.300, 52.800),
]
_POIS = [_poi("osm:n200", 52.501, -1.699), _poi("osm:n300", 52.503, -1.689),
         _poi("osm:n400", 52.505, -1.679, "water"),
         _poi("osm:n500", 52.507, -1.669, "food", "The Plough")]
_FORDS = [_ford("osm:n9001", 52.5015, -1.698),
          _ford("osm:n9003", 52.5055, -1.678),
          _ford("osm:n9005", 52.8015, -1.298)]
_RAIN = [_station("R1", 52.50, -1.70), _station("R3", 52.51, -1.68),
         _station("R5", 52.80, -1.30)]
_LEVEL = [_station("L1", 52.50, -1.699), _station("L3", 52.506, -1.675),
          _station("L5", 52.80, -1.29)]


def _live_pair(tmp):
    """(before, after): two consecutive builds with an edit in every table.

    Every table ends up with at least one row written AND one removed, BY ITS
    KEY - which is not the same as by uid. A POI removed and another added
    leaves the count, and so every rowid, where it was: nothing is removed at
    all, two rows just change. So two POIs go and one comes.
    """
    before = os.path.join(tmp, "before.tbmap")
    after = os.path.join(tmp, "after.tbmap")
    write_live_shape(before, _WAYS, _POIS, _FORDS, _RAIN, _LEVEL,
                     "2026-09-01T00:00:00Z", datetime.date(2026, 9, 1))

    ways = [w for w in _WAYS if w["properties"]["lane_uid"] != "W5"]
    # W2 CLOSED to a 4x4 by an order, W3's surface re-surveyed to mud (so its
    # wetness moves), and W6 new.
    ways[1] = _way("W2", -1.690, 52.502, name="Lane W2 (closed)", fourxfour=0)
    ways[2] = _way("W3", -1.680, 52.504, surface="mud")
    ways.append(_way("W6", -1.660, 52.508))
    # Two POIs gone, one new - with a uid that sorts FIRST, so every POI after
    # it is renumbered: build_pois numbers rows 1..N in uid order.
    pois = [p for p in _POIS if p["poi_uid"] not in ("osm:n300", "osm:n500")]
    pois.append(_poi("osm:n100", 52.509, -1.659, "toilets"))
    pois.sort(key=lambda p: p["poi_uid"])
    # A ford re-tagged and re-surveyed a few metres over, and W5's ford gone
    # with W5 (off our network now).
    fords = [dict(f) for f in _FORDS]
    fords[1].update(ford_tag="stepping_stones", lat=52.5056)
    # The EA renamed a rain gauge, and opened a river gauge nearer W3's ford.
    rain = [dict(s) for s in _RAIN]
    rain[1]["label"] = "Station R3 (relocated)"
    level = _LEVEL + [_station("L0", 52.5056, -1.6781)]
    write_live_shape(after, ways, pois, fords, rain, level,
                     "2026-10-01T00:00:00Z", datetime.date(2026, 10, 1),
                     note="Only byways are on this map. Revised.")
    return before, after


def apply_v2(container, changeset):
    """The documented algorithm in build_changeset.py, table by table.

    Driven by `carries` and nothing else, so it cannot quietly agree with the
    builder about a table the builder forgot: a table the container holds and
    the changeset does not carry is refused here, as the app must refuse it.
    """
    db = sqlite3.connect(container, isolation_level=None)
    db.execute("ATTACH DATABASE ? AS cs", (changeset,))
    try:
        meta = dict(db.execute("SELECT key, value FROM cs.meta"))
        carries = json.loads(meta["carries"])
        held = build_changeset.carried_tables(db)
        if set(held) != set(carries):
            raise AssertionError("the changeset carries %s and the container "
                                 "holds %s" % (sorted(carries), sorted(held)))
        db.execute("BEGIN")
        try:
            for table, key in sorted(carries.items()):
                cols = [r[1] for r in db.execute(
                    'PRAGMA cs.table_info("%s")' % table)]
                joined = ", ".join(c if c == "rowid" else '"%s"' % c
                                   for c in cols)
                k = key if key == "rowid" else '"%s"' % key
                db.execute('DELETE FROM main."%s" WHERE %s IN (SELECT id '
                           'FROM cs.removed_rows WHERE tbl = ?)'
                           % (table, k), (table,))
                db.execute('DELETE FROM main."%s" WHERE %s IN (SELECT %s '
                           'FROM cs."%s")' % (table, k, k, table))
                db.execute('INSERT INTO main."%s" (%s) SELECT %s FROM cs."%s"'
                           % (table, joined, joined, table))
            db.execute("DELETE FROM main.tiles WHERE (zoom_level, "
                       "tile_column, tile_row) IN (SELECT zoom_level, "
                       "tile_column, tile_row FROM cs.removed_tiles)")
            db.execute("INSERT OR REPLACE INTO main.tiles "
                       "SELECT * FROM cs.tiles")
            for key, value in meta.items():
                if key in build_changeset.BOOKKEEPING:
                    continue
                db.execute("INSERT OR REPLACE INTO main.meta VALUES (?,?)",
                           (key, value))
            for key in json.loads(meta["removed_meta"]):
                db.execute("DELETE FROM main.meta WHERE key = ?", (key,))
            db.execute("UPDATE main.meta SET value = ? WHERE key = 'built_at'",
                       (meta["to_build"],))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    finally:
        db.execute("DETACH DATABASE cs")
        db.close()


def every_table(path):
    """{table: sorted rows} for every table but the r-tree shadows - rowid
    first where it is implicit, because the wetness, the fords and every
    r-tree join on it - plus meta as a dict."""
    db = sqlite3.connect(path)
    try:
        virtual = [name for name, sql in db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'")
            if (sql or "").upper().startswith("CREATE VIRTUAL TABLE")]
        shadow = {"%s_%s" % (v, s) for v in virtual
                  for s in ("node", "parent", "rowid")}
        out = {}
        for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"):
            if name in shadow or name.startswith("sqlite_"):
                continue
            if name == "meta":
                out[name] = dict(db.execute("SELECT key, value FROM meta"))
            elif name == "tiles" or name in virtual:
                out[name] = sorted(db.execute('SELECT * FROM "%s"' % name))
            else:
                out[name] = sorted(db.execute(
                    'SELECT rowid, * FROM "%s"' % name))
        return out
    finally:
        db.close()


def test_every_live_table_round_trips():
    print("a live-shape container: every table lands on the new build")
    with tempfile.TemporaryDirectory() as tmp:
        before, after = _live_pair(tmp)
        cs = os.path.join(tmp, "d.tbchange")
        stats = build_changeset.build_changeset(before, after, cs)

        db = sqlite3.connect(cs)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        db.close()
        carries = json.loads(meta["carries"])
        db = sqlite3.connect(after)
        check("the fixture is the live shape",
              build_changeset.carried_tables(db) == LIVE_TABLES)
        db.close()
        check("it carries every table the container holds",
              carries == LIVE_TABLES, carries)

        # A REMOVAL IN EVERY TABLE, and something written in every table.
        for table in sorted(LIVE_TABLES):
            got = stats["tables"].get(table, {})
            check("%s: a row removed" % table, got.get("removed", 0) >= 1, got)
            check("%s: a row written" % table,
                  got.get("added", 0) + got.get("changed", 0) >= 1, got)

        working = os.path.join(tmp, "working.tbmap")
        shutil.copyfile(before, working)
        apply_v2(working, cs)
        want, got = every_table(after), every_table(working)
        check("the builds really differ", every_table(before) != want)
        for table in sorted(want):
            check("%s is the new build's, row for row" % table,
                  got.get(table) == want[table],
                  "%d vs %d rows" % (len(got.get(table) or ()),
                                     len(want[table])))
        check("and no table is missing or extra", set(got) == set(want),
              sorted(set(got) ^ set(want)))
        db = sqlite3.connect(working)
        check("the result passes integrity_check",
              db.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        for rtree in ("ways_bbox", "pois_bbox", "fords_bbox"):
            check("%s agrees with its shadow tables" % rtree,
                  db.execute("SELECT rtreecheck(?)", (rtree,)).fetchone()[0]
                  == "ok")
        check("no wetness row outlives its way",
              db.execute("SELECT COUNT(*) FROM way_wetness WHERE id NOT IN "
                         "(SELECT rowid FROM ways)").fetchone()[0] == 0)
        db.close()


def test_the_new_builds_meta_is_restated():
    print("every meta key of the new build is in the changeset")
    with tempfile.TemporaryDirectory() as tmp:
        before, after = _live_pair(tmp)
        cs = os.path.join(tmp, "d.tbchange")
        build_changeset.build_changeset(before, after, cs)
        metas = []
        for path in (cs, after, before):
            db = sqlite3.connect(path)
            metas.append(dict(db.execute("SELECT key, value FROM meta")))
            db.close()
        carried, new, old = metas
        for key in ("evidence_age", "context_note", "context_scope",
                    "schema_version", "way_count", "class_counts",
                    "legal_tier_counts", "authorities", "bounds",
                    "min_zoom", "max_zoom", "lane_count"):
            check("%s is restated as the new build has it" % key,
                  key in new and carried.get(key) == new[key],
                  "%r vs %r" % (carried.get(key), new.get(key)))
        check("evidence_age really did move between the builds",
              old["evidence_age"] != new["evidence_age"])
        check("and so did context_note",
              old["context_note"] != new["context_note"])
        check("to_build is the new built_at",
              carried["to_build"] == new["built_at"])
        check("built_at itself is not restated", "built_at" not in carried)
        missed = sorted(k for k in new if k not in ("built_at", "kind")
                        and carried.get(k) != new[k])
        check("nothing else is left behind", not missed, missed)


def test_a_meta_key_the_new_build_dropped_is_removed():
    print("a meta key the new build does not have comes off")
    with tempfile.TemporaryDirectory() as tmp:
        before, after = _live_pair(tmp)
        db = sqlite3.connect(before)
        db.execute("INSERT INTO meta VALUES ('retired_key', 'old news')")
        db.commit()
        db.close()
        cs = os.path.join(tmp, "d.tbchange")
        stats = build_changeset.build_changeset(before, after, cs)
        check("it is listed", stats["meta_removed"] == ["retired_key"],
              stats["meta_removed"])
        working = os.path.join(tmp, "w.tbmap")
        shutil.copyfile(before, working)
        apply_v2(working, cs)
        check("and applying drops it",
              every_table(working)["meta"] == every_table(after)["meta"])


def test_an_absent_meta_key_is_not_written_empty():
    print("a key the new build lacks is not restated as ''")
    # Format 1 wrote ('bounds', new_meta.get('bounds', '')), so a build with
    # no bounds sent an empty one, and an applier that trusted it blanked the
    # rider's.
    with tempfile.TemporaryDirectory() as tmp:
        before, after = _live_pair(tmp)
        for path in (before, after):
            db = sqlite3.connect(path)
            db.execute("DELETE FROM meta WHERE key IN ('bounds', "
                       "'lane_count')")
            db.commit()
            db.close()
        cs = os.path.join(tmp, "d.tbchange")
        build_changeset.build_changeset(before, after, cs)
        db = sqlite3.connect(cs)
        keys = {k for (k,) in db.execute("SELECT key FROM meta")}
        empty = [k for (k,) in db.execute(
            "SELECT key FROM meta WHERE value IS NULL OR value = ''")]
        db.close()
        check("no bounds key at all", "bounds" not in keys)
        check("and no empty value anywhere", not empty, empty)


def test_an_unchanged_side_table_costs_nothing():
    print("a build that moved only one way carries no pois, fords or gauges")
    with tempfile.TemporaryDirectory() as tmp:
        before = os.path.join(tmp, "before.tbmap")
        after = os.path.join(tmp, "after.tbmap")
        write_live_shape(before, _WAYS, _POIS, _FORDS, _RAIN, _LEVEL,
                         "2026-09-01T00:00:00Z", datetime.date(2026, 9, 1))
        ways = list(_WAYS)
        ways[3] = _way("W4", -1.670, 52.506, name="Lane W4, renamed")
        write_live_shape(after, ways, _POIS, _FORDS, _RAIN, _LEVEL,
                         "2026-10-01T00:00:00Z", datetime.date(2026, 9, 1))
        cs = os.path.join(tmp, "d.tbchange")
        stats = build_changeset.build_changeset(before, after, cs)
        moved = {t: s for t, s in stats["tables"].items()
                 if any(s.values())}
        check("only the way moved", moved == {
            "ways": {"changed": 1, "added": 0, "removed": 0}}, moved)
        working = os.path.join(tmp, "w.tbmap")
        shutil.copyfile(before, working)
        apply_v2(working, cs)
        check("and it still lands on the new build",
              every_table(working) == every_table(after))


def test_a_row_that_changes_key_lands_on_its_new_key():
    print("a ford renumbered by a newcomer lands on its new rowid")
    # build_fords numbers fords 1..N in uid order, so a new ford whose uid
    # sorts first moves every other ford up one WITHOUT changing a value in
    # it. Matched on the uid, those rows read as unchanged and are not
    # carried - and the rider's fords_bbox then points each box at the wrong
    # ford. Matched on the rowid, they are carried, and delete-then-insert
    # lets each land on a key the previous occupant has just left.
    with tempfile.TemporaryDirectory() as tmp:
        before = os.path.join(tmp, "before.tbmap")
        after = os.path.join(tmp, "after.tbmap")
        fords = _FORDS[:2]
        write_live_shape(before, _WAYS, _POIS, fords, _RAIN, _LEVEL,
                         "2026-09-01T00:00:00Z", datetime.date(2026, 9, 1))
        write_live_shape(after, _WAYS, _POIS,
                         [_ford("osm:n9000", 52.5012, -1.6985)] + fords,
                         _RAIN, _LEVEL, "2026-10-01T00:00:00Z",
                         datetime.date(2026, 9, 1))
        db = sqlite3.connect(before)
        was = dict(db.execute("SELECT ford_uid, rowid FROM fords"))
        db.close()
        db = sqlite3.connect(after)
        now = dict(db.execute("SELECT ford_uid, rowid FROM fords"))
        db.close()
        check("the fixture really renumbers", was["osm:n9001"] != now["osm:n9001"],
              "%r vs %r" % (was, now))
        cs = os.path.join(tmp, "d.tbchange")
        build_changeset.build_changeset(before, after, cs)
        working = os.path.join(tmp, "w.tbmap")
        shutil.copyfile(before, working)
        apply_v2(working, cs)
        got, want = every_table(working), every_table(after)
        for table in ("fords", "fords_bbox"):
            check("%s is the new build's, row for row" % table,
                  got[table] == want[table], "%r vs %r" % (got[table],
                                                           want[table]))


def _refused(before, after, tmp):
    try:
        build_changeset.build_changeset(before, after,
                                        os.path.join(tmp, "d.tbchange"))
    except SystemExit as e:
        return str(e)
    return None


def test_a_column_set_mismatch_is_refused_either_way():
    print("a column in one build and not the other is refused, both ways")
    for side in ("old", "new"):
        for table in ("pois", "fords", "way_wetness", "ways"):
            with tempfile.TemporaryDirectory() as tmp:
                before, after = _live_pair(tmp)
                db = sqlite3.connect(before if side == "old" else after)
                db.execute("ALTER TABLE %s ADD COLUMN extra TEXT" % table)
                db.commit()
                db.close()
                check("an extra %s column in the %s build" % (table, side),
                      _refused(before, after, tmp) is not None)


def test_a_table_in_one_build_only_is_refused():
    print("a table one build lacks cannot be bridged by a changeset")
    for side in ("old", "new"):
        with tempfile.TemporaryDirectory() as tmp:
            before, after = _live_pair(tmp)
            db = sqlite3.connect(before if side == "old" else after)
            db.execute("DROP TABLE ford_gauges")
            db.commit()
            db.close()
            why = _refused(before, after, tmp)
            check("ford_gauges only in the %s build"
                  % ("new" if side == "old" else "old"),
                  why is not None and "ford_gauges" in why, why)


def main():
    for fn in (test_applying_gets_the_new_build,
               test_a_removed_right_of_way_really_goes,
               test_an_unchanged_build_is_almost_nothing,
               test_two_builds_that_claim_to_be_the_same_are_refused,
               test_same_size_different_bytes_is_caught,
               test_every_live_table_round_trips,
               test_the_new_builds_meta_is_restated,
               test_a_meta_key_the_new_build_dropped_is_removed,
               test_an_absent_meta_key_is_not_written_empty,
               test_an_unchanged_side_table_costs_nothing,
               test_a_row_that_changes_key_lands_on_its_new_key,
               test_a_column_set_mismatch_is_refused_either_way,
               test_a_table_in_one_build_only_is_refused):
        fn()
    print()
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all changeset tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

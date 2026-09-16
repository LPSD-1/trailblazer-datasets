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
                   "(SELECT lane_uid FROM cs.removed_lanes)")
        db.execute("DELETE FROM lanes_bbox WHERE id NOT IN "
                   "(SELECT rowid FROM lanes)")
        db.execute("DELETE FROM tiles WHERE (zoom_level, tile_column, tile_row) "
                   "IN (SELECT zoom_level, tile_column, tile_row FROM cs.removed_tiles)")
        db.execute("INSERT OR REPLACE INTO tiles "
                   "SELECT zoom_level, tile_column, tile_row, tile_data FROM cs.tiles")
        db.execute("INSERT OR REPLACE INTO lanes SELECT * FROM cs.lanes")
        db.execute("DELETE FROM lanes_bbox WHERE id IN "
                   "(SELECT id FROM cs.lanes_bbox_rows)")
        db.execute("INSERT INTO lanes_bbox SELECT * FROM cs.lanes_bbox_rows")
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
        check("one lane amended", stats["lanes_changed"] == 1, stats)
        check("one lane added", stats["lanes_added"] == 1, stats)
        check("one lane removed", stats["lanes_removed"] == 1, stats)
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
        check("no rows", stats["lanes_changed"] == 0 and stats["tiles_changed"] == 0,
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


def main():
    for fn in (test_applying_gets_the_new_build,
               test_a_removed_right_of_way_really_goes,
               test_an_unchanged_build_is_almost_nothing,
               test_two_builds_that_claim_to_be_the_same_are_refused,
               test_same_size_different_bytes_is_caught):
        fn()
    print()
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all changeset tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

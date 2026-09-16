"""Diff two builds of an area into a `.tbchange`.

A rider who already holds build 104 of an area should not download build 105
whole to learn that twelve lanes were amended. This writes what changed.

A CHANGESET IS NOT A BINARY PATCH. It is the same SQLite container carrying only
the differences, plus a list of what went away:

    tiles        the tiles whose bytes differ, and the ones newly present
    lanes        the rows that differ, and the ones newly present
    removed      lane_uids and (z,x,y) that are in the old build and not the new
    meta         from_build, to_build, format_version

The app applies it in ONE transaction against a database whose build it has
checked first, and verifies the result against the signature of the build it
claims to produce. Either the area is provably at 105, or it rolled back and is
still at 104. There is no third state, which is exactly what a patch chain could
not promise.

Usage:
    python tools/build_changeset.py OLD.tbmap NEW.tbmap OUT.tbchange
"""

import argparse
import os
import sqlite3
import sys

SCHEMA = """
CREATE TABLE tiles (
  zoom_level  INTEGER NOT NULL,
  tile_column INTEGER NOT NULL,
  tile_row    INTEGER NOT NULL,
  tile_data   BLOB    NOT NULL,
  PRIMARY KEY (zoom_level, tile_column, tile_row)
) WITHOUT ROWID;

CREATE TABLE lanes (
  rowid             INTEGER PRIMARY KEY,
  lane_uid          TEXT UNIQUE NOT NULL,
  lane_class        TEXT NOT NULL,
  county            TEXT,
  name              TEXT,
  designation       TEXT,
  description       TEXT,
  authority         TEXT,
  vehicle_access    INTEGER,
  length_m          REAL,
  geometry          BLOB NOT NULL
);

CREATE TABLE lanes_bbox_rows (
  id      INTEGER PRIMARY KEY,
  min_lon REAL, max_lon REAL, min_lat REAL, max_lat REAL
);

-- What the new build does NOT have. Without this a changeset can only ever add,
-- and a right of way a council has REMOVED would stay on a rider's map for as
-- long as they never did a full download - which is the one direction this app
-- must not be wrong in.
CREATE TABLE removed_lanes (lane_uid TEXT PRIMARY KEY);
CREATE TABLE removed_tiles (
  zoom_level INTEGER NOT NULL,
  tile_column INTEGER NOT NULL,
  tile_row INTEGER NOT NULL,
  PRIMARY KEY (zoom_level, tile_column, tile_row)
) WITHOUT ROWID;

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

LANE_COLUMNS = ("lane_uid", "lane_class", "county", "name", "designation",
                "description", "authority", "vehicle_access", "length_m",
                "geometry")


def _meta(db):
    return {k: v for k, v in db.execute("SELECT key, value FROM meta")}


def build_changeset(old_path, new_path, out_path):
    old = sqlite3.connect("file:%s?mode=ro" % old_path.replace("\\", "/"), uri=True)
    new = sqlite3.connect("file:%s?mode=ro" % new_path.replace("\\", "/"), uri=True)

    # CLOSED ON EVERY PATH, including the refusals below. Leaving them open
    # made the tests fail on Windows, where an open handle stops the file being
    # deleted - and in the pipeline it would be a handle held for the rest of a
    # matrix job.
    try:
        return _build(old, new, out_path)
    finally:
        old.close()
        new.close()


def _build(old, new, out_path):
    old_meta, new_meta = _meta(old), _meta(new)
    if old_meta.get("kind") != new_meta.get("kind"):
        raise SystemExit("These are different kinds of container.")
    if old_meta.get("built_at") == new_meta.get("built_at"):
        raise SystemExit(
            "Both builds carry the same built_at. Either nothing changed, or "
            "the builder is stamping a value that does not follow the data - "
            "and a changeset between two builds the app cannot tell apart is "
            "worse than no changeset."
        )

    if os.path.exists(out_path):
        os.remove(out_path)
    out = sqlite3.connect(out_path)
    out.executescript(SCHEMA)

    # ---- tiles ------------------------------------------------------------
    # Read keys first and bodies only where the keys match, so a national
    # container is never held twice in memory to be compared with itself.
    old_tiles = {(z, x, y): n for z, x, y, n in old.execute(
        "SELECT zoom_level, tile_column, tile_row, LENGTH(tile_data) FROM tiles")}
    changed = added = 0
    for z, x, y, blob in new.execute(
            "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"):
        key = (z, x, y)
        if key not in old_tiles:
            out.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, y, blob))
            added += 1
            continue
        if old_tiles[key] != len(blob):
            out.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, y, blob))
            changed += 1
            old_tiles.pop(key)
            continue
        # Same length: compare the bytes, because a lane moving a few metres
        # changes content and not size.
        before = old.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? "
            "AND tile_row=?", key).fetchone()[0]
        if before != blob:
            out.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, y, blob))
            changed += 1
        old_tiles.pop(key)
    for (z, x, y) in old_tiles:
        out.execute("INSERT INTO removed_tiles VALUES (?,?,?)", (z, x, y))

    # ---- lanes ------------------------------------------------------------
    columns = ", ".join(LANE_COLUMNS)
    before_rows = {}
    for row in old.execute("SELECT %s FROM lanes" % columns):
        before_rows[row[0]] = row

    lane_changed = lane_added = 0
    for row in new.execute("SELECT rowid, %s FROM lanes" % columns):
        rowid, body = row[0], row[1:]
        uid = body[0]
        was = before_rows.pop(uid, None)
        if was is None:
            lane_added += 1
        elif was == body:
            continue
        else:
            lane_changed += 1
        out.execute(
            "INSERT INTO lanes VALUES (?,%s)" % ",".join("?" * len(body)),
            (rowid,) + body)
        bbox = new.execute(
            "SELECT min_lon, max_lon, min_lat, max_lat FROM lanes_bbox "
            "WHERE id = ?", (rowid,)).fetchone()
        if bbox:
            out.execute("INSERT INTO lanes_bbox_rows VALUES (?,?,?,?,?)",
                        (rowid,) + bbox)

    for uid in before_rows:
        out.execute("INSERT INTO removed_lanes VALUES (?)", (uid,))

    for key, value in (
            ("format_version", new_meta.get("format_version", "1")),
            ("kind", "changeset"),
            ("from_build", old_meta.get("built_at", "")),
            ("to_build", new_meta.get("built_at", "")),
            ("bounds", new_meta.get("bounds", "")),
            ("min_zoom", new_meta.get("min_zoom", "")),
            ("max_zoom", new_meta.get("max_zoom", "")),
            ("lane_count", new_meta.get("lane_count", ""))):
        out.execute("INSERT INTO meta VALUES (?,?)", (key, value))

    out.commit()
    out.execute("VACUUM")
    out.close()

    return {
        "tiles_changed": changed, "tiles_added": added,
        "tiles_removed": len(old_tiles),
        "lanes_changed": lane_changed, "lanes_added": lane_added,
        "lanes_removed": len(before_rows),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("out")
    args = ap.parse_args()

    stats = build_changeset(args.old, args.new, args.out)
    whole = os.path.getsize(args.new)
    delta = os.path.getsize(args.out)
    print(args.out)
    for key in sorted(stats):
        print("  %-15s %d" % (key, stats[key]))
    print("  changeset       %.2f MB" % (delta / 1048576.0))
    print("  whole build     %.2f MB" % (whole / 1048576.0))
    print("  saving          %.1f%%" % (100.0 * (1 - delta / float(whole))))
    return 0


if __name__ == "__main__":
    sys.exit(main())

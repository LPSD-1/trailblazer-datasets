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

CREATE TABLE bbox_rows (
  id      INTEGER PRIMARY KEY,
  min_lon REAL, max_lon REAL, min_lat REAL, max_lat REAL
);

-- What the new build does NOT have. Without this a changeset can only ever add,
-- and a right of way a council has REMOVED would stay on a rider's map for as
-- long as they never did a full download - which is the one direction this app
-- must not be wrong in.
CREATE TABLE removed_records (uid TEXT PRIMARY KEY);
CREATE TABLE removed_tiles (
  zoom_level INTEGER NOT NULL,
  tile_column INTEGER NOT NULL,
  tile_row INTEGER NOT NULL,
  PRIMARY KEY (zoom_level, tile_column, tile_row)
) WITHOUT ROWID;

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

#: The record table and its key, per container kind.
#:
#: Orders need this as much as lanes do and arguably more: lane data changes
#: when a council amends a definitive map, and orders change four times a day.
#: A national orders container is 8.4 MB, and a rider downloading that every six
#: hours to learn about a handful of new closures is the case changesets exist
#: for.
#: `ways` WAS MISSING, AND THAT STOPPED CHANGESETS ALTOGETHER.
#:
#: This detection asks sqlite_master for type='table' only - correctly, since
#: the compat `lanes` is a VIEW and diffing through it would drop every column
#: the pivot added. But with `ways` absent from this map the search found
#: NOTHING in a post-pivot container and raised "Expected exactly one record
#: table; found []", so no changeset could be built for any container the
#: pipeline now produces.
#:
#: That is the mechanism the 4x-daily refresh rests on: a rider downloading a
#: whole container every six hours to learn about a handful of new closures is
#: the case changesets exist for. It had been dead since step 1.2 and the only
#: thing that would have said so - `validate_changeset.py --selftest` - was
#: itself crashing earlier, on a container path that no longer existed.
RECORD_TABLES = {
    "ways": "way_uid",
    "lanes": "lane_uid",
    "orders": "tro_uid",
}


def _record_table(db):
    """Which record table this container carries, and what identifies a row.

    Detected rather than assumed from `kind`, because a container whose meta
    says one thing and whose schema says another should fail here rather than
    produce a changeset that silently drops every record.
    """
    names = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    found = [t for t in RECORD_TABLES if t in names]
    if len(found) != 1:
        raise SystemExit(
            "Expected exactly one record table (%s); found %s."
            % (", ".join(sorted(RECORD_TABLES)), sorted(found)))
    return found[0], RECORD_TABLES[found[0]]


def _columns(db, table):
    """Every column but the rowid, in declaration order."""
    return [row[1] for row in db.execute("PRAGMA table_info(%s)" % table)
            if row[1] != "rowid"]


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

    # ---- records ----------------------------------------------------------
    table, key = _record_table(new)
    old_table, _ = _record_table(old)
    if old_table != table:
        raise SystemExit("These containers hold different record tables.")

    columns = _columns(new, table)
    if _columns(old, table) != columns:
        raise SystemExit(
            "The two builds have different columns in `%s`. A changeset cannot "
            "bridge a schema change; the rider needs the whole build." % table)

    # The changeset carries a copy of the source table, so applying it is an
    # INSERT OR REPLACE and nothing has to know the column list at apply time.
    out.execute("CREATE TABLE %s (rowid INTEGER PRIMARY KEY, %s)"
                % (table, ", ".join("%s BLOB" % c for c in columns)))

    key_at = columns.index(key)
    joined = ", ".join(columns)
    before_rows = {}
    for row in old.execute("SELECT %s FROM %s" % (joined, table)):
        before_rows.setdefault(row[key_at], []).append(row)

    rec_changed = rec_added = 0
    for row in new.execute("SELECT rowid, %s FROM %s" % (joined, table)):
        rowid, body = row[0], row[1:]
        uid = body[key_at]
        was = before_rows.get(uid)
        if was is None:
            rec_added += 1
        elif body in was:
            # Unchanged. Removed from the pending set so it is not reported as
            # deleted - a uid can carry several rows (60 orders nationally do).
            was.remove(body)
            if not was:
                before_rows.pop(uid)
            continue
        else:
            rec_changed += 1
            was.remove(was[0])
            if not was:
                before_rows.pop(uid)
        out.execute(
            "INSERT INTO %s VALUES (?,%s)" % (table, ",".join("?" * len(body))),
            (rowid,) + body)
        bbox = new.execute(
            "SELECT min_lon, max_lon, min_lat, max_lat FROM %s_bbox "
            "WHERE id = ?" % table, (rowid,)).fetchone()
        if bbox:
            out.execute("INSERT INTO bbox_rows VALUES (?,?,?,?,?)",
                        (rowid,) + bbox)

    for uid in before_rows:
        out.execute("INSERT INTO removed_records VALUES (?)", (uid,))

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
        "record_table": table,
        "records_changed": rec_changed, "records_added": rec_added,
        "records_removed": len(before_rows),
        # WHAT IT WEIGHS, AND WHAT IT WOULD REPLACE. The catalogue publishes
        # both, and the app refuses a chain that is not actually smaller than
        # the container - so the figures have to leave this function rather
        # than being printed and thrown away.
        "bytes": os.path.getsize(out_path),
        "from_build": old_meta.get("built_at", ""),
        "to_build": new_meta.get("built_at", ""),
    }


#: The most of a container a changeset may weigh and still be published.
MAX_RATIO = 0.75


def is_worth_publishing(changeset_bytes, whole_bytes, max_ratio=MAX_RATIO):
    """Whether this changeset is small enough to be worth a rider's data.

    DO NOT PUBLISH A CHANGESET BIGGER THAN THE THING IT REPLACES. It is not a
    theoretical case: a container whose tiles were ALL re-cut - a zoom range
    changing, a tile encoder improving - has every tile in the diff plus a
    schema and a removal table on top, so the "saving" goes negative. The rider
    then pays twice, once for a download larger than the container and again
    for the work of applying it, and ends up exactly where a plain download
    would have put them.

    Measured on the two published South West builds - see
    tools/test_publish_changesets.py, which measures it again on every run
    rather than trusting this sentence.

    [max_ratio] is deliberately well under 1.0. A changeset at 95% of the
    container saves nothing worth the risk of applying it in place.
    """
    if whole_bytes <= 0:
        return False
    return changeset_bytes < whole_bytes * max_ratio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("out")
    ap.add_argument("--require-smaller", action="store_true",
                    help="exit 1 if the changeset is not worth publishing; "
                         "the file is still written so it can be looked at")
    ap.add_argument("--max-ratio", type=float, default=MAX_RATIO,
                    help="the most of the whole build a changeset may weigh "
                         "(default %g)" % MAX_RATIO)
    args = ap.parse_args()

    stats = build_changeset(args.old, args.new, args.out)
    whole = os.path.getsize(args.new)
    delta = os.path.getsize(args.out)
    print(args.out)
    for key in sorted(stats):
        value = stats[key]
        print("  %-17s %s" % (key, value))
    print("  changeset       %.2f MB" % (delta / 1048576.0))
    print("  whole build     %.2f MB" % (whole / 1048576.0))
    print("  saving          %.1f%%" % (100.0 * (1 - delta / float(whole))))
    print("  ratio           %.3f" % (delta / float(whole)))
    if not is_worth_publishing(delta, whole, args.max_ratio):
        print("  NOT WORTH PUBLISHING: a rider would fetch more than the "
              "whole build and then have to apply it.")
        if args.require_smaller:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

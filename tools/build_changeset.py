"""Diff two builds of an area into a `.tbchange`.

A rider who already holds build 104 of an area should not download build 105
whole to learn that twelve lanes were amended. This writes what changed.

A CHANGESET IS NOT A BINARY PATCH. It is a SQLite file carrying only the
differences, plus a list of what went away:

    tiles          the tiles whose bytes differ, and the ones newly present
    removed_tiles  (z,x,y) in the old build and not the new
    <table>        ONE PER TABLE THE CONTAINER HOLDS, same name: ways, pois,
                   fords, way_wetness, wet_gauges, ford_gauges, and each
                   r-tree (ways_bbox, pois_bbox, fords_bbox) as a plain table.
                   Each holds the rows whose key is new or whose values
                   differ, with the key the container uses (see `carries`)
    removed_rows   (tbl, id): rows the old build has and the new one does not,
                   by that same key
    meta           EVERY key of the new build's meta restated (built_at as
                   to_build), plus the bookkeeping: format_version, kind =
                   'changeset', from_build, to_build, `carries` and
                   `removed_meta`
    bbox_rows,     the record table's r-tree rows and removed uids exactly as
    removed_records  format 1 carried them - a mirror of what `ways_bbox` and
                   `removed_rows` already say, kept so the file an applier
                   that knows only the record table opens is still well formed

`carries` is JSON, {table: key column}, naming every table the changeset
brings up to date and the column its rows are matched on: `rowid` for a table
whose rowid is implicit (ways, pois, fords), the INTEGER PRIMARY KEY for one
that declares it (way_wetness.id, wet_gauges.id, ford_gauges.id), `id` for an
r-tree. It is the whole of the container's tables but `meta`, `tiles` and the
r-trees' own shadow tables - so an applier can check, before it writes a byte,
that the changeset carries everything the file it is about to change holds.

`removed_meta` is a JSON list of the keys the old build's meta had and the new
one does not.

APPLYING IT, in ONE transaction, against a container whose `built_at` equals
`from_build`:

    for table, key in carries:
        DELETE FROM table WHERE key IN
            (SELECT id FROM cs.removed_rows WHERE tbl = table)
        DELETE FROM table WHERE key IN (SELECT key FROM cs.table)
        INSERT INTO table (columns) SELECT columns FROM cs.table
            -- columns: the cs table's, which are the container's own with
            -- `rowid` first where the container's rowid is implicit
    DELETE FROM tiles WHERE (z,x,y) IN cs.removed_tiles
    INSERT OR REPLACE INTO tiles SELECT * FROM cs.tiles
    meta: every cs.meta key but the bookkeeping, written over; every key in
          removed_meta deleted; built_at = to_build

and the result is the new build ROW FOR ROW, rowids included: a row the two
builds agree on (same key, same values) is left alone, every other row the new
build has is written with its own key, and every key the new build lacks is
deleted. Delete-then-insert rather than INSERT OR REPLACE because an r-tree
has no conflict clause to lean on, and because a row can be re-keyed between
builds (a POI's rowid is its position in uid order) - the delete clears its
old key first, and the one it lands on is either free or carried too.
`tools/test_build_changeset.py` applies exactly this and compares every table.

The app applies it and verifies the result against the signature of the build
it claims to produce. Either the area is provably at 105, or it rolled back and
is still at 104. There is no third state, which is exactly what a patch chain
could not promise.

A SCHEMA CHANGE IS NOT BRIDGED. Two builds whose sqlite_master differs in any
table, index, view or column are refused here: a changeset cannot create a
table or add a column, and one that pretended to would leave a rider with the
old shape stamped as the new build. The rider takes the whole file.

Usage:
    python tools/build_changeset.py OLD.tbmap NEW.tbmap OUT.tbchange
"""

import argparse
import json
import os
import re
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
-- The same, for every table in `carries`, by the key `carries` names.
CREATE TABLE removed_rows (
  tbl TEXT    NOT NULL,
  id  INTEGER NOT NULL,
  PRIMARY KEY (tbl, id)
) WITHOUT ROWID;

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

#: The changeset's own tables. A container table with one of these names would
#: be indistinguishable from the changeset's bookkeeping, so it is refused.
OWN_TABLES = ("tiles", "bbox_rows", "removed_records", "removed_tiles",
              "removed_rows", "meta")

#: Meta keys the changeset writes for itself. Everything else in the new
#: build's meta is restated verbatim.
BOOKKEEPING = ("format_version", "kind", "from_build", "to_build", "carries",
               "removed_meta")

#: Keys of the new build's meta that are NOT restated: `built_at` is
#: `to_build`, and `kind` is checked equal in both builds and would collide
#: with the changeset's own.
NOT_RESTATED = ("built_at", "kind")

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
    """Every declared column, in declaration order - a column literally named
    `rowid` included, since in a table that declares one it IS the key."""
    return [row[1] for row in db.execute('PRAGMA table_info("%s")' % table)]


def _meta(db):
    return {k: v for k, v in db.execute("SELECT key, value FROM meta")}


def _schema(db):
    """Everything sqlite_master says, in a stable order.

    Compared whole between the two builds: a column added, a table appearing
    (the first build to carry fords), an index or a view redefined - any of
    them is a shape a changeset cannot bring a rider's file to.
    """
    return sorted(tuple("" if v is None else v for v in row)
                  for row in db.execute(
                      "SELECT type, name, tbl_name, sql FROM sqlite_master"))


_USING = re.compile(r"\bUSING\s+(\w+)", re.I)


def carried_tables(db):
    """{table: key column} for every table a changeset must bring up to date.

    Every table but the ones the changeset handles on its own terms (`tiles`,
    `meta`), SQLite's own, and an r-tree's shadow tables - which follow the
    r-tree's rows wherever they go and must never be written directly.

    THE KEY IS THE ONE THE CONTAINER USES, not a uid. `way_wetness.id` and
    `fords.way_id` are ways ROWIDS, and each r-tree's `id` is its table's
    rowid, so a changeset that matched rows on anything else could reproduce
    every value and still leave the joins pointing at the wrong rows.
    """
    tables = db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "ORDER BY name").fetchall()
    virtual = {}
    for name, sql in tables:
        if (sql or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
            match = _USING.search(sql)
            module = match.group(1).lower() if match else "?"
            if module != "rtree":
                raise SystemExit(
                    "`%s` is a %s virtual table, and a changeset only knows "
                    "how to carry an r-tree." % (name, module))
            virtual[name] = module
    shadow = {"%s_%s" % (v, part) for v in virtual
              for part in ("node", "parent", "rowid")}

    out = {}
    for name, sql in tables:
        if name in ("tiles", "meta") or name.startswith("sqlite_") \
                or name in shadow:
            continue
        if name in OWN_TABLES:
            raise SystemExit(
                "The container has a table called `%s`, which is one of the "
                "changeset's own." % name)
        info = db.execute('PRAGMA table_info("%s")' % name).fetchall()
        if name in virtual:
            # An r-tree's first column is its id, whatever it is called.
            out[name] = info[0][1]
            continue
        if re.search(r"\bWITHOUT\s+ROWID\b", sql or "", re.I):
            raise SystemExit(
                "`%s` is WITHOUT ROWID; a changeset keys rows by rowid." % name)
        pk = [row for row in info if row[5]]
        if len(pk) == 1 and (pk[0][2] or "").upper() == "INTEGER":
            out[name] = pk[0][1]      # an alias of the rowid
        else:
            out[name] = "rowid"       # implicit
    return out


def _canon(row):
    """A row as something that compares equal only to the same stored values.

    The storage class counts: 1 and 1.0 are equal in Python and are not the
    same bytes in a column with no affinity.
    """
    return tuple((type(v).__name__, v) for v in row)


def _quoted(column):
    # `rowid` UNQUOTED: quoted, it names a column, and a table whose rowid is
    # implicit has none by that name.
    return column if column == "rowid" else '"%s"' % column


def _diff_table(old, new, out, table, key):
    """Write [table]'s new and changed rows into [out], and its removals.

    Returns (changed, added, removed keys, carried keys).
    """
    columns = _columns(new, table)
    select = columns if key in columns else ["rowid"] + columns
    at = select.index(key)
    joined = ", ".join(_quoted(c) for c in select)
    declared = ", ".join(
        "%s INTEGER PRIMARY KEY" % _quoted(c) if i == at
        else "%s BLOB" % _quoted(c)
        for i, c in enumerate(select))
    out.execute('CREATE TABLE "%s" (%s)' % (table, declared))
    insert = 'INSERT INTO "%s" (%s) VALUES (%s)' % (
        table, joined, ",".join("?" * len(select)))

    before = {}
    for row in old.execute('SELECT %s FROM "%s"' % (joined, table)):
        before[row[at]] = _canon(row)

    changed = added = 0
    carried = []
    for row in new.execute('SELECT %s FROM "%s"' % (joined, table)):
        k = row[at]
        was = before.pop(k, None)
        if was is None:
            added += 1
        elif was == _canon(row):
            continue
        else:
            changed += 1
        out.execute(insert, row)
        carried.append(k)
    removed = sorted(before)
    out.executemany("INSERT INTO removed_rows VALUES (?,?)",
                    [(table, k) for k in removed])
    return changed, added, removed, carried


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

    table, key = _record_table(new)
    old_table, _ = _record_table(old)
    if old_table != table:
        raise SystemExit("These containers hold different record tables.")
    if _columns(old, table) != _columns(new, table):
        raise SystemExit(
            "The two builds have different columns in `%s`. A changeset cannot "
            "bridge a schema change; the rider needs the whole build." % table)
    old_schema, new_schema = _schema(old), _schema(new)
    if old_schema != new_schema:
        gone = sorted(set(old_schema) - set(new_schema))
        came = sorted(set(new_schema) - set(old_schema))
        raise SystemExit(
            "The two builds have different schemas (only in the old: %s; only "
            "in the new: %s). A changeset cannot create a table or add a "
            "column; the rider needs the whole build."
            % ([r[1] for r in gone], [r[1] for r in came]))
    clash = sorted(k for k in new_meta
                   if k in BOOKKEEPING and k not in ("format_version", "kind"))
    if clash:
        raise SystemExit(
            "The new build's meta has %s, which the changeset uses for its own "
            "bookkeeping." % clash)

    carries = carried_tables(new)

    if os.path.exists(out_path):
        os.remove(out_path)
    out = sqlite3.connect(out_path)
    try:
        out.executescript(SCHEMA)
        stats = _write(old, new, out, table, key, carries, old_meta, new_meta)
        out.commit()
        out.execute("VACUUM")
    finally:
        out.close()
    stats["bytes"] = os.path.getsize(out_path)
    return stats


def _write(old, new, out, table, key, carries, old_meta, new_meta):
    # ---- tiles ------------------------------------------------------------
    # Read keys first and bodies only where the keys match, so a national
    # container is never held twice in memory to be compared with itself.
    old_tiles = {(z, x, y): n for z, x, y, n in old.execute(
        "SELECT zoom_level, tile_column, tile_row, LENGTH(tile_data) FROM tiles")}
    changed = added = 0
    for z, x, y, blob in new.execute(
            "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"):
        tile = (z, x, y)
        if tile not in old_tiles:
            out.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, y, blob))
            added += 1
            continue
        if old_tiles[tile] != len(blob):
            out.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, y, blob))
            changed += 1
            old_tiles.pop(tile)
            continue
        # Same length: compare the bytes, because a lane moving a few metres
        # changes content and not size.
        before = old.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? "
            "AND tile_row=?", tile).fetchone()[0]
        if before != blob:
            out.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, y, blob))
            changed += 1
        old_tiles.pop(tile)
    for (z, x, y) in old_tiles:
        out.execute("INSERT INTO removed_tiles VALUES (?,?,?)", (z, x, y))

    # ---- every table ------------------------------------------------------
    # EVERY ONE, NOT JUST THE RECORD TABLE. A ways container also holds pois,
    # fords, ford gauges, way wetness and wet gauges, and a changeset that
    # carried only ways left the app two choices: refuse it - every region
    # update became a whole download, which is what the live set did - or
    # stamp the new build over last build's pois, fords and wetness (and
    # wetness rows for ways the changeset removed) for good.
    per_table = {}
    record_carried = []
    for name in sorted(carries):
        c, a, removed, carried = _diff_table(old, new, out, name, carries[name])
        per_table[name] = {"changed": c, "added": a, "removed": len(removed)}
        if name == table:
            record_carried = carried

    # ---- the format-1 mirror for the record table -------------------------
    # By uid, as format 1 said it. A way's rowid is derived from its uid
    # (build_map_container.stable_id), so this names the same rows
    # `removed_rows` names for `ways`.
    old_uids = {r[0] for r in old.execute(
        'SELECT "%s" FROM "%s"' % (key, table))}
    new_uids = {r[0] for r in new.execute(
        'SELECT "%s" FROM "%s"' % (key, table))}
    gone_uids = sorted(u for u in old_uids - new_uids if u is not None)
    out.executemany("INSERT INTO removed_records VALUES (?)",
                    [(u,) for u in gone_uids])
    bbox_table = "%s_bbox" % table
    if bbox_table in carries:
        for rowid in record_carried:
            box = new.execute(
                'SELECT min_lon, max_lon, min_lat, max_lat FROM "%s" '
                "WHERE id = ?" % bbox_table, (rowid,)).fetchone()
            if box:
                out.execute("INSERT INTO bbox_rows VALUES (?,?,?,?,?)",
                            (rowid,) + tuple(box))

    # ---- meta -------------------------------------------------------------
    # EVERY KEY THE NEW BUILD HAS, restated. `evidence_age`, `context_note`,
    # `context_scope`, `schema_version`, the counts - left out, the app either
    # refuses the changeset or keeps last build's value under this build's
    # stamp. Only keys the build HAS are written: format 1 wrote '' for an
    # absent `bounds`, and an applier that took it at its word blanked a
    # rider's bounds.
    removed_meta = sorted(k for k in old_meta if k not in new_meta
                          and k not in NOT_RESTATED)
    rows = [("format_version", new_meta.get("format_version", "1")),
            ("kind", "changeset"),
            ("from_build", old_meta.get("built_at", "")),
            ("to_build", new_meta.get("built_at", "")),
            ("carries", json.dumps(carries, sort_keys=True)),
            ("removed_meta", json.dumps(removed_meta))]
    for k in sorted(new_meta):
        if k in NOT_RESTATED or k in BOOKKEEPING:
            continue
        rows.append((k, new_meta[k]))
    out.executemany("INSERT INTO meta VALUES (?,?)", rows)

    record = per_table.get(table, {"changed": 0, "added": 0, "removed": 0})
    return {
        "tiles_changed": changed, "tiles_added": added,
        "tiles_removed": len(old_tiles),
        "record_table": table,
        "records_changed": record["changed"],
        "records_added": record["added"],
        "records_removed": len(gone_uids),
        "tables": per_table,
        "meta_restated": len(rows) - len(BOOKKEEPING),
        "meta_removed": removed_meta,
        # WHAT IT WEIGHS, AND WHAT IT WOULD REPLACE. The catalogue publishes
        # both, and the app refuses a chain that is not actually smaller than
        # the container - so the figures have to leave this function rather
        # than being printed and thrown away. `bytes` is added by the caller.
        "from_build": old_meta.get("built_at", ""),
        "to_build": new_meta.get("built_at", ""),
    }


def apply_changeset(container, changeset):
    """The algorithm at the top of this file, executable - the reference an
    applier on the device is checked against.

    Refuses, having changed nothing, when the build does not match or when
    the container holds a table `carries` does not name - the refusal the app
    makes, for the reason it makes it.
    """
    db = sqlite3.connect(container, isolation_level=None)
    try:
        db.execute("ATTACH DATABASE ? AS cs", (changeset,))
        meta = dict(db.execute("SELECT key, value FROM cs.meta"))
        held = dict(db.execute("SELECT key, value FROM main.meta"))
        if held.get("built_at") != meta.get("from_build"):
            raise SystemExit("the changeset is %s->%s and the container is at "
                             "%s" % (meta.get("from_build"),
                                     meta.get("to_build"),
                                     held.get("built_at")))
        carries = json.loads(meta["carries"])
        if carried_tables(db) != carries:
            raise SystemExit("the container holds %s and the changeset "
                             "carries %s" % (sorted(carried_tables(db)),
                                             sorted(carries)))
        db.execute("BEGIN")
        try:
            for table, key in sorted(carries.items()):
                columns = [r[1] for r in db.execute(
                    'PRAGMA cs.table_info("%s")' % table)]
                joined = ", ".join(_quoted(c) for c in columns)
                k = _quoted(key)
                db.execute('DELETE FROM main."%s" WHERE %s IN (SELECT id FROM '
                           'cs.removed_rows WHERE tbl = ?)' % (table, k),
                           (table,))
                db.execute('DELETE FROM main."%s" WHERE %s IN (SELECT %s FROM '
                           'cs."%s")' % (table, k, k, table))
                db.execute('INSERT INTO main."%s" (%s) SELECT %s FROM cs."%s"'
                           % (table, joined, joined, table))
            db.execute("DELETE FROM main.tiles WHERE (zoom_level, tile_column, "
                       "tile_row) IN (SELECT zoom_level, tile_column, tile_row "
                       "FROM cs.removed_tiles)")
            db.execute("INSERT OR REPLACE INTO main.tiles SELECT zoom_level, "
                       "tile_column, tile_row, tile_data FROM cs.tiles")
            db.executemany(
                "INSERT OR REPLACE INTO main.meta (key, value) VALUES (?,?)",
                [(k, v) for k, v in meta.items() if k not in BOOKKEEPING])
            db.executemany("DELETE FROM main.meta WHERE key = ?",
                           [(k,) for k in json.loads(meta["removed_meta"])])
            db.execute("UPDATE main.meta SET value = ? WHERE key = 'built_at'",
                       (meta["to_build"],))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    finally:
        db.close()


def snapshot(path):
    """Every table a changeset touches, as sorted rows - rowid first where it
    is implicit, since the wetness, the fords and every r-tree join on it -
    plus `tiles` and `meta`. Two containers with equal snapshots are the same
    build as far as anything that reads them can tell."""
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    try:
        out = {"meta": _meta(db),
               "tiles": sorted(db.execute("SELECT * FROM tiles"))}
        for table, key in carried_tables(db).items():
            select = "*" if key in _columns(db, table) else "rowid, *"
            out[table] = sorted(db.execute('SELECT %s FROM "%s"'
                                           % (select, table)))
        return out
    finally:
        db.close()


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

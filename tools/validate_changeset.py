#!/usr/bin/env python3
"""Refuse to publish a .tbchange that would leave a rider's map wrong.

    python tools/validate_changeset.py dist/changes/*.tbchange
    python tools/validate_changeset.py --selftest      # prove it can refuse

A CHANGESET IS THE ONE ARTEFACT THAT MODIFIES DATA A RIDER ALREADY HAS. Every
other published file replaces itself: a bad container is a bad download, and
the next one fixes it. A changeset is applied INTO a database the rider is
already navigating from, in one transaction, and the app then claims the build
the changeset says it produced. If the artefact is wrong, the phone is wrong
and says it is up to date.

check_pmtiles.py exists because six imagery packs downloaded perfectly and
could not be opened. The equivalents here are the ways a changeset can apply
perfectly and leave the wrong map:

  * TWO BUILDS THE APP CANNOT TELL APART. `from_build` equal to `to_build`
    means the diff was taken between two builds with the same stamp - either
    nothing changed, or the builder is stamping a value that does not follow
    the data. Applying it advances the rider's claimed build and changes
    nothing. `build_changeset.py` refuses to WRITE one; this refuses to publish
    one, which is the case where it was written by an older builder, edited, or
    rebuilt by hand.
  * NOTHING TO APPLY. No tiles, no rows, no removals, and no meta that moves.
    The app runs its transaction, succeeds, and records the new build over
    unchanged data. META IS CONTENT: the evidence_age -> evidence_dates switch
    changes no row of any container, and refusing it as empty cost every
    rider a whole download of all six regions (~94 MB) for two meta keys.
    Given the build the changeset is cut from (`against`, which the publisher
    always has), a restated key counts only when its value differs and a
    removed one only when that build holds it. Without it - the window gate
    in refresh-data.yml holds the files and none of their from-builds - it
    refuses what it can prove: a file that restates no key and removes none.
  * A UID BOTH WRITTEN AND REMOVED. `removed_records` is what makes deletion
    possible at all - a right of way a council has REMOVED must come off the
    rider's map, and that is the one direction this app must not be wrong in.
    A uid in both tables makes the outcome depend on the order the app happens
    to apply them in, and the two outcomes differ by a lane that either is or
    is not on the map.
  * THE SAME FOR TILES. A (z,x,y) written and removed leaves a hole or a stale
    square depending on statement order.
  * AN R-TREE ROW WITH NO RECORD. `bbox_rows` is copied into the target's
    r-tree; a row pointing at a rowid the changeset does not carry puts a
    phantom in the spatial index, where a tap finds a record that is not there.
  * A TABLE THE CHANGESET DOES NOT ACCOUNT FOR. `carries` names every table
    it brings up to date, and the app checks the container it is about to
    change holds nothing else. A table in the file that `carries` does not
    name, or a name in `carries` with no table behind it, is a changeset whose
    author and whose applier disagree about what it does.
  * A ROW BOTH WRITTEN AND REMOVED, in any carried table, by its key - the
    same contradiction as a uid, one level down.
  * A RESTATED VALUE THAT BLANKS ONE. An empty `bounds` or zoom, written over
    the container's, is a map that no longer knows where it is.

WHAT THIS IS NOT. It does not check that the changeset actually turns the old
build into the new one - that needs both builds, and it is what the app's own
verification against the new build's signature does on the device. This is the
artefact check: is it a changeset at all, does it contradict itself, and would
applying it do anything.
"""
import argparse
import json
import os
import pathlib
import sqlite3
import sys

SQLITE_MAGIC = b"SQLite format 3\x00"
FORMAT_VERSION = "1"
REQUIRED_TABLES = ("tiles", "bbox_rows", "removed_records", "removed_tiles",
                   "removed_rows", "meta")
#: `ways` first, for the reason build_changeset.py gives at length: a
#: changeset carries the record table of the container it describes, and since
#: step 1.2 that is `ways`. With it missing, this validator refused every
#: changeset the pipeline can now produce - and it never said so out loud,
#: because its selftest was crashing further up on a container path the pivot
#: had renamed.
RECORD_TABLES = {"ways": "way_uid", "lanes": "lane_uid", "orders": "tro_uid"}
REQUIRED_META = ("format_version", "kind", "from_build", "to_build",
                 "carries", "removed_meta")

#: Restated keys that must never arrive empty: written over the container's,
#: an empty one blanks what the rider has.
NEVER_EMPTY = ("bounds", "min_zoom", "max_zoom")


def _meta(db):
    return {k: v for k, v in db.execute("SELECT key, value FROM meta")}


def _tables(db):
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def problems_with(path, against=None):
    """What would leave a rider's map wrong, or [] for a good changeset.

    `against` is the container the changeset was cut FROM, when the caller
    holds it: it decides whether restated meta actually moves anything.
    """
    if not os.path.isfile(path):
        return ["missing"]
    with open(path, "rb") as fh:
        head = fh.read(len(SQLITE_MAGIC))
    if head != SQLITE_MAGIC:
        return ["not a SQLite database (first bytes are %r); a changeset is "
                "the same container format carrying only the differences"
                % head]
    if os.path.getsize(path) < 512:
        return ["%d bytes: too small to hold a page"
                % os.path.getsize(path)]

    db = sqlite3.connect(
        "%s?mode=ro" % pathlib.Path(path).absolute().as_uri(), uri=True)
    try:
        try:
            bad = [r[0] for r in db.execute("PRAGMA integrity_check")]
        except sqlite3.DatabaseError as e:
            return ["SQLite will not read it: %s" % e]
        if bad != ["ok"]:
            return ["SQLite integrity_check: %s" % "; ".join(bad[:3])]

        out = []
        tables = _tables(db)
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            out.append("no %s table%s. The app applies every one of these in "
                       "one transaction and a missing one is a silent half "
                       "apply." % (", ".join(missing),
                                   "s" if len(missing) > 1 else ""))
        if "meta" not in tables:
            return out

        meta = _meta(db)
        out.extend(_meta_problems(meta))
        from_meta = None
        if against is not None:
            from_meta, why = _from_build_meta(against, meta)
            if why:
                out.append(why)

        found = [t for t in RECORD_TABLES if t in tables]
        if len(found) != 1:
            out.append(
                "expected exactly one record table (%s); found %s. A "
                "changeset that cannot say which records it carries would "
                "silently drop every one of them."
                % (", ".join(sorted(RECORD_TABLES)), sorted(found) or "none"))
            return out

        out.extend(_content_problems(db, tables, found[0],
                                     _meta_moves(meta, from_meta)))
        out.extend(_carried_problems(db, tables, meta))
        return out
    finally:
        db.close()


def _from_build_meta(path, meta):
    """The meta of the build a changeset is checked against, and why it cannot
    be used, if it cannot.

    A BUILD THAT IS NOT `from_build` is refused rather than compared: judged
    against the wrong build's meta, a changeset that moves nothing for the
    rider it is meant for could pass because it moves something for another.
    """
    try:
        db = sqlite3.connect(
            "%s?mode=ro" % pathlib.Path(path).absolute().as_uri(), uri=True)
        try:
            held = _meta(db)
        finally:
            db.close()
    except sqlite3.Error as e:
        return None, "the build it is checked against will not open: %s" % e
    if meta.get("from_build") and held.get("built_at") != meta["from_build"]:
        return None, ("checked against build %r, but from_build is %r"
                      % (held.get("built_at"), meta["from_build"]))
    return held, None


def _meta_moves(meta, from_meta):
    """How many meta keys applying this would change in the rider's container.

    WITH THE FROM-BUILD, exact: a restated key whose value differs, and a
    removed key that build holds. build_changeset restates EVERY key, so
    counting restated keys without it would call every changeset non-empty.

    WITHOUT IT, what the file alone can show: every restated key and every
    removal might move something, so only a file with neither is provably
    empty.
    """
    try:
        gone = json.loads(meta.get("removed_meta") or "[]")
    except ValueError:
        gone = []           # reported by _carried_problems
    if not isinstance(gone, list):
        gone = []           # likewise
    restated = {k: v for k, v in meta.items()
                if k not in REQUIRED_META and k != "built_at"}
    if from_meta is None:
        return len(gone) + len(restated)
    return (sum(1 for k in gone if k in from_meta)
            + sum(1 for k, v in restated.items() if from_meta.get(k) != v))


def _meta_problems(meta):
    out = []
    for key in REQUIRED_META:
        if not meta.get(key):
            out.append("meta has no %s" % key)

    if meta.get("kind") and meta["kind"] != "changeset":
        out.append("kind is %r, not 'changeset'. The app decides how to apply "
                   "a file from this field." % meta["kind"])

    if meta.get("format_version") not in (None, "", FORMAT_VERSION):
        out.append("format_version is %r and the app applies %r"
                   % (meta["format_version"], FORMAT_VERSION))

    for key in NEVER_EMPTY:
        if key in meta and not (meta[key] or "").strip():
            out.append("meta restates %s as empty. Applied, it is written "
                       "over the rider's %s." % (key, key))
    if meta.get("evidence_age"):
        try:
            json.loads(meta["evidence_age"])
        except ValueError:
            out.append("meta.evidence_age is not JSON, so the app cannot say "
                       "how old the answer is")

    # THE ONE build_changeset REFUSES TO WRITE, restated where a hand-made or
    # older artefact can still reach the publish.
    if meta.get("from_build") and meta.get("to_build") \
            and meta["from_build"] == meta["to_build"]:
        out.append(
            "from_build and to_build are both %r. Either nothing changed, or "
            "the builder is stamping a value that does not follow the data - "
            "and a changeset between two builds the app cannot tell apart is "
            "worse than no changeset: it advances the build a rider believes "
            "they hold." % meta["from_build"])
    return out


def _content_problems(db, tables, table, meta_moves):
    out = []
    key = RECORD_TABLES[table]

    def count(name):
        # A MISSING TABLE IS ALREADY REPORTED, and must not become a crash on
        # top of it: dropping `removed_records` made the first draft of this
        # die with OperationalError instead of refusing the artefact, which is
        # a validator failing to validate.
        if name not in tables:
            return 0
        return db.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]

    tiles = count("tiles")
    records = count(table)
    gone_recs = count("removed_records")
    gone_tiles = count("removed_tiles")
    # Every other carried table and its removals count too: a build that
    # moved only its POIs is still a build to apply.
    others = sum(count(name) for name in tables
                 if name not in _OWN and name != table)
    gone_rows = count("removed_rows")

    # NOTHING TO APPLY - and meta is something to apply. Counting only rows
    # refused the evidence_age -> evidence_dates switch, which moves no row,
    # and sent every rider the whole of all six regions instead.
    if not (tiles or records or gone_recs or gone_tiles or others
            or gone_rows or meta_moves):
        out.append(
            "carries no tiles, no records, no removals and no meta the "
            "rider's build does not already hold. Applying it succeeds, "
            "changes nothing, and records a build the rider's data is not "
            "at.")

    # CONTRADICTIONS. Whichever way the app happens to order its statements,
    # one of the two outcomes is wrong, and they differ by a lane being on the
    # map or off it.
    if "removed_records" in tables:
        both = db.execute(
            "SELECT COUNT(*) FROM removed_records WHERE uid IN "
            "(SELECT %s FROM %s)" % (key, table)).fetchone()[0]
        if both:
            example = db.execute(
                "SELECT uid FROM removed_records WHERE uid IN "
                "(SELECT %s FROM %s) LIMIT 1" % (key, table)).fetchone()[0]
            out.append(
                "%d record(s) are both written and removed, e.g. %r. Whether "
                "that lane ends up on the rider's map depends on the order "
                "the app applies two tables in." % (both, example))
        blank = db.execute(
            "SELECT COUNT(*) FROM removed_records WHERE uid IS NULL "
            "OR uid = ''").fetchone()[0]
        if blank:
            out.append("%d removal(s) name no uid" % blank)

    if "removed_tiles" in tables:
        both = db.execute(
            "SELECT COUNT(*) FROM removed_tiles r WHERE EXISTS "
            "(SELECT 1 FROM tiles t WHERE t.zoom_level = r.zoom_level "
            "AND t.tile_column = r.tile_column AND t.tile_row = r.tile_row)"
        ).fetchone()[0]
        if both:
            out.append("%d tile(s) are both written and removed: a hole or a "
                       "stale square, depending on statement order" % both)

    # Coordinates nothing will ever ask for, in either table.
    for name in ("tiles", "removed_tiles"):
        if name not in tables:
            continue
        stray = db.execute(
            "SELECT zoom_level, tile_column, tile_row FROM %s "
            "WHERE tile_column < 0 OR tile_row < 0 "
            "OR tile_column >= (1 << zoom_level) "
            "OR tile_row >= (1 << zoom_level) LIMIT 1" % name).fetchone()
        if stray:
            out.append("%s holds z%d/%d/%d, which does not exist at that zoom "
                       "(the grid is %d wide)"
                       % (name, stray[0], stray[1], stray[2], 1 << stray[0]))

    if tiles:
        empty = db.execute(
            "SELECT COUNT(*) FROM tiles WHERE tile_data IS NULL "
            "OR LENGTH(tile_data) = 0").fetchone()[0]
        if empty:
            out.append(
                "%d tile(s) carry no bytes. Applied, they replace a drawn "
                "tile with an empty one, which reads on the map as 'there is "
                "nothing here'." % empty)

    if records:
        blank = db.execute(
            "SELECT COUNT(*) FROM %s WHERE %s IS NULL OR %s = ''"
            % (table, key, key)).fetchone()[0]
        if blank:
            out.append("%d record(s) carry no %s, so nothing can match them "
                       "against what the rider already holds" % (blank, key))

    # An r-tree row with no record behind it becomes a phantom in the target's
    # spatial index.
    if "bbox_rows" in tables:
        phantom = db.execute(
            "SELECT COUNT(*) FROM bbox_rows WHERE id NOT IN "
            "(SELECT rowid FROM %s)" % table).fetchone()[0]
        if phantom:
            out.append(
                "%d bbox row(s) point at records this changeset does not "
                "carry. Applied, a tap on the map finds a record that is not "
                "there." % phantom)
        bad_box = db.execute(
            "SELECT COUNT(*) FROM bbox_rows WHERE min_lon > max_lon "
            "OR min_lat > max_lat").fetchone()[0]
        if bad_box:
            out.append("%d bbox row(s) are inside out, so the lane they "
                       "describe is findable nowhere" % bad_box)
    return out


_OWN = ("tiles", "bbox_rows", "removed_records", "removed_tiles",
        "removed_rows", "meta")


def _carried_problems(db, tables, meta):
    """`carries` against the tables actually in the file, and removed_rows
    against both."""
    out = []
    raw = meta.get("carries")
    if not raw:
        return out          # already reported as missing meta
    try:
        carries = json.loads(raw)
    except ValueError:
        return ["meta.carries is not JSON"]
    if not isinstance(carries, dict) or not carries:
        return ["meta.carries names no tables"]
    try:
        if not isinstance(json.loads(meta.get("removed_meta") or "[]"),
                          list):
            # Now that a removal alone makes a changeset worth applying,
            # one the app cannot read as a list of keys is not a detail.
            out.append("meta.removed_meta is not a list of keys")
    except ValueError:
        out.append("meta.removed_meta is not JSON")

    absent = sorted(t for t in carries if t not in tables)
    if absent:
        out.append("carries names %s, which the file does not hold. An "
                   "applier that trusts `carries` fails on it; one that "
                   "trusts the tables applies less than it was told."
                   % absent)
    extra = sorted(t for t in tables if t not in _OWN and t not in carries
                   and not t.startswith("sqlite_"))
    if extra:
        out.append("the file holds %s, which carries does not name. The app "
                   "applies what carries names, so these rows would never "
                   "arrive." % extra)

    if "removed_rows" not in tables:
        return out
    stray = [r[0] for r in db.execute(
        "SELECT DISTINCT tbl FROM removed_rows")
        if r[0] not in carries]
    if stray:
        out.append("removed_rows names %s, which carries does not: removals "
                   "nothing will apply" % sorted(stray))
    blank = db.execute("SELECT COUNT(*) FROM removed_rows WHERE id IS NULL"
                       ).fetchone()[0]
    if blank:
        out.append("%d removal(s) in removed_rows name no row" % blank)
    for name, key in sorted(carries.items()):
        if name not in tables:
            continue
        k = key if key == "rowid" else '"%s"' % key
        try:
            both = db.execute(
                'SELECT COUNT(*) FROM "%s" WHERE %s IN (SELECT id FROM '
                'removed_rows WHERE tbl = ?)' % (name, k), (name,)
            ).fetchone()[0]
        except sqlite3.Error as e:
            out.append("`%s` has no column %s, the key carries names (%s)"
                       % (name, key, e))
            continue
        if both:
            out.append("%d row(s) of %s are both written and removed; which "
                       "one the rider keeps depends on statement order"
                       % (both, name))
        columns = [r[1] for r in db.execute('PRAGMA table_info("%s")'
                                            % name)]
        if {"min_lon", "max_lon", "min_lat", "max_lat"} <= set(columns):
            bad = db.execute(
                'SELECT COUNT(*) FROM "%s" WHERE min_lon > max_lon '
                "OR min_lat > max_lat" % name).fetchone()[0]
            if bad:
                out.append("%d %s row(s) are inside out, so what they "
                           "describe is findable nowhere" % (bad, name))
    return out


def report(paths):
    bad = 0
    for path in paths:
        found = problems_with(path)
        name = os.path.basename(path)
        if found:
            bad += 1
            print("REFUSED  %s" % name)
            for p in found:
                print("           %s" % p)
        else:
            print("ok       %s" % name)
    if bad:
        print("\n%d changeset(s) would leave a rider's map wrong and say it "
              "was current." % bad)
    return bad


# --------------------------------------------------------------------------
# proving it can refuse
# --------------------------------------------------------------------------

def _meta_only(before, removed=(), **values):
    """Reduce a copy of a good changeset to meta alone: every row of every
    table gone, every restated key set to what `before` (the from-build)
    already holds, then `removed` named in removed_meta (and not restated)
    and `values` restated over the top.

    The shape of the evidence_age -> evidence_dates switch, which moves no
    row, and of the changeset that moves nothing at all - the two this
    validator must tell apart.
    """
    held = _read_meta(before)
    restated = {k: v for k, v in held.items()
                if k not in REQUIRED_META and k != "built_at"
                and k not in removed}
    restated.update(values)

    def apply(path):
        db = sqlite3.connect(path)
        for name in sorted(_tables(db)):
            if name != "meta" and not name.startswith("sqlite_"):
                db.execute('DELETE FROM "%s"' % name)
        db.execute("DELETE FROM meta WHERE key NOT IN (%s)"
                   % ",".join("?" * len(REQUIRED_META)), REQUIRED_META)
        db.executemany("INSERT INTO meta VALUES (?,?)",
                       sorted(restated.items()))
        db.execute("UPDATE meta SET value=? WHERE key='removed_meta'",
                   (json.dumps(sorted(removed)),))
        db.commit()
        db.close()
    return apply


def _meta_only_changesets(before):
    """Changesets that carry no row and must still be ACCEPTED, because
    applying them moves the rider's meta. Refusing the first of these sent
    every rider all six regions (~94 MB) to receive two meta keys."""
    held = _read_meta(before)
    kept = sorted(k for k in held
                  if k not in REQUIRED_META and k != "built_at")
    return [
        ("a key removed and a new one restated, no row moving (the "
         "evidence_age -> evidence_dates switch)",
         _meta_only(before, removed=(kept[0],),
                    evidence_dates='{"ways": {"newest": "2026-01-14"}}')),
        ("one restated value moved, no row moving",
         _meta_only(before, context_note=(held.get("context_note") or "")
                    + " Amended.")),
    ]


def _read_meta(path):
    db = sqlite3.connect(
        "%s?mode=ro" % pathlib.Path(path).absolute().as_uri(), uri=True)
    try:
        return _meta(db)
    finally:
        db.close()


#: A corruption only the check against the from-build can see. Every other
#: one must be refused by the file alone as well, because the window gate in
#: refresh-data.yml re-checks published changesets without their from-builds.
FROM_BUILD_ONLY = "from-build"


def _corruptions(before):
    def sql(*statements):
        """Apply SQL to a copy of a good changeset.

        `{records}` AND `{uid}` COME FROM THE FILE. A changeset carries the
        record table of the container it describes, so since step 1.2 that is
        `ways` with a `way_uid` - and every statement here said `lanes`. They
        threw "no such table: lanes", which means the corruption was never
        applied, which means the refusal it was supposed to prove was never
        proved.
        """
        def apply(path):
            db = sqlite3.connect(path)
            names = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            records = "ways" if "ways" in names else "lanes"
            uid = "way_uid" if records == "ways" else "lane_uid"
            for s in statements:
                db.execute(s.format(records=records, uid=uid))
            db.commit()
            db.close()
        return apply

    def truncate(path):
        with open(path, "r+b") as fh:
            fh.truncate(4096)

    def rubbish(path):
        with open(path, "wb") as fh:
            fh.write(b"HTTP/1.1 404 Not Found")

    return [
        ("two builds the app cannot tell apart",
         sql("UPDATE meta SET value=(SELECT value FROM meta "
             "WHERE key='from_build') WHERE key='to_build'")),
        ("no from_build at all",
         sql("DELETE FROM meta WHERE key='from_build'")),
        ("a kind the app applies differently",
         sql("UPDATE meta SET value='area' WHERE key='kind'")),
        ("a format version the app does not apply",
         sql("UPDATE meta SET value='2' WHERE key='format_version'")),
        # NOTHING TO APPLY, three ways. Meta is content now, so "every table
        # emptied" alone is a meta-only changeset and may be a real one; what
        # is refused is the one that moves nothing the rider holds.
        ("nothing to apply: no row, no removal, no meta key at all",
         sql("DELETE FROM tiles", "DELETE FROM {records}",
             "DELETE FROM {records}_bbox", "DELETE FROM removed_rows",
             "DELETE FROM bbox_rows", "DELETE FROM removed_records",
             "DELETE FROM removed_tiles",
             "DELETE FROM meta WHERE key NOT IN (%s)"
             % ",".join("'%s'" % k for k in REQUIRED_META))),
        ("nothing to apply: only meta the rider already holds",
         _meta_only(before), FROM_BUILD_ONLY),
        ("nothing to apply: removing only a key the rider does not hold",
         _meta_only(before, removed=("no_such_key",)), FROM_BUILD_ONLY),
        ("removed_meta that is not a list of keys",
         sql("UPDATE meta SET value='{{\"a\": 1}}' "
             "WHERE key='removed_meta'")),
        ("no carries at all",
         sql("DELETE FROM meta WHERE key='carries'")),
        ("carries naming a table that is not there",
         sql("DROP TABLE {records}_bbox")),
        ("a table carries does not name",
         sql("CREATE TABLE pois (rowid INTEGER PRIMARY KEY, poi_uid BLOB)",
             "INSERT INTO pois VALUES (1, 'osm:n1')")),
        ("a row both written and removed, by its key",
         sql("INSERT INTO removed_rows SELECT '{records}', rowid "
             "FROM {records} LIMIT 1")),
        ("a removal for a table nothing carries",
         sql("INSERT INTO removed_rows VALUES ('pois', 1)")),
        ("an empty bounds restated",
         sql("UPDATE meta SET value='' WHERE key='bounds'")),
        ("evidence_age that is not JSON",
         sql("INSERT OR REPLACE INTO meta VALUES ('evidence_age', '{{oops')")),
        ("a carried box inside out",
         sql("UPDATE {records}_bbox SET min_lon = 9.0 "
             "WHERE id = (SELECT MIN(id) FROM {records}_bbox)")),
        ("the generic removals dropped", sql("DROP TABLE removed_rows")),
        ("a lane both written and removed",
         sql("INSERT INTO removed_records SELECT {uid} FROM {records} "
             "LIMIT 1")),
        ("a tile both written and removed",
         sql("INSERT INTO removed_tiles SELECT zoom_level, tile_column, "
             "tile_row FROM tiles LIMIT 1")),
        ("a tile at a coordinate that does not exist",
         sql("INSERT INTO tiles VALUES (11, 1 << 20, 3, x'0a')")),
        ("a tile with no bytes",
         sql("UPDATE tiles SET tile_data = x'' "
             "WHERE zoom_level = (SELECT MIN(zoom_level) FROM tiles)")),
        ("a record with no uid",
         sql("UPDATE {records} SET {uid} = '' "
             "WHERE rowid = (SELECT MIN(rowid) FROM {records})")),
        ("a removal naming nothing",
         sql("INSERT INTO removed_records VALUES ('')")),
        ("a bbox row for a record that is not here",
         sql("INSERT INTO bbox_rows VALUES (999999, -3.9, -3.8, 50.5, 50.6)")),
        ("a bbox row inside out",
         sql("UPDATE bbox_rows SET min_lon = 9.0 "
             "WHERE id = (SELECT MIN(id) FROM bbox_rows)")),
        ("the record table dropped", sql("DROP TABLE {records}")),
        ("the removals table dropped", sql("DROP TABLE removed_records")),
        ("truncated mid-file", truncate),
        ("an error page saved with the right extension", rubbish),
    ]


def _golden_changeset(work):
    """A real changeset: the golden build, and the golden build with one lane
    moved eleven metres, cut five weeks later."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import golden
    import build_changeset

    before = os.path.join(work, "before")
    after = os.path.join(work, "after")
    golden.build_into(before)
    golden.build_into(after, pack_stamp="2026-02-09T10:11:12Z", mutate=True)
    out = os.path.join(work, "golden.tbchange")
    stats = build_changeset.build_changeset(
        golden.container_path(before), golden.container_path(after), out)
    return out, stats, golden.container_path(before)


def selftest():
    import shutil
    import tempfile

    work = tempfile.mkdtemp(prefix="tb-validate-changeset-")
    failures = []
    try:
        good, stats, before = _golden_changeset(work)
        print("golden changeset: %d bytes, %d tiles changed, %d records "
              "added, %d removed"
              % (os.path.getsize(good), stats["tiles_changed"],
                 stats["records_added"], stats["records_removed"]))

        # EACH GOOD ONE BOTH WAYS: as publish_changesets checks it, against
        # the from-build, and as the window gate does, alone.
        goods = [("a correct changeset", None)] + _meta_only_changesets(before)
        for name, reduce in goods:
            path = os.path.join(work, "ok.tbchange")
            shutil.copyfile(good, path)
            if reduce:
                reduce(path)
            found = problems_with(path, against=before) + problems_with(path)
            if found:
                failures.append("REFUSED A GOOD ONE: %s: %s"
                                % (name, "; ".join(found)))
                print("  FAIL   refused %s" % name)
            else:
                print("  ok     accepted %s" % name)

        # And the from-build must BE the from-build.
        found = problems_with(good, against=_golden_after(work))
        if not any("from_build" in p for p in found):
            failures.append("NOT CAUGHT: checked against the wrong build")
            print("  MISSED checked against a build that is not from_build")
        else:
            print("  caught %-46s %s" % ("checked against the wrong build",
                                         found[0][:82]))

        corruptions = _corruptions(before)
        for name, corrupt, *mode in corruptions:
            path = os.path.join(work, "bad.tbchange")
            shutil.copyfile(good, path)
            corrupt(path)
            found = problems_with(path, against=before)
            alone = problems_with(path)
            if not found:
                failures.append("NOT CAUGHT: %s" % name)
                print("  MISSED %s" % name)
            elif not alone and FROM_BUILD_ONLY not in mode:
                failures.append("NOT CAUGHT without the from-build, as the "
                                "window gate checks it: %s" % name)
                print("  MISSED alone %s" % name)
            else:
                print("  caught %-46s %s" % (name[:46], found[0][:82]))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\nSELFTEST FAILED: %d" % len(failures))
        for f in failures:
            print("  " + f)
        return 1
    print("\nselftest ok: %d good changesets accepted, %d corruptions refused"
          % (len(goods), len(corruptions) + 1))
    return 0


def _golden_after(work):
    """The golden build the good changeset was cut TO - a real build, and
    the wrong one to check it against."""
    import golden
    return golden.container_path(os.path.join(work, "after"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("changesets", nargs="*")
    ap.add_argument("--selftest", action="store_true",
                    help="build a golden changeset, corrupt it, and prove "
                         "every corruption is refused")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.changesets:
        ap.error("give at least one changeset, or --selftest")
    return 1 if report(args.changesets) else 0


if __name__ == "__main__":
    sys.exit(main())

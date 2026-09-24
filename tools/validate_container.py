#!/usr/bin/env python3
"""Refuse to publish a .tbmap container a rider's phone cannot use.

    python tools/validate_container.py containers/*.tbmap
    python tools/validate_container.py --strict containers/*.tbmap
    python tools/validate_container.py --selftest      # prove it can refuse

THE MODEL FOR THIS IS check_pmtiles.py, and so is the lesson. Six of the ten
published imagery packs could not be opened by the app at all: they downloaded,
they matched their sha256, and they sat on the phone doing nothing, because
every check the pipeline had asked whether the BYTES were the bytes. A checksum
proves a file arrived intact. It says nothing about whether it was ever right.

A container can fail the same way and has more ways to do it, because it is a
SQLite database with a contract on top:

  * THE DECLARED ZOOM RANGE IS WHAT THE APP ASKS FOR. `min_zoom` and `max_zoom`
    in `meta` tell the app which tiles to request. A container whose tiles sit
    outside that range is a perfect download that draws an empty map, and
    nothing about the file looks wrong.
  * TILE COORDINATES OUT OF RANGE ARE TILES NOBODY WILL EVER FETCH. Rows are
    stored TMS - flipped from the north-up numbering MapLibre asks in - and a
    builder that flips the wrong way, or not at all, puts every tile at a
    coordinate no reader requests.
  * BOUNDS ARE A SAFETY FIELD, not decoration. Section 16.3: the app uses them
    to tell "there are no rights of way here" from "I have nothing downloaded
    for here". An empty or inverted box makes the app confidently say the first
    when it means the second, which is the one direction this app must not be
    wrong in.
  * A RECORD WITHOUT AN R-TREE ROW IS INVISIBLE TO EVERY MAP QUERY. The lane is
    in the file, its legal detail is in the file, and a tap on it finds
    nothing, because the r-tree is what the lookup goes through.

WHAT THIS IS NOT. `check_containers.py` compares containers AGAINST EACH OTHER
- tiles versus records per vehicle, one owning area per lane, the tile size
ceiling - and it assumes each file opens and holds the tables it expects. This
asks the prior question, of one file at a time: will a reader open it, and does
it agree with itself. Run both.

REFUSALS AND NOTES. A refusal is a fault in THIS file. A note is something only
a cross-container check can adjudicate, and it is printed rather than enforced
because it is a legitimate published state: 3 of the 109 containers published
today hold twelve to fifteen thousand lane records and no tiles at all, which
looks alarming and is the boundary rule working. `wales-monmouthshire-and-3-
more` is built after `south-west-monmouthshire-and-4-more`, every one of its
lanes is already owned by that earlier pack, so it ships records and draws
nothing. `--strict` promotes notes to refusals for a build that wants them.
"""
import argparse
import os
import pathlib
import sqlite3
import sys

SQLITE_MAGIC = b"SQLite format 3\x00"

#: The format version the app reads. A container claiming anything else is
#: refused on the device AFTER a full download, so it is refused here instead.
FORMAT_VERSION = "1"

KINDS = ("area", "overview", "both", "orders")
#: The record tables, and the column that identifies a row in each.
#:
#: `ways` WAS MISSING, and the consequence was quiet. A post-pivot container
#: carries a `ways` table and a `lanes` compatibility VIEW over it, and
#: `_tables` lists views alongside tables - so this map matched `lanes`, the
#: validator read every published container THROUGH THE VIEW, and never once
#: looked at `way_uid`, `legal_tier`, `source_date` or `access_evidence`. The
#: view carries none of them. The tool whose job is to refuse a bad container
#: was blind to the entire schema the pivot exists to ship.
RECORD_TABLES = {"ways": "way_uid", "lanes": "lane_uid", "orders": "tro_uid"}

#: The compat objects, and what they are a view OF. A container holding both
#: is the expected arrangement, not an ambiguity - see _record_problems.
COMPAT_OF = {"lanes": "ways"}
REQUIRED_META = ("format_version", "kind", "built_at", "bounds",
                 "min_zoom", "max_zoom")

REFUSE, NOTE = "refuse", "note"


def _meta(db):
    return {k: v for k, v in db.execute("SELECT key, value FROM meta")}


def _views(db):
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'view'")}


def _tables(db):
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def _bounds(text):
    """west,south,east,north as floats, or None if it is not usable."""
    parts = [p.strip() for p in (text or "").split(",")]
    if len(parts) != 4:
        return None
    try:
        w, s, e, n = (float(p) for p in parts)
    except ValueError:
        return None
    if not (-180 <= w <= 180 and -180 <= e <= 180):
        return None
    if not (-90 <= s <= 90 and -90 <= n <= 90):
        return None
    if w > e or s > n:
        return None
    return w, s, e, n


def problems_with(path):
    """[(severity, text)] - empty when the container is fit to publish."""
    if not os.path.isfile(path):
        return [(REFUSE, "missing")]
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(len(SQLITE_MAGIC))
    if head != SQLITE_MAGIC:
        return [(REFUSE, "not a SQLite database (first bytes are %r) - no "
                         "reader on the device will open this" % head)]
    if size < 512:
        return [(REFUSE, "%d bytes; a SQLite file with a page in it cannot be "
                         "this small" % size)]

    out = []
    # READ-ONLY, THROUGH A PROPER FILE URI. "file:C:/..." is read by SQLite as
    # a RELATIVE path called "C:"; pathlib writes the triple-slash form that
    # works and a format string does not. Learned in check_containers.py.
    db = sqlite3.connect(
        "%s?mode=ro" % pathlib.Path(path).absolute().as_uri(), uri=True)
    try:
        try:
            bad = [r[0] for r in db.execute("PRAGMA integrity_check")]
        except sqlite3.DatabaseError as e:
            return [(REFUSE, "SQLite will not read it: %s" % e)]
        if bad != ["ok"]:
            return [(REFUSE, "SQLite integrity_check: %s" % "; ".join(bad[:3]))]

        tables = _tables(db)
        if "meta" not in tables:
            return [(REFUSE, "no meta table, so nothing can say what this "
                             "container is")]
        if "tiles" not in tables:
            out.append((REFUSE, "no tiles table: nothing to draw"))

        meta = _meta(db)
        for key in REQUIRED_META:
            if not meta.get(key):
                out.append((REFUSE, "meta has no %s" % key))
        kind = meta.get("kind")
        if kind and kind not in KINDS:
            out.append((REFUSE, "kind is %r, and the app knows %s"
                        % (kind, ", ".join(KINDS))))
        if meta.get("format_version") not in (None, FORMAT_VERSION):
            out.append((REFUSE,
                        "format_version is %r; the app reads %r and will "
                        "refuse this after a rider has downloaded the whole "
                        "thing" % (meta["format_version"], FORMAT_VERSION)))

        # BOUNDS. Section 16.3 turns on this field being usable.
        if "bounds" in meta and _bounds(meta["bounds"]) is None:
            out.append((REFUSE,
                        "bounds %r is not a usable west,south,east,north box. "
                        "The app cannot tell 'no rights of way here' from "
                        "'nothing downloaded for here' without it."
                        % meta["bounds"]))

        records = _record_problems(db, tables, meta, kind, out)
        if "tiles" in tables:
            _tile_problems(db, meta, records, out)
        return out
    finally:
        db.close()


def _tile_problems(db, meta, records, out):
    n, lo, hi = db.execute(
        "SELECT COUNT(*), MIN(zoom_level), MAX(zoom_level) FROM tiles"
    ).fetchone()
    if not n:
        if records:
            # BY DESIGN, AND MEASURED: see the module docstring. Every lane in
            # here is drawn by an earlier area that claimed it first. Only a
            # check that can see the other containers can say whether that is
            # true, so this names the check rather than guessing.
            out.append((NOTE,
                        "no tiles, but %d records. Legitimate when every lane "
                        "is drawn by an earlier area; check_containers.py's "
                        "agreement check is what adjudicates it." % records))
        else:
            out.append((REFUSE, "no tiles and no records: an empty container"))
        return

    try:
        want_lo = int(meta.get("min_zoom", ""))
        want_hi = int(meta.get("max_zoom", ""))
    except ValueError:
        out.append((REFUSE, "min_zoom/max_zoom are not numbers (%r, %r)"
                    % (meta.get("min_zoom"), meta.get("max_zoom"))))
        want_lo = want_hi = None

    if want_lo is not None:
        if want_lo > want_hi:
            out.append((REFUSE,
                        "declared zooms run z%d to z%d, which is backwards"
                        % (want_lo, want_hi)))
        # THE ONE THAT DOWNLOADS PERFECTLY AND DRAWS NOTHING.
        outside = db.execute(
            "SELECT COUNT(*) FROM tiles WHERE zoom_level < ? OR zoom_level > ?",
            (want_lo, want_hi)).fetchone()[0]
        if outside:
            out.append((REFUSE,
                        "%d of %d tiles are outside the declared range "
                        "z%d-z%d (the file holds z%d-z%d). The app asks only "
                        "inside that range, so those tiles are bytes a rider "
                        "paid for and will never see."
                        % (outside, n, want_lo, want_hi, lo, hi)))

    # Coordinates no reader will ever request, which is how a wrong TMS flip
    # presents: every tile sits at a y the app does not ask for.
    stray = db.execute(
        "SELECT zoom_level, tile_column, tile_row FROM tiles "
        "WHERE tile_column < 0 OR tile_row < 0 "
        "OR tile_column >= (1 << zoom_level) OR tile_row >= (1 << zoom_level) "
        "LIMIT 1").fetchone()
    if stray:
        count = db.execute(
            "SELECT COUNT(*) FROM tiles WHERE tile_column < 0 OR tile_row < 0 "
            "OR tile_column >= (1 << zoom_level) "
            "OR tile_row >= (1 << zoom_level)").fetchone()[0]
        out.append((REFUSE,
                    "%d tiles are at coordinates that do not exist at their "
                    "zoom, e.g. z%d/%d/%d where the grid is %d wide. Nothing "
                    "will ever fetch them."
                    % (count, stray[0], stray[1], stray[2], 1 << stray[0])))

    empty = db.execute(
        "SELECT COUNT(*) FROM tiles WHERE tile_data IS NULL "
        "OR LENGTH(tile_data) = 0").fetchone()[0]
    if empty:
        out.append((REFUSE, "%d tiles have no bytes in them" % empty))


def _record_problems(db, tables, meta, kind, out):
    """Appends to [out]; returns how many records the container holds."""
    found = [t for t in RECORD_TABLES if t in tables]

    # A COMPAT VIEW BESIDE ITS OWN TABLE IS NOT TWO RECORD TABLES. The pivot
    # ships `lanes` as a view over `ways` precisely so an app built before it
    # keeps working, so a container with both is the normal arrangement and
    # the TABLE is the authoritative one. Refusing here would refuse every
    # container the pipeline now builds; not resolving it at all is how the
    # validator ended up reading them all through the view.
    #
    # Two real TABLES is still the ambiguity the rule was written for.
    views = _views(db)
    for compat, base in COMPAT_OF.items():
        if compat in found and base in found and compat in views:
            found.remove(compat)

    if len(found) > 1:
        out.append((REFUSE, "two record tables (%s); nothing downstream knows "
                            "which is authoritative" % ", ".join(sorted(found))))
        return 0

    if not found:
        # An overview carries no records on purpose: it exists to be looked at,
        # and every legal answer comes from an area container.
        if kind and kind != "overview":
            out.append((REFUSE,
                        "kind is %r and there is no record table, so every "
                        "lane in it is drawn and unidentifiable" % kind))
        return 0

    table = found[0]
    try:
        count = db.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    except sqlite3.Error as error:
        # A RECORD TABLE THAT IS THERE AND WILL NOT READ.
        #
        # The pivot ships `lanes` as a compatibility VIEW over `ways`, and a
        # view outlives the table under it: drop `ways` and `lanes` is still
        # listed in sqlite_master, still looks like a record table here, and
        # throws "no such table: main.ways" the moment anything selects from
        # it. That is a real shape for a damaged file to take, and this
        # function threw the error straight out of the validator - so the one
        # tool whose job is to refuse a bad container CRASHED on one instead,
        # and a crash is not a refusal: `validate` returns a count of bad
        # containers, and a process that died returns nothing at all.
        out.append((REFUSE, "the record table %r will not read (%s); a view "
                            "whose table is gone reads as present and answers "
                            "nothing" % (table, error)))
        return 0

    # THE TABLE IS ALWAYS CREATED; only the ROWS say what a container claims.
    # Refusing on the table's presence refused all four published overviews,
    # which carry an empty `lanes` because write_container runs one schema for
    # every kind. The rule is about rows.
    if kind == "overview":
        if count:
            out.append((REFUSE,
                        "an overview holds %d %s rows; overviews answer no "
                        "legal questions and must not look as though they do"
                        % (count, table)))
        return count

    if not count:
        out.append((REFUSE, "%s is empty: nothing in this container can "
                            "answer what a lane is" % table))

    declared = meta.get("lane_count")
    if declared not in (None, "") and declared.isdigit():
        if int(declared) != count:
            out.append((REFUSE,
                        "meta says lane_count=%s and %s holds %d rows. The "
                        "catalogue publishes that number."
                        % (declared, table, count)))

    # A record with no r-tree row is findable by id and invisible to every map
    # query, which is exactly the half-present shape nothing else catches.
    rtree = "%s_bbox" % table
    if rtree not in tables:
        out.append((REFUSE, "no %s r-tree, so a tap on the map can find no "
                            "record at all" % rtree))
    else:
        orphan = db.execute(
            "SELECT COUNT(*) FROM %s WHERE rowid NOT IN (SELECT id FROM %s)"
            % (table, rtree)).fetchone()[0]
        if orphan:
            out.append((REFUSE,
                        "%d of %d rows in %s have no %s entry: present in the "
                        "file, invisible to every map query."
                        % (orphan, count, table, rtree)))
        dangling = db.execute(
            "SELECT COUNT(*) FROM %s WHERE id NOT IN (SELECT rowid FROM %s)"
            % (rtree, table)).fetchone()[0]
        if dangling:
            out.append((REFUSE, "%d %s entries point at rows that are not "
                                "there" % (dangling, rtree)))

    # WRITTEN AGAINST WHICHEVER TABLE IS THERE. This read `if table ==
    # "lanes"`, so the moment the validator was taught to look at `ways` it
    # stopped checking geometry and ids at all - and the selftest said so, in
    # as many words: "MISSED a lane with no geometry". Two of the strongest
    # refusals in this file had been skipping every post-pivot container.
    uid = RECORD_TABLES[table]
    if uid:
        blank = db.execute(
            "SELECT COUNT(*) FROM %s WHERE geometry IS NULL "
            "OR LENGTH(geometry) = 0" % table).fetchone()[0]
        if blank:
            out.append((REFUSE, "%d rows in %s have no geometry"
                        % (blank, table)))
        nameless = db.execute(
            "SELECT COUNT(*) FROM %s WHERE %s IS NULL OR %s = ''"
            % (table, uid, uid)).fetchone()[0]
        if nameless:
            out.append((REFUSE, "%d rows in %s have no %s, so the app cannot "
                                "dedupe them across areas"
                        % (nameless, table, uid)))

    # AND THE ONE RULE THE SCHEMA EXISTS TO ENFORCE, checked at the only place
    # that can see every row of a published file.
    #
    # `build_map_container.check_access_evidence` asserts this over the
    # FEATURES going in. Nothing asserted it over the container coming out, so
    # a build that wrote the column correctly and a repack that did not would
    # look identical from here. F1 measured that we have physical evidence on
    # under 10% of ways, so a build that started hiding lanes on no evidence
    # would be hiding them on nothing - and a hidden lane is the one failure a
    # rider can never see.
    if table == "ways":
        unevidenced = db.execute(
            "SELECT COUNT(*) FROM ways WHERE fourxfour_ok = 0 "
            "AND (access_evidence IS NULL OR access_evidence IN ('', 'none'))"
        ).fetchone()[0]
        if unevidenced:
            out.append((REFUSE,
                        "%d ways are closed to a 4x4 with no evidence. "
                        "WAYS-SCHEMA.md: hiding a lane requires evidence, and "
                        "a wrongly hidden lane is invisible to the rider it "
                        "happened to" % unevidenced))

        # PROVENANCE IS THE PRODUCT. A way with no tier or no date cannot be
        # shown on the record card, which is the one thing the market scan
        # found nobody else shipping.
        untraceable = db.execute(
            "SELECT COUNT(*) FROM ways WHERE legal_tier IS NULL "
            "OR legal_tier = '' OR source IS NULL OR source = '' "
            "OR source_date IS NULL OR source_date = ''").fetchone()[0]
        if untraceable:
            out.append((REFUSE,
                        "%d ways carry no legal tier, source or date; the "
                        "record card cannot cite what it was told"
                        % untraceable))
    return count


def report(paths, strict=False):
    bad = notes = 0
    for path in paths:
        found = problems_with(path)
        refusals = [t for sev, t in found if sev == REFUSE or strict]
        noted = [t for sev, t in found if sev == NOTE and not strict]
        name = os.path.basename(path)
        if refusals:
            bad += 1
            print("REFUSED  %s" % name)
            for t in refusals:
                print("           %s" % t)
        elif noted:
            notes += 1
            print("note     %s" % name)
            for t in noted:
                print("           %s" % t)
        else:
            print("ok       %s" % name)
    print("\n%d checked, %d refused, %d noted" % (len(paths), bad, notes))
    if bad:
        print("%d container(s) would not work on a rider's phone." % bad)
    return bad


# --------------------------------------------------------------------------
# proving it can refuse
# --------------------------------------------------------------------------

def _corruptions():
    """(what was done to a good container). Every one MUST be refused."""

    def sql(*statements):
        """Apply SQL to a copy of a good container.

        `{records}` and `{bbox}` IN A STATEMENT ARE FILLED FROM THE FILE, not
        assumed. The pivot turned `lanes` and `lanes_bbox` into compatibility
        VIEWS over a `ways` table, and a view cannot be written to: four of
        the corruptions below - DELETE, UPDATE and DROP - died with "cannot
        modify lanes because it is a view" the moment the selftest was pointed
        at a post-pivot container.

        That mattered more than it sounds. A corruption that CANNOT BE APPLIED
        is not a corruption that was caught, and this selftest is the only
        evidence step 0.12 has that the validator refuses anything at all. It
        would have gone on reporting its own total while a third of what it
        proves had stopped happening.

        Written to whichever the file actually has, so one selftest covers
        both shapes for as long as both are in riders' hands.
        """
        def apply(path):
            db = sqlite3.connect(path)
            objects = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            records = "ways" if "ways" in objects else "lanes"
            bbox = "%s_bbox" % records
            for s in statements:
                db.execute(s.format(records=records, bbox=bbox))
            db.commit()
            db.close()
        return apply

    def truncate(path):
        with open(path, "r+b") as fh:
            fh.truncate(4096)

    def rubbish(path):
        with open(path, "wb") as fh:
            fh.write(b"not a database, but it has the right extension")

    return [
        ("zoom range excludes its own tiles",
         sql("UPDATE meta SET value='15' WHERE key='min_zoom'",
             "UPDATE meta SET value='16' WHERE key='max_zoom'")),
        ("tiles at coordinates nobody asks for",
         sql("INSERT INTO tiles VALUES (11, 99999, 4, x'00')")),
        ("nothing in it at all",
         sql("DELETE FROM tiles", "DELETE FROM {records}",
             "UPDATE meta SET value='0' WHERE key='lane_count'")),
        # `tiles` is WITHOUT ROWID, so it has no rowid to select on - the first
        # draft of this corruption died with "no such column: rowid", which is
        # a small reminder that a corruption never run is not a corruption
        # shown to be caught.
        ("empty tile bodies",
         sql("UPDATE tiles SET tile_data = x'' "
             "WHERE zoom_level = (SELECT MIN(zoom_level) FROM tiles)")),
        ("bounds wiped", sql("UPDATE meta SET value='' WHERE key='bounds'")),
        ("bounds inverted",
         sql("UPDATE meta SET value='1.0,52.0,-3.0,50.0' WHERE key='bounds'")),
        ("a format version the app cannot read",
         sql("UPDATE meta SET value='2' WHERE key='format_version'")),
        ("lane_count disagrees with the rows",
         sql("DELETE FROM {records} WHERE rowid = (SELECT MIN(rowid) FROM {records})")),
        ("a lane with no r-tree row",
         sql("DELETE FROM {bbox} WHERE id = (SELECT MIN(id) "
             "FROM {bbox})")),
        ("a lane with no geometry",
         sql("UPDATE {records} SET geometry = x'' "
             "WHERE rowid = (SELECT MIN(rowid) FROM {records})")),
        ("the record table dropped", sql("DROP TABLE {records}")),

        # THE TWO PIVOT RULES, corrupted so they are shown to bite. A check
        # added without a corruption beside it is a claim: this file's whole
        # authority is that every rule in it has been watched to fail.
        #
        # Written against `ways` and not `{records}` on purpose. On a
        # pre-pivot container these columns do not exist and the statement
        # throws, which is the loud answer - a corruption that silently did
        # nothing would be counted as applied and refused.
        ("a way hidden from a 4x4 on no evidence",
         sql("UPDATE ways SET fourxfour_ok = 0, access_evidence = 'none' "
             "WHERE rowid = (SELECT MIN(rowid) FROM ways)")),
        ("a way that cannot say where it came from",
         sql("UPDATE ways SET legal_tier = '', source = '', "
             "source_date = '' WHERE rowid = (SELECT MIN(rowid) FROM ways)")),
        ("truncated mid-file", truncate),
        ("not a database at all", rubbish),
    ]


def selftest():
    import shutil
    import tempfile
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import golden

    work = tempfile.mkdtemp(prefix="tb-validate-container-")
    failures = []
    try:
        built = os.path.join(work, "good")
        golden.build_into(built)
        good = golden.container_path(built)
        overview = golden.container_path(built, area="overview")
        print("golden area container:     %s, %d bytes"
              % (os.path.basename(good), os.path.getsize(good)))
        print("golden overview container: %s, %d bytes"
              % (os.path.basename(overview), os.path.getsize(overview)))

        # BOTH KINDS, because the first draft of this validator passed the
        # selftest and refused all four published overviews.
        for label, path in (("area", good), ("overview", overview)):
            found = problems_with(path)
            if found:
                failures.append("the GOOD %s container was refused: %s"
                                % (label, "; ".join(t for _s, t in found)))
                print("  FAIL   a correct %s container was refused" % label)
            else:
                print("  ok     a correct %s container passes" % label)

        for name, corrupt in _corruptions():
            path = os.path.join(work, "bad.tbmap")
            shutil.copyfile(good, path)
            corrupt(path)
            found = [t for sev, t in problems_with(path) if sev == REFUSE]
            if not found:
                failures.append("NOT CAUGHT: %s" % name)
                print("  MISSED %s" % name)
            else:
                print("  caught %-42s %s" % (name, found[0][:86]))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\nSELFTEST FAILED: %d" % len(failures))
        for f in failures:
            print("  " + f)
        return 1
    print("\nselftest ok: 2 good containers accepted, %d corruptions refused"
          % len(_corruptions()))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("containers", nargs="*")
    ap.add_argument("--strict", action="store_true",
                    help="treat notes as refusals")
    ap.add_argument("--selftest", action="store_true",
                    help="build a golden container, corrupt it, and prove "
                         "every corruption is refused")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.containers:
        ap.error("give at least one container, or --selftest")
    return 1 if report(args.containers, args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())

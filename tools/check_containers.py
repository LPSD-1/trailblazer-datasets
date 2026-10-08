"""Refuse to publish a container that would misbehave on a rider's phone.

`check_build.py` guards the `.tbpack` path and goes on doing so until the
cutover. This guards the new one, and its duties come from things that actually
went wrong while the format was being built:

  1. TILES AND RECORDS AGREE, IN EACH AREA. A lane in the tiles and not the
     records is drawn and unidentifiable; a lane in the records and not the
     tiles is findable and invisible. Both are silent. Checked per container,
     because a rider may hold one area and nothing else - see (3).

  2. NO TILE IS OVER THE CEILING. A 1.4 MB z6 tile took the app from 588 MB to
     1,478 MB on the tablet and nothing in the pipeline would have stopped it
     reaching a device.

  3. EVERY AREA DRAWS EVERY WAY IT CARRIES. This duty used to be the opposite
     - "one lane belongs to one area", for tiles - and that rule is RETIRED.
     A way straddling a boundary is published in both neighbouring packs so it
     is never cut in half; drawing it once is the APP's job, and it does it:
     PmTilesServer._tileFor merges the tiles of every mounted container of the
     same depth and mergeVectorTiles drops a feature whose id it has already
     seen (ids are stable_id(lane_uid), equal in every area). Enforcing one
     owner here instead left the way out of every later area's tiles, so a
     rider who held only Wales saw 536 of its 1,390 byways (2 Oct 2026). The
     per-container half of (1) is what now refuses that.

  4. THE BUILD IS REPRODUCIBLE. An area that has not changed must rebuild
     byte-identical, or every rider re-downloads the world every month for
     nothing.

  5. AND THE TOTAL IS REPORTED. What a rider with everything downloaded would
     have to fetch is the number the owner actually cares about and it is
     currently printed nowhere.

  7. AN UNSURFACED ROAD NEVER REACHES AN APP THAT WOULD DRAW IT RED. A
     `ucr` is a public road; an app before 119 reads one as unknown and draws
     it "you may not ride this". So a UCR lives only in `ucr_ways` and in
     tile layer `ucr`, which those apps never read - and a `ucr` row in
     `ways`, a `ucr` feature in layer `lanes`, or anything but a `ucr` in
     layer `ucr` refuses the publish. A UCR record counts as a record for
     (1), and must be drawn in its own container like any way.

  6. NO TEXT CARRIES AN HTML ENTITY. 2,012 published ways said their
     authority was `North&nbsp;Lincolnshire`, and the app showed it exactly
     so. Every text value in every table, meta included, and every string a
     tile carries, is read; one that still holds a character reference
     html.unescape would resolve, or a raw no-break space (the decoded
     `&nbsp;`, which ten published POI names carried), refuses the publish. The cleaning lives
     where source text enters (text_clean.py); this is the gate for a source
     that has not been met yet.

Usage:
    python tools/check_containers.py CONTAINER [CONTAINER ...]
    python tools/check_containers.py --reproducible PACK OUT   (builds twice)
"""

import argparse
import collections
import gzip
import hashlib
import os
import pathlib
import sqlite3
import subprocess
import sys

#: The largest a single tile may be.
#:
#: MEASURED. The biggest z6 tile in an all-vehicle national container was
#: 1,406 kB and took 19 ms merely to READ, far more to decode, and the app to
#: 1.5 GB. A motor container's biggest is 35.6 kB. 512 kB sits well above
#: anything a per-vehicle container has produced and well below the size that
#: hurt, so it catches the shape of that failure without tripping on ordinary
#: data.
MAX_TILE_BYTES = 512 * 1024

#: The record table of each container shape, and the column its uid is in.
#:
#: `ways` WAS MISSING, so every post-cutover area container was refused with
#: "no record table" - found by the cutover dry run on 24 Sep, the first time
#: this ran over a container the new builder wrote. Nothing had run it on one
#: before: golden never called it, and no suite built a ways container and
#: handed it here. test_check_containers.py now does.
TABLE_KEYS = {"lanes": "lane_uid", "ways": "way_uid", "orders": "tro_uid"}

#: The property a TILE feature carries its uid in. Not the same as the column:
#: a ways container stores `way_uid` and draws it as `lane_uid`, so the app's
#: tap handler reads one property name whichever shape it mounted.
TILE_KEYS = {"lanes": "lane_uid", "ways": "lane_uid", "orders": "tro_uid"}

#: Where an unsurfaced unclassified road lives, and nowhere else (7).
UCR_TABLE = "ucr_ways"
UCR_LAYER = "ucr"


class Problem(Exception):
    pass


def check_text(db, name, problems):
    """(6) Every text value the container publishes, entity-free.

    Rows AND tiles: the county a tile carries for the style is the same text
    as the record's, and the overview has no records at all - only tiles -
    so a check of the tables alone would pass the one container every rider
    downloads first. Returns how many values were read, so a caller can
    tell "clean" from "read nothing".
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from text_clean import unclean_in  # noqa: E402
    from test_mvt import decode_tile     # noqa: E402

    read = 0
    bad = collections.Counter()
    first = {}
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for table in tables:
        if table == "tiles":
            continue
        cursor = db.execute('SELECT * FROM "%s"' % table)
        columns = [d[0] for d in cursor.description]
        for row in cursor:
            for column, value in zip(columns, row):
                if not isinstance(value, str):
                    continue
                read += 1
                found = unclean_in(value)
                if found:
                    where = "%s.%s" % (table, column)
                    bad[where] += 1
                    first.setdefault(where, value)
    if "tiles" in tables:
        for (blob,) in db.execute("SELECT tile_data FROM tiles"):
            for layer in decode_tile(blob):
                for feature in layer["features"]:
                    for key, value in feature["props"].items():
                        if not isinstance(value, str):
                            continue
                        read += 1
                        if unclean_in(value):
                            where = "tiles(%s).%s" % (layer["name"], key)
                            bad[where] += 1
                            first.setdefault(where, value)
    for where in sorted(bad):
        problems.append(
            "%s: %d text value(s) in %s still carry an HTML entity or a "
            "no-break space, e.g. %r. "
            "The app shows it literally; clean it where the source enters "
            "(tools/text_clean.py)." % (name, bad[where], where,
                                        first[where][:80]))
    return read


def _meta(db):
    return {k: v for k, v in db.execute("SELECT key, value FROM meta")}


def _record_table(db):
    names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    found = [t for t in TABLE_KEYS if t in names]
    return (found[0], TABLE_KEYS[found[0]]) if len(found) == 1 else (None, None)


def _uids_in_tiles(db, table, misplaced=None):
    """Every uid the tiles mention, by decoding them.

    Decoded rather than trusted: the whole point of this check is that the tile
    builder and the record writer might disagree, and asking the builder what it
    wrote would ask the wrong witness.

    [misplaced], a list, gets a line for every feature in the wrong layer for
    its class (7): a `ucr` in `lanes`, or anything but a `ucr` in `ucr`.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from test_mvt import decode_tile  # noqa: E402  (the decoder, reused)

    key = TILE_KEYS[table] if table else "lane_uid"
    uids = set()
    for (blob,) in db.execute("SELECT tile_data FROM tiles"):
        for layer in decode_tile(blob):
            for feature in layer["features"]:
                klass = feature["props"].get("class")
                if misplaced is not None and (
                        (layer["name"] == UCR_LAYER) != (klass == "ucr")):
                    misplaced.append("a %r feature in tile layer %r"
                                     % (klass, layer["name"]))
                uid = feature["props"].get(key)
                if uid:
                    uids.add(uid)
    return uids


def check_container(path, problems, max_tile=MAX_TILE_BYTES):
    # READ-ONLY, THROUGH A PROPER FILE URI.
    #
    # "file:%s" % a Windows path gives "file:C:/Users/...", which SQLite reads
    # as a RELATIVE path called "C:" and refuses with "unable to open database
    # file". An absolute Windows path needs "file:///C:/Users/...", which
    # `pathlib` knows how to write and a format string does not.
    #
    # It only ever ran on Linux in CI, so this went unnoticed - and it is
    # exactly the guard worth running locally before spending twenty-five
    # minutes discovering the same answer from a workflow.
    db = sqlite3.connect(
        "%s?mode=ro" % pathlib.Path(path).absolute().as_uri(), uri=True)
    try:
        name = os.path.basename(path)
        meta = _meta(db)
        kind = meta.get("kind", "?")

        # (6) text, before anything returns early: an overview has only
        # tiles, and is checked too.
        check_text(db, name, problems)

        # (2) tile ceiling
        worst = db.execute(
            "SELECT zoom_level, tile_column, tile_row, LENGTH(tile_data) len "
            "FROM tiles ORDER BY len DESC LIMIT 1").fetchone()
        if worst and worst[3] > max_tile:
            problems.append(
                "%s: tile z%d/%d/%d is %.0f kB, over the %.0f kB ceiling. "
                "A tile this size is what took the app to 1.5 GB."
                % (name, worst[0], worst[1], worst[2],
                   worst[3] / 1024.0, max_tile / 1024.0))

        table, _ = _record_table(db)
        misplaced = []
        if table is None:
            if kind != "overview":
                problems.append("%s: no record table, and kind is %r not "
                                "'overview'." % (name, kind))
            if kind == "overview":
                _uids_in_tiles(db, None, misplaced)
                _refuse_misplaced(name, misplaced, problems)
            return meta, {"records": set(), "tiles": set(), "kind": kind}

        in_records = {r[0] for r in db.execute(
            "SELECT %s FROM %s" % (TABLE_KEYS[table], table))}
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if table == "ways":
            # (7) Not one `ucr` in the table every app reads.
            in_ways = db.execute("SELECT COUNT(*) FROM ways "
                                 "WHERE way_class = 'ucr'").fetchone()[0]
            if in_ways:
                problems.append(
                    "%s: %d unsurfaced road(s) in `ways`, which an app before "
                    "119 reads and draws red. They belong in `%s`."
                    % (name, in_ways, UCR_TABLE))
        if table == "ways" and UCR_TABLE in names:
            in_records |= {r[0] for r in db.execute(
                "SELECT way_uid FROM %s" % UCR_TABLE)}
            strays = db.execute("SELECT COUNT(*) FROM %s WHERE way_class "
                                "<> 'ucr'" % UCR_TABLE).fetchone()[0]
            if strays:
                problems.append("%s: %d way(s) in `%s` are not unsurfaced "
                                "roads; an app before 119 would never see "
                                "them." % (name, strays, UCR_TABLE))
        in_tiles = _uids_in_tiles(db, table,
                                  misplaced if table == "ways" else None)
        _refuse_misplaced(name, misplaced, problems)
        return meta, {"records": in_records, "tiles": in_tiles, "kind": kind}
    finally:
        db.close()


def _refuse_misplaced(name, misplaced, problems):
    """(7) A feature in the wrong tile layer for its class."""
    if misplaced:
        problems.append(
            "%s: %d tile feature(s) in the wrong layer, e.g. %s. A `ucr` "
            "drawn in `lanes` is drawn red by every app before 119."
            % (name, len(misplaced), misplaced[0]))


def check_agreement(containers, problems):
    """(1) and (3): every feature is drawn and identifiable IN ITS OWN AREA.

    PER CONTAINER, NOT PER VEHICLE. This used to pool a vehicle's containers
    and ask whether a record was drawn "somewhere", because section 19.2 then
    gave each boundary lane ONE area that drew it. That pooling is exactly why
    this guard passed a published set in which Wales drew 536 of the 1,390
    byways it carried: every missing one was drawn by an area a rider who held
    only Wales did not have. A rider mounts areas one at a time, so each area
    must stand on its own; a way two areas share is drawn by both, and the app
    dedupes it by id (pmtiles_server.dart mergeVectorTiles).
    """
    for path, found in containers:
        name = os.path.basename(path)
        drawn_only = found["tiles"] - found["records"]
        if drawn_only:
            problems.append(
                "%s: %d feature(s) are drawn and identify nothing - a tap on "
                "them finds no record. First: %s"
                % (name, len(drawn_only), sorted(drawn_only)[0]))
        held_only = found["records"] - found["tiles"]
        if held_only and found["kind"] != "orders":
            # Orders are held at every zoom and drawn from z10, deliberately.
            problems.append(
                "%s: %d of its %d record(s) are in none of its tiles - "
                "findable and invisible to a rider who holds this area. "
                "First: %s"
                % (name, len(held_only), len(found["records"]),
                   sorted(held_only)[0]))


def report_total(containers):
    """(5) What a rider with everything downloaded would fetch."""
    raw = compressed = 0
    for path, _ in containers:
        raw += os.path.getsize(path)
        with open(path, "rb") as fh:
            compressed += len(gzip.compress(fh.read(), mtime=0))
    print("  %d containers" % len(containers))
    print("  on disk        %.1f MB" % (raw / 1048576.0))
    print("  to download    %.1f MB" % (compressed / 1048576.0))
    return compressed


def check_reproducible(pack, out_dir):
    """(4) The same input builds the same bytes."""
    here = os.path.dirname(os.path.abspath(__file__))
    builder = os.path.join(here, "build_map_container.py")
    digests = []
    for i in (1, 2):
        target = os.path.join(out_dir, "repro-%d.tbmap" % i)
        subprocess.check_call(
            [sys.executable, builder, "--area", target, pack],
            stdout=subprocess.DEVNULL)
        with open(target, "rb") as fh:
            digests.append(hashlib.sha256(fh.read()).hexdigest())
        os.remove(target)
    if digests[0] != digests[1]:
        raise Problem(
            "Building %s twice gave different bytes (%s vs %s). Every rider "
            "would re-download every area on every run."
            % (os.path.basename(pack), digests[0][:12], digests[1][:12]))
    print("  reproducible   %s" % digests[0][:16])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("containers", nargs="*")
    ap.add_argument("--reproducible", nargs=2, metavar=("PACK", "OUTDIR"))
    ap.add_argument("--max-tile-kb", type=int, default=MAX_TILE_BYTES // 1024)
    args = ap.parse_args()

    problems = []
    try:
        if args.reproducible:
            check_reproducible(*args.reproducible)
    except Problem as e:
        problems.append(str(e))

    checked = []
    for path in args.containers:
        meta, found = check_container(path, problems, args.max_tile_kb * 1024)
        if meta.get("kind") in ("area", "both"):
            checked.append((path, found))

    if checked:
        check_agreement(checked, problems)
    if args.containers:
        report_total([(p, None) for p in args.containers])

    if problems:
        print()
        for p in problems:
            print("  REFUSED: %s" % p)
        print("\n%d problem(s). Nothing published." % len(problems))
        return 1
    print("\nContainers OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

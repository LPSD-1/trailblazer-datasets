"""Refuse to publish a container that would misbehave on a rider's phone.

`check_build.py` guards the `.tbpack` path and goes on doing so until the
cutover. This guards the new one, and its duties come from things that actually
went wrong while the format was being built:

  1. TILES AND RECORDS AGREE. A lane in the tiles and not the records is drawn
     and unidentifiable; a lane in the records and not the tiles is findable and
     invisible. Both are silent.

  2. NO TILE IS OVER THE CEILING. A 1.4 MB z6 tile took the app from 588 MB to
     1,478 MB on the tablet and nothing in the pipeline would have stopped it
     reaching a device.

  3. ONE LANE BELONGS TO ONE AREA. A lane straddling a boundary is published in
     both neighbouring packs so it is never cut in half. In tiles that would
     draw it twice, from two areas, with different simplification.

  4. THE BUILD IS REPRODUCIBLE. An area that has not changed must rebuild
     byte-identical, or every rider re-downloads the world every month for
     nothing.

  5. AND THE TOTAL IS REPORTED. What a rider with everything downloaded would
     have to fetch is the number the owner actually cares about and it is
     currently printed nowhere.

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


class Problem(Exception):
    pass


def _meta(db):
    return {k: v for k, v in db.execute("SELECT key, value FROM meta")}


def _record_table(db):
    names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    found = [t for t in TABLE_KEYS if t in names]
    return (found[0], TABLE_KEYS[found[0]]) if len(found) == 1 else (None, None)


def _uids_in_tiles(db, table):
    """Every uid the tiles mention, by decoding them.

    Decoded rather than trusted: the whole point of this check is that the tile
    builder and the record writer might disagree, and asking the builder what it
    wrote would ask the wrong witness.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from test_mvt import decode_tile  # noqa: E402  (the decoder, reused)

    key = TILE_KEYS[table]
    uids = set()
    for (blob,) in db.execute("SELECT tile_data FROM tiles"):
        for layer in decode_tile(blob):
            for feature in layer["features"]:
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
        if table is None:
            if kind != "overview":
                problems.append("%s: no record table, and kind is %r not "
                                "'overview'." % (name, kind))
            return meta, {"records": set(), "tiles": set(), "kind": kind}

        in_records = {r[0] for r in db.execute(
            "SELECT %s FROM %s" % (TABLE_KEYS[table], table))}
        in_tiles = _uids_in_tiles(db, table)
        # AGREEMENT IS CHECKED PER VEHICLE, NOT PER CONTAINER - see
        # check_agreement below. A record with no tile in THIS container is the
        # ordinary case for a boundary lane, whose tiles belong to the
        # neighbouring area; reporting it here flagged 1,737 perfectly correct
        # lanes on the first run.
        return meta, {"records": in_records, "tiles": in_tiles, "kind": kind}
    finally:
        db.close()


def check_agreement(containers, problems):
    """(1) Every feature is both drawn and identifiable, somewhere.

    Gathered across a vehicle's containers rather than within one, because the
    two rules interact: section 19.2 gives each lane ONE area that draws it and
    leaves its record in every area that carries it. So the question is not
    "does this container hold both halves" but "does this vehicle".
    """
    by_vehicle = {}
    for path, found in containers:
        vehicle = os.path.basename(path).split("-", 1)[0]
        got = by_vehicle.setdefault(vehicle, {"records": set(), "tiles": set(),
                                              "kind": found["kind"]})
        got["records"] |= found["records"]
        got["tiles"] |= found["tiles"]

    for vehicle, got in sorted(by_vehicle.items()):
        drawn_only = got["tiles"] - got["records"]
        if drawn_only:
            problems.append(
                "%s: %d feature(s) are drawn and identify nothing - a tap on "
                "them finds no record. First: %s"
                % (vehicle, len(drawn_only), sorted(drawn_only)[0]))
        held_only = got["records"] - got["tiles"]
        if held_only and got["kind"] != "orders":
            # Orders are held at every zoom and drawn from z10, deliberately.
            problems.append(
                "%s: %d record(s) are in no tile anywhere - findable and "
                "invisible. First: %s"
                % (vehicle, len(held_only), sorted(held_only)[0]))


def check_no_lane_in_two_areas(containers, problems):
    """(3) One lane belongs to one area, for tiles - WITHIN A VEHICLE.

    ACROSS vehicles it is expected and correct. A bridleway is a right of way
    for a bicycle, a horse and a walker, so it is published in all three packs;
    the first run of this check reported a foot container and a bicycle
    container sharing a uid as a fault, and it is the data being right.
    A rider only ever mounts one vehicle's containers, so that is the scope the
    rule has.
    """
    owner = {}
    for path, found in containers:
        vehicle = os.path.basename(path).split("-", 1)[0]
        for uid in found["tiles"]:
            key = (vehicle, uid)
            if key in owner:
                problems.append(
                    "%s: %s is also in %s. A lane in two of one vehicle's "
                    "areas is drawn twice, from two simplifications."
                    % (os.path.basename(path), uid,
                       os.path.basename(owner[key])))
                break
            owner[key] = path


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
    if len(checked) > 1:
        check_no_lane_in_two_areas(checked, problems)
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

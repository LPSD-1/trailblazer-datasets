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

TABLE_KEYS = {"lanes": "lane_uid", "orders": "tro_uid"}


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

    key = "lane_uid" if table == "lanes" else "tro_uid"
    uids = set()
    for (blob,) in db.execute("SELECT tile_data FROM tiles"):
        for layer in decode_tile(blob):
            for feature in layer["features"]:
                uid = feature["props"].get(key)
                if uid:
                    uids.add(uid)
    return uids


def check_container(path, problems, max_tile=MAX_TILE_BYTES):
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
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
            return meta, set()

        # (1) tiles and records agree
        in_records = {r[0] for r in db.execute(
            "SELECT %s FROM %s" % (TABLE_KEYS[table], table))}
        in_tiles = _uids_in_tiles(db, table)
        drawn_only = in_tiles - in_records
        held_only = in_records - in_tiles
        if drawn_only:
            problems.append(
                "%s: %d feature(s) are in the tiles and not the records - "
                "drawn and unidentifiable. First: %s"
                % (name, len(drawn_only), sorted(drawn_only)[0]))
        if held_only:
            # Orders below the tile floor are expected: they are held at every
            # zoom and drawn from z10, which is deliberate.
            if kind != "orders":
                problems.append(
                    "%s: %d record(s) are in no tile - findable and invisible. "
                    "First: %s" % (name, len(held_only), sorted(held_only)[0]))
        return meta, in_records
    finally:
        db.close()


def check_no_lane_in_two_areas(containers, problems):
    """(3) One lane belongs to one area, for tiles."""
    owner = {}
    for path, uids in containers:
        for uid in uids:
            if uid in owner:
                problems.append(
                    "%s: %s is also in %s. A lane in two areas' tiles is drawn "
                    "twice, from two simplifications."
                    % (os.path.basename(path), uid, os.path.basename(owner[uid])))
                break
            owner[uid] = path


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
        meta, uids = check_container(path, problems, args.max_tile_kb * 1024)
        if meta.get("kind") in ("area", "both"):
            checked.append((path, uids))

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

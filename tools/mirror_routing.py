#!/usr/bin/env python3
"""Take our own copy of the routing tiles, so riders never touch a third party.

    python mirror_routing.py --catalogue catalogue.json \
        --out dist/routing --index routing/index.json --budget 6

WHY
---
Every rider who wants to plan a route offline in Britain pulls a 139 MB tile
from brouter.de - a volunteer-run service we have no agreement with, no
relationship with and no fallback for. Across the published catalogue that is
7.9 GB of somebody else's bandwidth, spent by an app that charges money.

It will not fail by sending us a bill. It will fail by being rate-limited or
blocked, and then offline routing stops working for every rider at once, on a
moor, with the app reporting a download failure it cannot explain.

So we host it. GitHub releases have no total size limit and no bandwidth
limit, which makes this both possible and free.

IT ALSO FIXES THE CHECKSUMS
---------------------------
BRouter's directory index publishes sizes and no hashes, and it rebuilds its
segments nightly - so the size in our catalogue is a snapshot of a moving
target and cannot be a contract. Measured, every tile in the published index
was already the wrong size, and every routing download the app ever attempted
aborted partway because of it.

A tile WE host does not move under us. We hash it once, publish the hash, and
the app can check the download exactly, the way it checks a lane pack.

A TILE AT A TIME
----------------
Same shape as the imagery build: a budget per run, resumable, and a run either
publishes finished tiles or publishes nothing.

GREAT BRITAIN ONLY (plan step 1.6)
----------------------------------
This used to mirror every tile the catalogue offered - 526 distinct files,
7.9 GB - for a product that publishes lanes in England and Wales and nowhere
else. Six tiles cover Great Britain, 293 MB, and they are named below rather
than derived, because a derived set is one nobody can check at a glance and
the GB bounding box grazes two rows of tiles that hold no British ground.

IT DOES NOT DELETE ANYTHING.
Tiles already mirrored and published that fall outside the six are LISTED, not
removed. Deleting a published release asset is not a thing to do in the same
change that stops fetching: a rider mid-download holds a URL that would stop
answering. The list is the input to that separate, deliberate step, which is
`tools/prune_published.py` under a human.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

SOURCE = "https://brouter.de/brouter/segments4/"
USER_AGENT = "trailblazer-offline-maps mirror (contact: the repo owner)"

# Tiles are 130-250 MB. Release assets cap at 2 GiB, so every tile fits with
# room to spare; this guards against the index listing something absurd.
MAX_TILE_BYTES = 1024 * 1024 * 1024

# THE SIX TILES GREAT BRITAIN NEEDS, in the 5x5-degree scheme brouter.de uses.
#
# Written out rather than computed from a bounding box. The UK box in
# build_catalogue.py is (-8.7, 49.8) to (1.8, 60.9), and a box that size
# touches TWELVE tiles: it grazes the N45 row (45-50N, which is France and
# northern Spain) by two tenths of a degree at its southern edge, and the N60
# row (60-65N) at its northern. Neither row carries ground a British rider
# routes over, and between them they are 193,163,753 bytes - 193.2 MB of the
# 486.5 MB the catalogue offered for "the United Kingdom". Three of those six
# are already mirrored (the N45 row, 191,627,782 bytes); this run lists them
# and leaves them alone.
#
# N60 is the one worth stating out loud: it holds Shetland, and dropping it
# means a rider on Unst cannot route. That is deliberate and it is recorded -
# we publish no lanes north of the England/Wales border at all, so there is
# nothing there to route between. If Scotland ever ships, this tuple is where
# it starts.
GB_ROUTING_TILES = (
    "W10_N50", "W5_N50", "E0_N50",   # England and Wales, south of 55N
    "W10_N55", "W5_N55", "E0_N55",   # northern England, and the border
)


def published_tiles():
    """What brouter.de currently lists, and how big it says each one is."""
    req = urllib.request.Request(SOURCE, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as fh:
        html = fh.read().decode("utf8", "replace")
    sizes = {}
    for name, size in re.findall(
            r'<a href="([EW]\d+_[NS]\d+)\.rd5">[^<]*</a>\s+\S+\s+\S+\s+(\d+)',
            html):
        sizes[name] = int(size)
    if not sizes:
        sys.exit("could not parse the tile index; the listing format changed")
    return sizes


def tiles_we_need():
    """The tiles we mirror: Great Britain, and nothing else.

    It used to be "every routing tile the catalogue offers", which is the
    whole world, because the 5x5 grid covers everything for free. Free to
    LIST is not free to HOST or to keep fresh, and no rider has ever been
    offered a lane outside England and Wales.
    """
    return list(GB_ROUTING_TILES)


def catalogue_routing(catalogue):
    """Every routing tile id the catalogue currently offers, with its size."""
    offered = {}
    for continent in catalogue.get("continents", []):
        for country in continent.get("countries", []):
            for area in country.get("areas", []):
                for pack in area.get("packs", []):
                    if pack.get("kind") == "routing":
                        offered[pack["id"]] = pack.get("bytes", 0)
    return offered


def prunable(held, scope):
    """Tiles we have already mirrored that are no longer in scope.

    Returned, printed, and then left exactly where they are. See the module
    docstring: stopping the fetch and deleting the asset are two changes, and
    doing them together is how a rider's half-finished download becomes a 404.
    """
    return sorted(name for name in held if name not in set(scope))


def fetch(name, expected, out_dir):
    """Pull one tile, verify it arrived whole, and hash it."""
    url = SOURCE + name + ".rd5"
    target = os.path.join(out_dir, name + ".rd5")
    part = target + ".part"
    os.makedirs(out_dir, exist_ok=True)

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as r:
        advertised = int(r.headers.get("Content-Length") or 0)
        if advertised > MAX_TILE_BYTES:
            raise ValueError("%s is %d bytes, which is implausible"
                             % (name, advertised))
        digest = hashlib.sha256()
        received = 0
        with open(part, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                received += len(chunk)
                digest.update(chunk)
                f.write(chunk)

    # Against the SERVER's own Content-Length, which describes this transfer,
    # rather than the index figure, which is a snapshot of a nightly rebuild.
    if advertised and received != advertised:
        os.remove(part)
        raise ValueError("%s arrived short: %d of %d" % (name, received,
                                                         advertised))
    if received < 1024:
        os.remove(part)
        raise ValueError("%s is %d bytes; that is not a routing tile"
                         % (name, received))

    os.replace(part, target)
    return target, received, digest.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalogue", required=True)
    ap.add_argument("--out", required=True, help="where tiles are written")
    ap.add_argument("--index", required=True,
                    help="record of what we host, read by build_catalogue.py")
    ap.add_argument("--budget", type=int, default=6,
                    help="tiles to mirror in THIS run. Six at ~150 MB is "
                         "about a gigabyte, which is a polite hour.")
    ap.add_argument("--refresh-after-days", type=int, default=30,
                    help="re-mirror a tile older than this")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what this run would fetch and what would be "
                         "pruned, write nothing, download nothing. This is "
                         "how step 1.6's gate is read without pulling "
                         "293 MB to read it.")
    args = ap.parse_args()

    with open(args.catalogue, encoding="utf-8") as f:
        catalogue = json.load(f)

    index = {"tiles": {}}
    if os.path.exists(args.index):
        with open(args.index, encoding="utf-8") as f:
            index = json.load(f)
    held = index.setdefault("tiles", {})

    sizes = published_tiles()
    wanted = tiles_we_need()
    now = time.time()

    # BEFORE AND AFTER, printed every run, because the whole point of this
    # step is a number that fell and a reader who can check it fell.
    offered = catalogue_routing(catalogue)
    print("catalogue offers %d distinct routing files, %d bytes (%.0f MB)"
          % (len(offered), sum(offered.values()),
             sum(offered.values()) / 1e6))
    in_scope = {n: offered[n] for n in wanted if n in offered}
    missing_from_catalogue = [n for n in wanted if n not in offered]
    print("GB scope is %d tiles, %d bytes (%.1f MB)%s"
          % (len(wanted), sum(in_scope.values()),
             sum(in_scope.values()) / 1e6,
             "" if not missing_from_catalogue
             else "  (not yet in the catalogue: %s)"
                  % ", ".join(missing_from_catalogue)))
    for name in wanted:
        print("  %-8s %12d bytes" % (name, in_scope.get(name, 0)))

    # WHAT WOULD BE PRUNED, AND IS NOT.
    #
    # A tile we host and no longer want is a release asset plus an index
    # entry. Removing either here would delete published data in the change
    # that stops fetching it; this run only says which they are.
    retired = prunable(held, wanted)
    if retired:
        retired_bytes = sum(held[n].get("bytes", 0) for n in retired)
        print("\nWOULD PRUNE (not pruned by this run): %d mirrored tile(s), "
              "%d bytes (%.1f MB)"
              % (len(retired), retired_bytes, retired_bytes / 1e6))
        for name in retired:
            print("  %-8s %12d bytes  %s.rd5"
                  % (name, held[name].get("bytes", 0), name))
        print("  Deliberate step: tools/prune_published.py, run by a human "
              "once no rider is mid-download.")

    todo = []
    for name in wanted:
        if name not in sizes:
            continue  # ocean, or nothing mapped there
        have = held.get(name)
        if have is None:
            todo.append(name)
            continue
        age_days = (now - have.get("mirrored_at", 0)) / 86400.0
        # Re-mirrored when it is old OR when the upstream size has moved,
        # which is how we notice a genuine rebuild rather than daily drift.
        if age_days >= args.refresh_after_days or have.get(
                "upstream_bytes") != sizes[name]:
            todo.append(name)

    print("\n%d tiles in scope, %d already ours (%d of them in scope), "
          "%d to do" % (len(wanted), len(held),
                        len(held) - len(retired), len(todo)))
    if not todo:
        print("nothing due")
        return 0

    batch = todo[:args.budget]
    if args.dry_run:
        print("\ndry run: would fetch %d tile(s)" % len(batch))
        for name in batch:
            print("  %s  %.0f MB" % (name, sizes[name] / 1e6))
        return 0

    done = []
    for name in batch:
        try:
            print("  %s (%.0f MB)..." % (name, sizes[name] / 1e6), flush=True)
            path, length, digest = fetch(name, sizes[name], args.out)
            held[name] = {
                "bytes": length,
                "sha256": digest,
                "upstream_bytes": sizes[name],
                "mirrored_at": now,
            }
            done.append((name, path, length))
        except Exception as e:  # noqa: BLE001 - one bad tile is not the run
            print("    failed: %s" % e, file=sys.stderr)

    if not done:
        print("nothing mirrored this run", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(os.path.abspath(args.index)), exist_ok=True)
    with open(args.index, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, sort_keys=True)
        f.write("\n")

    total = sum(d[2] for d in done)
    print("\nmirrored %d tiles, %.0f MB" % (len(done), total / 1e6))
    for name, path, length in done:
        print("  %s  %.0f MB  %s" % (name, length / 1e6, held[name]["sha256"][:16]))
    # The workflow uploads exactly these.
    print("\nUPLOAD:" + " ".join(d[1] for d in done))
    return 0


if __name__ == "__main__":
    sys.exit(main())

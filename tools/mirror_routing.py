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
publishes finished tiles or publishes nothing. Tiles covering countries where
we publish lanes come first, because those are the riders we have.
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


def tiles_we_need(catalogue):
    """Every routing tile the catalogue offers, most wanted first.

    Tiles covering a country where we publish LANES come first. Those are the
    riders we actually have, and they are the ones currently pulling 139 MB
    from somebody else.
    """
    priority, rest = [], []
    for continent in catalogue.get("continents", []):
        for country in continent.get("countries", []):
            has_lanes = any(
                p.get("kind") == "lanes"
                for area in country.get("areas", [])
                for p in area.get("packs", [])
            )
            for area in country.get("areas", []):
                for pack in area.get("packs", []):
                    if pack.get("kind") != "routing":
                        continue
                    (priority if has_lanes else rest).append(pack["id"])
    seen = set()
    out = []
    for name in priority + rest:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


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
    args = ap.parse_args()

    with open(args.catalogue, encoding="utf-8") as f:
        catalogue = json.load(f)

    index = {"tiles": {}}
    if os.path.exists(args.index):
        with open(args.index, encoding="utf-8") as f:
            index = json.load(f)
    held = index.setdefault("tiles", {})

    sizes = published_tiles()
    wanted = tiles_we_need(catalogue)
    now = time.time()

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

    print("%d tiles offered, %d already ours, %d to do"
          % (len(wanted), len(held), len(todo)))
    if not todo:
        print("nothing due")
        return 0

    batch = todo[:args.budget]
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

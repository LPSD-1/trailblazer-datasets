#!/usr/bin/env python3
"""Drop what we publish and no longer should, and say what to delete.

    python prune_published.py --catalogue catalogue.json \
        --satellite satellite/index.json --routing routing/index.json

Prints the release assets that nothing references any more, one per line,
prefixed by the release they live in. The workflow deletes exactly those.

WHY
---
Nothing else removes anything. `record_satellite.py` replaces an entry with
the same id and `mirror_routing.py` overwrites a tile it re-fetches, so the
happy path is tidy — but neither has any idea what to do about something that
has been WITHDRAWN. A lane area that gets merged into its neighbour, a routing
tile upstream stops publishing: the index keeps offering both for ever, and
the catalogue keeps pointing riders at release assets nobody rebuilds.

That fails in the worst way it could. The pack is still listed, still sized,
still downloadable, and the data in it is frozen at whenever it was last
built — so a rider carries a definitive map that stopped being definitive and
nothing anywhere says so.

WHAT IT WILL NOT DO
-------------------
Prune on an empty or unreadable catalogue. An index that failed to build is
indistinguishable from one where everything was withdrawn, and the second
reading would delete the lot.
"""
import argparse
import json
import os
import re
import sys
import urllib.request

ROUTING_INDEX = "https://brouter.de/brouter/segments4/"


def load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def live_areas(catalogue):
    return {
        area["id"]
        for continent in catalogue.get("continents", [])
        for country in continent.get("countries", [])
        for area in country.get("areas", [])
    }


def upstream_tiles():
    """What brouter.de still lists. Empty means 'could not tell'."""
    try:
        req = urllib.request.Request(
            ROUTING_INDEX,
            headers={"User-Agent": "trailblazer-offline-maps prune"})
        with urllib.request.urlopen(req, timeout=60) as fh:
            html = fh.read().decode("utf8", "replace")
    except Exception as e:  # noqa: BLE001
        print("could not read the upstream index (%s); leaving routing alone"
              % e, file=sys.stderr)
        return set()
    return set(re.findall(r'<a href="([EW]\d+_[NS]\d+)\.rd5">', html))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalogue", required=True)
    ap.add_argument("--satellite", default="satellite/index.json")
    ap.add_argument("--routing", default="routing/index.json")
    ap.add_argument("--apply", action="store_true",
                    help="actually rewrite the indexes; otherwise just report")
    args = ap.parse_args()

    catalogue = load(args.catalogue, None)
    if not catalogue or not catalogue.get("continents"):
        # The one answer that must never be given by accident.
        print("catalogue is missing or empty; refusing to prune anything",
              file=sys.stderr)
        return 1

    areas = live_areas(catalogue)
    if not areas:
        print("catalogue lists no areas; refusing to prune anything",
              file=sys.stderr)
        return 1

    orphans = []

    # --- imagery: an area that no longer exists ---
    sat = load(args.satellite, {"packs": []})
    keep = []
    for pack in sat.get("packs", []):
        if pack.get("area") in areas:
            keep.append(pack)
        else:
            orphans.append(("satellite", "%s.pmtiles" % pack["id"]))
            print("withdrawn area: %s (%s)" % (pack["id"], pack.get("area")),
                  file=sys.stderr)
    if args.apply and len(keep) != len(sat.get("packs", [])):
        sat["packs"] = keep
        with open(args.satellite, "w", encoding="utf-8") as f:
            json.dump(sat, f, indent=2)
            f.write("\n")

    # --- routing: a tile upstream stopped publishing ---
    live_tiles = upstream_tiles()
    if live_tiles:
        routing = load(args.routing, {"tiles": {}})
        held = routing.get("tiles", {})
        gone = [name for name in held if name not in live_tiles]
        for name in gone:
            orphans.append(("routing", "%s.rd5" % name))
            print("withdrawn upstream: %s" % name, file=sys.stderr)
        if args.apply and gone:
            for name in gone:
                del held[name]
            with open(args.routing, "w", encoding="utf-8") as f:
                json.dump(routing, f, indent=2, sort_keys=True)
                f.write("\n")

    for release, asset in orphans:
        print("%s %s" % (release, asset))
    if not orphans:
        print("nothing to prune", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

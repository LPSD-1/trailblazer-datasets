#!/usr/bin/env python3
"""Remember a published satellite pack, so the catalogue can include it.

    python record_satellite.py --entry dist/entry.json \
        --url https://github.com/OWNER/REPO/releases/download/satellite/x.pmtiles \
        --index satellite/index.json

WHY THIS EXISTS SEPARATELY
--------------------------
The catalogue is rebuilt from scratch every month by build_catalogue.py.
Anything merged straight into catalogue.json would be wiped by the next lane
refresh, and the satellite packs would vanish from every rider's Downloads
screen without a single line of code changing. So the record lives here, in
the repository, and the catalogue builder reads it.

It also keeps the two jobs independent: the lane refresh does not need to know
how imagery is built, and the imagery job does not need to rebuild the world
to publish one area.
"""
import argparse
import datetime as dt
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entry", required=True,
                    help="the entry build_satellite.py wrote")
    ap.add_argument("--url", required=True,
                    help="where the pack was actually uploaded")
    ap.add_argument("--area", required=True,
                    help="the catalogue area id this belongs to")
    ap.add_argument("--index", required=True)
    args = ap.parse_args()

    with open(args.entry, encoding="utf-8") as f:
        entry = json.load(f)

    entry["file"] = args.url
    entry["area"] = args.area
    entry["generated"] = dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")

    index = {"packs": []}
    if os.path.exists(args.index):
        with open(args.index, encoding="utf-8") as f:
            index = json.load(f)

    # Replaced, not appended. A rebuild of the same area is the SAME pack with
    # new bytes: two entries would offer a rider the identical area twice and
    # double every size total that counts them.
    packs = [p for p in index.get("packs", []) if p.get("id") != entry["id"]]
    packs.append(entry)
    packs.sort(key=lambda p: p["id"])
    index["packs"] = packs

    os.makedirs(os.path.dirname(os.path.abspath(args.index)), exist_ok=True)
    with open(args.index, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
        f.write("\n")

    print("recorded %s (%.1f MB) for area %s"
          % (entry["id"], entry["bytes"] / 1024.0 / 1024.0, args.area))
    return 0


if __name__ == "__main__":
    sys.exit(main())

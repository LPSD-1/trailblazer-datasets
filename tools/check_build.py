#!/usr/bin/env python3
"""Refuse to publish a data build that would make things worse.

    python tools/check_build.py --previous manifest.json --new dist/manifest.json

Everything downstream of a bad publish lands on riders' phones: local package
names deliberately carry no build date, so a republished pack REPLACES the good
copy already on a device. A quietly gutted dataset is therefore worse than no
update at all, and worse than a loud failure.

The failure this exists for is not a crash. fetch_rights_of_way.py skips
authorities it cannot reach, so a half-down source produces a build where every
step succeeds and a third of the country has silently vanished.

Exit code 1 means do not publish.
"""
import argparse
import json
import os
import sys

# How much the lane count may fall before this is treated as data loss rather
# than councils tidying their records.
#
# Real amendments move the national total by a fraction of a percent. Losing
# more than 2% in a month means something upstream broke, not that 18,000 ways
# stopped being rights of way.
MAX_NATIONAL_DROP = 0.02

# One area can legitimately move more - a council republishing its map, or our
# own splitter regrouping authorities - but not by half.
MAX_AREA_DROP = 0.25

# Below this many authorities the fetch clearly did not complete, whatever the
# exit codes said.
MIN_AUTHORITIES = 140


def load(path):
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf8") as fh:
        return json.load(fh)


def lane_totals(manifest):
    """-> (total, {package/region: lanes}, {package: lanes})

    KEYED ON THE REGION, NOT THE AREA, and that distinction cost a publish.

    An `area` id is derived from the authorities that happened to be packed
    into it - "midlands-barnsley-and-38-more". Fetch more data, the chunking
    shifts, and the same ground comes back as "...-and-41-more". The per-area
    check then compares a name against a name that no longer exists and reports
    every renamed area as having "lost every one of its ways": 50 of them on a
    build whose data had GROWN by 8.94%, with all 149 authorities answering.

    A region is one of six fixed names and does not move. It is a coarser
    bucket, which the rule can afford - a partial fetch still shows as a region
    losing most of its ways, and the per-vehicle and national checks above are
    unchanged. What it cannot afford is a key that changes for reasons that have
    nothing to do with the data.
    """
    by_region = {}
    by_package = {}
    total = 0
    for pkg in manifest.get("packages", []):
        count = pkg.get("laneCount", 0)
        key = "%s/%s" % (pkg["package"], pkg["region"])
        by_region[key] = by_region.get(key, 0) + count
        by_package[pkg["package"]] = by_package.get(pkg["package"], 0) + count
        total += count
    return total, by_region, by_package


def check_authorities(cache_dir, problems):
    path = os.path.join(cache_dir, "authorities.json")
    known = load(path)
    if known is None:
        problems.append("no authorities.json - the fetch never ran")
        return

    fetched = 0
    for code in known:
        folder = os.path.join(cache_dir, code)
        if not os.path.isdir(folder):
            continue
        if any(f.endswith(".json") and os.path.getsize(os.path.join(folder, f)) > 0
               for f in os.listdir(folder)):
            fetched += 1

    print("  authorities with data: %d of %d" % (fetched, len(known)))
    if fetched < MIN_AUTHORITIES:
        problems.append(
            "only %d of %d authorities have data (need %d). The source was "
            "probably down; re-run rather than publish a partial map."
            % (fetched, len(known), MIN_AUTHORITIES))


def check_totals(previous, new, problems):
    new_total, new_areas, new_packages = lane_totals(new)
    print("  ways in this build: %d" % new_total)

    if new_total == 0:
        problems.append("this build contains no ways at all")
        return

    if previous is None:
        print("  no previous build to compare against - first publish")
        return

    old_total, old_areas, old_packages = lane_totals(previous)
    print("  ways in the published build: %d" % old_total)
    if old_total == 0:
        return

    change = (new_total - old_total) / old_total
    print("  change: %+.2f%%" % (change * 100))
    if change < -MAX_NATIONAL_DROP:
        problems.append(
            "the national total fell %.1f%% (%d ways). That is data loss, not "
            "an amendment." % (-change * 100, old_total - new_total))

    # PER VEHICLE TYPE, and this is the check that was missing.
    #
    # The national total above is dominated by footpaths: 635,242 of 875,827 on
    # the build published on 10 September 2026. Byways open to all traffic - the
    # lanes this app exists for, the only ones a rider may legally ride - were
    # 11,851 of that, which is 1.35%.
    #
    # So the 2% national threshold could not see them. EVERY BYWAY IN GREAT
    # BRITAIN could vanish and the national total would fall 1.35%, under the
    # limit, and this check would pass. The per-area check catches a type that
    # goes to exactly zero everywhere. Between the two there was nothing, and
    # the gap is wide enough to lose a fifth of the country in silence.
    #
    # The commit that added this cited a 12.1% fall measured on the 16 September
    # run. That was wrong and is corrected in tools/test_check_build.py: it
    # compared raw fetched rows against built package lanes, which count
    # differently. That run actually GREW by 7.2%. The arithmetic above is the
    # reason this check exists; no particular run is.
    for name, old in sorted(old_packages.items()):
        now = new_packages.get(name, 0)
        if old == 0:
            continue
        moved = (now - old) / old
        print("  %-8s %6d -> %6d (%+.2f%%)" % (name, old, now, moved * 100))
        if moved < -MAX_NATIONAL_DROP:
            problems.append(
                "%s ways fell %.1f%% nationally (%d -> %d). That is only "
                "%.2f%% of the national total, which is why the check above "
                "did not object - and it is %.1f%% of every %s way a rider "
                "would get."
                % (name, -moved * 100, old, now,
                   (old - now) / old_total * 100, -moved * 100, name))

    # An area vanishing entirely is the clearest sign of a partial fetch, and
    # the national check can miss it when the area is small.
    for key, old in sorted(old_areas.items()):
        now = new_areas.get(key, 0)
        if old == 0:
            continue
        if now == 0:
            problems.append("%s lost every one of its %d ways" % (key, old))
        elif (now - old) / old < -MAX_AREA_DROP:
            problems.append("%s fell %.0f%% (%d -> %d ways)"
                            % (key, (1 - now / old) * 100, old, now))

    missing = set(old_areas) - set(new_areas)
    if missing:
        problems.append("regions that disappeared entirely: %s"
                        % ", ".join(sorted(missing)))


def check_packages_readable(new, dist_dir, key_path, problems):
    """Open one sealed package, as the app would."""
    try:
        import base64
        import gzip
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        print("  (cryptography missing - skipping the decrypt check)")
        return

    motor = [p for p in new.get("packages", []) if p["package"] == "motor"]
    if not motor:
        problems.append("no motor packages were built")
        return
    pkg = max(motor, key=lambda p: p.get("laneCount", 0))

    path = os.path.join(dist_dir, os.path.basename(pkg["file"]))
    if not os.path.isfile(path):
        problems.append("the index lists %s but it was not built" % pkg["file"])
        return

    with open(key_path, encoding="utf8") as fh:
        key = base64.b64decode(fh.read().strip())
    blob = open(path, "rb").read()

    try:
        prefix, body = blob[:18], blob[18:]
        if prefix[:4] != b"TBPK":
            raise ValueError("not a .tbpack")
        plain = gzip.decompress(AESGCM(key).decrypt(prefix[6:18], body, prefix))
        collection = json.loads(plain)
    except Exception as e:
        problems.append(
            "%s could not be opened with the packaging key (%s). Publishing "
            "it would put a package on devices that the app cannot read."
            % (pkg["file"], e))
        return

    features = len(collection.get("features", []))
    if features != pkg.get("laneCount"):
        problems.append("%s holds %d ways but the index claims %d"
                        % (pkg["file"], features, pkg.get("laneCount")))
    else:
        print("  opened %s: %d ways" % (os.path.basename(pkg["file"]), features))

    ids = [f["properties"]["lane_uid"] for f in collection.get("features", [])]
    if len(set(ids)) != len(ids):
        problems.append(
            "%s contains duplicate way ids; the app dedupes on them and would "
            "silently drop the extras" % pkg["file"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--previous", default="manifest.json")
    ap.add_argument("--new", default="dist/manifest.json")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--dist", default="dist/packages")
    ap.add_argument("--key", default="")
    ap.add_argument("--force", action="store_true",
                    help="publish anyway; for a drop you have checked yourself")
    args = ap.parse_args()

    new = load(args.new)
    if new is None:
        sys.exit("FATAL: %s was not built" % args.new)

    problems = []
    print("checking the build...")
    check_authorities(args.cache, problems)
    check_totals(load(args.previous), new, problems)
    if args.key:
        check_packages_readable(new, args.dist, args.key, problems)

    if not problems:
        print("\nOK to publish.")
        return

    print("\n%d problem(s) with this build:" % len(problems))
    for problem in problems:
        print("  - %s" % problem)

    if args.force:
        print("\n--force given: publishing anyway.")
        return

    print("\nNOT publishing. The build stays where it is, and every rider "
          "keeps the data they already have.")
    print("Re-run the workflow once the source is healthy, or run it with "
          "'force' if the change is real.")
    sys.exit(1)


if __name__ == "__main__":
    main()

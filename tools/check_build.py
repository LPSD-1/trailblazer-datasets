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
    """-> (total, {package-area: lanes})"""
    by_area = {}
    total = 0
    for pkg in manifest.get("packages", []):
        key = "%s/%s" % (pkg["package"], pkg.get("area") or pkg["region"])
        by_area[key] = by_area.get(key, 0) + pkg.get("laneCount", 0)
        total += pkg.get("laneCount", 0)
    return total, by_area


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
    new_total, new_areas = lane_totals(new)
    print("  ways in this build: %d" % new_total)

    if new_total == 0:
        problems.append("this build contains no ways at all")
        return

    if previous is None:
        print("  no previous build to compare against - first publish")
        return

    old_total, old_areas = lane_totals(previous)
    print("  ways in the published build: %d" % old_total)
    if old_total == 0:
        return

    change = (new_total - old_total) / old_total
    print("  change: %+.2f%%" % (change * 100))
    if change < -MAX_NATIONAL_DROP:
        problems.append(
            "the national total fell %.1f%% (%d ways). That is data loss, not "
            "an amendment." % (-change * 100, old_total - new_total))

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
        problems.append("areas that disappeared entirely: %s"
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

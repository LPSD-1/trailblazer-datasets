#!/usr/bin/env python3
r"""Refuse to publish a data build that would make things worse.

    python tools/check_build.py --previous manifest.json --new dist/manifest.json

Everything downstream of a bad publish lands on riders' phones: local package
names deliberately carry no build date, so a republished pack REPLACES the good
copy already on a device. A quietly gutted dataset is therefore worse than no
update at all, and worse than a loud failure.

The failure this exists for is not a crash. fetch_rights_of_way.py skips
authorities it cannot reach, so a half-down source produces a build where every
step succeeds and a third of the country has silently vanished.

What it gates, in order:

  * the authority floor       - MIN_AUTHORITIES answered
  * the national drop         - MAX_NATIONAL_DROP, and the same per vehicle type
  * the per-region drop       - MAX_AREA_DROP, and no region vanishing
  * the bounding box          - GB_BOUNDS, over the declared region bounds and
                                over the geometry inside the opened packages
  * the closure factor        - MAX_CLOSURE_FACTOR, both directions
  * the packages themselves   - they open with the key and hold what the index
                                says they hold

A DELIBERATE drop - the pivot removes foot, horse and bicycle ways entirely,
which is a ~98.6% national fall - is not forced through. It is recorded:

    python tools/check_build.py --rebaseline --reason "..." \
        --previous manifest.json --new dist/manifest.json

writes tools/build_baseline.json, publishes nothing and exits 2. That file
names the build it supersedes, the types dropped, the counts either side and
the reason; it is reviewed and committed like any other change. It then excuses
those types and nothing else, and stops applying the moment the published build
is no longer the one it was recorded against. See rebaseline_note().

Exit code 1 means do not publish. Exit code 2 means a baseline was written and
nothing was checked or published.
"""
import argparse
import datetime
import json
import math
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

# THE BOX GREAT BRITAIN FITS IN - west, south, east, north.
#
# The same numbers tools/build_catalogue.py:35 publishes as the United
# Kingdom's bounds, so a lane this refuses is a lane the catalogue would place
# outside the country it was filed under.
#
# Deliberately the whole of GB and not the published coverage (England and
# Wales). This gate is not here to police which countries we ship; it is here
# to catch geometry that is WRONG - a (0, 0) left by a blank WKT field, an
# easting/northing that never went through the projection, a lat/lon pair the
# right way round in the source and the wrong way round in the build. Every one
# of those publishes cleanly today and draws a lane in the sea.
GB_BOUNDS = (-8.70, 49.80, 1.80, 60.90)

# How far a DECLARED region box may sit outside that, and only a declared box.
# See check_declared_bounds; measured geometry gets no slack at all.
BOUNDS_SLACK = 0.5

# How far the live closure count may move between two builds before it is a bad
# read rather than a busy week.
#
# build_tro.py already refuses to BUILD below two thirds of last time
# (build_tro.py:359). This is the other half of that and the other direction:
# it runs at publish time, over the index that actually reached the repository,
# and it fires on a jump as well as a fall. A count that TRIPLES is as much a
# sign of a changed extract - duplicated rows, an expiry filter that stopped
# filtering - as one that collapses, and nothing anywhere was watching for it.
MAX_CLOSURE_FACTOR = 3.0

# WIRING, for whoever owns .github/workflows/traffic-orders.yml. build_tro.py
# writes its index straight over the committed one (build_tro.py:298), so the
# previous count has to come out of git before the build overwrites it:
#
#     git show HEAD:tro/index.json > /tmp/tro-previous.json
#     python tools/build_tro.py --key /tmp/dataset.key
#     python tools/check_build.py --previous manifest.json --new manifest.json \
#         --cache cache --closures-previous /tmp/tro-previous.json \
#         --closures-new tro/index.json
#
# Until that lands, this gate prints "THIS GATE DID NOT RUN" wherever no
# traffic-order index was built - which is every run of refresh-data.yml, since
# lanes and traffic orders are separate workflows. That line is the point: a
# gate that has not run must say so rather than print nothing and be counted.

# Where the rebaseline record lives. See rebaseline_note().
BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "build_baseline.json")


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


# ---------------------------------------------------------------- geometry


def coordinates(geometry):
    """Every (lon, lat) in a GeoJSON geometry, whatever shape it is.

    Lane packages hold LineStrings (build_packages.py:452), the traffic-order
    pack holds points and lines, and a GeometryCollection is legal GeoJSON that
    nothing here writes today. Walking the nesting rather than matching on the
    type means a new geometry shape cannot quietly opt out of the bounds check.
    """
    if not isinstance(geometry, dict):
        return
    if geometry.get("type") == "GeometryCollection":
        for child in geometry.get("geometries") or []:
            for point in coordinates(child):
                yield point
        return
    stack = [geometry.get("coordinates")]
    while stack:
        item = stack.pop()
        if not isinstance(item, (list, tuple)) or not item:
            continue
        if isinstance(item[0], (int, float)) and not isinstance(item[0], bool):
            if len(item) >= 2 and isinstance(item[1], (int, float)):
                yield float(item[0]), float(item[1])
            continue
        stack.extend(item)


def outside_gb(lon, lat, slack=0.0):
    west, south, east, north = GB_BOUNDS
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return True
    return (lon < west - slack or lon > east + slack
            or lat < south - slack or lat > north + slack)


def check_geometry_in_gb(collection, name, problems):
    """No way in a package may be drawn outside Great Britain.

    A lane at (0, 0) is not a lane a rider can ride to; it is a parse failure
    that survived every other check in this file, because a null island lane
    counts as one lane in the manifest exactly like a real one does.
    """
    strays = []
    for feature in collection.get("features", []) or []:
        for lon, lat in coordinates(feature.get("geometry")):
            if outside_gb(lon, lat):
                uid = (feature.get("properties") or {}).get("lane_uid")
                strays.append((uid or "(no id)", lon, lat))
                break

    if not strays:
        return
    shown = ", ".join("%s at %.4f,%.4f" % s for s in strays[:3])
    problems.append(
        "%s draws %d way(s) outside Great Britain (%s). The bounding box is "
        "%s; geometry outside it did not come from a definitive map, it came "
        "from a bad parse."
        % (name, len(strays), shown, GB_BOUNDS))


def check_declared_bounds(new, problems):
    """The regions the index itself claims, before any package is opened.

    Cheap, needs no key, and runs on every build including the ones CI runs
    without one. If a region's own declared box has left the country then the
    lanes inside it were placed by the same arithmetic.

    A REGION BOX IS PADDED ON PURPOSE and is measured with BOUNDS_SLACK for
    that reason. It is a bucket boundary (build_packages.py:127-132), not a
    coastline: the published East Anglia and North boxes both run east to
    1.85, past the 1.80 the catalogue gives as the United Kingdom's eastern
    edge, because the easternmost ground in England is Lowestoft at 1.76 and
    the box is rounded outwards to hold it. Measured against the tight box
    this check fired on the September manifest, which is the shape of a gate
    that gets turned off in its first week.

    The slack is half a degree - about 35 km - which keeps every published box
    and still refuses one that has genuinely left the country.
    """
    for region in new.get("regions", []) or []:
        bounds = region.get("bounds")
        rid = region.get("id", "(unnamed region)")
        if not isinstance(bounds, dict):
            problems.append("region %s declares no bounds" % rid)
            continue
        corners = [(bounds.get("west"), bounds.get("south")),
                   (bounds.get("east"), bounds.get("north"))]
        for lon, lat in corners:
            if not isinstance(lon, (int, float)) or isinstance(lon, bool) \
                    or not isinstance(lat, (int, float)) or isinstance(lat, bool):
                problems.append("region %s has an unreadable bounds corner "
                                "(%r, %r)" % (rid, lon, lat))
                break
            if outside_gb(float(lon), float(lat), BOUNDS_SLACK):
                problems.append(
                    "region %s claims a corner at %.4f,%.4f, which is outside "
                    "Great Britain %s" % (rid, lon, lat, GB_BOUNDS))
                break


# ---------------------------------------------------------------- closures


def closure_count(index):
    """Live restrictions in a traffic-order index, or None if it says nothing.

    Read from the index rather than the pack for the reason build_tro.py:260
    gives: every job can read the index and only the job that built the pack
    has the pack.
    """
    if not isinstance(index, dict):
        return None
    total = 0
    seen = False
    for pack in index.get("packs", []) or []:
        count = (pack or {}).get("features")
        if isinstance(count, int) and not isinstance(count, bool):
            total += count
            seen = True
    return total if seen else None


def check_closures(previous_index, new_index, problems):
    """The closure count may move a lot between builds. It may not move wildly.

    A rider shown no closure rides into one. The count is the only signal in
    the pipeline separating "a quiet week" from "the extract truncated" or
    "the expiry filter stopped filtering", and nothing compared it across a
    publish at all: build_tro.py:359 refuses to BUILD below two thirds of last
    time, which is one direction, inside the builder, and says nothing about
    the index that actually reached the repository.
    """
    # A BUILD THAT DID NOT BUILD CLOSURES IS NOT A BUILD THAT LOST THEM.
    #
    # The lanes workflow and the traffic-order workflow are separate jobs
    # (.github/workflows/refresh-data.yml and traffic-orders.yml), and the
    # lanes one writes no traffic-order index at all. If that absence read as
    # "every closure vanished" this gate would refuse every monthly lane
    # publish, and a gate that fires on the normal case gets passed --force
    # forever after. Absent means not run, and says so out loud; PRESENT and
    # empty is the failure.
    if new_index is None:
        print("  closures: this build wrote no traffic-order index - "
              "THIS GATE DID NOT RUN")
        return

    old = closure_count(previous_index)
    now = closure_count(new_index)

    if now is None:
        problems.append(
            "this build wrote a traffic-order index with no closure count in "
            "it, so nothing can tell a quiet day from a truncated extract.")
        return

    print("  live closures: %d" % now)
    if now == 0:
        problems.append(
            "this build carries no live closures anywhere in Great Britain, "
            "which is never true; it is a bad read.")
        return

    if not old:
        print("  no previous closure count - nothing to compare")
        return

    factor = (now / old) if now >= old else (old / now)
    print("  closures: %d -> %d (x%.2f)" % (old, now, now / old))
    if factor > MAX_CLOSURE_FACTOR:
        direction = "rose" if now > old else "fell"
        problems.append(
            "live closures %s by a factor of %.1f (%d -> %d), past the limit "
            "of %.1f. That is not a busy week; it is a changed extract."
            % (direction, factor, old, now, MAX_CLOSURE_FACTOR))


# --------------------------------------------------------------- baseline


def rebaseline_note():
    r"""Why a rebaseline is a committed file and not another flag.

    The pivot drops foot, horse and bicycle ways from the dataset entirely.
    That is a ~98.6% national fall, fifty times MAX_NATIONAL_DROP, and this
    file would refuse the cutover build - correctly, because from in here it is
    indistinguishable from the fetch collapsing.

    --force already exists and would get it through. --force is a shrug: it
    leaves nothing behind saying what was allowed through or why, and the next
    person to meet a red build learns that the way past this file is to pass
    it.

    So a rebaseline is a RECORD. It names the build it supersedes, the package
    types deliberately dropped, the counts before and after, and a reason in
    somebody's words. It is written by

        python tools/check_build.py --rebaseline --reason "..." \
            --previous manifest.json --new dist/manifest.json

    which writes the file and publishes NOTHING, then reviewed and committed.
    It excuses the types it names and nothing else: every remaining type is
    still gated at MAX_NATIONAL_DROP, every remaining region at MAX_AREA_DROP,
    and the moment the published build stops being the one it was recorded
    against it stops applying at all.
    """


def load_baseline(path, previous, problems):
    """-> the set of package names this baseline deliberately dropped.

    Empty when there is no baseline, when it does not apply to the build in
    hand, or when it is not a record anybody could review.
    """
    baseline = load(path)
    if baseline is None:
        return frozenset()

    dropped = baseline.get("dropped") or []
    if not isinstance(dropped, list) or not all(isinstance(d, str) for d in dropped):
        problems.append("%s lists no readable 'dropped' package types" % path)
        return frozenset()
    if not dropped:
        return frozenset()

    reason = (baseline.get("reason") or "").strip()
    if not reason:
        problems.append(
            "%s drops %s with no recorded reason. A rebaseline with nothing "
            "written down is a silent bypass, which is the one thing it must "
            "not be." % (path, ", ".join(sorted(dropped))))
        return frozenset()

    # SELF-EXPIRING, and that is the point of the file.
    #
    # The record names the exact published build it supersedes. Once the
    # cutover publishes, manifest.json IS the new shape, the totals no longer
    # match, and the baseline stops excusing anything - it cannot sit in the
    # repository quietly permitting a 98% drop every month afterwards.
    supersedes = baseline.get("supersedes") or {}
    old_total, _, _ = lane_totals(previous or {})
    recorded = supersedes.get("total")
    if recorded != old_total:
        print("  baseline at %s was recorded against a build of %s ways; the "
              "published build now holds %d. It no longer applies."
              % (path, recorded, old_total))
        return frozenset()

    print("  REBASELINED against %s (recorded %s)"
          % (path, baseline.get("recorded", "an unrecorded date")))
    print("    dropping: %s" % ", ".join(sorted(dropped)))
    print("    reason: %s" % reason)
    return frozenset(dropped)


def write_baseline(path, previous, new, reason):
    """Write down what this cutover changes, for review."""
    old_total, _, old_packages = lane_totals(previous)
    new_total, _, new_packages = lane_totals(new)
    dropped = sorted(name for name, count in old_packages.items()
                     if count and not new_packages.get(name))

    record = {
        "recorded": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": reason,
        "dropped": dropped,
        "supersedes": {
            "generated": (previous or {}).get("generated"),
            "total": old_total,
            "packages": old_packages,
        },
        "becomes": {
            "total": new_total,
            "packages": new_packages,
        },
        "nationalDrop": (round((old_total - new_total) / float(old_total), 4)
                         if old_total else None),
    }
    with open(path, "w", encoding="utf8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return record


def excluding(dropped, areas, packages):
    """Both totals maps with the rebaselined package types taken out.

    An area key is "package/region" (lane_totals), so one set filters both -
    otherwise every foot region would read as having lost every one of its ways
    on the cutover, which is true, and is exactly what was signed off.
    """
    packages = {k: v for k, v in packages.items() if k not in dropped}
    areas = {k: v for k, v in areas.items()
             if k.split("/", 1)[0] not in dropped}
    return sum(packages.values()), areas, packages


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


def check_totals(previous, new, problems, dropped=frozenset()):
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

    if dropped:
        # A rebaseline excuses the types it names and NOTHING else. Taking
        # them out of both sides leaves every remaining type gated exactly as
        # before: 2% national, 25% per region, no region vanishing.
        still_here = sorted(n for n in dropped if new_packages.get(n))
        if still_here:
            problems.append(
                "the baseline says %s was dropped, but this build still "
                "carries %s. Either the build did not do what was signed off "
                "or the baseline describes a different build."
                % (", ".join(still_here),
                   ", ".join("%d %s ways" % (new_packages[n], n)
                             for n in still_here)))
        new_total, new_areas, new_packages = excluding(
            dropped, new_areas, new_packages)
        old_total, old_areas, old_packages = excluding(
            dropped, old_areas, old_packages)
        print("  excluding %s: %d -> %d ways"
              % (", ".join(sorted(dropped)), old_total, new_total))
        if old_total == 0:
            return
        if new_total == 0:
            problems.append(
                "with %s excluded this build holds no ways at all"
                % ", ".join(sorted(dropped)))
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


def packages_to_open(new):
    """Which sealed packages get opened, in order.

    EVERY motor package, because those are the lanes this app exists for and
    they are small - 11,851 of 875,827 ways on the September build, so opening
    all of them costs a couple of megabytes - and after the pivot they are the
    only packages there are. Plus the largest of every other type, as a sample,
    so a bad parse in foot data is still seen while foot data still ships.

    A geometry check that only ever opened one package would leave the other
    hundred unlooked at, and the bad parse this exists to catch does not
    politely land in the biggest motor pack.
    """
    chosen = []
    biggest = {}
    for pkg in new.get("packages", []):
        name = pkg.get("package")
        if name == "motor":
            chosen.append(pkg)
            continue
        best = biggest.get(name)
        if best is None or pkg.get("laneCount", 0) > best.get("laneCount", 0):
            biggest[name] = pkg
    chosen.sort(key=lambda p: -p.get("laneCount", 0))
    return chosen + [biggest[name] for name in sorted(biggest)]


def check_packages_readable(new, dist_dir, key_path, problems):
    """Open the sealed packages, as the app would, and look at what is inside."""
    try:
        import base64
        import gzip
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        print("  (cryptography missing - skipping the decrypt check)")
        return

    if not [p for p in new.get("packages", []) if p["package"] == "motor"]:
        problems.append("no motor packages were built")
        return

    with open(key_path, encoding="utf8") as fh:
        key = base64.b64decode(fh.read().strip())

    opened = 0
    for pkg in packages_to_open(new):
        path = os.path.join(dist_dir, os.path.basename(pkg["file"]))
        if not os.path.isfile(path):
            problems.append("the index lists %s but it was not built"
                            % pkg["file"])
            continue

        blob = open(path, "rb").read()
        try:
            prefix, body = blob[:18], blob[18:]
            if prefix[:4] != b"TBPK":
                raise ValueError("not a .tbpack")
            plain = gzip.decompress(
                AESGCM(key).decrypt(prefix[6:18], body, prefix))
            collection = json.loads(plain)
        except Exception as e:
            problems.append(
                "%s could not be opened with the packaging key (%s). "
                "Publishing it would put a package on devices that the app "
                "cannot read." % (pkg["file"], e))
            continue

        opened += 1
        features = len(collection.get("features", []))
        if features != pkg.get("laneCount"):
            problems.append("%s holds %d ways but the index claims %d"
                            % (pkg["file"], features, pkg.get("laneCount")))

        ids = [f["properties"]["lane_uid"]
               for f in collection.get("features", [])]
        if len(set(ids)) != len(ids):
            problems.append(
                "%s contains duplicate way ids; the app dedupes on them and "
                "would silently drop the extras" % pkg["file"])

        check_geometry_in_gb(collection, os.path.basename(pkg["file"]),
                             problems)

    print("  opened %d package(s) and checked their geometry" % opened)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--previous", default="manifest.json")
    ap.add_argument("--new", default="dist/manifest.json")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--dist", default="dist/packages")
    ap.add_argument("--key", default="")
    ap.add_argument("--force", action="store_true",
                    help="publish anyway; for a drop you have checked yourself")
    ap.add_argument("--closures-previous", default="tro/index.json",
                    help="the published traffic-order index")
    ap.add_argument("--closures-new", default="dist/tro/index.json",
                    help="this build's traffic-order index")
    ap.add_argument("--baseline", default=BASELINE,
                    help="the recorded rebaseline, if there is one")
    ap.add_argument("--rebaseline", action="store_true",
                    help="write down what this build changes and publish "
                         "nothing; needs --reason")
    ap.add_argument("--reason", default="",
                    help="why the drop in --rebaseline is deliberate")
    args = ap.parse_args()

    new = load(args.new)
    if new is None:
        sys.exit("FATAL: %s was not built" % args.new)
    previous = load(args.previous)

    if args.rebaseline:
        if not args.reason.strip():
            sys.exit("--rebaseline needs --reason \"why this drop is "
                     "deliberate\". A record with no reason in it is a "
                     "silent bypass with extra steps.")
        if previous is None:
            sys.exit("FATAL: nothing to rebaseline against - %s does not "
                     "exist" % args.previous)
        record = write_baseline(args.baseline, previous, new,
                                args.reason.strip())
        print("wrote %s" % args.baseline)
        print(json.dumps(record["supersedes"]["packages"], sort_keys=True))
        print("  -> %s" % json.dumps(record["becomes"]["packages"],
                                     sort_keys=True))
        print("  dropping: %s" % (", ".join(record["dropped"]) or "nothing"))
        print("  national drop: %s" % record["nationalDrop"])
        print("\nNOTHING WAS PUBLISHED. Read that file, commit it, and run "
              "check_build.py again; the gates will then hold every type it "
              "does not name.")
        sys.exit(2)

    problems = []
    print("checking the build...")
    check_authorities(args.cache, problems)
    dropped = load_baseline(args.baseline, previous, problems)
    check_totals(previous, new, problems, dropped)
    check_declared_bounds(new, problems)
    check_closures(load(args.closures_previous), load(args.closures_new),
                   problems)
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

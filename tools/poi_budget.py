#!/usr/bin/env python3
"""Do the nine POI categories fit inside the download budget?

Spec 6.3 asks for "a POI density budget so a region stays inside the size
budget"; spec 8 names the size budget - **UK full download under 1.5 GB**.
Nothing until now answered the question with numbers.

    # how many POIs are there, without downloading them
    python tools/poi_budget.py counts --region midlands --cache cache/poi-counts

    # what one POI costs, measured against a real container
    python tools/poi_budget.py cost --container containers/bicycle-north-york.tbmap

    # the answer: counts x cost against the 1.5 GB budget
    python tools/poi_budget.py budget --counts cache/poi-counts \
        --containers containers --cost 41.2

    python tools/poi_budget.py --selftest

WHY `counts` AND NOT `build_pois.py fetch`.
Overpass will answer `out count;` for a tag over a region in one small
response. Fetching the elements to count them means downloading the Midlands'
car parks to find out how many there are, four times over while tuning the
filter. The count is the only thing the budget question needs.

WHY THE COST IS MEASURED AND NOT DIVIDED.
`build_pois.do_build` already records the lesson: SQLite pages, the primary-key
index and the r-tree do not share out evenly, so bytes-per-POI taken by
dividing a total is wrong. The cost here is a DELTA - the same container
written with and without a given set of rows, VACUUMed both times so the
baseline is not carrying free pages the comparison would blame on POIs - and it
is measured gzipped as well as plain, because 1.5 GB is a DOWNLOAD budget and
containers ship compressed.

WHERE THE ROW SHAPE COMES FROM, STATED PLAINLY.
With a POI cache on disk this measures the real rows. Without one it SWEEPS a
range of shapes - names from absent to long, `opening_hours` from never present
to always - and reports the range rather than a single number. The sweep is an
assumption and is labelled `synthetic` in every output; a single invented
figure dressed up as a measurement would be worse than the range.

A MISSING COUNT IS NOT ZERO.
`parse_count` returns None when Overpass answers with something it does not
recognise. A category silently counted as zero is a category the budget says
is free, which is precisely the wrong answer to hand a density decision -
`build_pois.wanted_categories` refuses an unknown category name for the same
reason.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_pois as PO            # noqa: E402

#: Spec 8: "UK full download - under 1.5 GB". Gigabytes, decimal, because that
#: is how a phone's storage and a rider's data plan are both sold.
DOWNLOAD_BUDGET_BYTES = 1_500_000_000

#: The shapes swept when no POI cache is on disk. (name length, share of rows
#: carrying `opening_hours`). The ends are deliberately beyond anything real:
#: a POI set with no names at all and one where every row has a long name and
#: opening hours bracket whatever OSM actually holds, so the true cost is
#: inside the reported range rather than near one end of it.
SHAPES = [("bare", 0, 0.0), ("typical", 16, 0.25), ("heavy", 40, 1.0)]

#: `opening_hours` values in OSM are long. This is a real one, used so the
#: 'heavy' end of the sweep is heavy for the right reason.
HOURS = "Mo-Fr 08:00-18:00; Sa 09:00-17:00; Su,PH off"


# ------------------------------------------------------------- the counts

def count_query(selectors, bbox, timeout=300):
    """An Overpass query that returns only a count.

    `out count;` and not `out ids;`: the answer to "how many car parks are in
    the Midlands" must not be a list of car parks.
    """
    west, south, east, north = bbox
    box = "%.6f,%.6f,%.6f,%.6f" % (south, west, north, east)
    parts = "".join('nwr["%s"="%s"](%s);' % (k, v, box) for k, v in selectors)
    return "[out:json][timeout:%d];(%s);out count;" % (timeout, parts)


def parse_count(payload):
    """The total from an `out count;` response, or None.

    None and never zero. A shape we do not recognise means we did not get an
    answer; reporting that as zero would tell the budget the category is free.
    """
    if not isinstance(payload, dict):
        return None
    for element in payload.get("elements", []) or []:
        if not isinstance(element, dict):
            continue
        if element.get("type") != "count":
            continue
        total = (element.get("tags") or {}).get("total")
        try:
            return int(total)
        except (TypeError, ValueError):
            return None
    return None


def fetch_counts(region, bbox, opener=None, url=PO.OVERPASS_URL, log=print):
    """{category: count or None} for one region, one request per category."""
    out = {}
    opener = opener or urllib.request.urlopen
    for name, selectors in PO.CATEGORIES:
        query = count_query(selectors, bbox)
        request = urllib.request.Request(
            url, data=urllib.parse.urlencode({"data": query}).encode(),
            headers={"User-Agent": PO.USER_AGENT})
        try:
            with opener(request, timeout=600) as response:
                payload = json.loads(response.read().decode("utf-8"))
            total = parse_count(payload)
        except Exception as error:      # noqa: BLE001
            log("    %-10s FAILED: %s" % (name, error))
            total = None
        out[name] = total
        log("    %-10s %s" % (name, "?" if total is None else total))
    return out


def counts_path(cache, region):
    return os.path.join(cache, "%s.json" % region)


# --------------------------------------------------------------- the cost

def synthetic_pois(count, name_len, hours_share, source_date="2026-09-24"):
    """`count` rows of a stated shape, in `build_pois`' row format."""
    out = []
    for i in range(count):
        out.append({
            "poi_uid": "osm:n%d" % (1000000 + i),
            "category": PO.CATEGORY_NAMES[i % len(PO.CATEGORY_NAMES)],
            "name": ("N" * name_len) if name_len else None,
            # Spread over England and Wales so the r-tree is not packing a
            # single point a thousand times - an r-tree over coincident points
            # compresses to nothing and would flatter the measurement.
            "lat": round(50.0 + (i % 997) * 0.005, PO.COORD_DP),
            "lon": round(-5.0 + (i % 991) * 0.007, PO.COORD_DP),
            "opening_hours": HOURS if (i % 100) < int(hours_share * 100)
            else None,
            "source_date": source_date,
        })
    return out


def measure_cost(container, pois):
    """Bytes per POI, plain and gzipped, as a delta against the container.

    The baseline is VACUUMed before measuring, exactly as `build_pois.do_build`
    does, so the delta is POIs and not free pages the original was already
    carrying.
    """
    handle, work = tempfile.mkstemp(suffix=".tbmap")
    os.close(handle)
    try:
        shutil.copyfile(container, work)
        baseline = sqlite3.connect(work)
        baseline.execute("VACUUM")
        baseline.close()
        before = os.path.getsize(work)
        before_gz = PO.gzip_size(work)
        PO.write_pois(work, pois)
        after = os.path.getsize(work)
        after_gz = PO.gzip_size(work)
    finally:
        os.remove(work)
    n = max(1, len(pois))
    return {
        "rows": len(pois),
        "plain_delta": after - before,
        "gzip_delta": after_gz - before_gz,
        "plain_per_poi": (after - before) / float(n),
        "gzip_per_poi": (after_gz - before_gz) / float(n),
    }


def sweep_cost(container, count=20000, shapes=SHAPES):
    """The cost across SHAPES, since we cannot know the real row shape without
    a cache. Reported as a range, labelled synthetic."""
    out = {}
    for label, name_len, hours_share in shapes:
        out[label] = dict(
            measure_cost(container, synthetic_pois(count, name_len,
                                                   hours_share)),
            name_len=name_len, hours_share=hours_share)
    return out


def cost_from_cache(container, cache, region):
    """The real thing, when a POI cache exists. Returns None when it does not -
    NOT a zero, and not a guess."""
    try:
        pois, _dates = PO.load_cached(cache, region, PO.REGIONS[region],
                                      log=lambda _m: None)
    except SystemExit:
        return None
    if not pois:
        return None
    return dict(measure_cost(container, pois), source="cache")


# ------------------------------------------------------------- the budget

def gzip_sizes(directory):
    """{container: compressed bytes} for every .tbmap in a directory.

    Compressed, because spec 8's 1.5 GB is a download budget and containers
    ship compressed. Measuring the plain size would leave the budget with
    headroom it does not have, in the direction that matters.
    """
    out = {}
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".tbmap"):
            continue
        out[name] = PO.gzip_size(os.path.join(directory, name))
    return out


def budget(counts_by_region, gzip_per_poi, container_bytes,
           budget_bytes=DOWNLOAD_BUDGET_BYTES):
    """The verdict, with every input visible in the result.

    `unknown_categories` is the number of counts that came back as None. A
    verdict computed over an incomplete count is reported as `incomplete`
    rather than `inside` - a budget that says "fits" while four categories
    were never counted is the kind of green result this codebase exists to
    distrust.
    """
    total_pois = 0
    unknown = 0
    per_region = {}
    for region, counts in sorted(counts_by_region.items()):
        known = [v for v in counts.values() if v is not None]
        unknown += sum(1 for v in counts.values() if v is None)
        per_region[region] = {
            "pois": sum(known),
            "uncounted_categories": sum(1 for v in counts.values()
                                        if v is None),
            "poi_bytes": int(round(sum(known) * gzip_per_poi)),
            "by_category": dict(counts),
        }
        total_pois += sum(known)
    poi_bytes = int(round(total_pois * gzip_per_poi))
    containers = sum(container_bytes.values())
    total = containers + poi_bytes
    return {
        "budget_bytes": budget_bytes,
        "container_bytes": containers,
        "poi_bytes": poi_bytes,
        "total_bytes": total,
        "headroom_bytes": budget_bytes - total,
        "poi_share_pct": round(100.0 * poi_bytes / total, 2) if total else None,
        "pois": total_pois,
        "gzip_per_poi": gzip_per_poi,
        "uncounted_categories": unknown,
        "verdict": ("incomplete" if unknown
                    else ("inside" if total <= budget_bytes else "over")),
        "regions": per_region,
    }


# ------------------------------------------------------------------- cli

def do_counts(args, log=print, opener=None):
    if args.region not in PO.REGIONS:
        raise SystemExit("unknown region %s" % args.region)
    counts = fetch_counts(args.region, PO.REGIONS[args.region], opener=opener,
                          log=log)
    os.makedirs(args.cache, exist_ok=True)
    path = counts_path(args.cache, args.region)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"region": args.region,
                   "counted_at": datetime.now(timezone.utc).date().isoformat(),
                   "counts": counts}, handle, indent=1, sort_keys=True)
    known = [v for v in counts.values() if v is not None]
    log("%s: %d POIs over %d of %d categories -> %s"
        % (args.region, sum(known), len(known), len(counts), path))
    return 0


def do_cost(args, log=print):
    real = None
    if args.cache and args.region:
        real = cost_from_cache(args.container, args.cache, args.region)
    if real is not None:
        log("measured against the real cache for %s" % args.region)
        log("    %d rows: %.1f plain bytes per POI, %.1f compressed"
            % (real["rows"], real["plain_per_poi"], real["gzip_per_poi"]))
        result = {"source": "cache", "measured": real}
    else:
        if args.cache:
            log("NO POI CACHE for %s - sweeping synthetic row shapes instead"
                % (args.region or "any region"))
        swept = sweep_cost(args.container, count=args.rows)
        for label, _n, _h in SHAPES:
            entry = swept[label]
            log("    %-8s name=%2d hours=%3.0f%%  %6.1f plain  %6.1f gzip"
                % (label, entry["name_len"], 100 * entry["hours_share"],
                   entry["plain_per_poi"], entry["gzip_per_poi"]))
        low = min(e["gzip_per_poi"] for e in swept.values())
        high = max(e["gzip_per_poi"] for e in swept.values())
        log("    SYNTHETIC: %.1f to %.1f compressed bytes per POI"
            % (low, high))
        result = {"source": "synthetic", "sweep": swept,
                  "gzip_per_poi_low": low, "gzip_per_poi_high": high}
    result["container"] = os.path.basename(args.container)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
        log("    report -> %s" % args.report)
    return 0


def headroom(container_bytes, gzip_per_poi,
             budget_bytes=DOWNLOAD_BUDGET_BYTES):
    """How many POIs the budget still has room for, given what ships today.

    THE INVERTED QUESTION, and it is the one that can be answered without
    Overpass. `budget` needs a count of POIs; this needs none. It says what
    would have to be true for the budget to bite - "the nine categories would
    have to exceed N POIs" - which is a falsifiable claim rather than an
    estimate, and one a single `counts` run can later settle.
    """
    used = sum(container_bytes.values())
    left = budget_bytes - used
    return {
        "budget_bytes": budget_bytes,
        "container_bytes": used,
        "containers": len(container_bytes),
        "headroom_bytes": left,
        "gzip_per_poi": gzip_per_poi,
        "pois_that_fit": int(left // gzip_per_poi) if gzip_per_poi > 0
        else None,
        # Negative when the containers alone are already over. Reported rather
        # than clamped: a budget already blown is the single most important
        # thing this tool could say, and a clamp to zero would hide it.
        "already_over": left < 0,
    }


def do_headroom(args, log=print):
    sizes = gzip_sizes(args.containers)
    if not sizes:
        raise SystemExit("no .tbmap containers in %s" % args.containers)
    result = headroom(sizes, args.cost)
    log("POI headroom against spec 8's %.2f GB download budget"
        % (result["budget_bytes"] / 1e9))
    log("    %-16s %8.2f MB over %d containers"
        % ("published now", result["container_bytes"] / 1e6,
           result["containers"]))
    log("    %-16s %8.2f MB" % ("headroom", result["headroom_bytes"] / 1e6))
    log("    %-16s %8.1f compressed bytes" % ("per POI", result["gzip_per_poi"]))
    log("    %-16s %8d POIs would fit" % ("so", result["pois_that_fit"] or 0))
    if result["already_over"]:
        log("    THE CONTAINERS ALONE ARE ALREADY OVER THE BUDGET")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
        log("    report -> %s" % args.report)
    return 0 if not result["already_over"] else 1


def do_budget(args, log=print):
    counts_by_region = {}
    for name in sorted(os.listdir(args.counts)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(args.counts, name), encoding="utf-8") as handle:
            blob = json.load(handle)
        counts_by_region[blob["region"]] = blob["counts"]
    if not counts_by_region:
        raise SystemExit("no counts in %s - run `counts` first" % args.counts)
    sizes = gzip_sizes(args.containers)
    result = budget(counts_by_region, args.cost, sizes)
    log("POI density budget (spec 6.3 against spec 8's %.2f GB)"
        % (result["budget_bytes"] / 1e9))
    for region, entry in sorted(result["regions"].items()):
        log("    %-12s %7d POIs -> %8.2f MB%s"
            % (region, entry["pois"], entry["poi_bytes"] / 1e6,
               "" if not entry["uncounted_categories"]
               else "  (%d categories uncounted)"
                    % entry["uncounted_categories"]))
    log("    %-12s %8.2f MB" % ("containers", result["container_bytes"] / 1e6))
    log("    %-12s %8.2f MB  (%.2f%% of the download)"
        % ("POIs", result["poi_bytes"] / 1e6, result["poi_share_pct"] or 0.0))
    log("    %-12s %8.2f MB" % ("total", result["total_bytes"] / 1e6))
    log("    %-12s %8.2f MB" % ("headroom", result["headroom_bytes"] / 1e6))
    log("    VERDICT: %s" % result["verdict"].upper())
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
        log("    report -> %s" % args.report)
    return 0 if result["verdict"] == "inside" else 1


def selftest(log=print):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s%s" % (name, (": " + detail) if detail else ""))

    query = count_query([("amenity", "fuel")], (-3.0, 52.0, -1.0, 53.0))
    check("the query asks for a count and not a body",
          "out count;" in query and "out body" not in query, query)
    check("a count response parses",
          parse_count({"elements": [{"type": "count",
                                     "tags": {"total": "1234"}}]}) == 1234)
    check("an unrecognised response is unknown, not zero",
          parse_count({"elements": []}) is None)
    check("and so is a non-numeric total",
          parse_count({"elements": [{"type": "count",
                                     "tags": {"total": "lots"}}]}) is None)

    result = budget({"midlands": {"fuel": 100, "food": None}}, 40.0,
                    {"a.tbmap": 1000})
    check("an uncounted category makes the verdict incomplete",
          result["verdict"] == "incomplete", repr(result["verdict"]))
    check("and is counted", result["uncounted_categories"] == 1, repr(result))
    complete = budget({"midlands": {"fuel": 100}}, 40.0, {"a.tbmap": 1000})
    check("a complete count inside the budget is inside",
          complete["verdict"] == "inside", repr(complete["verdict"]))
    over = budget({"midlands": {"fuel": 100}}, 40.0,
                  {"a.tbmap": DOWNLOAD_BUDGET_BYTES})
    check("and over it is over", over["verdict"] == "over",
          repr(over["verdict"]))

    rows = synthetic_pois(10, 16, 0.5)
    check("the sweep produces the row shape build_pois writes",
          set(rows[0]) == {"poi_uid", "category", "name", "lat", "lon",
                           "opening_hours", "source_date"}, repr(rows[0]))
    check("and every category is exercised",
          len(set(r["category"] for r in synthetic_pois(90, 0, 0.0)))
          == len(PO.CATEGORY_NAMES))

    for failure in failures:
        log("  FAIL " + failure)
    log("selftest: %s" % ("FAILED %d" % len(failures) if failures else "ok"))
    return 1 if failures else 0


def parse_args(argv):
    parser = argparse.ArgumentParser(description="POI density budget")
    parser.add_argument("--selftest", action="store_true")
    sub = parser.add_subparsers(dest="command")

    c = sub.add_parser("counts", help="how many POIs, without fetching them")
    c.add_argument("--region", required=True)
    c.add_argument("--cache", default="cache/poi-counts")

    o = sub.add_parser("cost", help="bytes per POI, measured")
    o.add_argument("--container", required=True)
    o.add_argument("--cache", help="a build_pois cache, for the real row shape")
    o.add_argument("--region")
    o.add_argument("--rows", type=int, default=20000)
    o.add_argument("--report")

    b = sub.add_parser("budget", help="counts x cost against spec 8")
    b.add_argument("--counts", default="cache/poi-counts")
    b.add_argument("--containers", default="containers")
    b.add_argument("--cost", type=float, required=True,
                   help="compressed bytes per POI, from `cost`")
    b.add_argument("--report")

    h = sub.add_parser("headroom", help="how many POIs would still fit")
    h.add_argument("--containers", default="containers")
    h.add_argument("--cost", type=float, required=True,
                   help="compressed bytes per POI, from `cost`")
    h.add_argument("--report")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.selftest:
        return selftest()
    if args.command == "counts":
        return do_counts(args)
    if args.command == "cost":
        return do_cost(args)
    if args.command == "budget":
        return do_budget(args)
    if args.command == "headroom":
        return do_headroom(args)
    raise SystemExit("nothing to do; --selftest, counts, cost or budget")


if __name__ == "__main__":
    sys.exit(main())

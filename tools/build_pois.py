#!/usr/bin/env python3
"""Points of interest for a region container - the nine categories, nothing else.

Step 1.10 of `greenroadmap-app/docs/PIVOT-PLAN.md`, writing the `pois` table of
`docs/WAYS-SCHEMA.md`. The source is OpenStreetMap over the Overpass API, and
the tag list per category is spec section 6.2.

    # 1. fetch, once, into a cache that makes the build reproducible offline
    python tools/build_pois.py fetch --region midlands --cache cache/pois

    # 2. add them to a COPY of a region container and report what they cost
    python tools/build_pois.py build --region midlands --cache cache/pois \
        --container containers/bicycle-midlands-derbyshire-and-38-more.tbmap

WHY THE FILTER IS HARD, AND WHY IT IS NOT NEGOTIABLE.
A general POI extract for the Midlands is larger than the lane data it would
sit beside, and the lane data is the product. Spec section 6.2 picks nine
categories a rider on a green lane actually uses; this file carries exactly
those and refuses to grow. `CATEGORIES` is the whole list, and
`test_build_pois.py` asserts it matches the schema's set exactly, so adding a
tenth means arguing with a red test rather than editing a dict.

REPRODUCIBILITY. `fetch` writes raw Overpass JSON to the cache with the date it
was read; `build` reads only the cache. Two builds from one cache produce the
same rows in the same order with the same `source_date`, because every row is
keyed by its OSM id and the insert is sorted by that key.

HONESTY (spec 6.4). Every row carries `source_date`. A POI is advisory; the
container says when it was last read and the app is expected to say so too.
With a published container to compare against (`--previous`), `source_date`
is the day the row was first seen AS IT IS - unchanged since - and the day
the region was last read is `meta.pois_checked`. See write_pois.
"""
import argparse
import collections
import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stable_ids  # noqa: E402

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "trailblazer-datasets/1.0 (+https://trailblazer.app; POI build)"

# THE NINE CATEGORIES. docs/WAYS-SCHEMA.md names them; spec section 6.2 gives
# the tags and the reason. Order is PRIORITY order: an element matching two
# categories takes the first one here, so the answer never depends on dict
# iteration order. Fuel outranks food because a filling station with a cafe in
# it is a filling station to a rider who is nearly empty.
CATEGORIES = [
    ("fuel", [("amenity", "fuel")]),
    ("repair", [("shop", "motorcycle"), ("shop", "car_repair"),
                ("shop", "tyres")]),
    ("water", [("amenity", "drinking_water")]),
    ("toilets", [("amenity", "toilets")]),
    ("atm", [("amenity", "atm")]),
    ("camping", [("tourism", "camp_site"), ("tourism", "caravan_site")]),
    ("food", [("amenity", "cafe"), ("amenity", "pub"),
              ("amenity", "restaurant"), ("amenity", "fast_food")]),
    ("viewpoint", [("tourism", "viewpoint"), ("amenity", "shelter")]),
    ("parking", [("amenity", "parking"), ("highway", "services")]),
]

CATEGORY_NAMES = [name for name, _ in CATEGORIES]

# Must match REGIONS in build_packages.py. Copied rather than imported because
# build_packages exits the process on an absent `cryptography`, and a POI build
# has no business needing an encryption library. test_build_pois.py imports
# both and fails if a box ever drifts.
REGIONS = {
    "south-west": (-6.45, 49.85, -1.85, 51.95),
    "south-east": (-1.85, 50.50, 1.45, 51.95),
    "east-anglia": (-0.40, 51.50, 1.85, 53.05),
    "midlands": (-3.25, 51.90, 0.15, 53.60),
    "wales": (-5.35, 51.35, -2.60, 53.45),
    "north": (-3.70, 53.00, 1.85, 55.90),
}

# A POI nobody may use is not a POI. `access=private` and `access=no` are the
# two OSM says plainly; `customers` stays, because a pub car park is exactly
# where a rider parks.
CLOSED_ACCESS = ("private", "no")

# Coordinates are rounded before they are stored, so a rebuild against the same
# cache cannot differ in the last float digit. 7 dp is ~11 mm.
COORD_DP = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS pois (
  poi_uid TEXT PRIMARY KEY, category TEXT NOT NULL, name TEXT,
  lat REAL NOT NULL, lon REAL NOT NULL, opening_hours TEXT,
  source_date TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS pois_bbox
  USING rtree(id, min_lon, max_lon, min_lat, max_lat);
"""


# ---------------------------------------------------------------- categorise

def categorise(tags):
    """The one category this element ships as, or None if it does not ship.

    First match in CATEGORIES order wins. Returning None is the common case and
    the point of the file: everything OSM knows that a rider does not need.
    """
    if not tags:
        return None
    for name, selectors in CATEGORIES:
        for key, value in selectors:
            if tags.get(key) == value:
                return name
    return None


def is_usable(tags):
    """False when OSM says plainly that nobody may use it."""
    return (tags or {}).get("access") not in CLOSED_ACCESS


def element_uid(element):
    """Stable across builds: OSM ids are stable, and the type prefix keeps a
    node and a way that happen to share a number apart."""
    kind = element.get("type")
    if kind not in ("node", "way", "relation"):
        return None
    return "osm:%s%s" % (kind[0], element.get("id"))


def element_point(element):
    """lat/lon for a node, the Overpass `center` for a way or relation."""
    if element.get("lat") is not None and element.get("lon") is not None:
        return float(element["lat"]), float(element["lon"])
    center = element.get("center")
    if center and center.get("lat") is not None:
        return float(center["lat"]), float(center["lon"])
    return None


def poi_of(element, source_date):
    """One `pois` row, or None. Every rejection here is deliberate:
    uncategorised, access-denied, or no position to draw it at."""
    tags = element.get("tags") or {}
    category = categorise(tags)
    if category is None or not is_usable(tags):
        return None
    uid = element_uid(element)
    point = element_point(element)
    if uid is None or point is None:
        return None
    lat, lon = point
    return {
        "poi_uid": uid,
        "category": category,
        "name": tags.get("name"),
        "lat": round(lat, COORD_DP),
        "lon": round(lon, COORD_DP),
        "opening_hours": tags.get("opening_hours"),
        "source_date": source_date,
    }


def in_bbox(poi, bbox):
    west, south, east, north = bbox
    return west <= poi["lon"] <= east and south <= poi["lat"] <= north


# -------------------------------------------------------------------- fetch

def query_for(selectors, bbox, timeout):
    west, south, east, north = bbox
    box = "%.6f,%.6f,%.6f,%.6f" % (south, west, north, east)
    parts = "".join('nwr["%s"="%s"](%s);' % (k, v, box) for k, v in selectors)
    # `out tags center` and not `out body`: we want the tags and one point, and
    # NOT the node list of every car park polygon in the Midlands.
    return "[out:json][timeout:%d];(%s);out tags center;" % (timeout, parts)


def overpass(query, url=OVERPASS_URL, attempts=3, opener=None, pause=10.0):
    """POST a query. Raises the last failure; the caller splits the box."""
    opener = opener or urllib.request.urlopen
    last = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url, data=urllib.parse.urlencode({"data": query}).encode(),
            headers={"User-Agent": USER_AGENT})
        try:
            with opener(request, timeout=600) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:      # noqa: BLE001 - retried or re-raised
            last = error
            if attempt + 1 < attempts:
                time.sleep(pause * (attempt + 1))
    raise last


def split(bbox):
    west, south, east, north = bbox
    mid_lon = (west + east) / 2.0
    mid_lat = (south + north) / 2.0
    return [(west, south, mid_lon, mid_lat), (mid_lon, south, east, mid_lat),
            (west, mid_lat, mid_lon, north), (mid_lon, mid_lat, east, north)]


def fetch_category(name, selectors, bbox, depth=0, max_depth=3, log=print,
                   timeout=300, opener=None, pause=10.0):
    """Elements for one category over one box, splitting the box when Overpass
    refuses it. A 504 under load is the normal failure here - it is what
    stopped three counties in step 0.1 - so it is handled, not reported."""
    try:
        data = overpass(query_for(selectors, bbox, timeout), opener=opener,
                        pause=pause)
        elements = data.get("elements", [])
        log("    %-10s %s -> %d elements" % (name, _box(bbox), len(elements)))
        return elements
    except Exception as error:          # noqa: BLE001
        if depth >= max_depth:
            raise SystemExit(
                "overpass refused %s %s after %d splits: %s"
                % (name, _box(bbox), depth, error))
        log("    %-10s %s -> %s; splitting" % (name, _box(bbox), error))
        out = []
        for piece in split(bbox):
            out.extend(fetch_category(name, selectors, piece, depth + 1,
                                      max_depth, log, timeout, opener, pause))
        return out


def _box(bbox):
    return "%.2f,%.2f,%.2f,%.2f" % tuple(bbox)


def cache_path(cache, region, category):
    return os.path.join(cache, region, "%s.json" % category)


def do_fetch(args, log=print, opener=None, pause=10.0):
    bbox = region_bbox(args)
    os.makedirs(os.path.join(args.cache, args.region), exist_ok=True)
    fetched_at = datetime.now(timezone.utc).date().isoformat()
    for name, selectors in CATEGORIES:
        path = cache_path(args.cache, args.region, name)
        if os.path.exists(path) and not args.refresh:
            log("    %-10s cached" % name)
            continue
        elements = fetch_category(name, selectors, bbox, log=log,
                                  opener=opener, pause=pause)
        # Deduped here and not at build time: the box splitter overlaps on its
        # seams, so the same car park comes back from two sub-boxes.
        seen = {}
        for element in elements:
            uid = element_uid(element)
            if uid is not None:
                seen[uid] = element
        blob = {"region": args.region, "bbox": list(bbox), "category": name,
                "fetched_at": fetched_at,
                "elements": [seen[uid] for uid in sorted(seen)]}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(blob, handle)
        log("    %-10s %d elements -> %s" % (name, len(seen), path))
    return 0


# -------------------------------------------------------------------- build

def load_cached(cache, region, bbox, log=print):
    """Every POI the cache holds for a region, deduped, sorted, clipped.

    Clipped because a split box is fetched by its own bounds and Overpass
    returns a way whose centre can fall outside the box it was asked for; a POI
    in the next region is that region's to publish.
    """
    pois, source_dates = {}, set()
    for name, _ in CATEGORIES:
        path = cache_path(cache, region, name)
        if not os.path.exists(path):
            raise SystemExit("no cache for %s/%s - run `fetch` first"
                             % (region, name))
        with open(path, encoding="utf-8") as handle:
            blob = json.load(handle)
        source_dates.add(blob["fetched_at"])
        kept = 0
        for element in blob["elements"]:
            poi = poi_of(element, blob["fetched_at"])
            if poi is None or not in_bbox(poi, bbox):
                continue
            # An element can come back under two categories when it carries two
            # of the tags; `categorise` already picked one, and the first write
            # wins because the uid is the key.
            if poi["poi_uid"] not in pois:
                pois[poi["poi_uid"]] = poi
                kept += 1
        log("    %-10s %6d kept of %6d fetched"
            % (name, kept, len(blob["elements"])))
    return [pois[uid] for uid in sorted(pois)], sorted(source_dates)


def counts_by_category(pois):
    counts = collections.OrderedDict((name, 0) for name in CATEGORY_NAMES)
    for poi in pois:
        counts[poi["category"]] += 1
    return counts


def write_pois(db_path, pois, previous=None, checked=None):
    """Add the two POI tables to an existing container.

    The rowid is written explicitly and is the rtree id, so a bbox hit and a
    record are the same thing - the arrangement `lanes`/`lanes_bbox` already
    uses. Two builds from one cache give one file.

    THE NUMBERS COME FROM WHAT RIDERS HOLD. `previous` is the path of the
    region's PUBLISHED container. Every uid it carries keeps the rowid it was
    published under, a new uid gets the next number above the highest the
    published file used, and a uid that went away leaves a gap - see
    stable_ids.py. Numbered 1..N in uid order instead, one new POI whose uid
    sorted early renumbered every POI after it, and the changeset for that one
    POI was 5.87 MB of a 9.47 MB container. With no published container it is
    1..N over the sorted uids, exactly as before.

    AND SO DO THE DATES. A POI whose category, name, position and hours all
    equal the published row's keeps the published `source_date`, so the
    column says "unchanged since" and not "read on". Re-dated to the fetch
    instead, a monthly refresh of unchanged OSM rewrote every row in every
    region: 38.0 MB of changesets for 94 MB of containers. See
    stable_ids.keep_dates.

    WHEN WE LAST LOOKED is then the region's, written once as
    `meta.pois_checked` - `checked`, or when not given the OLDEST read date
    among the rows handed in (poi_staleness.py's rule: a region is as old as
    its stalest category). It moves one meta key a month. Measured on copies
    of the six published regions, a refresh of unchanged OSM is now a
    changeset of 15.9-16.4 kB raw / ~2.5 kB gzipped per region - the empty
    changeset's own pages - where it was 3.1-9.6 MB. No POIs and no `checked`
    writes no key:
    there is no read date to state. A file with no `meta` table is not a
    container and is not given one.
    """
    if checked is None:
        dates = [poi["source_date"] for poi in pois if poi.get("source_date")]
        checked = min(dates) if dates else None
    published = stable_ids.previous_rows(previous, "pois", "poi_uid")
    pois = stable_ids.keep_dates(pois, published, "poi_uid")
    numbers = stable_ids.number(
        [poi["poi_uid"] for poi in pois],
        stable_ids.previous_numbers(previous, "pois", "poi_uid"))
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        has_meta = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone() is not None
        if checked and has_meta:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES"
                       " ('pois_checked', ?)", (checked,))
        # In rowid order, so the b-tree is filled the way a 1..N build fills
        # it and the file does not grow for being numbered differently.
        for rowid, poi in sorted(((numbers[p["poi_uid"]], p) for p in pois),
                                 key=lambda pair: pair[0]):
            db.execute(
                "INSERT INTO pois (rowid, poi_uid, category, name, lat, lon,"
                " opening_hours, source_date) VALUES (?,?,?,?,?,?,?,?)",
                (rowid, poi["poi_uid"], poi["category"], poi["name"],
                 poi["lat"], poi["lon"], poi["opening_hours"],
                 poi["source_date"]))
            db.execute("INSERT INTO pois_bbox VALUES (?,?,?,?,?)",
                       (rowid, poi["lon"], poi["lon"], poi["lat"], poi["lat"]))
        db.commit()
        db.execute("VACUUM")
    finally:
        db.close()
    return len(pois)


class _Counter(object):
    """Counts compressed bytes without holding them."""

    def __init__(self):
        self.total = 0

    def write(self, data):
        self.total += len(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


def gzip_size(path):
    """What the container costs to DOWNLOAD. Containers ship compressed (spec
    18.2) and the 1.5 GB budget is a download budget, so the plain delta alone
    would overstate what POIs cost a rider."""
    sink = _Counter()
    compressor = gzip.GzipFile(fileobj=sink, mode="wb", mtime=0,
                               compresslevel=9)
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            compressor.write(chunk)
    compressor.close()
    return sink.total


def wanted_categories(only):
    """The `--only` set, checked against the nine.

    A typo here would measure nothing and report the category as free, which is
    exactly the wrong answer to give a density decision, so an unknown name is
    refused rather than filtered out.
    """
    if not only:
        return None
    wanted = set(part.strip() for part in only.split(",") if part.strip())
    unknown = wanted - set(CATEGORY_NAMES)
    if unknown:
        raise SystemExit("unknown category: %s; one of %s"
                         % (", ".join(sorted(unknown)),
                            ", ".join(CATEGORY_NAMES)))
    return wanted


def do_build(args, log=print):
    bbox = region_bbox(args)
    # One category at a time, so the density decision spec 6.3 asks for is
    # taken on what each category actually costs rather than on a guess.
    # Measured this way and not by dividing the total: sqlite pages, the index
    # and the rtree do not share out evenly.
    wanted = wanted_categories(getattr(args, "only", None))
    pois, source_dates = load_cached(args.cache, args.region, bbox, log=log)
    if wanted is not None:
        pois = [p for p in pois if p["category"] in wanted]
    counts = counts_by_category(pois)

    work = args.container
    if not args.in_place:
        handle, work = tempfile.mkstemp(suffix=".tbmap")
        os.close(handle)
        shutil.copyfile(args.container, work)
        # A fair baseline: the published container is measured after the same
        # VACUUM the POI write ends with, so the delta is POIs and not whatever
        # free pages the original was already carrying.
        baseline = sqlite3.connect(work)
        baseline.execute("VACUUM")
        baseline.close()
    try:
        before = os.path.getsize(work)
        before_gz = gzip_size(work)
        write_pois(work, pois, previous=getattr(args, "previous", None),
                   checked=source_dates[0] if source_dates else None)
        after = os.path.getsize(work)
        after_gz = gzip_size(work)
    finally:
        if not args.in_place:
            if args.keep:
                shutil.move(work, args.keep)
            else:
                os.remove(work)

    log("")
    log("POIs in %s (read %s)" % (args.region, ", ".join(source_dates)))
    for name, count in counts.items():
        log("    %-10s %7d" % (name, count))
    log("    %-10s %7d" % ("TOTAL", len(pois)))
    log("")
    log("container %s" % os.path.basename(args.container))
    log("    plain   %10d -> %10d  (+%d bytes, +%.1f%%)"
        % (before, after, after - before, 100.0 * (after - before) / before))
    log("    gzip -9 %10d -> %10d  (+%d bytes, +%.1f%%)"
        % (before_gz, after_gz, after_gz - before_gz,
           100.0 * (after_gz - before_gz) / before_gz))
    if pois:
        log("    %.1f plain bytes per POI, %.1f compressed"
            % ((after - before) / len(pois),
               (after_gz - before_gz) / len(pois)))

    if args.report:
        with open(args.report, "w", encoding="utf-8") as out:
            json.dump({"region": args.region, "bbox": list(bbox),
                       "source_dates": source_dates,
                       "counts": counts, "total": len(pois),
                       "container": os.path.basename(args.container),
                       "plain_before": before, "plain_after": after,
                       "gzip_before": before_gz, "gzip_after": after_gz},
                      out, indent=2)
        log("    report -> %s" % args.report)
    return 0


# ---------------------------------------------------------------------- cli

def region_bbox(args):
    if getattr(args, "bbox", None):
        parts = [float(p) for p in args.bbox.split(",")]
        if len(parts) != 4:
            raise SystemExit("--bbox wants W,S,E,N")
        return tuple(parts)
    if args.region not in REGIONS:
        raise SystemExit("unknown region %s; one of %s"
                         % (args.region, ", ".join(sorted(REGIONS))))
    return REGIONS[args.region]


def parse_args(argv):
    parser = argparse.ArgumentParser(description="POIs for a region container")
    sub = parser.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="read OSM into a cache")
    f.add_argument("--region", required=True)
    f.add_argument("--bbox", help="override the region box, W,S,E,N")
    f.add_argument("--cache", default="cache/pois")
    f.add_argument("--refresh", action="store_true",
                   help="re-read categories already cached")

    b = sub.add_parser("build", help="add cached POIs to a region container")
    b.add_argument("--region", required=True)
    b.add_argument("--bbox", help="override the region box, W,S,E,N")
    b.add_argument("--cache", default="cache/pois")
    b.add_argument("--container", required=True)
    b.add_argument("--in-place", action="store_true",
                   help="write the named container instead of a copy")
    b.add_argument("--keep", help="keep the copy at this path")
    b.add_argument("--only", help="measure only these categories, comma "
                                  "separated - for the density budget")
    b.add_argument("--report", help="write the measurement as JSON")
    b.add_argument("--previous",
                   help="the region's PUBLISHED container: every POI it "
                        "carries keeps its rowid (see stable_ids.py)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "fetch":
        return do_fetch(args)
    return do_build(args)


if __name__ == "__main__":
    sys.exit(main())

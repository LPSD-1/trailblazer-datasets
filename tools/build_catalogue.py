#!/usr/bin/env python3
"""Build the worldwide download catalogue the app reads.

    python build_catalogue.py --lanes dist/manifest.json --out dist/catalogue.json

Produces schema 2: continent -> country -> area -> packs. Every pack says what
kind it is (basemap, lanes, routing, gpx), how big it is, and - for lane data -
whether its legal standing is official or community-mapped.

Routing data was the 5x5-degree tile scheme, WORLDWIDE, on the argument that
the grid covers everything for free. Step 1.6 of the pivot plan settled that:
free to LIST is not free to host, to keep fresh or to stand behind, and the
listing was 526 distinct files and 7.9 GB for a product that publishes lanes
in England and Wales and nowhere else. Routing is now the six tiles that cover
Great Britain, 293 MB - see GB_ROUTING_TILES.

NO PACK CARRIES A `vehicle` FIELD (step 1.7). One dataset, classed per way,
replaces the foot/bicycle/horse/motor partition that built the same bridleway
four times over; see docs/WAYS-SCHEMA.md, which is the contract. A `vehicle`
key here is what that partition looked like from the app's side, so its
absence is the thing worth checking, and `--check-no-vehicle` checks it.

Packs are addressed as absolute URLs on a GitHub release rather than paths
beside this file, because GitHub Pages caps a site at 1 GB.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import urllib.parse
import urllib.request

# Countries we can say something useful about, with the boxes used to decide
# "which country is this rider in".
#
# Boxes, not borders. They overlap - France's contains Monaco entirely - and
# the app resolves that by preferring the smallest box that contains the
# point. This is choosing a download suggestion, not adjudicating a frontier.
COUNTRIES = [
    # (continent, code, label, west, south, east, north)
    ("europe", "gb", "United Kingdom", -8.7, 49.8, 1.8, 60.9),
    ("europe", "ie", "Ireland", -10.6, 51.4, -5.9, 55.4),
    ("europe", "fr", "France", -5.2, 41.3, 9.6, 51.1),
    ("europe", "es", "Spain", -9.4, 35.9, 3.4, 43.8),
    ("europe", "pt", "Portugal", -9.6, 36.9, -6.1, 42.2),
    ("europe", "de", "Germany", 5.8, 47.2, 15.1, 55.1),
    ("europe", "it", "Italy", 6.6, 36.6, 18.6, 47.1),
    ("europe", "ch", "Switzerland", 5.9, 45.8, 10.5, 47.9),
    ("europe", "at", "Austria", 9.5, 46.3, 17.2, 49.1),
    ("europe", "be", "Belgium", 2.5, 49.4, 6.4, 51.5),
    ("europe", "nl", "Netherlands", 3.3, 50.7, 7.2, 53.6),
    ("europe", "no", "Norway", 4.6, 57.9, 31.1, 71.2),
    ("europe", "se", "Sweden", 11.0, 55.3, 24.2, 69.1),
    ("europe", "fi", "Finland", 20.6, 59.8, 31.6, 70.1),
    ("europe", "pl", "Poland", 14.1, 49.0, 24.2, 54.9),
    ("europe", "cz", "Czechia", 12.1, 48.5, 18.9, 51.1),
    ("europe", "ro", "Romania", 20.3, 43.6, 29.7, 48.3),
    ("europe", "gr", "Greece", 19.4, 34.8, 28.3, 41.8),
    ("europe", "hr", "Croatia", 13.5, 42.4, 19.4, 46.6),
    ("europe", "is", "Iceland", -24.6, 63.3, -13.5, 66.6),

    ("north-america", "us", "United States", -125.0, 24.5, -66.9, 49.4),
    ("north-america", "ca", "Canada", -141.0, 41.7, -52.6, 70.0),
    ("north-america", "mx", "Mexico", -117.2, 14.5, -86.7, 32.7),

    ("south-america", "br", "Brazil", -74.0, -33.8, -34.8, 5.3),
    ("south-america", "ar", "Argentina", -73.6, -55.1, -53.6, -21.8),
    ("south-america", "cl", "Chile", -75.7, -55.9, -66.4, -17.5),
    ("south-america", "pe", "Peru", -81.3, -18.4, -68.7, -0.0),

    ("africa", "za", "South Africa", 16.4, -34.9, 32.9, -22.1),
    ("africa", "ma", "Morocco", -13.2, 27.7, -1.0, 35.9),
    ("africa", "na", "Namibia", 11.7, -28.9, 25.3, -16.9),
    ("africa", "ke", "Kenya", 33.9, -4.7, 41.9, 5.5),

    ("asia", "jp", "Japan", 129.4, 31.0, 145.8, 45.5),
    ("asia", "th", "Thailand", 97.3, 5.6, 105.6, 20.5),
    ("asia", "in", "India", 68.1, 6.7, 97.4, 35.5),
    ("asia", "my", "Malaysia", 99.6, 0.8, 119.3, 7.4),
    ("asia", "id", "Indonesia", 95.0, -11.0, 141.0, 6.1),
    ("asia", "vn", "Vietnam", 102.1, 8.2, 109.5, 23.4),
    ("asia", "ph", "Philippines", 116.9, 4.6, 126.6, 21.1),
    ("asia", "ae", "United Arab Emirates", 51.5, 22.6, 56.4, 26.1),

    ("oceania", "au", "Australia", 112.9, -43.7, 153.6, -10.1),
    ("oceania", "nz", "New Zealand", 166.5, -47.3, 178.6, -34.4),
]

CONTINENT_LABELS = {
    "europe": "Europe",
    "north-america": "North America",
    "south-america": "South America",
    "africa": "Africa",
    "asia": "Asia",
    "oceania": "Oceania",
}

ROUTING_INDEX = "https://brouter.de/brouter/segments4/"

# THE SIX TILES GREAT BRITAIN NEEDS. Step 1.6.
#
# The same tuple as tools/mirror_routing.py, and deliberately written out in
# both rather than imported: one is a fetcher and one is a publisher, they run
# in different jobs, and a set derived from the UK bounding box below is not
# the set anybody means. That box is (-8.7, 49.8) to (1.8, 60.9) and touches
# TWELVE tiles - it grazes the N45 row (France and northern Spain) by two
# tenths of a degree, and the N60 row (Shetland, where we publish no lanes).
# Those six extra tiles were 193,163,753 bytes - 193.2 MB of the 486,484,824
# the catalogue offered for "the United Kingdom", covering ground no rider
# here can route over.
GB_ROUTING_TILES = frozenset((
    "W10_N50", "W5_N50", "E0_N50",
    "W10_N55", "W5_N55", "E0_N55",
))

# The budget in §2 of the pivot plan, in DECIMAL megabytes, because that is
# the arithmetic the plan does ("~1,708 MB ... fails by 208 MB").
DEFAULT_UK_BUDGET_BYTES = 1_500_000_000

ATTRIBUTION = (
    "Map and routing data (c) OpenStreetMap contributors, ODbL. "
    "United Kingdom rights of way contain public sector information licensed "
    "under the Open Government Licence v3.0, via rowmaps.com."
)


def tile_name(lon, lat):
    """The 5x5-degree tile whose lower-left corner contains this point."""
    tl = int(math.floor(lon / 5.0) * 5)
    tb = int(math.floor(lat / 5.0) * 5)
    return "%s%d_%s%d" % (
        "E" if tl >= 0 else "W", abs(tl),
        "N" if tb >= 0 else "S", abs(tb),
    )


def tiles_covering(west, south, east, north):
    """Every routing tile a country's box touches."""
    names = []
    lat = math.floor(south / 5.0) * 5
    while lat <= north:
        lon = math.floor(west / 5.0) * 5
        while lon <= east:
            names.append((tile_name(lon + 0.1, lat + 0.1), lon, lat))
            lon += 5
        lat += 5
    return names


def routing_index():
    """Which routing tiles exist, and how big they are."""
    print("reading the routing tile index...")
    try:
        with urllib.request.urlopen(ROUTING_INDEX, timeout=60) as fh:
            html = fh.read().decode("utf8", "replace")
    except Exception as e:
        sys.exit("could not read %s: %s" % (ROUTING_INDEX, e))

    # A plain <pre> directory listing, not a table:
    #   <a href="E0_N10.rd5">E0_N10.rd5</a>   10-Sep-2026 01:03   12092058
    import re
    sizes = {}
    for name, size in re.findall(
            r'<a href="([EW]\d+_[NS]\d+)\.rd5">[^<]*</a>\s+\S+\s+\S+\s+(\d+)',
            html):
        sizes[name] = int(size)
    if not sizes:
        # The listing format changed. Better to stop than to publish a
        # catalogue whose routing packs are all zero bytes.
        sys.exit("could not parse any tile sizes from the routing index")
    print("  %d tiles published" % len(sizes))
    return sizes


def mirrored_routing(path):
    """Routing tiles we host ourselves, by tile name.

    Riders pulling 139 MB each from a volunteer-run third party is a
    dependency that fails by being blocked rather than by billing us, and it
    takes offline routing down for everyone at once when it does. Anything in
    here is served from our own release instead.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("tiles", {})
    except (ValueError, OSError) as e:
        print("warning: could not read %s (%s); routing falls back upstream"
              % (path, e), file=sys.stderr)
        return {}


def routing_packs(country, tile_sizes, mirror=None, mirror_base=""):
    """Routing packs for one country - Great Britain, and no other.

    GB ONLY, AND SIX TILES OF IT. Step 1.6.

    This used to emit a pack for every tile every country's box touched,
    forty countries deep, which is where the catalogue's 526 distinct routing
    files and 7.9 GB came from. Not one of those riders exists: the app
    publishes lanes in England and Wales. A country with nothing else in it
    therefore ends up with no areas at all, and `build` drops it - which is
    the intended result, not an accident of the filter.
    """
    _, code, label, w, s, e, n = country
    if code != "gb":
        return []
    mirror = mirror or {}
    packs = []
    for name, lon, lat in tiles_covering(w, s, e, n):
        if name not in tile_sizes:
            continue  # ocean, or nothing mapped there
        if name not in GB_ROUTING_TILES:
            # The bounding box grazes two rows of tiles that hold no ground a
            # British rider routes over. Listing them cost 193.2 MB and bought
            # a download nobody wanted.
            continue
        packs.append({
            # The tile name IS the id, exactly as published, because the app
            # saves a pack as "<id>.rd5" and the routing engine opens tiles by
            # computing that name from a coordinate. Anything prettier here
            # and a downloaded tile is a file the engine never looks for.
            "id": name,
            "kind": "routing",
            "label": "Roads %s" % _tile_label(lon, lat),
            # The tile's own 5-degree box. Without it the app cannot tell
            # which routing tile covers a rider, and one-tap setup hands
            # someone lanes with no way to route across them.
            "bounds": {
                "west": lon, "south": lat,
                "east": lon + 5, "north": lat + 5,
            },
            # Ours where we have it. A tile we host does not move under us, so
            # it carries a REAL hash - and the app can then check a routing
            # download exactly, the way it checks a lane pack, instead of
            # against a size that was a snapshot of a nightly rebuild.
            **({
                "file": mirror_base + name + ".rd5",
                "sha256": mirror[name]["sha256"],
                "bytes": mirror[name]["bytes"],
            } if name in mirror else {
                "file": ROUTING_INDEX + name + ".rd5",
                # Upstream publishes no checksums and rebuilds nightly, so
                # the length is all there is and it is a moving target. Every
                # tile not yet mirrored is still in this position.
                "sha256": "",
                "bytes": tile_sizes[name],
            }),
        })
    return packs


def _tile_label(lon, lat):
    return "%d%s %d%s" % (
        abs(lat), "N" if lat >= 0 else "S",
        abs(lon), "E" if lon >= 0 else "W",
    )


def load_containers(path):
    """{(dataset, area): entry} from the container build, or {} if none.

    `dataset` is whatever the container manifest calls the thing - today's
    manifest still says `vehicle` and names four of them, and after step 1.2
    there is one dataset and no such key, at which point `dataset` is None.
    Both shapes are read here so that this file is not the thing that has to
    land in the same commit as the builder.

    A MISSING FILE IS NOT A QUIET FALLBACK. The catalogue that comes out will
    have lane entries with no file, `verify_catalogue.py` will refuse it, and
    the build stops - which is the right end for a run whose container step did
    not happen, because the app can no longer read the packs.
    """
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf8") as fh:
        manifest = json.load(fh)
    out = {}
    for entry in manifest.get("containers", []):
        dataset = entry.get("vehicle")
        if entry.get("kind") == "area":
            out[(dataset, entry["area"])] = entry
        elif entry.get("kind") == "overview":
            out[(dataset, None)] = entry
    return out


def container_for(containers, dataset, area_id):
    """The container covering this ground, partitioned or not.

    Exact match first, because while the vehicle partition still exists there
    are four containers over the same ground and picking one at random would
    publish a walker's data under a rider's id. Once 1.2 has collapsed them
    there is exactly one, and no `dataset` to match it by.
    """
    entry = containers.get((dataset, area_id))
    if entry is not None:
        return entry
    same_ground = [v for (_, a), v in containers.items() if a == area_id]
    return same_ground[0] if len(same_ground) == 1 else None


def lane_areas(lanes_manifest, containers=None):
    """Turn the Great Britain lane manifest into catalogue areas.

    [containers] maps (vehicle, area) to the entry `build_containers.py` wrote
    for it. That is what a rider downloads; the pack is only the thing it was
    built from.
    """
    containers = containers or {}
    if not lanes_manifest or not os.path.isfile(lanes_manifest):
        return {}
    with open(lanes_manifest, encoding="utf8") as fh:
        manifest = json.load(fh)

    region_bounds = {
        r["id"]: r["bounds"] for r in manifest.get("regions", [])
    }

    # Grouped by REGION, not by the lane splitter's authority chunks.
    #
    # The splitter cuts dense regions into pieces small enough to open, which
    # produces names like "Barking and Dagenham and 10 more". Those are fine
    # as pack labels - they say exactly what is inside - but as 82 top-level
    # browse entries they are unusable. Six regions, each holding its pieces,
    # is what a rider can actually navigate.
    areas = {}
    for pkg in manifest.get("packages", []):
        region_id = pkg["region"]
        area = areas.setdefault(region_id, {
            "id": "gb-%s" % region_id,
            "label": pkg.get("regionLabel") or region_id,
            "bounds": region_bounds.get(region_id, {
                "west": -8.7, "south": 49.8, "east": 1.8, "north": 60.9,
            }),
            "packs": [],
        })
        area_id = pkg.get("area") or region_id
        # THE CONTAINER IS WHAT SHIPS, where one has been built.
        #
        # `build_containers.py` turns each pack into a `.tbmap` - tiles for the
        # map, records for every legal answer - and the app reads only that.
        # The pack stays in this entry as `legacyFile` so a build can be
        # compared against the one before it, but nothing downloads it.
        #
        # An entry with no container is a lane pack the container build did not
        # produce, which is a broken build rather than a mixed one: it is left
        # POINTING AT NOTHING rather than silently falling back to a format the
        # app can no longer read.
        dataset = pkg.get("package")
        container = container_for(containers, dataset, area_id)
        area["packs"].append({
            # NO VEHICLE IN THE ID EITHER, once there is no vehicle. While the
            # partition still exists the id has to keep saying which of the
            # four this is, or four packs collapse onto one id and three
            # downloads vanish.
            "id": "gb-%s-%s" % (area_id, dataset or "ways"),
            "kind": "lanes",
            "format": "tbmap",
            "label": pkg["label"],
            "file": container["file"] if container else None,
            "sha256": container["sha256"] if container else None,
            "bytes": container["bytes"] if container else 0,
            "downloadBytes": container["downloadBytes"] if container else 0,
            "signature": (container or {}).get("signature"),
            "legacyFile": pkg["file"],
            "plainBytes": pkg.get("plainBytes", 0),
            # NO `vehicle` KEY. Step 1.7.
            #
            # It was the app-facing half of the partition that built the same
            # bridleway into a bicycle pack and a horse pack byte-identically,
            # produced 109 containers and froze a phone. What a way permits is
            # a property of the WAY now - `motorbike_ok`, `fourxfour_ok` and
            # `access_reason` in docs/WAYS-SCHEMA.md - and it is carried
            # inside the container, per way, with its evidence. A pack cannot
            # answer that question and must not look as though it can.
            "featureCount": pkg.get("laneCount", 0),
            # England and Wales publish a legal register of rights of way.
            # Nowhere else in this catalogue can claim that yet.
            "legalBasis": "official",
            "note": pkg.get("note"),
            "generated": pkg.get("generated"),
        })
    return areas


def satellite_packs(path):
    """Imagery packs already published, keyed by the area they cover.

    Read from a record the imagery job keeps, rather than carried in this
    file, because the two jobs run on completely different clocks: lanes are
    rebuilt monthly from council data, imagery an area at a time over the
    weeks between. Without this the monthly lane refresh would rebuild the
    catalogue from scratch and every satellite pack would vanish from every
    rider's Downloads screen, with no code having changed.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            index = json.load(f)
    except (ValueError, OSError) as e:
        # A broken record must not take the whole catalogue down with it: the
        # lanes are what the app cannot work without.
        print("warning: could not read %s (%s); no imagery in this build"
              % (path, e), file=sys.stderr)
        return {}

    by_area = {}
    for pack in index.get("packs", []):
        area = pack.get("area")
        if not area:
            continue
        by_area.setdefault(area, []).append({
            k: v for k, v in pack.items() if k != "area"
        })
    return by_area


def _sha256_of(path):
    """The hash the app checks a download against."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def trips_pack(path, base_url):
    """The published trips pack, if the trips job has written one.

    One pack per country and a tiny one - a name, a description and the points
    each route passes through, never a line. It rides along with the lane data
    for the first area of that country so a rider who downloads the ground
    also gets the days out over it, without a separate thing to find.
    """
    if not path or not os.path.exists(path):
        # Loudly. The satellite index prints a warning when it cannot be read
        # and this said nothing at all - which is why a whole workflow quietly
        # publishing a catalogue with no trips in it left no trace anywhere.
        print("warning: %s is missing; NO ready-made trips in this build"
              % path, file=sys.stderr)
        return None
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (ValueError, OSError) as e:
        print("warning: could not read %s (%s); no trips in this build"
              % (path, e), file=sys.stderr)
        return None

    count = len(doc.get("trips", []))
    if not count:
        return None

    name = os.path.basename(path)
    country = doc.get("country", "gb")
    print("  %d ready-made trips (%d bytes)" % (count, size))
    return {
        "id": "%s-trips" % country,
        "kind": "trips",
        "label": "Ready-made trips",
        "file": urllib.parse.urljoin(base_url, "trips/%s" % name),
        "sha256": _sha256_of(path),
        "bytes": size,
    }


def tro_pack(path):
    """The national traffic-orders pack, from the record the TRO job keeps.

    Read from a committed index rather than from the sealed file, for the same
    reason imagery is: the two jobs run on completely different clocks. The TRO
    job runs several times a day and the lane refresh monthly, and a monthly
    rebuild that could not see this would delete traffic orders from every
    rider's Downloads screen with no code having changed.

    The sealed pack itself is a release asset - three megabytes several times a
    day would be a gigabyte a year of git history - so what is committed is its
    size, its hash and where it lives.
    """
    if not path or not os.path.exists(path):
        print("warning: %s is missing; NO traffic orders in this build"
              % path, file=sys.stderr)
        return None
    try:
        with open(path, encoding="utf-8") as f:
            index = json.load(f)
    except (ValueError, OSError) as e:
        print("warning: could not read %s (%s); no traffic orders"
              % (path, e), file=sys.stderr)
        return None

    packs = index.get("packs") or []
    if not packs:
        return None
    pack = packs[0]
    print("  traffic orders pack (%s bytes, cut %s)"
          % (pack.get("bytes"), pack.get("generated")))
    return pack


#: The two live feeds, as (directory under the conditions root, kind). The
#: directory names are `build_wet.py feed --out published/wet` and
#: `build_fords.py feed --out published/rivers`, which is where the LIVE half
#: of refresh-data.yml writes them.
CONDITION_FEEDS = (("wet", "wet"), ("rivers", "rivers"))

#: How often the app should re-read a feed. The same clock the LIVE half runs
#: on, said here so it can be changed without shipping an app - exactly as
#: `updates` does for every pack kind.
CONDITIONS_UPDATES = "sixHourly"


def _served_prefix(conditions_dir):
    """The path a feed directory is served at, under baseUrl.

    Derived from where the files ACTUALLY are rather than from a constant, so
    pointing this at another directory cannot produce a catalogue whose feeds
    404: `published/wet/north.json` on disk is `<baseUrl>published/wet/
    north.json` served, because `published/` is committed at the Pages root
    exactly as `names/` and `trips/` are.

    AN ABSOLUTE PATH FALLS BACK TO THE BASENAME, and that is not a shortcut.
    golden.py builds into a temporary directory, so deriving the URL from the
    full path would put `C:/Users/.../golden-ab12cd/published` into the
    catalogue - machine-dependent, which breaks the one claim golden exists to
    make, and a 404 for every rider besides. The same is true of any caller
    that passes an absolute path. The last component is the served one.
    """
    rel = conditions_dir
    try:
        rel = os.path.relpath(conditions_dir, os.getcwd())
    except ValueError:
        # Windows: a different drive from the working directory has no
        # relative path at all, and raises rather than returning something.
        rel = conditions_dir
    rel = rel.replace(os.sep, "/").strip("/")
    if os.path.isabs(conditions_dir) or rel.startswith("..") or rel in (".", ""):
        rel = os.path.basename(os.path.normpath(conditions_dir))
    return rel


def conditions_block(conditions_dir, base_url):
    """Where the live wet and river feeds are, so the app can find them.

    A TOP-LEVEL BLOCK, NOT PACKS. This is the one design decision in here and
    it is not stylistic. `Pack.fromJson` used to refuse the WHOLE index when it
    met a `kind` it did not know - one unknown pack cost every download on
    every install, and it took `real_catalogue_test.dart` parsing the live file
    with the app's own reader to catch it. An unknown kind is skipped now
    rather than refused, but these are not packs in any case: a pack is
    something a rider downloads once and keeps, and these are a few kilobytes
    that go stale in six hours. Putting them beside the containers would also
    have put them in the default-download budget, which is a number about how
    much of the country fits on a phone.

    ALWAYS EMITTED, EVEN EMPTY. `conditions` with no feeds says "this build
    published none"; a MISSING key says "this catalogue was built by something
    that had never heard of them". The app can act on the first and only guess
    at the second, and a whole pipeline sitting unrun with nothing anywhere
    able to tell those two apart is exactly how this got here.

    Sizes and hashes, for the same reason every pack carries them: the app
    checks a download before it trusts it. They are only true while the feed
    files and this catalogue are written in ONE commit, which is what the LIVE
    half of refresh-data.yml does - see the comment on its publish step.
    """
    block = {
        "schema": 1,
        "updates": CONDITIONS_UPDATES,
        "licence": "Environment Agency flood-monitoring data, OGL v3",
        "feeds": [],
    }
    if not conditions_dir or not os.path.isdir(conditions_dir):
        # Loudly, for the reason trips_pack gives. A workflow that quietly
        # published a catalogue with no conditions in it is the whole failure
        # this pipeline is being wired in to end.
        print("warning: %s is missing; NO wet or river feeds in this build"
              % conditions_dir, file=sys.stderr)
        return block

    prefix = _served_prefix(conditions_dir)

    for sub, kind in CONDITION_FEEDS:
        here = os.path.join(conditions_dir, sub)
        if not os.path.isdir(here):
            print("warning: no %s feeds in %s" % (kind, conditions_dir),
                  file=sys.stderr)
            continue
        for name in sorted(os.listdir(here)):
            if not name.endswith(".json"):
                continue
            region = name[: -len(".json")]
            path = os.path.join(here, name)
            rel = "/".join(p for p in (prefix, sub, name) if p)
            entry = {
                "id": "gb-%s-%s" % (region, kind),
                "kind": kind,
                "region": region,
                "file": urllib.parse.urljoin(base_url, rel),
                "sha256": _sha256_of(path),
                "bytes": os.path.getsize(path),
            }
            # `as_of` is the feed's own reading of when the Environment Agency
            # last said anything, not when we ran. It is what lets the app show
            # "measured at 04:15" and go quiet when a feed has stopped moving,
            # which is the difference between advice and a stale reassurance.
            try:
                with open(path, encoding="utf-8") as fh:
                    entry["asOf"] = json.load(fh).get("as_of")
            except (ValueError, OSError) as e:
                print("warning: could not read %s (%s); listing it without a "
                      "timestamp" % (path, e), file=sys.stderr)
                entry["asOf"] = None
            block["feeds"].append(entry)

    if block["feeds"]:
        total = sum(f["bytes"] for f in block["feeds"])
        print("  %d live condition feeds (%.1f kB)"
              % (len(block["feeds"]), total / 1e3))
    return block


def names_packs(names_dir, base_url):
    """The gazetteer packs, one per region, keyed by the region they cover.

    Built by build_names.py from OS Open Names - 1.7 million unit postcodes,
    882,000 named roads and every settlement in Great Britain, under the Open
    Government Licence. Without them, typing a postcode or a village needs a
    signal, which is the wrong way round for this app.

    Split by the SAME regions the lane packs use, so "download the Midlands"
    means one shape of ground rather than two.
    """
    if not names_dir or not os.path.isdir(names_dir):
        # Loudly, for the reason trips_pack gives: a workflow quietly
        # publishing a catalogue with no names in it leaves no trace, and the
        # only symptom is a rider typing their own village and being told to
        # find a signal.
        print("warning: %s is missing; NO offline place search in this build"
              % names_dir, file=sys.stderr)
        return {}

    out = {}
    for name in sorted(os.listdir(names_dir)):
        if not name.endswith(".tbnames"):
            continue
        region = name[: -len(".tbnames")]
        path = os.path.join(names_dir, name)
        size = os.path.getsize(path)
        out[region] = {
            "id": "gb-%s-names" % region,
            "kind": "names",
            "label": "Place search",
            "file": urllib.parse.urljoin(base_url, "names/%s" % name),
            "sha256": _sha256_of(path),
            "bytes": size,
        }
    if out:
        total = sum(p["bytes"] for p in out.values())
        print("  %d gazetteer packs (%.1f MB)" % (len(out), total / 1e6))
    return out


def build(lanes_manifest, base_url, stamp, satellite_index=None,
          routing_mirror_index=None, routing_mirror_base="",
          trips_index=None, names_dir=None, height_index=None,
          tro_path=None, containers=None, conditions_dir=None):
    tile_sizes = routing_index()
    gb_areas = lane_areas(lanes_manifest, containers)
    imagery = satellite_packs(satellite_index)
    # Read exactly the way imagery is, and for exactly the same reason: the
    # height job runs on its own clock, and a monthly lane refresh that
    # rebuilt the catalogue without this would delete every height pack from
    # every rider's Downloads screen with no code having changed. That has
    # happened three times in this repository to other kinds.
    heights = satellite_packs(height_index)
    trips = trips_pack(trips_index, base_url)
    names = names_packs(names_dir, base_url)
    tro = tro_pack(tro_path)
    conditions = conditions_block(conditions_dir, base_url)
    mirror = mirrored_routing(routing_mirror_index)
    if mirror:
        print("  %d routing tiles served from our own mirror" % len(mirror))

    by_continent = {}
    for country in COUNTRIES:
        continent, code, label, w, s, e, n = country
        routing = routing_packs(country, tile_sizes, mirror,
                                routing_mirror_base)

        areas = []
        if code == "gb" and gb_areas:
            # Britain has real lane data, split by area. Routing tiles are
            # country-wide, so they sit in their own area rather than being
            # duplicated into every one.
            for area in sorted(gb_areas.values(), key=lambda a: a["label"]):
                # Imagery goes in beside the lanes for the same ground, so a
                # rider choosing "the area I am in" gets both.
                for pack in imagery.get(area["id"], []):
                    area.setdefault("packs", []).append(pack)
                # Ground height, beside the imagery over the same ground.
                for pack in heights.get(area["id"], []):
                    area.setdefault("packs", []).append(pack)
                # The gazetteer for THIS ground, beside the lanes over it.
                #
                # Per area rather than duplicated into all of them the way
                # trips are: trips are seven kilobytes and these are three to
                # fifteen megabytes, so a rider downloading Wales should get
                # Welsh names and not the whole country's.
                region = area["id"][len("gb-"):]
                if region in names:
                    area.setdefault("packs", []).append(names[region])
                areas.append(area)

            # Into EVERY area, not one of them.
            #
            # Trips cover the whole country, so the tempting thing is to list
            # the pack once. That leaves a rider who downloads only Wales
            # without the Welsh trips, because the pack went to whichever area
            # sorted first - which is exactly the failure the geographic join
            # in PackSelection exists to prevent for routing.
            #
            # Duplicating costs nothing real: it is seven kilobytes, the app
            # dedupes downloads by local name, and a selection counts it once.
            if trips:
                for area in areas:
                    area.setdefault("packs", []).append(trips)

        # The country-wide area carries the things that are not regional.
        #
        # Routing tiles were always here. Traffic orders join them rather than
        # being duplicated into every region, which is how trips are done —
        # and this is better for both reasons that matter. An order does not
        # stop at a regional boundary, and this area's bounds cover the whole
        # country, so a rider anywhere in it picks the pack up whichever region
        # they are standing in. It also cannot be silently lost: duplicating
        # into regions means a build where the lane manifest is missing
        # publishes no orders at all, and says nothing about it.
        national = list(routing)
        if code == "gb" and tro:
            national.append(tro)

        if national:
            areas.append({
                "id": "%s-roads" % code,
                # Named for the country, not "United Kingdom roads": this
                # area is what the app falls back to when a rider is outside
                # every region, and "You're near United Kingdom roads" reads
                # like a place nobody has ever been.
                "label": label,
                "bounds": {"west": w, "south": s, "east": e, "north": n},
                "packs": national,
            })

        if not areas:
            continue

        by_continent.setdefault(continent, []).append({
            "code": code.upper(),
            "label": label,
            "bounds": {"west": w, "south": s, "east": e, "north": n},
            "areas": areas,
        })

    # WHERE THE LANES OF THE COUNTRY ARE, BEFORE ANY AREA IS DOWNLOADED.
    #
    # There WAS one per vehicle, for a measured reason: a single national
    # overview across every vehicle type put 462,684 points in one z6 tile and
    # took the app to 1.5 GB. The motor one is 10,114 lanes and 0.42 MB.
    #
    # That reason was about the WALKERS' 627 MB of footpaths, which the ways
    # schema does not carry at all (docs/WAYS-SCHEMA.md: "Footpaths are not
    # carried"). Whatever the container build hands over is listed here; this
    # file no longer partitions it, and after step 1.2 there is one.
    #
    # `minZoom` travels with it because the FLOOR MOVES: the builder picks the
    # lowest zoom whose tiles all fit under the ceiling. The app needs it to
    # say where the lanes come back rather than drawing an empty map and
    # leaving a rider to wonder.
    overviews = sorted(
        (
            {
                "id": entry["id"],
                "kind": "overview",
                "format": "tbmap",
                "label": entry["label"],
                "file": entry["file"],
                "sha256": entry["sha256"],
                "bytes": entry["bytes"],
                "downloadBytes": entry["downloadBytes"],
                "signature": entry.get("signature"),
                "minZoom": entry.get("minZoom"),
                "featureCount": entry["laneCount"],
                "generated": entry["generated"],
            }
            for (_dataset, area), entry in (containers or {}).items()
            if area is None
        ),
        # By id, not by vehicle: there is no vehicle to sort by any more, and
        # a stable order is what keeps two builds of unchanged data identical.
        key=lambda e: e["id"],
    )

    # AND EVERY BRITISH REGION GETS A COPY, which is what actually delivers it.
    #
    # The section above is the right place to DESCRIBE the overviews - it
    # carries minZoom, and it is where a reader looks for "what covers the
    # country". It is not a place anything downloads from: the app builds its
    # pack list from `area["packs"]`, so an overview that lives only up there
    # is published, signed, and fetched by nobody. A rider would download their
    # region, get z11-z14, and find an empty map every time they zoomed out -
    # the one thing the overviews were built for.
    #
    # Nor is the national `gb-roads` area a home for them. `packsForArea` in
    # the app pulls packs across from another area ONLY when the kind is
    # `routing`; a lane-kind pack sitting there is never reached from the
    # region a rider is actually downloading.
    #
    # So: duplicated into every region, which is how trips are done, and the
    # app dedupes by id (see CatalogueCountry.allPacks).
    #
    # `kind` is "lanes" rather than "overview" on purpose. Here it decides the
    # folder the file lands in, and that answer is the lane one. What the
    # container IS stays in its own
    # meta, where TbMapStore.kind reads it and the tile server orders area
    # before overview.
    overview_packs = [
        {
            "id": o["id"],
            "kind": "lanes",
            "format": "tbmap",
            "label": o["label"],
            "file": o["file"],
            "sha256": o["sha256"],
            "bytes": o["bytes"],
            "downloadBytes": o["downloadBytes"],
            "signature": o.get("signature"),
            "featureCount": o["featureCount"],
            "generated": o["generated"],
            "minZoom": o.get("minZoom"),
            # THE SAME LEGAL BASIS AS THE AREAS IT IS BUILT FROM, because it is
            # built from them: an overview is the same council definitive-map
            # data, coalesced for drawing small. Left off, it published a lane
            # pack that did not say it was the official record, and
            # real_catalogue_test.dart caught it against the live catalogue -
            # "a rider in Derby is offered the Midlands" asserts every lane
            # pack in a region is `official`, and one of mine was not.
            #
            # That assertion is not bookkeeping. It is the difference between
            # the definitive map and somebody's idea of where a byway goes.
            "legalBasis": "official",
        }
        for o in overviews
    ]
    for countries_here in by_continent.values():
        for country in countries_here:
            if country["code"] != "GB":
                continue
            for area in country["areas"]:
                # Only where there are lanes to overview. The roads area holds
                # routing and traffic orders and would just carry the bytes.
                if not any(pk.get("kind") == "lanes" for pk in area["packs"]):
                    continue
                have = {pk["id"] for pk in area["packs"]}
                for op in overview_packs:
                    if op["id"] not in have:
                        area["packs"].append(dict(op))

    catalogue = {
        "schema": 2,
        "generated": stamp,
        "attribution": ATTRIBUTION,
        "baseUrl": base_url,
        "overviews": overviews,
        # THE LIVE HALF OF THE CONDITIONS PIPELINE, and the only part of this
        # catalogue that is not about downloading ground. See
        # `conditions_block`: always present, empty when nothing is published,
        # so "we published none" and "this build had never heard of them" are
        # different things on the wire.
        "conditions": conditions,
        # How often the app should CHECK each kind, published here so it can
        # be changed without shipping an app. They move on different clocks:
        # councils amend a definitive map every few weeks and the road network
        # is rebuilt about as often, while ready-made routes are published by
        # people, continuously - a month is a long time to miss those. The
        # rider can still override any of it; they pay for the connection.
        "updates": {
            "lanes": "monthly",
            "routing": "monthly",
            "basemap": "monthly",
            "gpx": "weekly",
            # Four times a day. The only kind here that goes OFF: measured,
            # 1,124 orders change nationally in a single day, and the one that
            # matters is the one made this morning. Three megabytes makes that
            # affordable; see tools/build_tro.py.
            "tro": "sixHourly",
        },
        "continents": [
            {
                "id": cid,
                "label": CONTINENT_LABELS[cid],
                "countries": sorted(by_continent[cid],
                                    key=lambda c: c["label"]),
            }
            for cid in CONTINENT_LABELS
            if cid in by_continent
        ],
    }
    return catalogue


# --- what a rider in Britain actually downloads -----------------------------
#
# Step 1.7's gate, and it needed care, because "the UK" has two readings and
# they land either side of the 1.5 GB line in §2 of the pivot plan. Stated
# here rather than in prose so the verdict does not depend on who runs it.

# Everything in the DEFAULT download. `basemap` is here conditionally: see
# `_is_opt_in` - the standard imagery tier is in, the high-detail tier is not.
DEFAULT_KINDS = ("lanes", "overview", "routing", "names", "height", "tro",
                 "trips", "pois", "basemap")

# The tier id a satellite pack carries when it is the opt-in one. Areas built
# before the tiers existed carry no `detail` at all and are standard: they are
# maxZoom 13, which is exactly what the standard tier is.
OPT_IN_DETAIL = "high"


def packs_in(catalogue):
    """Every pack entry in the catalogue, duplicates included."""
    for continent in catalogue.get("continents", []):
        for country in continent.get("countries", []):
            for area in country.get("areas", []):
                for pack in area.get("packs", []):
                    yield country.get("code"), area.get("id"), pack


def _is_opt_in(pack):
    """True for a pack outside the default download.

    High-detail imagery only, today. It is twice the pixels over the SAME
    10 m source - what improves is how it is drawn, not what it can show -
    and it is 917.3 MB over four regions. A rider who is never told about it
    loses nothing they could have seen; a rider who gets it by default loses
    the budget.
    """
    return (pack.get("kind") == "basemap"
            and (pack.get("detail") or {}).get("id") == OPT_IN_DETAIL)


def packs_carrying_vehicle(catalogue):
    """Every pack id that still carries a `vehicle` key. Step 1.7's gate.

    Reads the top-level `overviews` too. They are not in `area["packs"]`, so
    a check that walked only the areas would return zero while four vehicle-
    partitioned entries sat in the catalogue the app parses.
    """
    guilty = []
    for _code, area_id, pack in packs_in(catalogue):
        if "vehicle" in pack:
            guilty.append("%s/%s" % (area_id, pack.get("id")))
    for entry in catalogue.get("overviews", []):
        if "vehicle" in entry:
            guilty.append("overviews/%s" % entry.get("id"))
    return guilty


def default_uk_download(catalogue, country="GB"):
    """The bytes a rider gets when they take the default for the whole UK.

    Distinct by pack id, because the catalogue duplicates on purpose: trips
    and the national overviews are listed into every region so a rider who
    takes one region still gets them, and the app dedupes by id
    (CatalogueCountry.allPacks). Summing entries counts them six times.
    """
    default, opt_in = {}, {}
    for code, _area_id, pack in packs_in(catalogue):
        if code != country:
            continue
        kind = pack.get("kind")
        if kind not in DEFAULT_KINDS:
            continue
        where = opt_in if _is_opt_in(pack) else default
        where.setdefault(kind, {})[pack.get("id")] = pack.get("bytes", 0) or 0
    return default, opt_in


def _kind_total(group, kind):
    return sum(group.get(kind, {}).values())


def report_default_uk(catalogue, budget=DEFAULT_UK_BUDGET_BYTES,
                      country="GB"):
    """Print the default download, the opt-in, and whether it fits.

    Returns True when the default fits. The caller decides whether that is a
    gate; `--check-default-uk` makes it one.
    """
    default, opt_in = default_uk_download(catalogue, country)
    total = sum(sum(v.values()) for v in default.values())
    opt_total = sum(sum(v.values()) for v in opt_in.values())

    print("\nDEFAULT %s DOWNLOAD - what a rider gets by taking the default "
          "for the whole country" % country)
    for kind in DEFAULT_KINDS:
        if kind not in default:
            continue
        packs = default[kind]
        print("  %-9s %3d pack(s)  %12d bytes  %8.1f MB"
              % (kind, len(packs), sum(packs.values()),
                 sum(packs.values()) / 1e6))
    if "pois" not in default:
        # Not missing. WAYS-SCHEMA.md puts `pois` in the SAME container as the
        # ways, own table, so their bytes are already inside the lanes line
        # above. A zero here would read as "no POIs shipped".
        print("  %-9s carried inside the ways container (WAYS-SCHEMA.md), "
              "not a separate pack" % "pois")
    print("  %-9s %26d bytes  %8.1f MB" % ("TOTAL", total, total / 1e6))
    print("  budget    %26d bytes  %8.1f MB" % (budget, budget / 1e6))
    verdict = "UNDER by %.1f MB" if total <= budget else "OVER by %.1f MB"
    print("  %-9s %s" % ("verdict", verdict % (abs(budget - total) / 1e6)))

    print("\nOPT-IN, OUTSIDE THE BUDGET")
    if not opt_in:
        print("  none in this catalogue")
    for kind, packs in sorted(opt_in.items()):
        print("  %-9s %3d pack(s)  %12d bytes  %8.1f MB"
              % (kind, len(packs), sum(packs.values()),
                 sum(packs.values()) / 1e6))
    if opt_total:
        # BOTH READINGS, because the gate's verdict must not depend on which
        # one the reader had in mind. High detail is an alternative tier over
        # the same ground, so a rider who chooses it does not also keep the
        # standard pack - but the app does not stop them holding both.
        # A standard pack and its high counterpart are the same ground:
        # "gb-north-satellite-standard" and "gb-north-satellite-high". The
        # ground is the id with the tier suffix taken off.
        replaced = {o.rsplit("-", 1)[0] for o in opt_in.get("basemap", {})}
        superseded = sum(b for pid, b in default.get("basemap", {}).items()
                         if pid.rsplit("-", 1)[0] in replaced)
        instead = total - superseded + opt_total
        print("  default + opt-in, both held     %12d bytes  %8.1f MB  (%s)"
              % (total + opt_total, (total + opt_total) / 1e6,
                 "over" if total + opt_total > budget else "under"))
        print("  high detail INSTEAD of standard %12d bytes  %8.1f MB  (%s)"
              % (instead, instead / 1e6,
                 "over" if instead > budget else "under"))
        print("  Either reading is over the %.1f MB budget, which is why "
              "high detail is opt-in and outside it."
              % (budget / 1e6))
    return total <= budget


def summarise(catalogue):
    """What is on offer, counted in FILES.

    An entry is not a file. Trips and the national overviews are listed into
    every British region on purpose - so a rider who takes one region still
    gets them, and the app dedupes by id - and this used to add each copy up
    again. That is the same double count that had the routing figure at 732
    files and 16.3 GB when it is 526 and 7.9, and it was quoted for weeks.
    """
    total, entries = 0, 0
    print("\ncatalogue:")
    for continent in catalogue["continents"]:
        seen = {}
        for country in continent["countries"]:
            for area in country["areas"]:
                for pack in area["packs"]:
                    entries += 1
                    seen[pack["id"]] = pack["bytes"]
        total += sum(seen.values())
        print("  %-16s %2d countries  %4d files  %8.1f GB"
              % (continent["label"], len(continent["countries"]),
                 len(seen), sum(seen.values()) / (1024 ** 3)))
    print("  %-16s %8.1f GB total, over %d entries"
          % ("", total / (1024 ** 3), entries))


def main():
    ap = argparse.ArgumentParser()
    # READ A CATALOGUE INSTEAD OF BUILDING ONE.
    #
    # The two gates below are the ones step 1.6 and 1.7 are read by, and a
    # gate you can only reach by rebuilding from a live network and a full
    # container set is a gate nobody runs. This reads the published file.
    ap.add_argument("--report", metavar="CATALOGUE",
                    help="report on an existing catalogue and exit: the "
                         "default UK download, and any pack still carrying a "
                         "vehicle field. Builds nothing.")
    ap.add_argument("--check-default-uk", action="store_true",
                    help="exit 1 if the default UK download is over budget")
    ap.add_argument("--check-no-vehicle", action="store_true",
                    help="exit 1 if any pack carries a `vehicle` field")
    ap.add_argument("--budget", type=int, default=DEFAULT_UK_BUDGET_BYTES,
                    help="the default-download budget in bytes "
                         "(default %d, which is 1.5 GB decimal)"
                         % DEFAULT_UK_BUDGET_BYTES)
    ap.add_argument("--lanes", default="dist/manifest.json",
                    help="the Great Britain lane manifest")
    ap.add_argument("--base-url", default="", help="where packs are hosted")
    ap.add_argument("--trips", default="trips/gb.tbtrips",
                    help="the built trips pack; missing is fine")
    ap.add_argument("--satellite", default="satellite/index.json",
                    help="record of published imagery packs; missing is fine "
                         "and simply means no imagery in this build")
    ap.add_argument("--routing-mirror", default="routing/index.json",
                    help="record of routing tiles we host; missing is fine "
                         "and simply leaves them pointing upstream")
    ap.add_argument("--routing-mirror-base", default="",
                    help="where our mirrored tiles are served from")
    ap.add_argument("--height", default="height/index.json",
                    help="record of published ground-height packs; missing is "
                         "fine and simply means no hill shading in this build")
    ap.add_argument("--names", default="dist/names",
                    help="directory of .tbnames gazetteer packs")
    ap.add_argument("--tro", default="tro/index.json",
                    help="the committed record of the traffic-orders pack")
    # A DEFAULT, NOT A FLAG A WORKFLOW HAS TO REMEMBER.
    #
    # `tools/rebuild_catalogue.sh` is the only way any job may rebuild this
    # file, and it exists because the set of flags was written down in three
    # places and only ever corrected in one - which is how imagery, trips and
    # the routing mirror each got deleted by a job that forgot them. Adding a
    # fourth line there would have been a fourth thing to forget in every job
    # that does NOT go through the script. Defaulting to where the LIVE half
    # writes means every caller picks the feeds up without knowing they exist,
    # exactly as `--satellite` and `--tro` already do.
    ap.add_argument("--conditions", default="published",
                    help="directory holding wet/ and rivers/ live feeds; "
                         "missing is fine and simply means no conditions in "
                         "this build")
    ap.add_argument("--containers", default="dist/containers/manifest.json",
                    help="what build_containers.py wrote; the .tbmap files "
                         "are what the app downloads")
    ap.add_argument("--out", default="dist/catalogue.json")
    args = ap.parse_args()

    if args.report:
        with open(args.report, encoding="utf8") as fh:
            return 0 if gates(json.load(fh), args) else 1

    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    catalogue = build(args.lanes, args.base_url, stamp,
                      containers=load_containers(args.containers),
                      satellite_index=args.satellite,
                      trips_index=args.trips,
                      names_dir=args.names,
                      height_index=args.height,
                      routing_mirror_index=args.routing_mirror,
                      routing_mirror_base=args.routing_mirror_base,
                      tro_path=args.tro,
                      conditions_dir=args.conditions)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf8") as fh:
        json.dump(catalogue, fh, indent=1)

    summarise(catalogue)
    print("\nwrote %s" % args.out)
    if not gates(catalogue, args):
        return 1
    return 0


def gates(catalogue, args):
    """The two readings step 1.6 and 1.7 are gated on. True if both pass.

    They PRINT either way and only FAIL when asked to, because this runs in
    the middle of a publish job and a build that refuses on a figure nobody
    has agreed yet is a build somebody disables.
    """
    ok = True

    routing = {}
    for code, _area, pack in packs_in(catalogue):
        if pack.get("kind") == "routing":
            routing[pack.get("id")] = pack.get("bytes", 0) or 0
    print("\nROUTING: %d distinct file(s), %d bytes (%.1f MB)"
          % (len(routing), sum(routing.values()),
             sum(routing.values()) / 1e6))
    # Every one when the set is the six it should be; a sample otherwise,
    # because 526 lines of tile names is how a reading stops being read.
    shown = sorted(routing)
    for name in shown[:12]:
        print("  %-8s %12d bytes" % (name, routing[name]))
    if len(shown) > 12:
        print("  ... and %d more" % (len(shown) - 12))

    guilty = packs_carrying_vehicle(catalogue)
    print("\nVEHICLE FIELD: %d pack(s) carry one" % len(guilty))
    for entry in guilty[:10]:
        print("  %s" % entry)
    if guilty and args.check_no_vehicle:
        print("REFUSED: a pack carrying `vehicle` is the partition that built "
              "the same bridleway four times (docs/WAYS-SCHEMA.md).",
              file=sys.stderr)
        ok = False

    fits = report_default_uk(catalogue, args.budget)
    if not fits and args.check_default_uk:
        print("REFUSED: the default UK download is over budget.",
              file=sys.stderr)
        ok = False
    return ok


if __name__ == "__main__":
    sys.exit(main() or 0)

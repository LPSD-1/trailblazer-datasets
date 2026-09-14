#!/usr/bin/env python3
"""Build the worldwide download catalogue the app reads.

    python build_catalogue.py --lanes dist/manifest.json --out dist/catalogue.json

Produces schema 2: continent -> country -> area -> packs. Every pack says what
kind it is (basemap, lanes, routing, gpx), how big it is, and - for lane data -
whether its legal standing is official or community-mapped.

Routing data is the 5x5-degree tile scheme, worldwide. Deliberately NOT cut to
our own regions: that works for one country and falls apart across a continent,
where the tile grid already covers everything for free.

Packs are addressed as absolute URLs on a GitHub release rather than paths
beside this file, because GitHub Pages caps a site at 1 GB and the routing
tiles alone are 9.3 GB.
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
    """Routing packs for one country, one per 5-degree tile it touches."""
    _, code, label, w, s, e, n = country
    mirror = mirror or {}
    packs = []
    for name, lon, lat in tiles_covering(w, s, e, n):
        if name not in tile_sizes:
            continue  # ocean, or nothing mapped there
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


def lane_areas(lanes_manifest):
    """Turn the Great Britain lane manifest into catalogue areas."""
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
        area["packs"].append({
            "id": "gb-%s-%s" % (area_id, pkg["package"]),
            "kind": "lanes",
            "label": pkg["label"],
            "file": pkg["file"],
            "sha256": pkg["sha256"],
            "bytes": pkg["bytes"],
            "plainBytes": pkg.get("plainBytes", 0),
            "vehicle": pkg["package"],
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


def tro_pack(path, base_url):
    """The national traffic-orders pack, if the TRO job has written one.

    ONE for the whole country, and it goes into every area. An order does not
    stop at a regional boundary: a rider who downloaded the Midlands must still
    be told about the closure two miles into Wales, and the pack is under three
    megabytes, so there is nothing to save by splitting it.

    Duplicated into each area the way trips are, and for the same reason - a
    pack listed once lands in whichever area sorts first, and everybody else
    silently goes without.
    """
    if not path or not os.path.exists(path):
        # Loudly, like trips. A workflow that quietly published a catalogue
        # with no traffic orders in it would leave no trace anywhere, and the
        # app would simply show no closures - which is indistinguishable from
        # there being none.
        print("warning: %s is missing; NO traffic orders in this build"
              % path, file=sys.stderr)
        return None
    try:
        size = os.path.getsize(path)
    except OSError as e:
        print("warning: could not read %s (%s); no traffic orders"
              % (path, e), file=sys.stderr)
        return None

    name = os.path.basename(path)
    print("  traffic orders pack (%d bytes)" % size)
    return {
        "id": "gb-tro",
        "kind": "tro",
        "label": "Traffic orders",
        "file": urllib.parse.urljoin(base_url, "tro/%s" % name),
        "sha256": _sha256_of(path),
        "bytes": size,
    }


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
          tro_path=None):
    tile_sizes = routing_index()
    gb_areas = lane_areas(lanes_manifest)
    imagery = satellite_packs(satellite_index)
    # Read exactly the way imagery is, and for exactly the same reason: the
    # height job runs on its own clock, and a monthly lane refresh that
    # rebuilt the catalogue without this would delete every height pack from
    # every rider's Downloads screen with no code having changed. That has
    # happened three times in this repository to other kinds.
    heights = satellite_packs(height_index)
    trips = trips_pack(trips_index, base_url)
    names = names_packs(names_dir, base_url)
    tro = tro_pack(tro_path, base_url)
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

    catalogue = {
        "schema": 2,
        "generated": stamp,
        "attribution": ATTRIBUTION,
        "baseUrl": base_url,
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


def summarise(catalogue):
    total = 0
    print("\ncatalogue:")
    for continent in catalogue["continents"]:
        cbytes = 0
        for country in continent["countries"]:
            for area in country["areas"]:
                for pack in area["packs"]:
                    cbytes += pack["bytes"]
        total += cbytes
        print("  %-16s %2d countries  %8.1f GB"
              % (continent["label"], len(continent["countries"]),
                 cbytes / (1024 ** 3)))
    print("  %-16s %8.1f GB total" % ("", total / (1024 ** 3)))


def main():
    ap = argparse.ArgumentParser()
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
    ap.add_argument("--tro", default="dist/tro/gb-tro.tbpack",
                    help="the national traffic-orders pack")
    ap.add_argument("--out", default="dist/catalogue.json")
    args = ap.parse_args()

    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    catalogue = build(args.lanes, args.base_url, stamp,
                      satellite_index=args.satellite,
                      trips_index=args.trips,
                      names_dir=args.names,
                      height_index=args.height,
                      routing_mirror_index=args.routing_mirror,
                      routing_mirror_base=args.routing_mirror_base,
                      tro_path=args.tro)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf8") as fh:
        json.dump(catalogue, fh, indent=1)

    summarise(catalogue)
    print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()

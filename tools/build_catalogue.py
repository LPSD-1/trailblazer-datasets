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
import json
import math
import os
import sys
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


def routing_packs(country, tile_sizes):
    """Routing packs for one country, one per 5-degree tile it touches."""
    _, code, label, w, s, e, n = country
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
            "file": ROUTING_INDEX + name + ".rd5",
            # The tile index publishes no checksums. The exact byte length
            # is recorded instead, and the app refuses anything that does not
            # match it - which catches truncated transfers and captive
            # portals, the failures that actually happen. It is weaker than a
            # hash and is not pretended otherwise.
            "sha256": "",
            "bytes": tile_sizes[name],
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


def build(lanes_manifest, base_url, stamp):
    tile_sizes = routing_index()
    gb_areas = lane_areas(lanes_manifest)

    by_continent = {}
    for country in COUNTRIES:
        continent, code, label, w, s, e, n = country
        routing = routing_packs(country, tile_sizes)

        areas = []
        if code == "gb" and gb_areas:
            # Britain has real lane data, split by area. Routing tiles are
            # country-wide, so they sit in their own area rather than being
            # duplicated into every one.
            areas.extend(sorted(gb_areas.values(), key=lambda a: a["label"]))

        if routing:
            areas.append({
                "id": "%s-roads" % code,
                # Named for the country, not "United Kingdom roads": this
                # area is what the app falls back to when a rider is outside
                # every region, and "You're near United Kingdom roads" reads
                # like a place nobody has ever been.
                "label": label,
                "bounds": {"west": w, "south": s, "east": e, "north": n},
                "packs": routing,
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
    ap.add_argument("--out", default="dist/catalogue.json")
    args = ap.parse_args()

    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    catalogue = build(args.lanes, args.base_url, stamp)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf8") as fh:
        json.dump(catalogue, fh, indent=1)

    summarise(catalogue)
    print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()

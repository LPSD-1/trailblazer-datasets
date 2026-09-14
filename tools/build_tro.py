#!/usr/bin/env python3
"""Build the national Traffic Regulation Order pack from D-TRO.

    python tools/build_tro.py --key ../trailblazer-keys/dataset-encryption-key-256.b64
    python tools/build_tro.py --key KEY --csv path/to/dtros_all.csv   (offline)

WHY THIS IS ONE NATIONAL PACK AND NOT ONE PER REGION
----------------------------------------------------
Measured against the whole published corpus on 14 September 2026:

    146,202 records published
     36,821 live restrictions worth carrying
    216,663 coordinate pairs, about 1.7 MB before gzip

The entire country fits in a couple of megabytes. Splitting that into six
regional packs would save a rider nothing worth having and would cost them the
thing that matters: an order does not stop at a regional boundary, and a rider
who has downloaded the Midlands should still be told about the closure two
miles into Wales.

WHY IT IS BUILT TWICE A DAY
---------------------------
Every other dataset here is a snapshot of something that changes slowly -
council definitive maps are amended over months, OS place names over years. A
traffic regulation order is the opposite: the ones that matter most are the
ones made last week, and a road closure a rider finds out about by arriving at
it is exactly the failure this is meant to prevent. Small and perishable, so
it is fetched often and fetched first.

WHAT IT IS NOT
--------------
It is not the legal record and it does not make the app's caveat go away. Not
every authority publishes - 91 of them appear in the corpus, out of about 174 -
so an empty stretch of map means "nothing published here", never "nothing in
force here". The sign on the post still wins. See docs/TRO_SPEC.md.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_packages import load_key, pack  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402
from tro import features  # noqa: E402

csv.field_size_limit(2**31 - 1)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# SRID=27700;LINESTRING(e n, e n) — also POINT and POLYGON.
_WKT = re.compile(r"SRID=(\d+);\s*(\w+)\s*\((.*)\)\s*$", re.S)

# Great Britain, generously. A coordinate outside this did not come from the
# National Grid, and drawing it would put a road closure in the sea.
_GB = (-9.0, 49.0, 2.5, 61.5)

# Five decimal places is about a metre. The datum shift in osgb.py is good to
# about five, so more places would be recording noise - and every digit is
# bytes in a file riders fetch twice a day.
_PLACES = 5


def parse_wkt(text):
    """`SRID=27700;LINESTRING(...)` to (kind, [(easting, northing), ...])."""
    match = _WKT.match((text or "").strip())
    if not match:
        return None, []
    srid, kind, body = match.group(1), match.group(2).upper(), match.group(3)
    if srid != "27700":
        # Everything published so far is 27700. Anything else is a change in
        # the service, and guessing at it would silently misplace the order.
        return None, []
    body = body.replace("(", " ").replace(")", " ")
    points = []
    for chunk in body.split(","):
        parts = chunk.split()
        if len(parts) < 2:
            continue
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return kind, points


def to_wgs84(points):
    """National Grid pairs to rounded [lon, lat], dropping anything absurd."""
    out = []
    for easting, northing in points:
        lon, lat = grid_to_wgs84(easting, northing)
        if not (_GB[0] <= lon <= _GB[2] and _GB[1] <= lat <= _GB[3]):
            continue
        point = [round(lon, _PLACES), round(lat, _PLACES)]
        # Consecutive duplicates survive the rounding and carry no shape.
        if out and out[-1] == point:
            continue
        out.append(point)
    return out


def geojson(feature, dtro_id=None):
    """One normalised restriction as a GeoJSON feature, or None."""
    kind, points = parse_wkt(feature["wkt"])
    if not points:
        return None
    coords = to_wgs84(points)
    if not coords:
        return None

    if kind == "POINT" or len(coords) == 1:
        geometry = {"type": "Point", "coordinates": coords[0]}
    elif kind == "POLYGON":
        # Closed ring, which GeoJSON requires and WKT does not always carry.
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        if len(coords) < 4:
            return None
        geometry = {"type": "Polygon", "coordinates": [coords]}
    else:
        if len(coords) < 2:
            return None
        geometry = {"type": "LineString", "coordinates": coords}

    properties = {
        "code": feature["code"],
        "label": feature["label"],
    }
    # The order this came from. Carried so a delta can REPLACE everything one
    # order contributed, rather than trying to match feature by feature: an
    # amended order routinely changes how many stretches it covers, and a
    # merge that cannot delete what is gone leaves ghosts on the map.
    if dtro_id:
        properties["dtro"] = dtro_id
    # Only what is actually there. An empty string for every absent field
    # would add a hundred kilobytes to say nothing.
    for key in ("name", "where", "start", "end", "ref", "tra"):
        value = feature.get(key)
        if value not in (None, ""):
            properties[key] = value

    # A stable id, so the app can tell "this order again" from "a new order"
    # across two days' packs without the service offering one that survives an
    # amendment. Content-derived, so an order that has not changed keeps its
    # id and one that has gets a new one.
    seed = "%s|%s|%s|%s" % (feature.get("ref"), feature["code"],
                            feature.get("where"), coords[0])
    properties["tro_uid"] = hashlib.sha256(seed.encode()).hexdigest()[:16]
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def read_corpus(path, today, keep_expired=False):
    """Every live restriction in the national CSV extract."""
    kept, records, skipped = [], 0, 0
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            records += 1
            try:
                record = json.loads(row["Data"])
            except (ValueError, KeyError):
                skipped += 1
                continue
            dtro_id = row.get("Id")
            for feature in features(record, today=today,
                                    keep_expired=keep_expired):
                built = geojson(feature, dtro_id)
                if built is not None:
                    kept.append(built)
    return kept, records, skipped


def corpus_date(path):
    """The day the extract was cut, from its own filename.

    `/dtros/all` hands back a URL like `dtros_20260906_010013.csv`, and that
    date - not today's - is what goes in the pack. The pack is then a function
    of the data alone, so rebuilding an unchanged extract produces identical
    bytes and riders do not re-download a file that has not changed.
    """
    match = re.search(r"(\d{4})(\d{2})(\d{2})", os.path.basename(path))
    if match:
        return "-".join(match.groups())
    return datetime.date.today().isoformat()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--key", required=True, help="base64 32-byte key file")
    ap.add_argument("--csv", help="a local dtros_all.csv; otherwise fetched")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "tro"))
    ap.add_argument("--index", default=os.path.join(ROOT, "tro", "index.json"),
                    help="the committed record of what was published")
    ap.add_argument("--base", default="https://github.com/lpsd-1/"
                    "trailblazer-datasets/releases/download/tro/",
                    help="where the sealed pack is hosted")
    ap.add_argument("--today", help="override the expiry date, for testing")
    args = ap.parse_args()

    path = args.csv
    if not path:
        from dtro_fetch import download_corpus  # local import: needs .env
        path = download_corpus(os.path.join(args.out, "_corpus"))

    cut = corpus_date(path)
    today = args.today or datetime.date.today().isoformat()
    print("corpus  %s (cut %s)" % (os.path.basename(path), cut))

    built, records, skipped = read_corpus(path, today)
    print("records %d, unreadable %d" % (records, skipped))
    print("live restrictions %d" % len(built))
    if not built:
        sys.exit("Nothing to publish. Refusing to write an empty pack: an "
                 "empty TRO pack is indistinguishable from 'no orders in "
                 "force anywhere', which is never true.")

    # Sorted, so the file is a function of its contents and not of the order
    # the service happened to return them in.
    built.sort(key=lambda f: f["properties"]["tro_uid"])

    collection = {
        "type": "FeatureCollection",
        "generated": cut,
        "package": "tro",
        "label": "Traffic regulation orders - Great Britain",
        "note": "Orders published to the DfT D-TRO service. Not every "
                "authority publishes, so blank ground means nothing has been "
                "published there - not that nothing is in force. Follow the "
                "signs on the road.",
        "attribution": "Contains public sector information licensed under the "
                       "Open Government Licence v3.0. Source: Department for "
                       "Transport D-TRO service.",
        "features": built,
    }

    body = json.dumps(collection, separators=(",", ":")).encode("utf8")
    sealed = pack(body, load_key(args.key))

    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, "gb-tro.tbpack")
    with open(out, "wb") as handle:
        handle.write(sealed)
    digest = hashlib.sha256(sealed).hexdigest()
    with open(out + ".sha256", "w", encoding="utf-8") as handle:
        handle.write(digest + "\n")

    # A COMMITTED INDEX, and the sealed pack published as a release asset.
    #
    # Both halves of that matter. The pack is three megabytes and is rebuilt
    # several times a day, so committing the binary would add a gigabyte a year
    # to a repository riders clone nothing from — releases carry it instead,
    # exactly as the mirrored routing tiles are carried.
    #
    # But the catalogue is rebuilt FROM SCRATCH by whichever job runs, and a
    # flag pointing at a file that job does not have is a flag it silently
    # skips — which deletes this pack from every rider's Downloads screen with
    # no code having changed. That has happened three times in this repository
    # to other kinds; see tools/rebuild_catalogue.sh. So the size and hash live
    # in a small file that IS committed, the way satellite and height already
    # do it, and every job can read it whether or not it built the pack.
    index = {
        "generated": cut,
        "packs": [{
            "id": "gb-tro",
            "kind": "tro",
            "label": "Traffic orders",
            "file": args.base.rstrip("/") + "/" + os.path.basename(out),
            "sha256": digest,
            "bytes": len(sealed),
            "generated": cut,
        }],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.index)), exist_ok=True)
    with open(args.index, "w", encoding="utf-8") as handle:
        json.dump(index, handle, indent=2, sort_keys=True)
        handle.write(chr(10))

    print("wrote %s" % out)
    print("  %.2f MB plain, %.2f MB sealed"
          % (len(body) / 1e6, len(sealed) / 1e6))
    print("  sha256 %s" % digest)
    print("wrote %s" % args.index)
    return 0


if __name__ == "__main__":
    sys.exit(main())

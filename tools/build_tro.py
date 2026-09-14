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

# SRID=27700;LINESTRING(e n, e n) — also POINT, POLYGON and the MULTI forms.
#
# CASE-INSENSITIVE, AND IT TOLERATES A DIMENSION TAG. `POINT Z (x y z)` is
# ordinary WKT and the service is free to start sending it; the old pattern
# wanted a bare word hard against the bracket, so every such order would have
# vanished from the pack with nothing logged anywhere. The same for a
# lowercase `srid=`.
_WKT = re.compile(r"SRID=(\d+);\s*([A-Za-z]+)\s*(?:Z|M|ZM)?\s*\((.*)\)\s*$",
                  re.S | re.I)

# The National Grid's own extent, in metres, generously.
#
# CHECKED ON THE EASTING AND NORTHING, not on the degrees that come out.
# (0, 0) — far and away the commonest missing value — converts to a point in
# the Celtic Sea about 130 km southwest of Land's End, which sits comfortably
# inside any box drawn round the British Isles and sailed straight through the
# check that used to be here. Inside a linestring it was worse: a 130 m closure
# near Sheffield became a 400 km V out into the Atlantic and back, and
# everything downstream that reads a bounding box then covered half of England.
_GRID = (0.0, 0.0, 800000.0, 1400000.0)

# Great Britain, generously, as a second net under the first.
_GB = (-9.0, 49.0, 2.5, 61.5)


def in_grid(easting, northing):
    """Whether a pair could have come from the National Grid at all.

    (0, 0) IS REJECTED EXPLICITLY. It is a real corner of the grid — the
    southwest of square SV, out in the sea beyond Scilly — so a range test
    admits it, and it is also the value a missing number arrives as far more
    often than it is a place anybody has closed a road. Treating the origin as
    the sentinel it is in practice costs nothing real and is the whole reason
    this function exists.
    """
    if easting == 0 and northing == 0:
        return False
    return (_GRID[0] <= easting <= _GRID[2]
            and _GRID[1] <= northing <= _GRID[3])

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
    def read(run):
        points = []
        for chunk in run.split(","):
            parts = chunk.split()
            if len(parts) < 2:
                continue
            try:
                points.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
        return points

    # THE MULTI FORMS KEEP THEIR PARTS APART. Flattened into one run of
    # points — which is what happened — two closed stretches a mile apart get
    # joined by a straight line drawn along roads that are open.
    if kind.startswith("MULTI") or kind == "GEOMETRYCOLLECTION":
        parts = [read(run) for run in re.findall(r"\(([^()]*)\)", body)]
        return kind, [p for p in parts if p]

    return kind, read(body.replace("(", " ").replace(")", " "))


def to_wgs84(points):
    """National Grid pairs to rounded [lon, lat], dropping anything absurd."""
    out = []
    for easting, northing in points:
        # The grid check FIRST — see the note on _GRID. The degree check below
        # cannot catch (0, 0) because (0, 0) converts to somewhere plausible.
        if not in_grid(easting, northing):
            continue
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

    # A MULTI form arrives as a list of runs. Its parts stay apart: joining
    # them draws a line along roads that are open.
    if kind and (kind.startswith("MULTI") or kind == "GEOMETRYCOLLECTION"):
        parts = [to_wgs84(run) for run in points if isinstance(run, list)]
        parts = [p for p in parts if len(p) >= 2]
        if not parts:
            return None
        geometry = {"type": "MultiLineString", "coordinates": parts}
        return _wrap(geometry, parts[0][0], feature, dtro_id)

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

    return _wrap(geometry, coords[0], feature, dtro_id)


def _wrap(geometry, first, feature, dtro_id):
    """Geometry plus the properties every feature carries."""
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
    # amendment.
    #
    # SEEDED FROM WHAT THE ORDER ACTUALLY SAYS. It used to be
    # (ref, code, where, first coordinate) under a comment claiming "an order
    # that has not changed keeps its id and one that has gets a new one" — and
    # it was neither. Amending the dates kept the id, so an app caching by uid
    # went on showing the old ones; extending the geometry by a hundred
    # kilometres kept it too; and two different closures at one junction with
    # no ref and no road name collided into one, so one of them simply
    # disappeared from the rider's map.
    #
    # The dates and the whole geometry are in the seed now. The rounding in
    # to_wgs84 is what keeps it stable: identical published geometry gives
    # identical coordinates gives an identical id, which is what lets an
    # unchanged cut rebuild to identical bytes.
    seed = "|".join([
        str(feature.get("ref")),
        str(feature["code"]),
        str(feature.get("where")),
        str(feature.get("start")),
        str(feature.get("end")),
        geometry["type"],
        json.dumps(geometry["coordinates"], separators=(",", ":")),
    ])
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


def previous_count(index_path):
    """How many restrictions the last published pack carried, or 0.

    Read from the committed index rather than from the pack itself, because
    every job can read the index and only the job that built the pack has the
    pack. See the note where the index is written.
    """
    try:
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)
        for pack in index.get("packs", []):
            count = pack.get("features")
            if isinstance(count, int) and count > 0:
                return count
    except (IOError, ValueError, KeyError):
        pass
    return 0


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
    ap.add_argument("--allow-shrink", action="store_true",
                    help="publish even if the count has collapsed against the "
                         "last run - for a genuine change, never to get a red "
                         "build green")
    args = ap.parse_args()

    path = args.csv
    if not path:
        from dtro_fetch import download_corpus  # local import: needs .env
        path = download_corpus(os.path.join(args.out, "_corpus"))

    cut = corpus_date(path)
    # FILTERED AGAINST THE EXTRACT'S OWN DATE, not today's.
    #
    # `corpus_date` promises two lines up that "the pack is then a function of
    # the data alone, so rebuilding an unchanged extract produces identical
    # bytes and riders do not re-download a file that has not changed" — and
    # passing today's date here broke that promise every midnight. The job runs
    # four times a day against an extract DfT re-cuts every few days, so a
    # rider was handed a fresh 3.7 MB pack each morning because a handful of
    # orders had rolled past their end date, not because anything new had been
    # published.
    #
    # The orders that expire between the cut and the rider looking are handled
    # where they should be: the app filters `inForceOn(today)` before it draws
    # anything, so a lapsed order is carried and not shown. Deciding that here
    # would mean deciding it once, on a build server, for a file somebody opens
    # a fortnight later.
    day = args.today or cut
    print("corpus  %s (cut %s, filtered against %s)"
          % (os.path.basename(path), cut, day))

    built, records, skipped = read_corpus(path, day)
    print("records %d, unreadable %d" % (records, skipped))
    print("live restrictions %d" % len(built))
    if not built:
        sys.exit("Nothing to publish. Refusing to write an empty pack: an "
                 "empty TRO pack is indistinguishable from 'no orders in "
                 "force anywhere', which is never true.")

    # A FLOOR, NOT JUST A ZERO CHECK.
    #
    # The line above catches the one case that cannot happen quietly. What can
    # happen quietly is a pack with four hundred restrictions in it instead of
    # thirty-four thousand: a truncated extract, a schema change that makes
    # `parse_wkt` drop a geometry shape, an authority's rows failing to parse.
    # Every one of those publishes cleanly and shows almost every closed road
    # in Great Britain as open, and nothing in the pipeline could tell that
    # pack from a genuinely quiet day.
    #
    # Two thirds of what was published last time, because the real count moves
    # with the ninety-day horizon and with how much each authority has filed —
    # day to day that is a few per cent. A third of the country disappearing
    # between two cuts is not a quiet day, it is a broken read.
    previous = previous_count(args.index)
    if previous and len(built) < previous * 2 // 3 and not args.allow_shrink:
        sys.exit(
            "REFUSING TO PUBLISH: %d restrictions, against %d last time.\n"
            "That is not a quiet day, it is a bad read - a truncated "
            "extract, or geometry this build no longer understands.\n"
            "%d of %d records were unreadable.\n"
            "Pass --allow-shrink if the drop is genuine."
            % (len(built), previous, skipped, records))

    # Unreadable rows are a signal in their own right: the corpus is machine
    # written, so a sudden crop of them means the shape changed under us.
    if records and skipped > records // 10 and not args.allow_shrink:
        sys.exit("REFUSING TO PUBLISH: %d of %d records could not be read. "
                 "The extract's shape has probably changed."
                 % (skipped, records))

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
            # What the next run's floor check compares against. Not read by
            # the app; the catalogue builder ignores fields it does not know.
            "features": len(built),
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

#!/usr/bin/env python3
"""Turn well-known trips into anchors riders' phones can route between.

    python tools/build_trips.py --trips trips/gb.json --out dist/trips

WHAT THIS PUBLISHES
-------------------
A name, a description, and a handful of points. NOT a route. There is no
geometry in the output and none is ever published: the app works the line out
on the phone, from the routing tiles the rider already has, using the same
green-lane profile it uses for "hold on the map and get directions".

Three things fall out of that, and they are the whole reason for the design:

  * Nothing is copied from anybody. WHICH trips are famous is a fact and anyone
    may state it. The GPX files people post on forums are their work, and
    republishing those in a paid app would be theft however freely they were
    shared.

  * The route cannot rot. A stored line is a photograph of what was legal on
    the day it was drawn; rights of way get amended and Traffic Regulation
    Orders come and go. Recomputing from anchors means a trip re-opened next
    season follows this season's data.

  * It is a few hundred bytes rather than a few hundred kilobytes.

WHAT IT REFUSES TO DO
---------------------
Publish a trip whose anchors it cannot place on a real, motor-legal byway. An
anchor typed into the definitions file is somebody's recollection of where a
village is; snapping it to a byway in the rights-of-way data is what turns it
into a fact. An anchor with no byway near it is a wrong anchor, and a wrong
anchor sends a rider somewhere the trip does not go - so it fails the build
instead.
"""
import argparse
import json
import math
import os
import sys

# The rights-of-way type that may legally be ridden. Same definition the
# "motor" lane package uses, and it has to stay the same one: a trip anchored
# to a bridleway is a trip that tells a rider to break the law.
MOTOR_TYPE = "byway_open_to_all_traffic"

# How far an anchor may be from a byway and still be believed, in kilometres.
#
# Generous on purpose. The anchors are villages, car parks and passes, and the
# byway they belong to often starts a mile out of the village - so a tight
# radius would reject good trips. What it still catches is the mistake that
# matters: an anchor in the wrong valley, or the wrong county, which is what a
# transposed digit or a half-remembered name produces.
MAX_SNAP_KM = 6.0

# How far an anchor may move and still be published without a human looking.
#
# The radius above is what makes a trip PLACEABLE; this is what makes it
# believable. Six kilometres is wider than any British valley, so an anchor
# that moved five of them has almost certainly landed on a different lane in
# the next valley along - which snapped, published, and read as a success.
# Anything past this fails the build and asks for the anchor to be corrected.
MAX_MOVED_KM = 2.5

EARTH_KM = 6371.0088


def haversine_km(a_lat, a_lon, b_lat, b_lon):
    p = math.pi / 180
    dlat = (b_lat - a_lat) * p
    dlon = (b_lon - a_lon) * p
    h = (math.sin(dlat / 2) ** 2
         + math.cos(a_lat * p) * math.cos(b_lat * p) * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_KM * math.asin(math.sqrt(h))


def nearest_on_byways(lat, lon, features, max_km=MAX_SNAP_KM):
    """The closest byway vertex to (lat, lon), or None if none is near enough.

    Vertices rather than true perpendicular distance to each segment. The
    difference is metres on data this dense, and the answer is fed to a router
    that snaps to the network again anyway - so the extra precision would buy
    nothing and cost a great deal of arithmetic over a quarter of a million
    coordinates.
    """
    best = None
    best_km = max_km
    for f in features:
        for lon2, lat2 in f["geometry"]["coordinates"]:
            # Cheap rejection before the trigonometry. At GB latitudes a degree
            # of latitude is ~111 km, so anything further than max_km/111
            # degrees away cannot possibly win, and this skips the overwhelming
            # majority of the country without calling sin() once.
            if abs(lat2 - lat) > max_km / 111.0:
                continue
            km = haversine_km(lat, lon, lat2, lon2)
            if km < best_km:
                best_km = km
                best = (lat2, lon2, f)
    if best is None:
        return None
    return {"lat": round(best[0], 6), "lon": round(best[1], 6),
            "km": round(best_km, 3), "feature": best[2]}


def load_motor_features():
    """Every byway open to all traffic in the cache.

    Imported from build_packages rather than re-read, so there is one
    definition of what a rideable lane is. Two would drift, and the direction
    they would drift in is a trip anchored to something a rider may not ride.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_packages  # noqa: E402

    cache = build_packages.cache_dir()
    authorities_path = os.path.join(cache, "authorities.json")
    if not os.path.exists(authorities_path):
        return None
    with open(authorities_path, encoding="utf8") as fh:
        authorities = json.load(fh)
    by_type = build_packages.load_all(authorities)
    return by_type.get(MOTOR_TYPE, [])


def build(trips_doc, features):
    """-> (published, rejected). Neither is allowed to be silent."""
    published = []
    rejected = []

    for trip in trips_doc.get("trips", []):
        anchors = trip.get("anchors") or []
        if len(anchors) < 2:
            rejected.append((trip.get("id"), "fewer than two anchors"))
            continue

        placed = []
        why = None
        for anchor in anchors:
            hit = nearest_on_byways(anchor["lat"], anchor["lon"], features)
            if hit is None:
                why = ("no byway within %.0f km of %s (%.4f, %.4f)"
                       % (MAX_SNAP_KM, anchor.get("name", "?"),
                          anchor["lat"], anchor["lon"]))
                break
            # Two anchors landing on the SAME vertex is a leg from A to A.
            # It happens on sparse moorland - which is where these trips are -
            # when two places share one nearby byway, and it publishes a trip
            # that routes nowhere with nothing to show it went wrong.
            if any(p["lat"] == hit["lat"] and p["lon"] == hit["lon"]
                   for p in placed):
                why = ("%s snapped onto a point already used by an earlier "
                       "anchor - the trip would route from a place to itself"
                       % anchor.get("name", "?"))
                break

            placed.append({
                "name": anchor.get("name"),
                "lat": hit["lat"],
                "lon": hit["lon"],
                # Kept so a reviewer can see how far the definitions file was
                # out, and so a creeping error shows up as a number rather than
                # as a trip that quietly goes somewhere else.
                "movedKm": hit["km"],
            })

        if not why:
            worst = max((p["movedKm"] for p in placed), default=0)
            if worst > MAX_MOVED_KM:
                why = ("an anchor moved %.1f km to reach a byway (limit %.1f). "
                       "That is far enough to be a different lane in the next "
                       "valley; check the coordinates" % (worst, MAX_MOVED_KM))

        if why:
            rejected.append((trip.get("id"), why))
            continue

        out = {
            "id": trip["id"],
            "name": trip["name"],
            "region": trip.get("region"),
            "summary": trip.get("summary", ""),
            "stops": placed,
        }
        if trip.get("days"):
            out["days"] = trip["days"]
        if trip.get("warning"):
            out["warning"] = trip["warning"]
        published.append(out)

    return published, rejected


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trips", required=True, help="a trip definitions file")
    ap.add_argument("--out", default="dist/trips", help="output directory")
    ap.add_argument("--allow-unsnapped", action="store_true",
                    help="publish the anchors as written, WITHOUT checking "
                         "them against the lane data. For validating the "
                         "definitions file only - never for a real build.")
    args = ap.parse_args()

    with open(args.trips, encoding="utf8") as fh:
        doc = json.load(fh)

    country = doc.get("country")
    if not country:
        sys.exit("%s names no country" % args.trips)

    if args.allow_unsnapped:
        print("NOT SNAPPING - anchors published as written. Not a real build.")
        features = None
        published = []
        for trip in doc.get("trips", []):
            anchors = trip.get("anchors") or []
            if len(anchors) < 2:
                continue
            published.append({
                "id": trip["id"], "name": trip["name"],
                "region": trip.get("region"), "summary": trip.get("summary", ""),
                "days": trip.get("days"), "warning": trip.get("warning"),
                "stops": [{"name": a.get("name"), "lat": a["lat"],
                           "lon": a["lon"], "movedKm": None} for a in anchors],
            })
        rejected = []
    else:
        features = load_motor_features()
        if features is None:
            sys.exit("no rights-of-way cache - run fetch_rights_of_way.py "
                     "first, or pass --allow-unsnapped to check the "
                     "definitions file alone")
        if not features:
            # An empty cache and a country with no byways look identical from
            # here, and the second reading publishes nothing while claiming
            # success.
            sys.exit("the cache holds no %s at all; refusing to publish"
                     % MOTOR_TYPE)
        print("snapping against %d byways..." % len(features))
        published, rejected = build(doc, features)

    for trip_id, why in rejected:
        print("REJECTED %s: %s" % (trip_id, why), file=sys.stderr)

    if not published:
        sys.exit("nothing could be published")

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "%s.tbtrips" % country)
    payload = {
        "version": 1,
        "country": country,
        "attribution": doc.get("attribution",
                               "Routes computed on the device from local "
                               "highway authority definitive maps via "
                               "rowmaps.com, Open Government Licence v3.0."),
        "trips": published,
    }
    # newline="", so the same input produces the same BYTES on every platform.
    #
    # A pack is published with its length and its SHA-256, and the app refuses
    # any download that does not match them exactly. Python's text mode
    # translates a newline into a carriage-return-newline pair on Windows, so
    # this file - the only pack written in text mode - comes out 243 bytes
    # longer there than it does on
    # the Linux runner. Build the catalogue on one platform and serve the file
    # built on the other and every rider gets "That download was corrupted.
    # Try again.", for ever, with nothing anywhere explaining it.
    #
    # .gitattributes stops git rewriting it as well. Both are needed: this one
    # stops the wrong bytes being WRITTEN, that one stops the right bytes
    # being rewritten on the way into the repository.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    worst = max((s["movedKm"] or 0 for t in published for s in t["stops"]),
                default=0)
    print("wrote %s: %d trips, %d bytes, worst anchor moved %.1f km"
          % (path, len(published), os.path.getsize(path), worst))
    if rejected:
        # NOT a warning. The whole purpose of snapping is that a trip which
        # cannot be placed must not reach a rider, and exiting 0 meant seven of
        # eight published, green CI, and the eighth quietly gone. The workflow
        # comment promised "a transposed digit fails the build"; now it does.
        print("", file=sys.stderr)
        print("%d trip(s) rejected. Fix the anchors in the definitions file, "
              "or remove the trip; a build that silently drops one is how a "
              "published trip disappears without anybody noticing."
              % len(rejected), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

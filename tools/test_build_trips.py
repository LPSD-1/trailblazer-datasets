#!/usr/bin/env python3
"""Whether a trip is fit to publish.

    python tools/test_build_trips.py

The thing under test is the refusal. Snapping an anchor to a byway is what
turns somebody's recollection of where a village is into a fact, and a trip
that publishes with an anchor in the wrong valley sends a rider somewhere the
trip does not go - with our name on it.

No framework, because the repo has none and one test file does not justify
adding a dependency to a build that has to run unattended.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_trips  # noqa: E402

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s\n    got:  %r\n    want: %r" % (name, got, want))


def check_true(name, got):
    check(name, bool(got), True)


def byway(coords):
    """A byway as build_packages normalises one: a LineString of lon/lat."""
    return {"geometry": {"coordinates": coords}, "properties": {}}


# A short byway through the middle of Wales, near Pontrhydfendigaid.
WALES = [byway([(-3.850, 52.290), (-3.840, 52.295), (-3.830, 52.300)])]


def test_an_anchor_on_a_byway_snaps_to_it():
    hit = build_trips.nearest_on_byways(52.295, -3.840, WALES)
    check_true("an anchor sitting on a byway is placed", hit is not None)
    check("it snaps to the nearest vertex", (hit["lat"], hit["lon"]),
          (52.295, -3.84))
    check_true("and reports that it barely moved", hit["km"] < 0.01)


def test_an_anchor_near_a_byway_still_snaps():
    # The ordinary case: the village is a mile from where the byway starts.
    hit = build_trips.nearest_on_byways(52.300, -3.870, WALES)
    check_true("an anchor a mile off is still placed", hit is not None)
    check_true("and the distance is reported honestly",
               0.5 < hit["km"] < 4.0)


def test_an_anchor_in_the_wrong_place_is_refused():
    # The mistake that matters: a transposed digit putting a Welsh trip in
    # Norfolk. Publishing it would send a rider 200 miles to a lane that is
    # not there.
    hit = build_trips.nearest_on_byways(52.290, 1.000, WALES)
    check("an anchor with no byway near it is refused", hit, None)


def test_the_radius_is_a_real_boundary():
    # Just outside MAX_SNAP_KM, due north. One degree of latitude is ~111 km.
    far = 52.290 + (build_trips.MAX_SNAP_KM + 1.0) / 111.0
    check("an anchor beyond the radius is refused",
          build_trips.nearest_on_byways(far, -3.850, WALES), None)
    near = 52.290 + (build_trips.MAX_SNAP_KM - 1.0) / 111.0
    check_true("an anchor inside it is accepted",
               build_trips.nearest_on_byways(near, -3.850, WALES) is not None)


def test_a_trip_whose_anchors_all_place_is_published():
    doc = {"country": "gb", "trips": [{
        "id": "t1", "name": "A trip", "region": "gb-wales",
        "summary": "s", "days": 1,
        "anchors": [{"name": "start", "lat": 52.290, "lon": -3.850},
                    {"name": "end", "lat": 52.300, "lon": -3.830}],
    }]}
    published, rejected = build_trips.build(doc, WALES)
    check("a good trip is published", len(published), 1)
    check("and nothing is rejected", rejected, [])
    check("its stops are the snapped ones", len(published[0]["stops"]), 2)
    check_true("and how far each moved is recorded",
               all(s["movedKm"] is not None for s in published[0]["stops"]))


def test_one_bad_anchor_rejects_the_WHOLE_trip():
    # Not "publish the bits that worked". A trip missing its middle is a
    # different trip, and the rider has no way to know that is what they got.
    doc = {"country": "gb", "trips": [{
        "id": "t1", "name": "A trip",
        "anchors": [{"name": "start", "lat": 52.290, "lon": -3.850},
                    {"name": "nowhere", "lat": 52.290, "lon": 1.000},
                    {"name": "end", "lat": 52.300, "lon": -3.830}],
    }]}
    published, rejected = build_trips.build(doc, WALES)
    check("the trip is not published", published, [])
    check("and it is reported, not swallowed", len(rejected), 1)
    check_true("with the anchor named", "nowhere" in rejected[0][1])


def test_two_anchors_on_the_same_point_are_refused():
    # A leg from a place to itself. It happens on sparse moorland - which is
    # where these trips are - when two named places share one nearby byway,
    # and it published a trip that routes nowhere with nothing to show it.
    doc = {"country": "gb", "trips": [{
        "id": "t1", "name": "A trip",
        "anchors": [{"name": "start", "lat": 52.2900, "lon": -3.8500},
                    {"name": "also start", "lat": 52.2901, "lon": -3.8501}],
    }]}
    published, rejected = build_trips.build(doc, WALES)
    check("the trip is not published", published, [])
    check("and it says which anchor collided", len(rejected), 1)
    check_true("naming the anchor", "also start" in rejected[0][1])


def test_an_anchor_that_moved_miles_is_refused():
    # The snap radius is what makes a trip PLACEABLE; this is what makes it
    # believable. Six km is wider than any British valley, so an anchor that
    # moved five of them has almost certainly landed on a lane in the next
    # valley along - which snapped, published, and read as a success.
    far = 52.290 + (build_trips.MAX_MOVED_KM + 1.5) / 111.0
    doc = {"country": "gb", "trips": [{
        "id": "t1", "name": "A trip",
        "anchors": [{"name": "start", "lat": 52.290, "lon": -3.850},
                    {"name": "miles off", "lat": far, "lon": -3.830}],
    }]}
    published, rejected = build_trips.build(doc, WALES)
    check("a far-flung anchor is not published", published, [])
    check_true("and the distance is named",
               "km to reach a byway" in rejected[0][1])


def test_a_small_correction_is_still_fine():
    # The ordinary case must survive: a village is usually a little way from
    # where its byway starts, and rejecting that would reject everything.
    near = 52.290 + 0.5 / 111.0
    doc = {"country": "gb", "trips": [{
        "id": "t1", "name": "A trip",
        "anchors": [{"name": "start", "lat": near, "lon": -3.850},
                    {"name": "end", "lat": 52.300, "lon": -3.830}],
    }]}
    published, rejected = build_trips.build(doc, WALES)
    check("a half-kilometre correction still publishes", len(published), 1)
    check("and nothing is rejected", rejected, [])


def test_every_shipped_trip_would_survive_its_own_rules():
    # The definitions file has to satisfy the checks that guard it, or the
    # first real build fails on data that was committed as good.
    import json, os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    doc = json.load(open(os.path.join(here, "trips", "gb.json"),
                         encoding="utf8"))
    for trip in doc["trips"]:
        seen = set()
        for a in trip["anchors"]:
            key = (round(a["lat"], 4), round(a["lon"], 4))
            check_true("%s has no duplicate anchor" % trip["id"],
                       key not in seen)
            seen.add(key)


def test_a_trip_with_one_anchor_is_not_a_trip():
    doc = {"country": "gb", "trips": [{
        "id": "t1", "name": "A trip",
        "anchors": [{"name": "only", "lat": 52.290, "lon": -3.850}],
    }]}
    published, rejected = build_trips.build(doc, WALES)
    check("a single point cannot be routed between", published, [])
    check("and it says so", len(rejected), 1)


def test_no_geometry_is_ever_published():
    # The whole design. If a line ever appears in the output, the copyright
    # position and the "it cannot rot" position both evaporate.
    doc = {"country": "gb", "trips": [{
        "id": "t1", "name": "A trip",
        "anchors": [{"name": "a", "lat": 52.290, "lon": -3.850},
                    {"name": "b", "lat": 52.300, "lon": -3.830}],
    }]}
    published, _ = build_trips.build(doc, WALES)
    text = json.dumps(published)
    check_true("no geometry key", "geometry" not in text)
    check_true("no coordinate list", "coordinates" not in text)
    # Two stops, and a handful of fields each - not a traced line.
    check_true("and it stays small", len(text) < 600)


def test_the_shipped_definitions_file_is_well_formed():
    # The research itself: every trip needs the fields the app will read, and
    # a region the catalogue actually publishes.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "trips", "gb.json")
    with open(path, encoding="utf8") as fh:
        doc = json.load(fh)

    regions = {"gb-south-west", "gb-south-east", "gb-east-anglia",
               "gb-midlands", "gb-wales", "gb-north"}
    ids = set()
    for trip in doc["trips"]:
        for field in ("id", "name", "region", "summary", "anchors"):
            check_true("%s has %s" % (trip.get("id"), field), trip.get(field))
        check_true("%s has a region the catalogue publishes" % trip["id"],
                   trip["region"] in regions)
        check_true("%s has at least two anchors" % trip["id"],
                   len(trip["anchors"]) >= 2)
        check_true("%s id is unique" % trip["id"], trip["id"] not in ids)
        ids.add(trip["id"])
        for a in trip["anchors"]:
            check_true("%s anchor %s is in Great Britain"
                       % (trip["id"], a.get("name")),
                       49.8 <= a["lat"] <= 61.0 and -8.7 <= a["lon"] <= 1.8)

    # The one that is deliberately absent, and stays absent.
    check_true("the TET is not reproduced",
               not any("trans euro" in t["name"].lower()
                       or "tet" == t["id"].lower() for t in doc["trips"]))


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print("FAILED %d of %d" % (len(FAILURES), len(tests)))
        for f in FAILURES:
            print("  " + f)
        return 1
    print("ok: %d tests" % len(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main())

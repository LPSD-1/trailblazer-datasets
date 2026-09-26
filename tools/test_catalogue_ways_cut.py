#!/usr/bin/env python3
"""The catalogue carries the date the lanes were cut, as checks that can fail.

    python tools/test_catalogue_ways_cut.py

`stamp_build.py` rule 3 moves a container's `generated` to the run's clock
when a POI, a ford or a gauge changes under unchanged ways, and keeps the
lanes' own date as `waysCut` in the build manifest. `build_catalogue.py` did
not copy it, so the app had nothing to date a region by but the restamp: a
rider browsing for an area read "cut <the POI refresh>" over lanes cut months
before. Each assertion has a paired NEGATIVE, as tools/test_build_catalogue.py
does: a gate nobody has watched fail is a gate nobody has watched.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_catalogue as bc  # noqa: E402

checks = 0
failures = []


def ok(condition, what):
    global checks
    checks += 1
    if not condition:
        failures.append(what)
        print("  FAIL  %s" % what)
    else:
        print("  ok    %s" % what)


POI_RUN = "2026-10-24T06:00:00Z"
LANES_CUT = "2026-01-02T03:04:05Z"

CONTAINER = {
    "kind": "area", "area": "south-west", "id": "gb-south-west-ways",
    "label": "South West", "file": "containers/ways-south-west.tbmap",
    "sha256": "a" * 64, "bytes": 65536, "downloadBytes": 65536,
    "laneCount": 5, "generated": POI_RUN,
}
PACKAGE = {
    "region": "south-west", "regionLabel": "South West", "area": "south-west",
    "label": "South West", "file": "packages/south-west.tbpack",
    "plainBytes": 1000, "laneCount": 5, "generated": POI_RUN,
}


def lane_pack(container):
    with tempfile.TemporaryDirectory() as tmp:
        lanes = os.path.join(tmp, "lanes.json")
        conts = os.path.join(tmp, "containers.json")
        with open(lanes, "w", encoding="utf8") as f:
            json.dump({"regions": [{"id": "south-west", "bounds": {
                "west": -6.0, "south": 49.9, "east": -1.9, "north": 51.8}}],
                "packages": [dict(PACKAGE)]}, f)
        with open(conts, "w", encoding="utf8") as f:
            json.dump({"containers": [container]}, f)
        areas = bc.lane_areas(lanes, bc.load_containers(conts))
    return areas["south-west"]["packs"][0]


print("a restamped region's catalogue entry says when its lanes were cut")

restamped = lane_pack(dict(CONTAINER, waysCut=LANES_CUT))
ok(restamped.get("waysCut") == LANES_CUT,
   "the lane pack carries the manifest's waysCut (got %r)"
   % restamped.get("waysCut"))
ok(restamped.get("generated") == POI_RUN,
   "PREMISE: and its generated is the POI run, which is why it matters")

never = lane_pack(dict(CONTAINER))
ok("waysCut" not in never,
   "a container never restamped publishes no waysCut, not a null "
   "(got %r)" % never.get("waysCut"))

ok(bc.ways_cut_of({"waysCut": LANES_CUT}) == {"waysCut": LANES_CUT},
   "ways_cut_of copies it from an overview entry too")
ok(bc.ways_cut_of({"waysCut": None}) == {} and bc.ways_cut_of(None) == {},
   "and gives nothing where there is nothing")

print("\nNEGATIVE: the check sees an entry that dropped it")

dropped = dict(restamped)
dropped.pop("waysCut", None)
ok(dropped.get("waysCut") != LANES_CUT,
   "NEGATIVE: an entry without waysCut fails the first check above")

print("\n%d checks, %d failed" % (checks, len(failures)))
sys.exit(1 if failures else 0)

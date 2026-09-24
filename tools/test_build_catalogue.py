#!/usr/bin/env python3
"""Steps 1.6 and 1.7, as checks that can come back red.

    python tools/test_build_catalogue.py

Every assertion here has a paired NEGATIVE: the same check run against input
carrying the defect, asserted to fail. A gate nobody has watched fail is a
gate nobody has watched.

Written because the two readings these steps are gated on - routing files, and
a pack carrying `vehicle` - are both "count something and compare", which is
exactly the shape that passes vacuously when the thing being counted has
quietly stopped being produced.
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


# The upstream index, as the world actually looks: every tile the GB box
# touches, including the two rows that carry no British ground, plus a
# scattering of the other 520.
WORLD_TILES = {
    "W10_N45": 144971, "W5_N45": 64221161, "E0_N45": 127261650,
    "W10_N50": 43038092, "W5_N50": 138928526, "E0_N50": 79463820,
    "W10_N55": 5506646, "W5_N55": 26343430, "E0_N55": 40557,
    "W10_N60": 749230, "W5_N60": 331280, "E0_N60": 455461,
    "E5_N45": 252409847, "E10_N45": 198707653, "W75_N40": 119411237,
    "E135_N35": 100404580,
}

GB = ("europe", "gb", "United Kingdom", -8.7, 49.8, 1.8, 60.9)
FR = ("europe", "fr", "France", -5.2, 41.3, 9.6, 51.1)


print("routing is Great Britain, and six tiles of it (step 1.6)")

gb_packs = bc.routing_packs(GB, WORLD_TILES)
ids = sorted(p["id"] for p in gb_packs)
ok(len(gb_packs) == 6, "GB gets 6 routing packs, not 12 (got %d)" % len(gb_packs))
ok(ids == sorted(bc.GB_ROUTING_TILES),
   "and they are exactly GB_ROUTING_TILES: %s" % ", ".join(ids))
total = sum(p["bytes"] for p in gb_packs)
ok(total == 293321071,
   "293,321,071 bytes / 293.3 MB (got %d)" % total)

# THE NEGATIVE. The same call, with the filter's tuple widened to what the
# bounding box alone would give. If this does not blow up, the filter is not
# what is producing the six and the check above proves nothing.
kept = bc.GB_ROUTING_TILES
try:
    bc.GB_ROUTING_TILES = frozenset(
        n for n, _lon, _lat in bc.tiles_covering(-8.7, 49.8, 1.8, 60.9))
    widened = bc.routing_packs(GB, WORLD_TILES)
finally:
    bc.GB_ROUTING_TILES = kept
ok(len(widened) == 12,
   "NEGATIVE: without the six-tile filter the same box gives 12 packs "
   "(got %d) - so the filter is what cuts it" % len(widened))
ok(sum(p["bytes"] for p in widened) - total == 193163753,
   "NEGATIVE: the six dropped tiles are 193,163,753 bytes / 193.2 MB of "
   "France, northern Spain and Shetland (got %d)"
   % (sum(p["bytes"] for p in widened) - total))

# THE OWNER KEPT THE OTHER COUNTRIES (24 Sep 2026). Step 1.6 had France
# return nothing; this is the reversal, and the reason the three mirrored N45
# tiles have somewhere to be listed again.
fr = bc.routing_packs(FR, WORLD_TILES)
fr_ids = sorted(p["id"] for p in fr)
ok(len(fr) > 0, "France gets routing packs again (got %d)" % len(fr))
ok("E0_N45" in fr_ids and "E5_N45" in fr_ids,
   "including the N45 row that GB leaves out: %s" % ", ".join(fr_ids))
ok(all(p["file"].startswith(bc.ROUTING_INDEX) for p in fr),
   "unmirrored tiles are fetched from upstream")
mirror = {"E0_N45": {"sha256": "b" * 64, "bytes": 127261650}}
fr_m = {p["id"]: p for p in bc.routing_packs(
    FR, WORLD_TILES, mirror, "https://example.org/rel/")}
ok(fr_m["E0_N45"]["file"] == "https://example.org/rel/E0_N45.rd5"
   and fr_m["E0_N45"]["sha256"] == "b" * 64,
   "and a tile we mirror is served from our copy, with its real hash")
ok(len(bc.routing_packs(GB, WORLD_TILES)) == 6,
   "while Great Britain is still six tiles, not twelve")


print("\nno pack carries a vehicle field (step 1.7)")


def _manifest(tmp, packages, containers):
    lanes = os.path.join(tmp, "lanes.json")
    conts = os.path.join(tmp, "containers.json")
    with open(lanes, "w", encoding="utf8") as f:
        json.dump({"regions": [{"id": "north",
                                "bounds": {"west": -3.7, "south": 53.0,
                                           "east": 0.2, "north": 55.9}}],
                   "packages": packages}, f)
    with open(conts, "w", encoding="utf8") as f:
        json.dump({"containers": containers}, f)
    return lanes, conts


CONTAINER = {
    "kind": "area", "area": "north", "id": "gb-north-ways",
    "label": "The North", "file": "containers/gb-north.tbmap",
    "sha256": "a" * 64, "bytes": 202985472, "downloadBytes": 202985472,
    "laneCount": 1861, "generated": "2026-09-16T21:47:46Z",
}
PACKAGE = {
    "region": "north", "regionLabel": "The North", "area": "north",
    "label": "The North", "file": "packages/north.tbpack",
    "plainBytes": 12000000, "laneCount": 1861,
}

with tempfile.TemporaryDirectory() as tmp:
    # The POST-1.2 shape: no `vehicle` on the container, no `package` on the
    # lane manifest entry. This is what the builder is moving to.
    lanes, conts = _manifest(tmp, [dict(PACKAGE)], [dict(CONTAINER)])
    areas = bc.lane_areas(lanes, bc.load_containers(conts))
    pack = areas["north"]["packs"][0]
    ok("vehicle" not in pack, "a lane pack has no `vehicle` key")
    ok(pack["id"] == "gb-north-ways",
       "and its id is gb-north-ways, not gb-north-<vehicle> (got %s)"
       % pack["id"])
    ok(pack["bytes"] == 202985472,
       "and it still found its container through the collapsed key")

    # The PRE-1.2 shape, which must keep working while 1.2 is in flight:
    # four vehicles over the same ground, and picking one at random would
    # publish a walker's data under a rider's id.
    lanes, conts = _manifest(
        tmp,
        [dict(PACKAGE, package="motor"), dict(PACKAGE, package="foot")],
        [dict(CONTAINER, vehicle="motor", id="gb-north-motor"),
         dict(CONTAINER, vehicle="foot", id="gb-north-foot",
              bytes=627000000)])
    areas = bc.lane_areas(lanes, bc.load_containers(conts))
    by_id = {p["id"]: p for p in areas["north"]["packs"]}
    ok(sorted(by_id) == ["gb-north-foot", "gb-north-motor"],
       "while the partition exists the ids still separate the four")
    ok(all("vehicle" not in p for p in by_id.values()),
       "and still no pack carries a `vehicle` key")
    ok(by_id["gb-north-motor"]["bytes"] == 202985472
       and by_id["gb-north-foot"]["bytes"] == 627000000,
       "each matched its OWN container, not whichever was first")


print("\nthe check that finds a vehicle field, and its negative")

CLEAN = {"continents": [{"id": "europe", "countries": [{
    "code": "GB", "areas": [{"id": "gb-north", "packs": [
        {"id": "gb-north-ways", "kind": "lanes", "bytes": 136749056},
    ]}]}]}], "overviews": [{"id": "gb-overview"}]}

ok(bc.packs_carrying_vehicle(CLEAN) == [], "a clean catalogue reports none")

dirty = json.loads(json.dumps(CLEAN))
dirty["continents"][0]["countries"][0]["areas"][0]["packs"][0]["vehicle"] = "motor"
ok(bc.packs_carrying_vehicle(dirty) == ["gb-north/gb-north-ways"],
   "NEGATIVE: one put back in an area is found")

dirty = json.loads(json.dumps(CLEAN))
dirty["overviews"][0]["vehicle"] = "bicycle"
ok(bc.packs_carrying_vehicle(dirty) == ["overviews/gb-overview"],
   "NEGATIVE: and one in the top-level `overviews`, which is not inside any "
   "area - a check walking only the areas would say zero here")


print("\nthe default UK download, and what is outside it (step 1.7)")

STANDARD = {"id": "gb-north-satellite-standard", "kind": "basemap",
            "bytes": 133752810, "detail": {"id": "standard"}}
HIGH = {"id": "gb-north-satellite-high", "kind": "basemap",
        "bytes": 391766619, "detail": {"id": "high"}}
UNTIERED = {"id": "gb-east-anglia-satellite", "kind": "basemap",
            "bytes": 53984644}

CAT = {"continents": [{"id": "europe", "countries": [
    {"code": "GB", "areas": [
        {"id": "gb-north", "packs": [
            {"id": "gb-north-ways", "kind": "lanes", "bytes": 136749056},
            STANDARD, HIGH,
            {"id": "gb-north-height", "kind": "height", "bytes": 89101944},
            # Duplicated into two areas on purpose, as trips and the
            # overviews are. Counted ONCE.
            {"id": "gb-trips", "kind": "trips", "bytes": 6764},
        ]},
        {"id": "gb-east-anglia", "packs": [
            UNTIERED,
            {"id": "gb-trips", "kind": "trips", "bytes": 6764},
        ]},
        {"id": "gb-roads", "packs": [
            {"id": "W5_N50", "kind": "routing", "bytes": 138928526},
        ]},
    ]},
    {"code": "FR", "areas": [{"id": "fr-roads", "packs": [
        {"id": "E5_N45", "kind": "routing", "bytes": 252409847},
    ]}]},
]}], "overviews": []}

default, opt_in = bc.default_uk_download(CAT)
ok("high" not in json.dumps(default),
   "high-detail imagery is NOT in the default")
ok(list(opt_in) == ["basemap"]
   and list(opt_in["basemap"]) == ["gb-north-satellite-high"],
   "it is the one thing in the opt-in group")
ok(sorted(default["basemap"]) == ["gb-east-anglia-satellite",
                                  "gb-north-satellite-standard"],
   "the standard tier IS in it, and so is an area built before the tiers "
   "existed - those are maxZoom 13, which is what standard means")
ok(default["trips"]["gb-trips"] == 6764
   and sum(default["trips"].values()) == 6764,
   "a pack duplicated into two areas is counted once, not twice")
ok(sum(default["routing"].values()) == 138928526,
   "France's routing is not in Britain's download")

total = sum(sum(v.values()) for v in default.values())
ok(total == 136749056 + 133752810 + 53984644 + 89101944 + 6764 + 138928526,
   "the total is the sum of exactly those (got %d)" % total)

ok(bc.report_default_uk(CAT, budget=total) is True,
   "NEGATIVE PAIR: a budget of exactly the total passes")
ok(bc.report_default_uk(CAT, budget=total - 1) is False,
   "NEGATIVE PAIR: one byte less fails - the boundary is where it says")


print("\n%d checks, %d failed" % (checks, len(failures)))
for f in failures:
    print("  %s" % f)
sys.exit(1 if failures else 0)

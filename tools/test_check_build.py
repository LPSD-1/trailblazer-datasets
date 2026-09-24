#!/usr/bin/env python3
"""The guard that decides whether a build is safe to publish.

    python tools/test_check_build.py

THE HOLE THIS CLOSES is arithmetic, and it is read straight off the published
manifest of 10 September 2026. The national total is dominated by footpaths -
635,242 of 875,827 ways - and byways open to all traffic, the only ones a rider
may legally ride and the whole reason this app exists, are 11,851 of it. That is
1.35%, against a national drop threshold of 2%.

So every byway in Great Britain could disappear and the national check would
report a 1.35% movement, under the limit, and publish. The per-area rule catches
a type that goes to exactly zero in every area. Between those two there was
nothing at all, and that gap is wide enough to lose a fifth of the country's
byways in silence.

A CORRECTION, recorded because the commit that added this check got it wrong.
That commit claimed the 16 September run had measured a 12.1% fall in byways.
It had not. 10,420 is the count of raw byway rows fetched from councils and
10,342 is that count after de-duplication; 11,851 is a count of LANES IN BUILT
PACKAGES, which splits lanes by area and publishes a boundary-straddling lane in
both. They are not comparable, and comparing them was the error. Measured like
for like, that run's motor packages held 12,702 lanes against 11,851 published -
a 7.2% RISE. Nothing had been lost.

The check below is still right, and still needed; what was wrong was the story
attached to it. It is kept because of the arithmetic in the first paragraph,
which does not depend on any particular run.
"""
import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_build import (  # noqa: E402
    BOUNDS_SLACK,
    MAX_AREA_DROP,
    GB_BOUNDS,
    MAX_CLOSURE_FACTOR,
    check_authorities,
    check_closures,
    check_declared_bounds,
    check_geometry_in_gb,
    check_totals,
    closure_count,
    coordinates,
    lane_totals,
    check_packages_readable,
    packages_to_open,
    write_baseline,
    load_baseline,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHECK = os.path.join(HERE, "check_build.py")


def manifest(**counts):
    """A manifest holding `counts` ways of each package type, spread over areas.

    Spread across four areas per type on purpose: a drop concentrated in one
    area is caught by the per-area check, so a test that put everything in one
    area would prove the wrong guard.
    """
    packages = []
    for name, total in counts.items():
        per = total // 4
        remainder = total - per * 3
        for i in range(4):
            packages.append({
                "package": name,
                "region": "region-%d" % i,
                "area": "area-%d" % i,
                "laneCount": remainder if i == 3 else per,
            })
    return {"packages": packages}


# The build published on 10 September 2026, read off manifest.json.
PUBLISHED = manifest(foot=635242, bicycle=114367, horse=114367, motor=11851)


class Totals(unittest.TestCase):
    def test_reports_each_vehicle_type_separately(self):
        total, _, by_package = lane_totals(PUBLISHED)
        self.assertEqual(total, 875827)
        self.assertEqual(by_package["motor"], 11851)
        self.assertEqual(by_package["foot"], 635242)


class EveryBywayCouldVanish(unittest.TestCase):
    """The shape of the hole, at its sharpest."""

    def test_a_loss_too_big_to_be_an_amendment_is_now_refused(self):
        # A twelve percent fall, which is the size that would have gone unseen.
        # NOT a measurement of any real run - see the correction in the module
        # docstring. The figure is chosen because it is comfortably inside both
        # old thresholds and far too large to be an amendment.
        after = manifest(foot=635242, bicycle=114367, horse=114367, motor=10420)
        problems = []
        check_totals(PUBLISHED, after, problems)
        self.assertTrue(problems, "a 12% byway loss must not publish")
        self.assertIn("motor", problems[0])

    def test_and_the_national_check_alone_would_have_let_it_through(self):
        # Proving WHY the new check is needed rather than just that it fires:
        # the national movement this produces is well inside the old limit.
        old_total, _, _ = lane_totals(PUBLISHED)
        after = manifest(foot=635242, bicycle=114367, horse=114367, motor=10420)
        new_total, _, _ = lane_totals(after)
        national = (old_total - new_total) / old_total
        self.assertLess(national, 0.02,
                        "if this ever exceeds 2%% the test has lost its point")

    def test_losing_nearly_every_byway_is_refused(self):
        # HONESTLY LABELLED: this one was already caught, by the per-area 25%
        # rule - each area falling from ~2,960 ways to one trips it on its own.
        # Checked by disabling the new per-type check, and this case stayed
        # green.
        #
        # It is kept because it pins the floor, not because it is evidence for
        # the check above. The gap the per-type check closes is everything
        # BETWEEN a total collapse and a 2% national wobble.
        after = manifest(foot=635242, bicycle=114367, horse=114367, motor=4)
        problems = []
        check_totals(PUBLISHED, after, problems)
        self.assertTrue(problems)

    def test_a_drop_spread_thin_enough_to_hide_from_the_per_area_rule(self):
        # The actual gap, stated as a case: 20% off every motor area is under
        # the per-area limit of 25% and is 0.27% of the national total, so
        # BOTH old checks pass and 2,370 byways disappear in silence.
        after = manifest(foot=635242, bicycle=114367, horse=114367, motor=9481)
        problems = []
        check_totals(PUBLISHED, after, problems)
        self.assertTrue(problems, "20% of the country's byways is not a wobble")
        self.assertTrue(any("motor" in p for p in problems))


class OrdinaryAmendments(unittest.TestCase):
    def test_a_council_tidying_its_records_still_publishes(self):
        # Real amendments move a type by a fraction of a percent. A guard that
        # fires on those is a guard people start passing --force to, which is
        # worse than no guard.
        after = manifest(foot=635000, bicycle=114367, horse=114367, motor=11820)
        problems = []
        check_totals(PUBLISHED, after, problems)
        self.assertEqual(problems, [])

    def test_growth_is_never_a_problem(self):
        after = manifest(foot=640000, bicycle=115000, horse=115000, motor=12500)
        problems = []
        check_totals(PUBLISHED, after, problems)
        self.assertEqual(problems, [])

    def test_a_type_that_did_not_exist_before_is_not_a_loss(self):
        # A new vehicle type appearing must not read as the old ones shrinking.
        after = manifest(foot=635242, bicycle=114367, horse=114367,
                         motor=11851, tractor=40)
        problems = []
        check_totals(PUBLISHED, after, problems)
        self.assertEqual(problems, [])


class RechunkingIsNotALoss(unittest.TestCase):
    """An area id is not a stable key, and treating it as one cost a publish.

    Area ids are built from the authorities packed into them -
    "midlands-barnsley-and-38-more". Fetch more data, the chunking shifts, and
    the same ground comes back as "...-and-41-more". Keyed on that, the check
    compared names against names that no longer existed and reported 50 areas
    as having "lost every one of its ways" - on a build whose data had GROWN by
    8.94%, with all 149 authorities answering.
    """

    @staticmethod
    def _chunked(total, names):
        """`total` ways of motor data spread over the named areas of one region."""
        per = total // len(names)
        return {
            "packages": [
                {
                    "package": "motor",
                    "region": "midlands",
                    "area": name,
                    "laneCount": per if i else total - per * (len(names) - 1),
                }
                for i, name in enumerate(names)
            ]
        }

    def test_renaming_every_area_is_not_a_loss(self):
        before = self._chunked(12000, ["midlands-barnsley-and-38-more",
                                       "midlands-derby-and-12-more"])
        # More data, so the chunker packs the authorities differently and every
        # area comes back under a new name.
        after = self._chunked(13000, ["midlands-barnsley-and-41-more",
                                      "midlands-derby-and-9-more",
                                      "midlands-stoke-and-3-more"])
        problems = []
        check_totals(before, after, problems)
        self.assertEqual(problems, [])

    def test_but_a_region_actually_losing_its_data_is_still_caught(self):
        # The thing the per-area rule was written for: a partial fetch. The
        # region is a coarser bucket than an area and still catches it.
        before = self._chunked(12000, ["midlands-a", "midlands-b"])
        after = {"packages": [
            {"package": "motor", "region": "midlands", "area": "midlands-a",
             "laneCount": 0},
        ]}
        problems = []
        check_totals(before, after, problems)
        self.assertTrue(problems, "a region losing everything must be refused")

    def test_and_a_region_disappearing_entirely_is_caught(self):
        before = self._chunked(12000, ["midlands-a"])
        after = {"packages": [
            {"package": "motor", "region": "north", "area": "north-a",
             "laneCount": 12000},
        ]}
        problems = []
        check_totals(before, after, problems)
        self.assertTrue(problems, "midlands vanished and nothing said so")


class ThePerRegionGate(unittest.TestCase):
    """MAX_AREA_DROP, which had no test until 0.13 went looking.

    Raising the constant to 1.0 - switching the gate off outright - left the
    whole suite green, because every case that reached it was already being
    caught by the national check, the per-type check or the "lost every one of
    its ways" branch beside it. The threshold itself was unpinned.

    What only this gate can see is one region falling while another grows to
    cover it: the national total does not move, the vehicle type does not
    move, and a fifth of the Midlands has gone.
    """

    @staticmethod
    def _regions(**counts):
        return {"packages": [
            {"package": "motor", "region": region, "area": region,
             "laneCount": count}
            for region, count in sorted(counts.items())]}

    BEFORE = _regions.__func__(midlands=3000, north=3000)

    def test_a_region_collapsing_behind_another_one_growing_is_refused(self):
        # -40% in the Midlands, +40% in the North. National: unchanged.
        # motor: unchanged. Nothing else in this file can see it.
        after = self._regions(midlands=1800, north=4200)
        self.assertEqual(lane_totals(after)[0], lane_totals(self.BEFORE)[0],
                         "the national total must not move, or this proves "
                         "nothing about the per-region gate")
        problems = []
        check_totals(self.BEFORE, after, problems)
        self.assertTrue(problems, "a region losing 40% must not publish")
        self.assertTrue(any("midlands" in p for p in problems))

    def test_exactly_the_threshold_still_publishes(self):
        share = 1.0 - MAX_AREA_DROP
        after = self._regions(midlands=int(3000 * share),
                              north=6000 - int(3000 * share))
        problems = []
        check_totals(self.BEFORE, after, problems)
        self.assertEqual(problems, [])

    def test_a_hair_past_it_does_not(self):
        share = 1.0 - MAX_AREA_DROP
        after = self._regions(midlands=int(3000 * share) - 50,
                              north=6000 - int(3000 * share) + 50)
        problems = []
        check_totals(self.BEFORE, after, problems)
        self.assertTrue(problems)


class StillCatchesWhatItAlwaysDid(unittest.TestCase):
    def test_an_empty_build(self):
        problems = []
        check_totals(PUBLISHED, {"packages": []}, problems)
        self.assertIn("no ways at all", problems[0])

    def test_a_first_publish_has_nothing_to_compare_against(self):
        problems = []
        check_totals(None, PUBLISHED, problems)
        self.assertEqual(problems, [])

    def test_a_national_collapse_across_every_type(self):
        after = manifest(foot=300000, bicycle=50000, horse=50000, motor=5000)
        problems = []
        check_totals(PUBLISHED, after, problems)
        self.assertTrue(any("national total" in p for p in problems))


# --------------------------------------------------------------- geometry
#
# THE HOLE THIS CLOSES: every check above this line counts ways. None of them
# looks at where a way IS. A lane whose geometry came back as (0, 0) - a blank
# WKT field, a projection that never ran, a lat/lon pair swapped somewhere
# between a council and a package - counts as exactly one lane in the manifest,
# the same as a real one. The totals are perfect and the map draws a byway in
# the Gulf of Guinea.


DERBYSHIRE = [[-1.6200, 53.1300], [-1.6100, 53.1400]]


def lanes(*geometries):
    """A decrypted package holding one feature per geometry given."""
    return {
        "features": [
            {"properties": {"lane_uid": "lane-%d" % i}, "geometry": geom}
            for i, geom in enumerate(geometries)
        ]
    }


def line(coords):
    return {"type": "LineString", "coordinates": coords}


class GeometryOutsideGreatBritain(unittest.TestCase):
    def test_a_real_lane_is_not_objected_to(self):
        problems = []
        check_geometry_in_gb(lanes(line(DERBYSHIRE)), "motor-midlands", problems)
        self.assertEqual(problems, [])

    def test_null_island_is_refused(self):
        # The shape of a blank geometry field surviving the parse.
        problems = []
        check_geometry_in_gb(lanes(line([[0.0, 0.0], [0.0, 0.0]])),
                             "motor-midlands", problems)
        self.assertTrue(problems, "a lane at (0, 0) must not publish")
        self.assertIn("outside Great Britain", problems[0])

    def test_the_message_names_the_lane_and_the_place(self):
        problems = []
        check_geometry_in_gb(lanes(line([[0.0, 0.0]])), "motor-midlands",
                             problems)
        self.assertIn("lane-0", problems[0])
        self.assertIn("motor-midlands", problems[0])

    def test_a_lane_in_france_is_refused(self):
        problems = []
        check_geometry_in_gb(lanes(line([[2.3522, 48.8566]])), "motor-south-east",
                             problems)
        self.assertTrue(problems)

    def test_eastings_and_northings_that_never_went_through_the_projection(self):
        # OSGB36 metres, which is what a definitive map arrives in. Published
        # raw they are a number pair that looks perfectly reasonable.
        problems = []
        check_geometry_in_gb(lanes(line([[414000.0, 365000.0]])), "motor-north",
                             problems)
        self.assertTrue(problems, "grid metres are not degrees")

    def test_a_lat_lon_pair_the_wrong_way_round(self):
        # 53.13, -1.62 is Derbyshire. Written lat-first it is longitude 53,
        # which is Turkmenistan, and nothing else in this file would notice.
        problems = []
        check_geometry_in_gb(lanes(line([[53.13, -1.62]])), "motor-midlands",
                             problems)
        self.assertTrue(problems)

    def test_only_the_strays_are_counted(self):
        problems = []
        check_geometry_in_gb(
            lanes(line(DERBYSHIRE), line([[0.0, 0.0]]), line(DERBYSHIRE)),
            "motor-midlands", problems)
        self.assertEqual(len(problems), 1)
        self.assertIn("1 way(s)", problems[0])

    def test_a_multilinestring_is_walked(self):
        problems = []
        check_geometry_in_gb(
            lanes({"type": "MultiLineString",
                   "coordinates": [[[-1.62, 53.13]], [[0.0, 0.0]]]}),
            "gb-tro", problems)
        self.assertTrue(problems, "nesting must not smuggle a stray past")

    def test_a_point_is_walked(self):
        # The traffic-order pack holds points as well as lines.
        problems = []
        check_geometry_in_gb(lanes({"type": "Point", "coordinates": [0.0, 0.0]}),
                             "gb-tro", problems)
        self.assertTrue(problems)

    def test_a_geometrycollection_is_walked(self):
        problems = []
        check_geometry_in_gb(
            lanes({"type": "GeometryCollection",
                   "geometries": [line([[0.0, 0.0]])]}),
            "gb-tro", problems)
        self.assertTrue(problems)

    def test_a_nan_coordinate_is_refused(self):
        problems = []
        check_geometry_in_gb(lanes(line([[float("nan"), 53.13]])), "motor-north",
                             problems)
        self.assertTrue(problems, "NaN compares false against every bound")

    def test_elevation_on_a_position_does_not_confuse_the_walk(self):
        problems = []
        check_geometry_in_gb(lanes(line([[-1.62, 53.13, 220.0]])),
                             "motor-midlands", problems)
        self.assertEqual(problems, [])

    def test_geometry_gets_no_slack_even_where_a_region_box_would(self):
        # A declared region box is allowed BOUNDS_SLACK past the box because
        # it is a padded bucket boundary. A way is not: it is ground somebody
        # can stand on, and there is none at 2.2E.
        problems = []
        check_geometry_in_gb(lanes(line([[2.20, 52.00]])), "motor-east-anglia",
                             problems)
        self.assertTrue(problems)
        self.assertLess(2.20, GB_BOUNDS[2] + BOUNDS_SLACK,
                        "this point must be inside the slack, or the test "
                        "proves nothing about the slack")

    def test_the_box_is_the_whole_of_gb_not_the_published_coverage(self):
        # Scotland is not published today. The gate must not become the reason
        # it cannot be, so this pins the box rather than the coverage.
        west, south, east, north = GB_BOUNDS
        self.assertLess(west, -6.0)
        self.assertGreater(north, 58.0)  # Shetland
        for point in coordinates(line([[-3.20, 55.95]])):   # Edinburgh
            self.assertFalse(point[0] < west or point[0] > east
                             or point[1] < south or point[1] > north)


class DeclaredRegionBounds(unittest.TestCase):
    """The cheap half of the same gate: what the index claims about itself."""

    @staticmethod
    def _manifest(bounds):
        return {"packages": [], "regions": [{"id": "midlands",
                                             "bounds": bounds}]}

    def test_the_published_bounds_are_accepted(self):
        problems = []
        check_declared_bounds(self._manifest(
            {"west": -3.25, "south": 51.90, "east": 0.15, "north": 53.60}),
            problems)
        self.assertEqual(problems, [])

    def test_a_region_in_the_atlantic_is_refused(self):
        problems = []
        check_declared_bounds(self._manifest(
            {"west": -20.0, "south": 51.90, "east": 0.15, "north": 53.60}),
            problems)
        self.assertTrue(problems)
        self.assertIn("midlands", problems[0])

    def test_the_padded_east_anglia_box_is_accepted(self):
        # Read off the published manifest: East Anglia and The North both run
        # east to 1.85, past the catalogue's 1.80, because the box is rounded
        # outwards past Lowestoft. Measured tight, this gate fired on the live
        # manifest - a gate that fires on the normal case gets turned off.
        problems = []
        check_declared_bounds(self._manifest(
            {"west": -0.40, "south": 51.50, "east": 1.85, "north": 53.05}),
            problems)
        self.assertEqual(problems, [])

    def test_but_a_box_that_has_genuinely_left_the_country_is_not(self):
        problems = []
        check_declared_bounds(self._manifest(
            {"west": -0.40, "south": 51.50, "east": 3.50, "north": 53.05}),
            problems)
        self.assertTrue(problems, "3.5E is the Netherlands")

    def test_a_region_with_no_bounds_is_refused(self):
        problems = []
        check_declared_bounds({"regions": [{"id": "midlands"}]}, problems)
        self.assertTrue(problems)

    def test_an_unreadable_corner_is_refused(self):
        problems = []
        check_declared_bounds(self._manifest(
            {"west": "-3.25", "south": 51.90, "east": 0.15, "north": 53.60}),
            problems)
        self.assertTrue(problems)

    def test_a_manifest_with_no_regions_is_not_a_problem(self):
        problems = []
        check_declared_bounds({"packages": []}, problems)
        self.assertEqual(problems, [])

    def test_the_build_actually_published_passes(self):
        # A gate that fires on the live data is a gate somebody turns off.
        path = os.path.join(ROOT, "manifest.json")
        if not os.path.isfile(path):
            self.skipTest("no published manifest in this checkout")
        with open(path, encoding="utf8") as fh:
            published = json.load(fh)
        problems = []
        check_declared_bounds(published, problems)
        self.assertEqual(problems, [], "the published manifest must pass")


# --------------------------------------------------------------- closures


def tro_index(count):
    return {"generated": "2026-09-06",
            "packs": [{"id": "gb-tro", "kind": "tro", "features": count}]}


class ClosureCount(unittest.TestCase):
    """36,584 live restrictions on the 6 September cut, read off tro/index.json.

    A rider shown no closure rides into one, and the count is the only thing
    that separates a quiet week from a truncated extract. build_tro.py:359
    refuses to BUILD below two thirds of last time - one direction, inside the
    builder, over a file it is about to overwrite. Nothing compared the counts
    at publish time, and nothing at all watched the count going UP: duplicated
    rows, or an expiry filter that stopped filtering, both publish cleanly.
    """

    def test_a_steady_count_publishes(self):
        problems = []
        check_closures(tro_index(36584), tro_index(35110), problems)
        self.assertEqual(problems, [])

    def test_a_truncated_extract_is_refused(self):
        # The case build_tro.py's floor was written for, caught again on the
        # far side of the build.
        problems = []
        check_closures(tro_index(36584), tro_index(400), problems)
        self.assertTrue(problems)
        self.assertIn("fell", problems[0])

    def test_a_count_that_triples_is_refused(self):
        # Nothing anywhere watched this direction before.
        problems = []
        check_closures(tro_index(36584), tro_index(146336), problems)
        self.assertTrue(problems, "four times the orders is not a busy week")
        self.assertIn("rose", problems[0])

    def test_exactly_the_factor_still_publishes(self):
        # The boundary, both sides of it. Named so that moving the constant
        # moves this test rather than silently widening the gate.
        problems = []
        check_closures(tro_index(10000),
                       tro_index(int(10000 * MAX_CLOSURE_FACTOR)), problems)
        self.assertEqual(problems, [])

    def test_a_hair_past_the_factor_does_not(self):
        problems = []
        check_closures(tro_index(10000),
                       tro_index(int(10000 * MAX_CLOSURE_FACTOR) + 100),
                       problems)
        self.assertTrue(problems)

    def test_the_same_boundary_downwards(self):
        problems = []
        check_closures(tro_index(30000),
                       tro_index(int(30000 / MAX_CLOSURE_FACTOR)), problems)
        self.assertEqual(problems, [])
        problems = []
        check_closures(tro_index(30000),
                       tro_index(int(30000 / MAX_CLOSURE_FACTOR) - 100),
                       problems)
        self.assertTrue(problems)

    def test_no_closures_at_all_is_never_true(self):
        problems = []
        check_closures(tro_index(36584), tro_index(0), problems)
        self.assertTrue(problems)
        self.assertIn("never true", problems[0])

    def test_an_index_with_no_count_in_it_is_refused(self):
        problems = []
        check_closures(tro_index(36584), {"packs": [{"id": "gb-tro"}]}, problems)
        self.assertTrue(problems)

    def test_a_build_that_wrote_no_index_did_not_lose_anything(self):
        # The lanes workflow builds no traffic orders. If that read as "every
        # closure vanished" this gate would refuse every monthly lane publish,
        # and a gate that fires on the normal case gets --forced forever.
        problems = []
        check_closures(tro_index(36584), None, problems)
        self.assertEqual(problems, [])

    def test_a_first_closure_build_has_nothing_to_compare(self):
        problems = []
        check_closures(None, tro_index(36584), problems)
        self.assertEqual(problems, [])

    def test_the_count_is_summed_over_every_pack(self):
        self.assertEqual(closure_count(
            {"packs": [{"features": 10}, {"features": 5}]}), 15)
        self.assertIsNone(closure_count({"packs": []}))
        self.assertIsNone(closure_count(None))
        self.assertIsNone(closure_count({"packs": [{"features": True}]}),
                          "a bool is not a count")


# -------------------------------------------------------------- rebaseline
#
# The pivot drops foot, horse and bicycle entirely: 863,976 of 875,827 ways,
# 98.6% of the national total, fifty times MAX_NATIONAL_DROP. check_build.py
# would refuse that build, and it is right to - from inside this file a
# deliberate cutover and a collapsed fetch look identical. The question is what
# gets it through, and --force gets it through leaving nothing behind.


PIVOT = manifest(motor=11851)


class EveryTypeDropped(unittest.TestCase):
    """THE CUTOVER ITSELF, which no test covered and which the guard waved
    through.

    Every existing rebaseline case drops foot, horse and bicycle and leaves
    motor in, so `old_total` after the exclusion is never zero and the
    ratios below it always have something to divide by. The real cutover
    drops ALL FOUR - the vehicle partition is retired entirely - and that
    is the one shape nothing exercised.

    With all four gone the old total is zero by construction, and
    `check_totals` used to `return` there, silently. That skipped
    MAX_NATIONAL_DROP, the per-type loop, MAX_AREA_DROP and the
    vanished-region check at once, on the only build they exist for: a
    synthetic cutover holding 105 ways across six regions, two of them at
    zero, printed "OK to publish." and exited 0.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "build_baseline.json")

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    #: The published shape: four partitions, no `ways` at all.
    OLD = manifest(foot=635242, bicycle=114367, horse=114367, motor=11851)

    def _baseline(self, new):
        """A record dropping all four, as the real cutover does."""
        write_baseline(self.path, self.OLD, new, "the pivot: one dataset")
        becomes = []
        problems = []
        dropped = load_baseline(self.path, self.OLD, problems, becomes)
        return dropped, becomes[0] if becomes else None, problems

    def test_the_premise_all_four_really_are_dropped(self):
        # Without this every assertion below could be measuring the
        # three-type case the other tests already cover.
        dropped, becomes, _ = self._baseline(manifest(ways=31369))
        self.assertEqual(dropped,
                         frozenset(["bicycle", "foot", "horse", "motor"]))
        self.assertEqual(becomes, 31369)

    def test_the_intended_cutover_publishes(self):
        new = manifest(ways=31369)
        dropped, becomes, problems = self._baseline(new)
        check_totals(self.OLD, new, problems, dropped, becomes)
        self.assertEqual(problems, [],
                         "the cutover that was signed off must publish")

    def test_a_collapsed_fetch_is_refused(self):
        # THE CASE THAT WAS MEASURED PASSING. 105 ways where the baseline
        # predicted 31,369.
        new = manifest(ways=105)
        dropped, becomes, problems = self._baseline(manifest(ways=31369))
        check_totals(self.OLD, new, problems, dropped, becomes)
        self.assertTrue(problems, "a 99.7% shortfall printed OK to publish")
        self.assertTrue(any("collapsed fetch" in p for p in problems),
                        problems)

    def test_a_region_at_zero_is_refused(self):
        # Needs no ratio to see, and the volume gates cannot see it at all
        # once the baseline has taken the old side to zero.
        new = {"packages": [
            {"package": "ways", "region": "midlands", "area": "a",
             "laneCount": 31369},
            {"package": "ways", "region": "wales", "area": "b",
             "laneCount": 0},
        ]}
        dropped, becomes, problems = self._baseline(manifest(ways=31369))
        check_totals(self.OLD, new, problems, dropped, becomes)
        self.assertTrue(any("no ways at all" in p for p in problems),
                        problems)

    def test_the_blindness_is_stated_rather_than_passed(self):
        # BLIND IS NOT PASS. The gates that need a before-and-after cannot
        # run here and the operator has to be told so, rather than reading
        # a clean run as a clean build.
        new = manifest(ways=31369)
        dropped, becomes, problems = self._baseline(new)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            check_totals(self.OLD, new, problems, dropped, becomes)
        printed = out.getvalue()
        self.assertIn("NO COMPARABLE BASELINE", printed)
        self.assertIn("not run", printed)


class Rebaselining(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "build_baseline.json")

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _record(self, previous=PUBLISHED, new=PIVOT, reason="the pivot"):
        return write_baseline(self.path, previous, new, reason)

    def _edit(self, **changes):
        with open(self.path, encoding="utf8") as fh:
            record = json.load(fh)
        record.update(changes)
        with open(self.path, "w", encoding="utf8") as fh:
            json.dump(record, fh)

    # --- without a record ---------------------------------------------

    def test_the_cutover_is_refused_with_no_baseline_at_all(self):
        problems = []
        dropped = load_baseline(self.path, PUBLISHED, problems)
        self.assertEqual(dropped, frozenset())
        check_totals(PUBLISHED, PIVOT, problems, dropped)
        self.assertTrue(problems, "a 98.6% fall must not publish unrecorded")
        self.assertTrue(any("national total" in p for p in problems))

    # --- with one -----------------------------------------------------

    def test_a_recorded_cutover_publishes(self):
        self._record()
        problems = []
        dropped = load_baseline(self.path, PUBLISHED, problems)
        self.assertEqual(dropped, frozenset(["foot", "horse", "bicycle"]))
        check_totals(PUBLISHED, PIVOT, problems, dropped)
        self.assertEqual(problems, [])

    def test_the_record_says_what_changed(self):
        record = self._record(reason="phase 1.2: motor only")
        self.assertEqual(record["dropped"], ["bicycle", "foot", "horse"])
        self.assertEqual(record["supersedes"]["total"], 875827)
        self.assertEqual(record["becomes"]["total"], 11851)
        self.assertEqual(record["nationalDrop"], 0.9865)
        self.assertEqual(record["reason"], "phase 1.2: motor only")
        self.assertTrue(record["recorded"])
        with open(self.path, encoding="utf8") as fh:
            self.assertEqual(json.load(fh)["dropped"],
                             ["bicycle", "foot", "horse"])

    # --- what it does NOT excuse --------------------------------------

    def test_it_does_not_excuse_a_drop_in_what_is_left(self):
        # The whole point. Motor is the dataset after the pivot; a baseline
        # that let motor fall too would be --force with a JSON file attached.
        self._record()
        problems = []
        dropped = load_baseline(self.path, PUBLISHED, problems)
        check_totals(PUBLISHED, manifest(motor=9000), problems, dropped)
        self.assertTrue(problems, "motor is still gated at 2%")
        self.assertTrue(any("motor" in p for p in problems))

    def test_it_does_not_excuse_a_region_vanishing_from_what_is_left(self):
        self._record()
        gone = {"packages": [p for p in PIVOT["packages"]
                             if p["region"] != "region-0"]}
        problems = []
        dropped = load_baseline(self.path, PUBLISHED, problems)
        check_totals(PUBLISHED, gone, problems, dropped)
        self.assertTrue(problems)

    def test_a_record_with_no_reason_is_not_a_record(self):
        self._record()
        self._edit(reason="   ")
        problems = []
        dropped = load_baseline(self.path, PUBLISHED, problems)
        self.assertEqual(dropped, frozenset(), "it must not apply")
        self.assertIn("silent bypass", problems[0])
        check_totals(PUBLISHED, PIVOT, problems, dropped)
        self.assertTrue(any("national total" in p for p in problems),
                        "and the drop it was meant to excuse is still refused")

    def test_it_stops_applying_once_the_cutover_has_published(self):
        # Self-expiring, and this is what stops it becoming a standing licence
        # to lose 98% of the data every month afterwards. Once manifest.json IS
        # the motor-only build, the record no longer describes it.
        self._record()
        problems = []
        dropped = load_baseline(self.path, PIVOT, problems)
        self.assertEqual(dropped, frozenset())
        check_totals(PIVOT, manifest(motor=200), problems, dropped)
        self.assertTrue(problems, "a later collapse is refused again")

    def test_a_record_describing_some_other_build_does_not_apply(self):
        self._record()
        self._edit(supersedes={"total": 123456, "packages": {}})
        problems = []
        self.assertEqual(load_baseline(self.path, PUBLISHED, problems),
                         frozenset())

    def test_a_record_whose_drop_did_not_happen_is_refused(self):
        # A stale record left in the tree while the build still ships foot
        # data: it must not sit there quietly excusing a type that is present.
        self._record()
        problems = []
        dropped = load_baseline(self.path, PUBLISHED, problems)
        check_totals(PUBLISHED, manifest(motor=11851, foot=300000), problems,
                     dropped)
        self.assertTrue(any("still carries" in p for p in problems))

    def test_a_record_naming_nothing_changes_nothing(self):
        self._record(previous=PUBLISHED, new=PUBLISHED)
        problems = []
        self.assertEqual(load_baseline(self.path, PUBLISHED, problems),
                         frozenset())
        self.assertEqual(problems, [])

    def test_a_first_publish_under_a_baseline_is_still_a_first_publish(self):
        self._record()
        problems = []
        check_totals(None, PIVOT, problems, frozenset(["foot"]))
        self.assertEqual(problems, [])


class TheCommandLine(unittest.TestCase):
    """main() wiring. A gate nothing calls is decoration."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        for root, _, files in os.walk(self.dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            if root != self.dir:
                os.rmdir(root)
        os.rmdir(self.dir)

    def _write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf8") as fh:
            json.dump(data, fh)
        return path

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, CHECK] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True)

    def test_the_cutover_is_refused_and_then_recorded_and_then_published(self):
        previous = self._write("manifest.json", PUBLISHED)
        new = self._write("new.json", PIVOT)
        baseline = os.path.join(self.dir, "build_baseline.json")
        common = ["--previous", previous, "--new", new,
                  "--cache", os.path.join(self.dir, "cache"),
                  "--baseline", baseline,
                  "--closures-previous", os.path.join(self.dir, "none.json"),
                  "--closures-new", os.path.join(self.dir, "none.json")]

        refused = self._run(*common)
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("NOT publishing", refused.stdout)

        recorded = self._run(*(common + ["--rebaseline", "--reason",
                                         "phase 1.2 drops foot, horse, bicycle"]))
        self.assertEqual(recorded.returncode, 2, recorded.stdout)
        self.assertIn("NOTHING WAS PUBLISHED", recorded.stdout)
        self.assertTrue(os.path.isfile(baseline))

        allowed = self._run(*common)
        self.assertIn("REBASELINED", allowed.stdout)
        # The authority floor still fires - there is no cache in this temp
        # directory - so the exit code is 1 for that reason alone. What is
        # being read here is that the 98.6% drop is no longer among the
        # problems listed.
        self.assertNotIn("national total", allowed.stdout)
        self.assertIn("phase 1.2 drops foot, horse, bicycle", allowed.stdout)

    def test_rebaseline_without_a_reason_writes_nothing(self):
        previous = self._write("manifest.json", PUBLISHED)
        new = self._write("new.json", PIVOT)
        baseline = os.path.join(self.dir, "build_baseline.json")
        out = self._run("--previous", previous, "--new", new,
                        "--baseline", baseline, "--rebaseline")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--reason", out.stdout)
        self.assertFalse(os.path.isfile(baseline))


class WhichPackagesGetOpened(unittest.TestCase):
    """The geometry check is only as wide as the set of packages opened."""

    @staticmethod
    def _index(**counts):
        built = manifest(**counts)
        for i, pkg in enumerate(built["packages"]):
            pkg["file"] = "packages/%s-%d.tbpack" % (pkg["package"], i)
        return built

    def test_every_motor_package_is_opened(self):
        built = self._index(motor=11851, foot=635242)
        chosen = packages_to_open(built)
        motor = [p for p in built["packages"] if p["package"] == "motor"]
        self.assertEqual(len([p for p in chosen if p["package"] == "motor"]),
                         len(motor), "all four motor areas, not just one")

    def test_one_of_every_other_type_is_sampled(self):
        built = self._index(motor=11851, foot=635242, horse=114367)
        chosen = packages_to_open(built)
        self.assertEqual(len([p for p in chosen if p["package"] == "foot"]), 1)
        self.assertEqual(len([p for p in chosen if p["package"] == "horse"]), 1)

    def test_the_sample_is_the_largest_of_its_type(self):
        built = {"packages": [
            {"package": "foot", "region": "a", "laneCount": 10,
             "file": "packages/foot-a.tbpack"},
            {"package": "foot", "region": "b", "laneCount": 900,
             "file": "packages/foot-b.tbpack"},
            {"package": "motor", "region": "a", "laneCount": 5,
             "file": "packages/motor-a.tbpack"},
        ]}
        chosen = packages_to_open(built)
        foot = [p for p in chosen if p["package"] == "foot"]
        self.assertEqual([p["laneCount"] for p in foot], [900])


class TheWaysDatasetIsOpened(unittest.TestCase):
    """After the pivot the rideable packages are called `ways`, not `motor`.

    The first byways-only build was refused with "no motor packages were
    built" and nothing was opened: the gate named the retired partition.
    """

    WAYS = {"packages": [
        {"package": "ways", "region": r, "laneCount": 2000,
         "file": "packages/ways-%s.tbpack" % r}
        for r in ("midlands", "north", "wales")]}

    def test_every_ways_package_is_opened(self):
        chosen = packages_to_open(self.WAYS)
        self.assertEqual(len(chosen), 3, "every ways area, not a sample")

    def test_a_ways_build_is_not_refused_for_having_no_motor(self):
        problems = []
        where = tempfile.mkdtemp()
        key = os.path.join(where, "throwaway.key")
        with open(key, "w", encoding="utf8") as fh:
            fh.write(base64.b64encode(bytes(32)).decode("ascii"))
        check_packages_readable(self.WAYS, where, key, problems)
        self.assertFalse([p for p in problems if "no rideable" in p
                          or "no motor" in p], problems)
        # THE PREMISE: it went on to look for the ways packs themselves, so
        # this is not passing by returning early. (The files do not exist.)
        self.assertTrue([p for p in problems if "ways-midlands" in p],
                        problems)

    def test_a_build_with_nothing_rideable_is_still_refused(self):
        problems = []
        check_packages_readable(
            {"packages": [{"package": "foot", "region": "a",
                           "laneCount": 9, "file": "packages/foot-a.tbpack"}]},
            tempfile.mkdtemp(), "unused", problems)
        self.assertTrue([p for p in problems if "no rideable" in p], problems)


class TheAuthorityFloorStillFires(unittest.TestCase):
    """An existing gate, on its existing case: a half-down source.

    MIN_AUTHORITIES has never had a test. It is the check standing between a
    fetch that reached 80 councils and a map with a third of the country
    missing, so it gets one here rather than being taken on trust.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        for root, dirs, files in os.walk(self.dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.dir)

    def _cache(self, answered, known=150):
        codes = ["auth-%03d" % i for i in range(known)]
        with open(os.path.join(self.dir, "authorities.json"), "w",
                  encoding="utf8") as fh:
            json.dump(codes, fh)
        for code in codes[:answered]:
            folder = os.path.join(self.dir, code)
            os.makedirs(folder)
            with open(os.path.join(folder, "rows.json"), "w",
                      encoding="utf8") as fh:
                fh.write("[1]")

    def test_a_half_down_source_is_refused(self):
        self._cache(answered=139)
        problems = []
        check_authorities(self.dir, problems)
        self.assertTrue(problems)
        self.assertIn("139 of 150", problems[0])

    def test_a_full_fetch_is_not(self):
        self._cache(answered=141)
        problems = []
        check_authorities(self.dir, problems)
        self.assertEqual(problems, [])

    def test_no_cache_at_all_is_refused(self):
        problems = []
        check_authorities(os.path.join(self.dir, "nothing"), problems)
        self.assertTrue(problems)

    def test_an_empty_file_does_not_count_as_an_answer(self):
        self._cache(answered=150)
        for name in os.listdir(self.dir):
            folder = os.path.join(self.dir, name)
            if os.path.isdir(folder):
                open(os.path.join(folder, "rows.json"), "w").close()
        problems = []
        check_authorities(self.dir, problems)
        self.assertTrue(problems, "an empty file is not data")


class TheCommittedBaseline(unittest.TestCase):
    """tools/build_baseline.json ITSELF, against the build it has to admit.

    Every other rebaseline test writes its own record, so none of them could
    notice the committed one predicting the wrong build. It did: it was
    recorded at 31,369 rows for step 1.2c's 'near' set, and the owner then
    chose byways only (2026-09-24) - a build of ~12,702 rows, 60% short of
    that prediction and so refused by MAX_REBASELINE_SHORTFALL as a collapsed
    fetch. The decided cutover would not have published.
    """

    PATH = os.path.join(HERE, "build_baseline.json")

    def _record(self):
        with open(self.PATH, encoding="utf8") as fh:
            return json.load(fh)

    def _check(self, new):
        record = self._record()
        # The published build it supersedes, rebuilt from its own figures so
        # that load_baseline finds the record still applies.
        old = manifest(**record["supersedes"]["packages"])
        problems, becomes = [], []
        dropped = load_baseline(self.PATH, old, problems, becomes)
        with contextlib.redirect_stdout(io.StringIO()):
            check_totals(old, new, problems, dropped,
                         becomes[0] if becomes else None)
        return dropped, problems

    def test_it_still_applies_to_the_published_build(self):
        record = self._record()
        self.assertEqual(sum(record["supersedes"]["packages"].values()),
                         record["supersedes"]["total"])
        dropped, _ = self._check(manifest(ways=12702))
        self.assertEqual(dropped,
                         frozenset(["bicycle", "foot", "horse", "motor"]))

    def test_it_predicts_the_byways_only_build(self):
        # 12,702 BOAT rows across overlapping regions, plus 0 osm_track rows
        # (none is sourced yet). docs/DECISIONS-1.2.md has the derivation.
        record = self._record()
        self.assertEqual(record["becomes"],
                         {"packages": {"ways": 12702}, "total": 12702})
        self.assertEqual(
            record["nationalDrop"],
            round((record["supersedes"]["total"] - 12702)
                  / float(record["supersedes"]["total"]), 4))
        self.assertIn("2026-09-24", record["reason"])
        self.assertIn("Byways only".lower(), record["reason"].lower())

    def test_the_decided_byways_only_cutover_publishes(self):
        _, problems = self._check(manifest(ways=12702))
        self.assertEqual(problems, [],
                         "the owner's byways-only build must publish")

    def test_a_collapsed_fetch_is_still_refused(self):
        # The guard must not have been loosened to let the smaller build in.
        _, problems = self._check(manifest(ways=9000))
        self.assertTrue(any("collapsed fetch" in p for p in problems),
                        problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)


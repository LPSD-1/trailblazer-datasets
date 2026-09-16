#!/usr/bin/env python3
"""The guard that decides whether a build is safe to publish.

    python tools/test_check_build.py

THE HOLE THIS FIXES, measured rather than imagined. The national total is
dominated by footpaths - 635,242 of 875,827 ways on the build published on
10 September 2026 - and byways open to all traffic, the only ones a rider may
legally ride and the entire reason this app exists, were 11,851 of it. That is
1.35%, against a national threshold of 2%.

So every byway in Great Britain could have disappeared and this check would
have passed.

It was not hypothetical. On 16 September the motor total came back 10,420
against 11,851 published: 12.1% of every rideable byway in the country, gone,
reading here as a 0.16% national movement. Nothing in check_build.py objected.
What caught it was the ready-made trips builder, by accident, when five trip
anchors could no longer find a byway to snap to - and the issue it raised named
five Welsh and northern villages, which sends the next reader off to correct
trip anchors that were never wrong.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_build import check_totals, lane_totals  # noqa: E402


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

    def test_a_twelve_percent_byway_loss_is_now_refused(self):
        # The real run: 11,851 -> 10,420.
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
        # BETWEEN a total collapse and a 2% national wobble, which is where the
        # real 12.1% loss sat.
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

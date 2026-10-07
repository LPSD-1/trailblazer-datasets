#!/usr/bin/env python3
"""A council's record lands on the right byway, and only that one.

    python tools/test_byway_match.py

The two ways a record can land on the wrong lane are the two that matter:
a road that crosses or runs beside a byway taking the byway's closure, and a
council's path numbering failing to line up with ours. Synthetic ways in
metres around a point in Essex; no containers, no network.
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from byway_match import (Byways, Way, norm_number, norm_parish,  # noqa: E402
                         split_name)

LAT0, LON0 = 51.9454, 0.2744


def deg(points_m):
    """(east, north) metres from the origin to (lon, lat)."""
    kx = 111320.0 * math.cos(math.radians(LAT0))
    return [(LON0 + e / kx, LAT0 + n / 110574.0) for e, n in points_m]


def way(uid, points_m, authority="Essex",
        name="Byway open to all traffic (BOAT) Debden 75"):
    return Way(uid, authority, name, [deg(points_m)])


class Geometry(unittest.TestCase):
    def setUp(self):
        self.byways = Byways([
            # A 2 km byway running east.
            way("EX-75", [(0, 0), (2000, 0)]),
            # Another council's byway 30 m north of it, parallel.
            way("HD-9", [(0, 30), (2000, 30)], authority="Hertfordshire",
                name="Byway open to all traffic (BOAT) ARDELEY 009"),
            # A byway a kilometre away.
            way("EX-63", [(0, 1000), (500, 1000)],
                name="Byway open to all traffic (BOAT) Debden 63"),
        ])

    def test_a_closure_of_part_of_a_byway_matches_it(self):
        got = self.byways.match_geometry([deg([(300, 5), (500, 8)])],
                                         authorities={"Essex"})
        self.assertEqual([g[0] for g in got], ["EX-75"])

    def test_a_road_crossing_the_byway_does_not_take_its_closure(self):
        crossing = deg([(1000, -400), (1000, 400)])
        self.assertEqual(self.byways.match_geometry([crossing]), [])

    def test_a_line_drawn_40_m_away_is_not_on_the_byway(self):
        beside = deg([(200, -40), (1800, -40)])
        self.assertEqual(self.byways.match_geometry(
            [beside], authorities={"Essex"}), [])

    def test_a_council_never_closes_its_neighbours_byway(self):
        # Within 25 m of BOTH lines; only Essex's may be closed by Essex.
        got = self.byways.match_geometry([deg([(100, 15), (900, 15)])],
                                         authorities={"Essex"})
        self.assertEqual([g[0] for g in got], ["EX-75"])

    def test_a_long_order_does_not_take_a_stub_it_only_touches(self):
        byways = Byways([way("EX-1", [(0, 0), (40, 0)]),
                         way("EX-2", [(40, 0), (40, 3000)])])
        # A 3 km order along EX-2 that starts at EX-1's end.
        got = byways.match_geometry([deg([(40, 0), (40, 3000)])])
        self.assertEqual([g[0] for g in got], ["EX-2"])

    def test_a_point_on_the_byway_matches_it(self):
        got = self.byways.match_geometry([deg([(700, 10)])],
                                         authorities={"Essex"})
        self.assertEqual([g[0] for g in got], ["EX-75"])


class References(unittest.TestCase):
    def test_numbers_compare_without_leading_zeros_or_a_zero_suffix(self):
        self.assertEqual(norm_number("024/0"), "24")
        self.assertEqual(norm_number("BOAT 34"), "34")
        self.assertEqual(norm_number("13A"), "13a")
        self.assertEqual(norm_number("10/33/1"), "10/33/1")
        self.assertNotEqual(norm_number("14"), norm_number("140"))

    def test_parishes_compare_by_their_letters(self):
        self.assertEqual(norm_parish("Abbess Beauchamp & Berners Roding"),
                         norm_parish("ABBESS BEAUCHAMP AND BERNERS RODING"))
        self.assertEqual(norm_parish("Leigh CP"), norm_parish("Leigh"))
        self.assertNotEqual(norm_parish("Little Henny"),
                            norm_parish("Great Henny"))

    def test_a_way_name_splits_into_parish_and_number(self):
        self.assertEqual(split_name(
            "Byway open to all traffic (BOAT) Debden 75"), ("debden", "75"))
        self.assertEqual(split_name(
            "Byway open to all traffic (BOAT) WS 10/33/1"), ("ws", "10/33/1"))
        self.assertEqual(split_name(
            "Byway open to all traffic (BOAT) 538 054"), ("538", "54"))

    def test_a_reference_matches_only_its_own_authority(self):
        byways = Byways([
            way("EX-75", [(0, 0), (100, 0)]),
            way("XX-75", [(0, 0), (100, 0)], authority="Suffolk"),
        ])
        self.assertEqual(byways.match_ref("Essex", "DEBDEN", "075"),
                         ["EX-75"])
        self.assertEqual(byways.match_ref("Essex", "Debden", "7"), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)

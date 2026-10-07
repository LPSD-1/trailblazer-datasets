#!/usr/bin/env python3
"""A council's order reaches the pack in the shape the app reads, dated
right, and is never counted twice or folded into something it is not.

    python tools/test_council_orders.py

Pure: council_orders.py takes a day and returns features. The cases are the
ones that would put a wrong picture on a rider's map - a seasonal ban drawn
all year, a permanent ban that leaves the map when an unrelated one-day road
closure ends, a closure folded into a dot on a crossing road.
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_orders as co  # noqa: E402

SOURCE = {"id": "essex-prow-tros", "name": "Essex County Council - PRoW "
          "traffic regulation orders", "kind": "council-layer"}
LAT0, LON0 = 51.9454, 0.2744


def line(points_m):
    kx = 111320.0 * math.cos(math.radians(LAT0))
    return {"type": "LineString",
            "coordinates": [[LON0 + e / kx, LAT0 + n / 110574.0]
                            for e, n in points_m]}


def item(**kw):
    base = {"id": "a", "authority": "Essex", "ref": "Debden Byway 75",
            "title": "Essex County Council: Prohibition of Driving Order",
            "where": "Byway 75, Debden", "vehicles": "motor_vehicles",
            "form": "permanent", "url": "https://example.gov.uk/order.pdf",
            "ways": ["EX-75"], "geometry": line([(0, 0), (800, 0)])}
    base.update(kw)
    return base


def tra(_authority):
    return "1585"


class Seasons(unittest.TestCase):
    WINTER = {"from": "10-01", "to": "04-30"}

    def test_in_season_it_is_the_current_season(self):
        self.assertEqual(co.season_dates(self.WINTER, "2026-10-07"),
                         ("2026-10-01", "2027-04-30"))
        self.assertEqual(co.season_dates(self.WINTER, "2027-02-01"),
                         ("2026-10-01", "2027-04-30"))

    def test_the_last_day_is_still_in_season(self):
        self.assertEqual(co.season_dates(self.WINTER, "2027-04-30"),
                         ("2026-10-01", "2027-04-30"))

    def test_out_of_season_and_far_off_it_is_nothing(self):
        self.assertIsNone(co.season_dates(self.WINTER, "2026-05-01"))

    def test_out_of_season_but_inside_the_horizon_it_is_the_next(self):
        self.assertEqual(co.season_dates(self.WINTER, "2026-09-01"),
                         ("2026-10-01", "2027-04-30"))

    def test_a_summer_season_does_not_wrap(self):
        self.assertEqual(co.season_dates({"from": "05-01", "to": "09-30"},
                                         "2026-06-15"),
                         ("2026-05-01", "2026-09-30"))


class Features(unittest.TestCase):
    def test_it_carries_every_property_the_app_reads(self):
        f = co.feature_of(item(), SOURCE, "2026-10-07", tra_of=tra)
        p = f["properties"]
        for key in ("code", "label", "otype", "name", "where", "ref", "tra",
                    "tro_uid"):
            self.assertIn(key, p)
        self.assertEqual(p["code"], "movementOrderProhibitedAccess")
        self.assertEqual(p["otype"], "prohibition")
        self.assertNotIn("oform", p, "permanent carries no oform, as D-TRO")
        self.assertEqual(p["tra"], "1585")
        self.assertEqual(p["source"], "essex-prow-tros")
        self.assertIn("Essex County Council", p["source_name"])
        self.assertEqual(p["url"], "https://example.gov.uk/order.pdf")
        self.assertEqual(p["ways"], ["EX-75"])
        self.assertEqual(f["geometry"]["type"], "LineString")

    def test_the_uid_is_stable_and_differs_from_any_dtro_one(self):
        a = co.feature_of(item(), SOURCE, "2026-10-07")
        b = co.feature_of(item(), SOURCE, "2026-10-08")
        self.assertEqual(a["properties"]["tro_uid"],
                         b["properties"]["tro_uid"])
        self.assertTrue(a["properties"]["tro_uid"].startswith("c"))

    def test_a_seasonal_order_is_dated_by_its_season(self):
        f = co.feature_of(item(form="seasonal",
                               season={"from": "11-01", "to": "03-31"}),
                          SOURCE, "2026-12-01")
        p = f["properties"]
        self.assertEqual((p["start"], p["end"]), ("2026-11-01", "2027-03-31"))
        self.assertEqual(p["oform"], "seasonal")

    def test_a_seasonal_order_with_no_season_is_not_published(self):
        # Undated, it would be drawn shut all year.
        self.assertIsNone(co.feature_of(item(form="seasonal"), SOURCE,
                                        "2026-12-01"))

    def test_a_seasonal_order_that_was_revoked_stays_off(self):
        self.assertIsNone(co.feature_of(
            item(form="seasonal", season={"from": "11-01", "to": "03-31"},
                 end="2022-08-15"), SOURCE, "2026-12-01"))

    def test_a_finished_closure_is_dropped_and_a_live_one_kept(self):
        self.assertIsNone(co.feature_of(
            item(form="temporary", vehicles="all_users", start="2026-01-01",
                 end="2026-10-06"), SOURCE, "2026-10-07"))
        live = co.feature_of(
            item(form="temporary", vehicles="all_users", start="2026-01-01",
                 end="2026-10-07"), SOURCE, "2026-10-07")
        self.assertEqual(live["properties"]["code"], "miscRoadClosure")
        self.assertEqual(live["properties"]["oform"], "seasonal")

    def test_motorcycles_exempt_is_its_own_type_never_a_plain_ban(self):
        f = co.feature_of(item(vehicles="motor_vehicles_except_motorcycles"),
                          SOURCE, "2026-10-07")
        self.assertEqual(f["properties"]["otype"],
                         "motors_except_motorcycles")
        self.assertNotEqual(f["properties"]["code"],
                            "movementOrderProhibitedAccess")

    def test_a_width_limit_says_its_width(self):
        f = co.feature_of(item(vehicles="vehicles_over_width", width_m=1.6),
                          SOURCE, "2026-10-07")
        self.assertEqual(f["properties"]["label"], "Width limit 1.6 m")
        self.assertEqual(f["properties"]["otype"], "width")


def dtro(geometry, **props):
    base = {"code": "miscRoadClosure", "label": "Road closed",
            "otype": "prohibition", "tro_uid": "d1", "tra": 1585}
    base.update(props)
    return {"type": "Feature", "geometry": geometry, "properties": base}


def council(day="2026-10-07", **kw):
    return co.feature_of(item(**kw), SOURCE, day, tra_of=tra)


def merge(dtros, councils):
    return co.merge_council(dtros, [(c, "council-layer") for c in councils],
                            lambda k: co.PRECEDENCE.get(k, 9))


class Dedupe(unittest.TestCase):
    def test_the_same_closure_in_dtro_is_folded_into_it(self):
        mine = council(form="temporary", vehicles="all_users",
                       start="2026-10-01", end="2026-12-01")
        theirs = dtro(line([(0, 3), (800, 3)]), oform="seasonal",
                      start="2026-10-02", end="2026-12-10")
        added, folded = merge([theirs], [mine])
        self.assertEqual((added, folded), ([], 1))
        also = theirs["properties"]["also"]
        self.assertEqual(also[0]["source"], "essex-prow-tros")
        self.assertEqual(also[0]["url"], "https://example.gov.uk/order.pdf")

    def test_a_permanent_ban_is_not_folded_into_a_one_day_closure(self):
        # Measured on the published pack: "License (Other) on The Green",
        # one day in November, on Suffolk's Shop Drove ban.
        mine = council()
        theirs = dtro(line([(0, 3), (800, 3)]), oform="seasonal",
                      start="2026-11-30", end="2026-11-30")
        added, folded = merge([theirs], [mine])
        self.assertEqual((len(added), folded), (1, 0))

    def test_a_point_on_a_crossing_road_never_takes_a_closure(self):
        mine = council(form="temporary", vehicles="all_users",
                       start="2026-10-01", end="2026-12-01")
        dot = dtro({"type": "Point", "coordinates":
                    line([(400, 0)])["coordinates"][0]}, oform="seasonal",
                   start="2026-10-01", end="2026-12-01")
        added, folded = merge([dot], [mine])
        self.assertEqual((len(added), folded), (1, 0))

    def test_another_councils_order_is_never_this_one(self):
        mine = council(form="temporary", vehicles="all_users",
                       start="2026-10-01", end="2026-12-01")
        theirs = dtro(line([(0, 3), (800, 3)]), oform="seasonal",
                      start="2026-10-01", end="2026-12-01", tra=1900)
        self.assertEqual(merge([theirs], [mine])[1], 0)

    def test_a_width_limit_is_not_the_same_order_as_a_closure(self):
        mine = council(vehicles="vehicles_over_width", width_m=1.6)
        theirs = dtro(line([(0, 3), (800, 3)]))
        self.assertEqual(merge([theirs], [mine])[1], 0)

    def test_one_sources_byways_meeting_end_to_end_both_stay(self):
        a = council(id="a", ways=["EX-1"], geometry=line([(0, 0), (60, 0)]))
        b = council(id="b", ways=["EX-2"],
                    geometry=line([(0, 0), (100, 0)]))
        added, folded = merge([], [a, b])
        self.assertEqual((len(added), folded), (2, 0))

    def test_two_sources_for_one_order_keep_the_better(self):
        layer = council(id="layer")
        page = council(id="page")
        page["properties"]["source"] = "essex-register"
        added, folded = co.merge_council(
            [], [(page, "register"), (layer, "council-layer")],
            lambda k: co.PRECEDENCE.get(k, 9))
        self.assertEqual(folded, 1)
        self.assertEqual(added[0]["properties"]["source"], "essex-prow-tros")
        self.assertEqual(added[0]["properties"]["also"][0]["source"],
                         "essex-register")


if __name__ == "__main__":
    unittest.main(verbosity=1)

#!/usr/bin/env python3
"""What survives the filter, and what must not.

    python tools/test_tro.py

The record shapes here are taken from the real national corpus, trimmed to the
fields the code reads. Every one of these is a decision that changes what a
rider sees on a lane, so each gets a test that would fail if the decision were
quietly reversed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tro import (  # noqa: E402
    expired, features, is_placeholder, kind_of, not_yet, worth_hydrating)

TODAY = "2026-09-14"

LINE = "SRID=27700;LINESTRING(464946.33 293262.84, 464918.98 293239.27)"
DIVERSION = "SRID=27700;LINESTRING(464958.4 293262.37, 464747.25 293078.68)"


def _record(regulation, places=None, name="THE TEST ORDER 2026"):
    return {
        "source": {
            "troName": name,
            "reference": "149816802",
            "traCreator": 2460,
            "currentTraOwner": 2460,
            "provision": [{
                "reference": "149816802/44819603",
                "regulation": [regulation],
                "regulatedPlace": places if places is not None else [{
                    "type": "regulationLocation",
                    "description": "Victoria Street",
                    "linearGeometry": {"linestring": LINE},
                }],
            }],
        }
    }


def _closure(start="2026-06-23T09:48:11", end="2026-06-25T18:00:00"):
    condition = {"timeValidity": {"start": start}}
    if end is not None:
        condition["timeValidity"]["end"] = end
    return {
        "timeZone": "Europe/London",
        "condition": [condition],
        "generalRegulation": {"regulationType": "miscRoadClosure"},
    }


class WhatIsKept(unittest.TestCase):
    def test_a_live_closure_survives(self):
        got = features(_record(_closure(end="2026-12-01T00:00:00")),
                       today=TODAY)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["code"], "miscRoadClosure")
        self.assertEqual(got[0]["label"], "Road closed")
        self.assertEqual(got[0]["wkt"], LINE)
        self.assertEqual(got[0]["where"], "Victoria Street")
        self.assertEqual(got[0]["tra"], 2460)

    def test_an_expired_one_does_not(self):
        # Two thirds of the corpus. Nothing removes a finished closure, so
        # without this the app tells riders about roadworks from last year.
        got = features(_record(_closure(end="2026-06-25T18:00:00")),
                       today=TODAY)
        self.assertEqual(got, [])

    def test_an_order_ending_today_is_still_on_today(self):
        # The boundary that decides whether somebody is told about the closure
        # they are riding towards this afternoon.
        got = features(_record(_closure(end=TODAY + "T18:00:00")),
                       today=TODAY)
        self.assertEqual(len(got), 1)

    def test_an_open_ended_order_survives_forever(self):
        got = features(_record(_closure(end=None)), today=TODAY)
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["end"])

    def test_the_widest_window_wins(self):
        # One condition ends, another does not. In practice the order is
        # open-ended, and saying it had expired is the worse mistake.
        regulation = {
            "condition": [
                {"timeValidity": {"start": "2026-01-01", "end": "2026-02-01"}},
                {"timeValidity": {"start": "2026-01-01"}},
            ],
            "generalRegulation": {"regulationType": "miscRoadClosure"},
        }
        got = features(_record(regulation), today=TODAY)
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["end"])

    def test_keep_expired_is_available_for_auditing(self):
        got = features(_record(_closure(end="2020-01-01T00:00:00")),
                       today=TODAY, keep_expired=True)
        self.assertEqual(len(got), 1)


class WhatIsDropped(unittest.TestCase):
    def test_a_placeholder_is_not_a_restriction(self):
        # The authority has reserved a slot for an order that does not exist
        # yet. Drawing it says a road is shut when it is open.
        regulation = {
            "condition": [{"timeValidity": {
                "start": "2026-01-01", "isPlaceholderTro": True}}],
            "generalRegulation": {"regulationType": "miscRoadClosure"},
        }
        self.assertTrue(is_placeholder(regulation))
        self.assertEqual(features(_record(regulation), today=TODAY), [])

    def test_kerbside_parking_is_not_a_rider_s_problem(self):
        # 15,129 "no waiting" and 6,513 "no stopping" in the corpus. Carrying
        # them would multiply the pack to give a bay-by-bay account of town
        # centres that the app has no way to use.
        for code in ("kerbsideNoWaiting", "kerbsideNoStopping",
                     "kerbsidePermitParkingPlace", "kerbsideTaxiRank"):
            regulation = {
                "condition": [{"timeValidity": {"start": "2026-01-01"}}],
                "generalRegulation": {"regulationType": code},
            }
            self.assertEqual(features(_record(regulation), today=TODAY), [],
                             "%s should not be carried" % code)

    def test_a_diversion_route_is_not_drawn_as_a_closure(self):
        # A diversion is where traffic is SENT. Drawn in the same colour it
        # puts "road closed" along a road that is open, which is the exact
        # opposite of what it means.
        got = features(_record(_closure(end=None), places=[
            {"type": "diversionRoute", "description": "Saddington Road",
             "linearGeometry": {"linestring": DIVERSION}},
        ]), today=TODAY)
        self.assertEqual(got, [])

    def test_a_regulation_with_no_geometry_is_not_a_place(self):
        got = features(_record(_closure(end=None), places=[
            {"type": "regulationLocation", "description": "Somewhere"},
        ]), today=TODAY)
        self.assertEqual(got, [])


class SizeAndWeight(unittest.TestCase):
    """Few of these exist, and every one is worth knowing before committing."""

    def test_a_weight_limit_is_kept(self):
        regulation = {
            "condition": [{"timeValidity": {"start": "2026-01-01"}}],
            "generalRegulation": {
                "regulationType": "dimensionMaximumWeightStructural"},
        }
        got = features(_record(regulation), today=TODAY)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["label"], "Weight limit")

    def test_a_height_limit_is_kept(self):
        regulation = {
            "condition": [{"timeValidity": {"start": "2026-01-01"}}],
            "generalRegulation": {
                "regulationType": "dimensionMaximumHeightStructural"},
        }
        self.assertEqual(len(features(_record(regulation), today=TODAY)), 1)


class SpeedLimits(unittest.TestCase):
    def test_a_speed_limit_carries_its_value(self):
        regulation = {
            "condition": [{"timeValidity": {"start": "2026-01-01"}}],
            "speedLimitValueBased": {
                "type": "maximumSpeedLimit", "mphValue": 40},
        }
        code, label = kind_of(regulation)
        self.assertEqual(code, "speedLimitValueBased")
        self.assertEqual(label, "40 mph")
        got = features(_record(regulation), today=TODAY)
        self.assertEqual(got[0]["label"], "40 mph")

    def test_a_speed_limit_with_no_value_says_nothing(self):
        # Better silent than a sign with no number on it.
        regulation = {
            "condition": [{"timeValidity": {"start": "2026-01-01"}}],
            "speedLimitValueBased": {"type": "maximumSpeedLimit"},
        }
        self.assertEqual(kind_of(regulation), (None, None))


class ManyPlaces(unittest.TestCase):
    def test_one_order_over_three_stretches_is_three_features(self):
        # Routine in the corpus: one order, several stretches of road. Each is
        # a separate thing to draw and a separate thing to be inside.
        places = [
            {"type": "regulationLocation", "description": "A",
             "linearGeometry": {"linestring": LINE}},
            {"type": "regulationLocation", "description": "B",
             "linearGeometry": {"linestring": LINE}},
            {"type": "diversionRoute", "description": "Diversion",
             "linearGeometry": {"linestring": DIVERSION}},
            {"type": "regulationLocation", "description": "C",
             "pointGeometry": {"point": "SRID=27700;POINT(464946 293262)"}},
        ]
        got = features(_record(_closure(end=None), places=places), today=TODAY)
        self.assertEqual([f["where"] for f in got], ["A", "B", "C"])


class MalformedInput(unittest.TestCase):
    """The corpus carries more than one schema version, and it shows.

    Measured: 132 records whose `source` is not an object at all. A survey that
    assumed the common shape crashed on the first one and reported nothing.
    """

    def test_nothing_here_raises(self):
        for bad in (None, [], "", 42, {}, {"source": "not an object"},
                    {"source": {"provision": "not a list"}},
                    {"source": {"provision": [{"regulation": "no"}]}},
                    {"source": {"provision": [{"regulation": [None]}]}}):
            self.assertEqual(features(bad, today=TODAY), [],
                             "%r should be skipped, not crash" % (bad,))

    def test_a_single_provision_may_arrive_unwrapped(self):
        record = _record(_closure(end=None))
        record["source"]["provision"] = record["source"]["provision"][0]
        self.assertEqual(len(features(record, today=TODAY)), 1)


class NotYetInForce(unittest.TestCase):
    """Nearly half the live corpus has not started yet.

    Measured: 17,511 of 36,821, running out to 2036. An order starting next
    week is worth carrying; one starting in 2028 is weight in a pack rebuilt
    every day.
    """

    def test_next_week_is_carried(self):
        got = features(_record(_closure(start="2026-09-21T00:00:00",
                                        end=None)), today=TODAY)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["start"], "2026-09-21")

    def test_and_keeps_its_start_date_so_the_app_can_say_so(self):
        # Drawn as a closure that is already in force, this would be a lie
        # about a road that is open. The date is what lets the app say "from
        # Monday" instead.
        got = features(_record(_closure(start="2026-10-01T00:00:00",
                                        end=None)), today=TODAY)
        self.assertEqual(got[0]["start"], "2026-10-01")

    def test_two_years_out_is_not(self):
        got = features(_record(_closure(start="2028-01-01T00:00:00",
                                        end=None)), today=TODAY)
        self.assertEqual(got, [])

    def test_the_horizon_boundary(self):
        self.assertFalse(not_yet(None, TODAY))
        self.assertFalse(not_yet("2026-09-14", TODAY))
        self.assertFalse(not_yet("2026-12-13", TODAY))  # exactly 90 days
        self.assertTrue(not_yet("2026-12-14", TODAY))   # one day past

    def test_an_already_running_order_is_never_caught_by_the_horizon(self):
        got = features(_record(_closure(start="2020-01-01T00:00:00",
                                        end=None)), today=TODAY)
        self.assertEqual(len(got), 1)


class Expiry(unittest.TestCase):
    def test_the_boundary(self):
        self.assertFalse(expired(None, TODAY))
        self.assertFalse(expired(TODAY, TODAY))
        self.assertFalse(expired("2026-09-15", TODAY))
        self.assertTrue(expired("2026-09-13", TODAY))


class WhichChangesAreWorthFetching(unittest.TestCase):
    """1,118 orders changed on 14 September alone, and the service rate-limits.

    An event carries the order's types and dates but not its geometry, so
    every order kept costs a request. Rejecting from the event alone is worth
    more than any amount of pacing - but a maybe must always be fetched,
    because missing a real closure to save a request is the wrong way round.
    """

    def test_a_closure_is_fetched(self):
        self.assertTrue(worth_hydrating(
            {"eventType": "update", "regulationType": ["miscRoadClosure"]},
            TODAY))

    def test_parking_is_not(self):
        self.assertFalse(worth_hydrating(
            {"eventType": "update", "regulationType": ["kerbsideNoWaiting"]},
            TODAY))

    def test_an_order_that_is_partly_interesting_is_fetched(self):
        # One order can carry several regulations. If ANY of them is one we
        # carry, the order has to be fetched.
        self.assertTrue(worth_hydrating(
            {"eventType": "update",
             "regulationType": ["kerbsideNoWaiting", "miscRoadClosure"]},
            TODAY))

    def test_an_order_that_has_already_ended_is_not_fetched(self):
        self.assertFalse(worth_hydrating(
            {"eventType": "update", "regulationType": ["miscRoadClosure"],
             "regulationEnd": ["2026-08-01T00:00:00"]}, TODAY))

    def test_one_that_ends_today_still_is(self):
        self.assertTrue(worth_hydrating(
            {"eventType": "update", "regulationType": ["miscRoadClosure"],
             "regulationEnd": [TODAY + "T18:00:00"]}, TODAY))

    def test_one_that_starts_years_out_is_not(self):
        self.assertFalse(worth_hydrating(
            {"eventType": "update", "regulationType": ["miscRoadClosure"],
             "regulationStart": ["2029-01-01T00:00:00"]}, TODAY))

    def test_a_deletion_is_always_acted_on(self):
        # Nothing to fetch, but the order has to come OUT of the pack. Treated
        # as worth handling so a caller looping over events cannot skip it.
        self.assertTrue(worth_hydrating({"eventType": "delete"}, TODAY))

    def test_an_event_with_no_type_is_a_maybe_and_maybes_are_fetched(self):
        self.assertTrue(worth_hydrating({"eventType": "create"}, TODAY))
        self.assertTrue(worth_hydrating(
            {"eventType": "create", "regulationType": []}, TODAY))

    def test_rubbish_is_not_fetched(self):
        for bad in (None, [], "", 7):
            self.assertFalse(worth_hydrating(bad, TODAY))


if __name__ == "__main__":
    unittest.main(verbosity=2)

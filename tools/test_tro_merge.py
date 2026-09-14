#!/usr/bin/env python3
"""Merging a day's changes into what is already published.

    python tools/test_tro_merge.py

The cases that matter are the ones where something has to LEAVE the pack.
Adding is easy and fails loudly; removing is quiet, and a merge that cannot
remove leaves last month's roadworks on the map for ever.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tro_merge import index_by_order, is_current, merge  # noqa: E402

TODAY = "2026-09-14"


def _f(uid, dtro, end=None, start=None, label="Road closed"):
    props = {"tro_uid": uid, "dtro": dtro, "label": label}
    if end is not None:
        props["end"] = end
    if start is not None:
        props["start"] = start
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-1.0, 53.0]},
            "properties": props}


class Grouping(unittest.TestCase):
    def test_features_group_by_their_order(self):
        grouped = index_by_order([_f("a", "A"), _f("b", "A"), _f("c", "B")])
        self.assertEqual(sorted(grouped), ["A", "B"])
        self.assertEqual(len(grouped["A"]), 2)

    def test_a_feature_with_no_order_id_keeps_its_own_identity(self):
        # From before ids were carried. It must not collapse together with
        # every other id-less feature and be overwritten by one of them.
        grouped = index_by_order([
            {"properties": {"tro_uid": "x"}},
            {"properties": {"tro_uid": "y"}},
        ])
        self.assertEqual(len(grouped), 2)


class Adding(unittest.TestCase):
    def test_a_new_order_appears(self):
        got = merge([_f("a", "A")], {"B": [_f("b", "B")]})
        self.assertEqual([f["properties"]["tro_uid"] for f in got], ["a", "b"])

    def test_the_result_is_sorted_so_an_unchanged_day_rebuilds_identically(self):
        got = merge([_f("z", "Z"), _f("a", "A")], {})
        self.assertEqual([f["properties"]["tro_uid"] for f in got], ["a", "z"])


class Removing(unittest.TestCase):
    def test_a_deleted_order_goes_completely(self):
        got = merge([_f("a", "A"), _f("b", "A"), _f("c", "B")],
                    {}, deleted=["A"])
        self.assertEqual([f["properties"]["tro_uid"] for f in got], ["c"])

    def test_an_order_amended_to_nothing_goes_too(self):
        # It changed, and now contributes nothing: expired, or amended into a
        # kind we do not carry. The empty list is the signal.
        got = merge([_f("a", "A"), _f("c", "B")], {"A": []})
        self.assertEqual([f["properties"]["tro_uid"] for f in got], ["c"])

    def test_an_amended_order_that_shrinks_loses_its_old_stretches(self):
        # THE ONE THAT LEAVES GHOSTS. An order covering three stretches is
        # amended to cover one. A merge that only overwrites what it
        # recognises keeps the other two on the map for ever.
        published = [_f("a1", "A"), _f("a2", "A"), _f("a3", "A")]
        got = merge(published, {"A": [_f("a9", "A")]})
        self.assertEqual([f["properties"]["tro_uid"] for f in got], ["a9"])

    def test_expiry_removes_what_no_event_will_ever_announce(self):
        # An order does not get an event when it simply reaches its end date.
        # Without re-checking, the pack only ever grows.
        published = [_f("live", "A", end="2026-12-01"),
                     _f("over", "B", end="2026-09-13"),
                     _f("today", "C", end=TODAY)]
        got = merge(published, {},
                    still_valid=lambda f: is_current(f, TODAY))
        self.assertEqual([f["properties"]["tro_uid"] for f in got],
                         ["live", "today"])

    def test_an_order_that_drifts_past_the_horizon_is_not_dropped(self):
        # The horizon only ever moves FORWARD, so something inside it stays
        # inside it. This guards against an off-by-one that would drop live
        # orders every run.
        published = [_f("soon", "A", start="2026-10-01")]
        got = merge(published, {}, still_valid=lambda f: is_current(f, TODAY))
        self.assertEqual(len(got), 1)

    def test_but_something_far_out_is(self):
        published = [_f("later", "A", start="2029-01-01")]
        got = merge(published, {}, still_valid=lambda f: is_current(f, TODAY))
        self.assertEqual(got, [])


class Replacing(unittest.TestCase):
    def test_an_updated_order_replaces_only_its_own_features(self):
        published = [_f("a", "A"), _f("b", "B")]
        got = merge(published, {"A": [_f("a2", "A")]})
        self.assertEqual([f["properties"]["tro_uid"] for f in got],
                         ["a2", "b"])

    def test_a_delete_and_an_update_for_the_same_order_deletes(self):
        # The feed can report both in one window and the arguments do not say
        # which came first. Gone wins: continuing to publish an order the
        # authority has removed is worse than dropping one that comes back,
        # because the pack rebuilds within hours and it returns on its own.
        #
        # This test was written asserting the opposite of its own name, and
        # passed, because the code applied deletions first. Both were wrong.
        got = merge([_f("a", "A")], {"A": [_f("a2", "A")]}, deleted=["A"])
        self.assertEqual(got, [])


class StillCurrent(unittest.TestCase):
    def test_the_boundaries(self):
        self.assertTrue(is_current(_f("x", "A"), TODAY))
        self.assertTrue(is_current(_f("x", "A", end=TODAY), TODAY))
        self.assertFalse(is_current(_f("x", "A", end="2026-09-13"), TODAY))
        self.assertTrue(is_current(_f("x", "A", start="2026-12-13"), TODAY))
        self.assertFalse(is_current(_f("x", "A", start="2026-12-14"), TODAY))

    def test_a_full_timestamp_is_handled_not_just_a_date(self):
        feature = _f("x", "A", end="2026-09-13T23:59:59")
        self.assertFalse(is_current(feature, TODAY))


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""A council's voluntary closure is published as a REQUEST, never an order.

Owner's decision, 7 October 2026: a voluntary closure (Wiltshire's winter
requests to keep off a lane) is shown by the app as a request - its own
style, "Wiltshire Council asks riders not to use this lane" - and blocks
nothing for any vehicle. So the pack carries it under a type of its own,
`voluntary`, which binds nobody, and the switch that publishes Wiltshire's
stays OFF until app build 118 (the first that knows the type) is with
testers: an older build draws a type it does not know as a closure.

    python tools/test_voluntary_closures.py

No network. The register pages are tools/fixtures/wiltshire/, as saved on
7 October 2026.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_map_container  # noqa: E402
import build_tro  # noqa: E402
import council_orders  # noqa: E402
import council_sources as cs  # noqa: E402
import wiltshire_closures as w  # noqa: E402
from byway_match import Byways, Way  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "wiltshire")

#: Inside the winter the register dates both saved voluntary closures by:
#: 1 October 2025 to 30 April 2026.
IN_THE_WINTER = "2026-01-15"
#: The day the register was saved. Its voluntary closures still carry the
#: 2025-26 winter's dates, so on this day none is in its window.
SAVED = "2026-10-07"

SOURCE = {"id": "wiltshire-closures",
          "name": "Wiltshire Council - rights of way closures register"}


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


def clean(text):
    return cs.strip_personal(cs.clean_text(text or ""))


def candidate_for(row):
    entry = next(e for e in w.parse_results(fixture("result-boat.html"))
                 if e["row"] == row)
    found = w.parse_detail(fixture("detail-%s.html" % row.replace("$", "_")))
    return w.candidate(entry, found, cs.parse_date, cs.parse_season, clean)


def feature(item, day):
    item = dict(item, geometry={"type": "LineString",
                                "coordinates": [[-1.8, 51.4],
                                                [-1.79, 51.41]]})
    return council_orders.feature_of(item, SOURCE, day)


def wilts_way(uid, parish, number):
    return Way(uid, "Wiltshire", "Byway open to all traffic (BOAT) %s %s"
               % (parish, number), [[(-1.85, 51.42), (-1.84, 51.43)]])


def published(switch):
    return mock.patch.object(w, "PUBLISH_VOLUNTARY", switch)


class TheType(unittest.TestCase):
    """build_tro.ORDER_TYPES["voluntary"]: a request binds nobody."""

    def test_it_has_a_row_that_binds_neither_vehicle(self):
        row = build_tro.ORDER_TYPES["voluntary"]
        self.assertEqual((row["bike"], row["x4"]), ("no", "no"))
        self.assertIn("request", row["label"])
        self.assertIn("not an order", row["label"])

    def test_it_never_shuts_or_hides_a_way_in_any_form(self):
        for form in build_tro.ORDER_FORMS:
            got = build_tro.way_access([("voluntary", form)])
            self.assertEqual((got["motorbike_ok"], got["fourxfour_ok"]),
                             (1, 1), form)
            # Not evidence: evidence is what makes hiding a lane lawful.
            self.assertEqual(got["access_evidence"], "none", form)
            # But the reason says what is asked.
            self.assertIn("request", got["access_reason"], form)

    def test_it_leaves_a_real_order_on_the_same_way_as_it_was(self):
        both = build_tro.way_access([("voluntary", "seasonal"),
                                     ("width", "permanent")])
        width = build_tro.way_access([("width", "permanent")])
        self.assertEqual((both["motorbike_ok"], both["fourxfour_ok"],
                          both["access_evidence"]),
                         (width["motorbike_ok"], width["fourxfour_ok"],
                          width["access_evidence"]))

    def test_the_order_type_check_covers_it_and_passes(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(build_tro.check_order_types(), 0, out.getvalue())
        self.assertIn("voluntary", out.getvalue())

    def test_the_check_fails_if_a_council_type_has_no_row(self):
        import contextlib
        import io
        stray = dict(council_orders.VEHICLES,
                     invented=("councilInvented", "invented", "Invented"))
        with mock.patch.object(council_orders, "VEHICLES", stray), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build_tro.check_order_types(), 1)


class TheFeature(unittest.TestCase):
    """council_orders.feature_of for a voluntary item."""

    def item(self, **more):
        item = {"id": "X", "ref": "X/1/001", "vehicles": "voluntary",
                "form": "temporary", "start": "2025-10-01",
                "end": "2026-04-30", "asked_by": "Wiltshire Council",
                "title": "Wiltshire Council voluntary closure X/1/001"}
        item.update(more)
        return item

    def test_it_is_published_under_its_own_code_and_type(self):
        props = feature(self.item(), IN_THE_WINTER)["properties"]
        self.assertEqual((props["code"], props["otype"]),
                         ("councilVoluntaryClosure", "voluntary"))
        self.assertEqual(props["asked_by"], "Wiltshire Council")
        self.assertIn("not a legal order", props["label"])

    def test_who_is_asking_is_never_written_on_an_order(self):
        props = feature(self.item(vehicles="motor_vehicles"),
                        IN_THE_WINTER)["properties"]
        self.assertEqual(props["otype"], "prohibition")
        self.assertNotIn("asked_by", props)

    def test_the_orders_container_bakes_it_as_a_request_not_a_closure(self):
        # Its code says "Closure"; the container's kind must not.
        self.assertEqual(
            build_map_container.order_kind("councilVoluntaryClosure"),
            "request")
        self.assertEqual(build_map_container.order_kind("miscRoadClosure"),
                         "closure")


class WiltshiresRegister(unittest.TestCase):
    """tools/wiltshire_closures.py, with PUBLISH_VOLUNTARY each way."""

    def test_the_switch_is_committed_off(self):
        # App builds before 118 draw an unknown type as a closure; on only
        # once 118 is with testers.
        self.assertIs(w.PUBLISH_VOLUNTARY, False)

    def test_switched_off_a_voluntary_closure_is_held(self):
        with published(False):
            item = candidate_for("AVEB14$001")
            self.assertIn("review_only", item)
            items, _unmatched, review = cs.match(
                [item], Byways([wilts_way("WT-14-a", "AVEB", "14")]),
                "Wiltshire")
        self.assertEqual(items, [])
        self.assertEqual([r["ways"] for r in review], [["WT-14-a"]])

    def test_switched_on_it_is_published_as_a_request(self):
        with published(True):
            item = candidate_for("AVEB14$001")
            self.assertNotIn("review_only", item)
            items, _unmatched, review = cs.match(
                [item], Byways([wilts_way("WT-14-a", "AVEB", "14")]),
                "Wiltshire")
        self.assertEqual(review, [])
        (published_item,) = items
        self.assertEqual(published_item["ways"], ["WT-14-a"])
        self.assertEqual(published_item["vehicles"], "voluntary")
        self.assertEqual(published_item["asked_by"], "Wiltshire Council")

    def test_switched_on_it_carries_the_registers_own_dates(self):
        for row, start in (("AVEB14$001", "2025-10-01"),
                           ("BSTO21$001", "2025-10-01")):
            with published(True):
                item = candidate_for(row)
            props = feature(item, IN_THE_WINTER)["properties"]
            self.assertEqual(props["otype"], "voluntary", row)
            self.assertEqual(props["code"], "councilVoluntaryClosure", row)
            self.assertEqual(props.get("oform"), "seasonal", row)
            self.assertEqual((props["start"], props["end"]),
                             (start, "2026-04-30"), row)
            self.assertEqual(props["asked_by"], "Wiltshire Council", row)
            self.assertIn("not an order", props["name"], row)

    def test_a_request_past_its_dates_is_not_published(self):
        # MEASURED on the saved register: every voluntary closure on it is
        # dated to the 2025-26 winter. A request is published for the dates
        # the council gave it, so on the day it was saved there is none to
        # draw until the council dates this winter's.
        with published(True):
            item = candidate_for("AVEB14$001")
        self.assertIsNone(feature(item, SAVED))

    def test_a_season_its_words_state_dates_it_this_winter(self):
        with published(True):
            entry = next(e for e in w.parse_results(
                fixture("result-boat.html")) if e["row"] == "AVEB14$001")
            found = w.parse_detail(fixture("detail-AVEB14_001.html"))
            found = dict(found, reason="Keep off from 1 October to 30 April "
                                       "every year", start=None, end=None)
            item = w.candidate(entry, found, cs.parse_date, cs.parse_season,
                               clean)
        self.assertEqual((item["form"], item["season"]),
                         ("seasonal", {"from": "10-01", "to": "04-30"}))
        props = feature(item, SAVED)["properties"]
        self.assertEqual((props["otype"], props["start"], props["end"]),
                         ("voluntary", "2026-10-01", "2027-04-30"))


if __name__ == "__main__":
    unittest.main(verbosity=1)

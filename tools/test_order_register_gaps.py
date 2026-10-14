#!/usr/bin/env python3
"""Two gaps in the order register are closed: the Peak District park's
orders on byways we carry, and the orders that were held back because they
cover only part of a lane. (North Yorkshire Council's unsurfaced-road orders
come in a later change, with their own tests.)

    python tools/test_order_register_gaps.py

An audit on 14 Oct 2026 found lanes riders have that official lists close
while our data drew them open:

- Derbyshire County Council's page of permanent orders names four orders
  the Peak District National Park Authority made on byways we carry (Long
  Causeway, Chapel Gate, Leys Lane, Derby Lane). The register held them as
  five untitled "listed-only" rows, so none was published.
- Seven transcribed orders were "needs-review" only because each covers
  part of a byway. The owner's rule (14 Oct 2026; Turbary Road, 13 Oct, is
  the precedent): such an order is pinned to the WHOLE lane, never held back,
  and its notes say which part it actually covers.

Like test_order_register_ydnpa.py, these tests read the committed register
(tro/register/orders.json) and its committed output
(tro/council/order-register.json), and build through synthetic ways. They
never read the ways containers, which move on every lane refresh. When a
pinned lane leaves the published data the register's build holds the order
back with "no longer in the published byways", and that is accepted here as
the one honest alternative to publishing it.
"""
import datetime
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

import council_orders as CO  # noqa: E402
import order_register as reg  # noqa: E402
from byway_match import Byways, Way  # noqa: E402

GONE = "no longer in the published byways"

DY_PAGE = ("https://www.derbyshire.gov.uk/leisure/countryside/access/"
           "rights-of-way/traffic-regulation-orders/permanent-traffic-"
           "regulation-orders-affecting-public-rights-of-way.aspx")
PARK = "Peak District National Park Authority"
_DY = "derbyshire-county-council/"

# Every order these gaps publish and the lanes it is pinned to, by value,
# with what each lane is: "ucr" where every lane is a road, None for byways.
PEAK = {
    _DY + "outseats/boat-48-long-causeway": (sorted([
        "DY-48/1-b2643d891a", "DY-48/2-8ad2079a1f",
        "DY-48/3-6da9913800"]), None),
    _DY + "chapel-en-le-frith/boat-144-and-edale-boat-16-chapel-gate": (
        sorted(["DY-144/1-641c22a110", "DY-144/2-a88b760795",
                "DY-16/1-b22e00a8ec", "DY-16/2-18c7ce1398",
                "DY-16/3-fe435e0302"]), None),
    _DY + "little-longstone/boat-14-leys-lane": (sorted([
        "DY-14/1-af5666a618", "DY-14/2-a725f04961",
        "DY-14/3-f07cfdbf2b", "DY-14/4-dfda9d8adf"]), None),
    _DY + "monyash/boat-29-derby-lane": (sorted([
        "DY-29/1-0b814fecb5", "DY-29/2-93ac53e2f7"]), None),
}
ROYCH = _DY + "chapel-en-le-frith/the-roych"

# The seven orders that were held back because each covers part of a lane.
PARTIAL = {
    "central-bedfordshire-council/dunstable/boat-4":
        (["BK-4-a512b94e09"], None),
    "central-bedfordshire-council/totternhoe/boat-12":
        (["BK-12-afd0c061c4"], None),
    "central-bedfordshire-council/totternhoe/boat-42":
        (["BK-42-dfce59b88f"], None),
    "central-bedfordshire-council/totternhoe/boat-45":
        (["BK-45-89128fc070"], None),
    "east-sussex-county-council/crowborough/byway-30c-and-b-part":
        (sorted(["ES-30b-138131bb82", "ES-30c-0ee9a1ac0a"]), None),
    "east-sussex-county-council/heathfield-and-waldron/byway-115-part-of":
        (["ES-115-de9329bbc9"], None),
    "surrey-county-council/esher-claygate/47":
        (sorted(["SU-47-b62f6b3535", "SU-47-e3dc086ea9"]), None),
}
PINNED = {}
PINNED.update(PEAK)
PINNED.update(PARTIAL)

LABEL = ("Traffic order made by the Peak District National Park Authority. "
         "The published list does not give its terms; check the signs and "
         "the official order.")
_WHOLE = "the order covers part of it; the whole lane is shown"
_DROVERS = ("Central Bedfordshire Council: motor vehicles and horse-drawn "
            "carriages are banned on The Drovers Way. Motorcycles are banned "
            "on part of it, and the whole lane is shown as closed to them")
_WHEEL = ("Central Bedfordshire Council: horse-drawn carriages are banned on "
          "Wheelbarrow Green Highway, and motor vehicles other than "
          "motorcycles on part of it. Motorcycles are not banned")
_SEASON = ("East Sussex County Council: motor vehicles other than motorcycles "
           "are banned from 1 October to 31 March each year")
# What a rider reads for each partial order, and what it closes to whom:
# (vehicles, form, authority, where, title).
PARTIAL_TEXT = {
    "central-bedfordshire-council/dunstable/boat-4": (
        "motor_vehicles", "permanent", "Central Bedfordshire",
        "Dunstable BOAT 4 (The Drovers Way)", _DROVERS),
    "central-bedfordshire-council/totternhoe/boat-12": (
        "motor_vehicles", "permanent", "Central Bedfordshire",
        "Totternhoe BOAT 12 (The Drovers Way)", _DROVERS),
    "central-bedfordshire-council/totternhoe/boat-42": (
        "motor_vehicles_except_motorcycles", "permanent",
        "Central Bedfordshire",
        "Totternhoe BOAT 42 (Wheelbarrow Green Highway; %s)" % _WHOLE, _WHEEL),
    "central-bedfordshire-council/totternhoe/boat-45": (
        "motor_vehicles_except_motorcycles", "permanent",
        "Central Bedfordshire",
        "Totternhoe BOAT 45 (Wheelbarrow Green Highway; %s)" % _WHOLE, _WHEEL),
    "east-sussex-county-council/crowborough/byway-30c-and-b-part": (
        "motor_vehicles_except_motorcycles", "seasonal", "East Sussex",
        "Crowborough Byway 30b and 30c (the order covers 30c and part of "
        "30b; the whole lane is shown)", _SEASON),
    "east-sussex-county-council/heathfield-and-waldron/byway-115-part-of": (
        "motor_vehicles_except_motorcycles", "seasonal", "East Sussex",
        "Heathfield and Waldron Byway 115 (%s)" % _WHOLE, _SEASON),
    "surrey-county-council/esher-claygate/47": (
        "all_vehicles", "permanent", "Surrey", "Claygate BOAT 47 (New Road)",
        "Elmbridge Borough Council: road closure order 1996 — all "
        "vehicles banned on New Road, Claygate (BOAT 47)"),
}


def _orders():
    with open(os.path.join(ROOT, "tro", "register", "orders.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def _committed():
    with open(os.path.join(ROOT, "tro", "council", "order-register.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def _order(oid):
    found = [o for o in _orders() if o["id"] == oid]
    assert len(found) == 1, "%s is in the register once" % oid
    return found[0]


def _today():
    return datetime.date.today().isoformat()


class TheRegisterHoldsEachOrder(unittest.TestCase):
    def test_every_order_is_approved_and_pinned_as_reviewed(self):
        for oid, (ways, _on) in PINNED.items():
            with self.subTest(oid):
                o = _order(oid)
                self.assertEqual(o["status"], "approved")
                self.assertEqual(sorted(o["ways"]), ways)
                self.assertTrue(o.get("reviewed"), "says who pinned it")

    def test_no_untitled_park_rows_are_left(self):
        # The five "listed-only" rows had no route and no terms; each is
        # now one of the five routes the page names.
        left = [o["id"] for o in _orders()
                if o["id"].startswith(_DY + "listed/permanent-tros-made-by")]
        self.assertEqual(left, [])

    def test_the_park_orders_say_who_made_them_and_what_is_unknown(self):
        for oid in list(PEAK) + [ROYCH]:
            with self.subTest(oid):
                o = _order(oid)
                self.assertEqual(o["source_url"], DY_PAGE)
                self.assertEqual(o["council"], "Derbyshire County Council")
                self.assertEqual(o["made_by"], PARK)
                self.assertEqual(o["form"], "permanent")
                # The page names the routes, not the terms: never guessed.
                self.assertEqual(o["vehicles"], "other")
                self.assertEqual(o["label"], LABEL)
                self.assertNotIn("start", o)
                self.assertNotIn("end", o)
                self.assertIn(o["path_as_written"].split(" - ")[0],
                              o["evidence"])

    def test_the_roych_is_approved_and_held_because_we_do_not_carry_it(self):
        o = _order(ROYCH)
        self.assertEqual(o["status"], "approved")
        self.assertNotIn("ways", o)
        out = _committed()
        held = dict((p["id"], p["why"]) for p in out["review"])
        self.assertIn("no byway of ours matches", held[ROYCH])
        self.assertNotIn(ROYCH, [i["id"] for i in out["items"]])

    def test_each_partial_order_says_what_part_it_covers(self):
        # Owner's rule, 14 Oct 2026: the whole lane, with the part noted.
        for oid in PARTIAL:
            with self.subTest(oid):
                o = _order(oid)
                self.assertIn("whole", o["reviewed"])
                self.assertIn("owner", o["reviewed"])
                self.assertTrue(o.get("review_note"))
                self.assertNotIn("would be a guess", o["review_note"])

    def test_no_personal_data_in_the_new_entries(self):
        for oid in list(PINNED) + [ROYCH]:
            o = _order(oid)
            text = json.dumps(o)
            # No e-mail address, telephone number or personal title.
            self.assertNotIn("@", text, oid)
            self.assertIsNone(re.search(r"\b0\d{3} ?\d{3} ?\d{3,4}\b",
                                        text), oid)
            self.assertIsNone(re.search(r"\b(Mr|Mrs|Ms|Miss|Dr)\.? [A-Z]",
                                        text), oid)


class TheCommittedOutput(unittest.TestCase):
    """tro/council/order-register.json, which the order build reads."""

    @classmethod
    def setUpClass(cls):
        cls.out = _committed()
        cls.items = dict((i["id"], i) for i in cls.out["items"])
        cls.held = dict((p["id"], p["why"]) for p in cls.out["review"])

    def test_it_was_built_from_this_register(self):
        self.assertEqual(self.out["records"], len(_orders()))

    def test_every_pinned_order_is_published_on_its_lanes(self):
        for oid, (ways, on) in PINNED.items():
            with self.subTest(oid):
                if oid in self.held:
                    self.assertIn(GONE, self.held[oid])
                    continue
                item = self.items.get(oid)
                self.assertIsNotNone(item, "published")
                self.assertEqual(item["ways"], ways)
                self.assertEqual(item.get("on"), on)

    def test_each_is_drawn_while_its_order_is_in_force(self):
        today = _today()
        for oid in PINNED:
            item = self.items.get(oid)
            if item is None:
                continue
            end = item.get("end")
            if end and end < today:
                continue
            with self.subTest(oid):
                feature = CO.feature_of(item, self.out["source"], today)
                self.assertIsNotNone(feature, "drawn today")
                self.assertEqual(feature["properties"]["ways"],
                                 PINNED[oid][0])

    def test_every_item_says_what_its_order_says(self):
        # Everything in an item but its lines comes from its order; the
        # lines come from the ways containers, which this suite never reads.
        orders = dict((o["id"], o) for o in _orders())
        for oid, item in self.items.items():
            with self.subTest(oid):
                on = "ucr" if item.get("on") == "ucr" else "boat"
                byways = Byways([Way(u, item["authority"], u,
                                     [[(0.0, 0.0), (0.001, 0.001)]],
                                     way_class=on) for u in item["ways"]])
                want = reg.item_of(orders[oid], byways, item["ways"],
                                   item["match"])
                want.pop("geometry")
                got = dict(item)
                geometry = got.pop("geometry")
                self.assertEqual(got, want)
                self.assertEqual(geometry["type"], "LineString"
                                 if len(geometry["coordinates"][0]) == 2 and
                                 isinstance(geometry["coordinates"][0][0],
                                            float) else "MultiLineString")
                if oid in PINNED:
                    self.assertEqual(item["match"], "reviewed")

    def test_the_park_orders_are_labelled_not_guessed(self):
        for oid in PEAK:
            item = self.items.get(oid)
            if item is None:
                continue
            with self.subTest(oid):
                code, otype, label = CO.label_for(item)
                self.assertEqual(otype, "other")
                self.assertEqual(label, LABEL)
                self.assertNotIn("start", item)
                self.assertNotIn("end", item)
                self.assertEqual(item["title"], "Peak District National "
                                 "Park Authority: traffic order on " +
                                 item["where"].split("(")[-1].rstrip(")"))

    def test_each_partial_order_reads_as_reviewed(self):
        for oid, (vehicles, form, authority, where, title) in                 PARTIAL_TEXT.items():
            with self.subTest(oid):
                item = self.items[oid]
                self.assertEqual(item["vehicles"], vehicles)
                self.assertEqual(item["form"], form)
                self.assertEqual(item["authority"], authority)
                self.assertEqual(item["where"], where)
                self.assertEqual(item["title"], title)
                if form == "seasonal":
                    self.assertEqual(item["season"],
                                     {"from": "10-01", "to": "03-31"})
                else:
                    self.assertNotIn("season", item)

    def test_exactly_these_orders_were_pinned_in_this_change(self):
        mine = set(o["id"] for o in _orders()
                   if "2026-10-14" in (o.get("reviewed") or ""))
        self.assertEqual(mine, set(PINNED))
        self.assertEqual(set(PARTIAL_TEXT), set(PARTIAL))
        published = set(i["id"] for i in self.out["items"]) & mine
        held = set(self.held) & mine
        self.assertEqual(published | held, set(PINNED))

    def test_the_authorities_are_named_as_sources(self):
        name = self.out["source"]["name"]
        for council in ("Derbyshire County Council",
                        "Central Bedfordshire Council",
                        "East Sussex County Council",
                        "Surrey County Council"):
            self.assertIn(council, name)


if __name__ == "__main__":
    unittest.main(verbosity=1)

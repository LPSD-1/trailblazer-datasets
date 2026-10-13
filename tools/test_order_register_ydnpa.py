#!/usr/bin/env python3
"""The Yorkshire Dales National Park Authority's list of traffic orders is
in the register in full, and Mastiles Lane reaches the app closed.

    python tools/test_order_register_ydnpa.py

A rider reported, 13 Oct 2026, that Mastiles Lane (Street Gate to Kilnsey)
drew open although the Park Authority's own list has closed it to all motor
vehicles since August 2008. The list, "Current Traffic Regulation Orders in
the Yorkshire Dales National Park" (last updated 12/10/18), had been read
into tro/register/ but only two of its twelve rows were transcribed, and
neither was published.

These tests read the committed register (tro/register/orders.json) and the
committed register output (tro/council/order-register.json), and build
through synthetic ways. They never read the ways containers: those move on
every lane refresh, and a refresh that redraws a road must not turn a tool
suite red (an exit 1 stops the refresh). When a pinned road leaves the
published data, the register's own build holds the order back with "no
longer in the published byways", and that is accepted here as the one
honest alternative to publishing it.
"""
import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

import council_orders as CO  # noqa: E402
import order_register as reg  # noqa: E402
from byway_match import Byways, Way  # noqa: E402

PARK = "Yorkshire Dales National Park Authority"
PDF = ("https://www.yorkshiredales.org.uk/wp-content/uploads/sites/13/2019/"
       "08/CURRENT-TRAFFIC-REGULATION-ORDERS-IN-THE-YORKSHIRE-DALES-"
       "12.10.18.pdf")

# Every row of the list, as the PDF names the route.
ROWS = [
    "Foxup Road/Horton Scar Lane",
    "Ling Gill (Old Ing to Cam End)",
    "Cam High Road (Far Gearstones to Cam Houses)",
    "Mastiles Lane from Street Gate to Kilnsey",
    "Long Lane from Clapham to Selside",
    "Horsehead Pass from Halton Gill to Yockenthwaite",
    "The High Way",
    "Pockstones Moor",
    "Barth Bridge to Garsdale",
    "Carlton to Middleham High Moor",
    "Gorbeck Road (Langcliffe to Malham)",
    "Turbary Road",
]

MASTILES = "yorkshire-dales-national-park-authority//mastiles-lane"
# U808 is drawn by the council in two pieces with a gap between them;
# build_packages.ucr_lanes gives the second its own suffixed id.
MASTILES_WAYS = ["NY-UCR-craven-u808", "NY-UCR-craven-u808-a9d791"]
TURBARY = "yorkshire-dales-national-park-authority//turbary-road"
_P = "yorkshire-dales-national-park-authority//"
# Every pinned Park order and the lanes a reviewer pinned it to, by value,
# with what each is: "ucr" where every lane is a road, None for byways.
PINNED = {
    _P + "mastiles-lane": (sorted(MASTILES_WAYS), "ucr"),
    _P + "horsehead-pass-from-halton-gill-to-yockenthwaite":
        (["NY-UCR-craven-u8035"], "ucr"),
    _P + "the-high-way-south-section": (["NY-1/1-9b128ac3dd"], None),
    _P + "pockstones-moor": (sorted([
        "NY-UCR-craven-u2281", "NY-UCR-craven-u8130",
        "NY-UCR-harrogate-u2281", "NY-UCR-harrogate-u8130"]), "ucr"),
    _P + "carlton-to-middleham-high-moor":
        (["NY-UCR-richmondshire-u7021"], "ucr"),
    _P + "gorbeck-road-langcliffe-to-malham": (sorted([
        "NY-20/1-87e3435213", "NY-32/1-b37b226ef1",
        "NY-UCR-craven-u8040"]), None),
    # Owner's decision, 13 Oct 2026: the whole road, though the order
    # covers only its Yordas end.
    TURBARY: (["NY-UCR-craven-u695"], "ucr"),
}
GONE = "no longer in the published byways"


def _orders():
    with open(os.path.join(ROOT, "tro", "register", "orders.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def _committed():
    with open(os.path.join(ROOT, "tro", "council", "order-register.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def _from_the_list(orders):
    return [o for o in orders if o.get("source_url") == PDF]


def _order(oid):
    found = [o for o in _orders() if o["id"] == oid]
    assert len(found) == 1, "%s is in the register once" % oid
    return found[0]


class EveryRowIsTranscribed(unittest.TestCase):
    def test_each_of_the_twelve_rows_is_there_once(self):
        listed = _from_the_list(_orders())
        self.assertEqual(sorted(o.get("path_as_written") for o in listed),
                         sorted(ROWS))

    def test_each_row_is_what_the_list_says(self):
        for o in _from_the_list(_orders()):
            with self.subTest(o["id"]):
                # The council is the authority whose list it is; who made
                # the order, where the list says, is `made_by`.
                self.assertEqual(o["council"], PARK)
                self.assertEqual(o["restriction"], "All motor vehicles")
                self.assertEqual(o["vehicles"], "motor_vehicles")
                self.assertEqual(o["form"], "permanent")
                self.assertGreaterEqual(len(o["grid_refs"]), 2)
                for ref in o["grid_refs"]:
                    self.assertIsNotNone(reg.grid_ref(ref), ref)
                self.assertIn(o["path_as_written"].split(" ")[0],
                              o["evidence"])
                self.assertIn(o["status"], ("approved", "needs-review"))
                # Begun, and in the year the list's start column gives.
                if o.get("start"):
                    self.assertLessEqual(o["start"],
                                         datetime.date.today().isoformat())
                    self.assertIn(o["start"][:4], o["order_date"])

    def test_the_rows_the_list_says_nycc_made_say_so(self):
        made = dict((o["path_as_written"], o.get("made_by"))
                    for o in _from_the_list(_orders()))
        self.assertEqual(made["Pockstones Moor"],
                         "North Yorkshire County Council")
        self.assertEqual(made["Turbary Road"],
                         "North Yorkshire County Council")
        self.assertEqual(
            [r for r, m in made.items() if m and r not in (
                "Pockstones Moor", "Turbary Road")], [])

    def test_mastiles_is_approved_and_pinned_to_both_pieces(self):
        o = _order(MASTILES)
        self.assertEqual(o["status"], "approved")
        self.assertEqual(sorted(o["ways"]), sorted(MASTILES_WAYS))
        self.assertEqual(o["vehicles"], "motor_vehicles")
        self.assertEqual(o["form"], "permanent")
        self.assertNotIn("end", o)

    def test_turbary_road_closes_the_whole_road_on_the_owners_word(self):
        # The order covers only the Yordas end (about 0.8 km) of the
        # council's 4.9 km U695; the owner chose the whole road closed.
        o = _order(TURBARY)
        self.assertEqual(o["status"], "approved")
        self.assertEqual(o["ways"], ["NY-UCR-craven-u695"])
        self.assertIn("Yordas", o["review_note"])
        self.assertIn("owner", o["review_note"])

    def test_the_pins_are_the_reviewed_ones(self):
        pinned = dict((o["id"], sorted(o["ways"]))
                      for o in _from_the_list(_orders()) if o.get("ways"))
        self.assertEqual(pinned, dict((k, v[0]) for k, v in PINNED.items()))
        for oid in PINNED:
            self.assertEqual(_order(oid)["status"], "approved", oid)

    def test_the_review_notes_carry_what_review_found(self):
        # Review of d40f800: what a later reviewer needs to know is on the
        # entry, not only in a commit message.
        for oid in ("gorbeck-road-langcliffe-to-malham",
                    "the-high-way-south-section"):
            self.assertIn("recreational motor vehicles",
                          _order(_P + oid)["review_note"])
        self.assertIn("SD974 677", _order(MASTILES)["reviewed"])
        self.assertIn("the whole road the council's notices name",
                      _order(_P + "carlton-to-middleham-high-moor")
                      ["reviewed"])

    def test_trial_names_stay_in_the_quote_only(self):
        for o in _from_the_list(_orders()):
            for key in ("review_note", "reviewed"):
                for name in ("Jeffries", "Gerald Simpson"):
                    self.assertNotIn(name, o.get(key) or "", o["id"])


class MastilesLaneIsClosed(unittest.TestCase):
    """The register's own build, through ways shaped as U808 is."""

    @classmethod
    def setUpClass(cls):
        cls.byways = Byways([
            Way(MASTILES_WAYS[0], "North Yorkshire",
                "Unsurfaced unclassified road (UCR) Craven U808",
                [[(-2.08687, 54.09154), (-2.06489, 54.0984)],
                 [(-2.06489, 54.0984), (-2.04046, 54.10643)]],
                way_class="ucr"),
            Way(MASTILES_WAYS[1], "North Yorkshire",
                "Unsurfaced unclassified road (UCR) Craven U808",
                [[(-2.14665, 54.08667), (-2.10938, 54.08514)]],
                way_class="ucr"),
            # The byway that meets it at Street Gate, and is not part of it.
            Way("NY-31/1-a63d9c2e31", "North Yorkshire",
                "Byway open to all traffic (BOAT) 05.70 31/1",
                [[(-2.1465, 54.0867), (-2.1396, 54.0931)]]),
        ])
        cls.data, cls.problems = reg.build(cls.byways, [_order(MASTILES)])

    def test_the_built_register_closes_both_pieces_and_nothing_else(self):
        self.assertEqual(self.problems, [])
        [item] = self.data["items"]
        self.assertEqual(item["id"], MASTILES)
        self.assertEqual(item["ways"], sorted(MASTILES_WAYS))
        self.assertEqual(item["on"], "ucr")
        self.assertEqual(item["authority"], "North Yorkshire")
        self.assertEqual(self.data["source"]["authorities"],
                         ["North Yorkshire"])
        self.assertIn(PARK, self.data["source"]["name"])

    def test_it_is_a_prohibition_of_motor_vehicles_in_force_today(self):
        [item] = self.data["items"]
        today = datetime.date.today().isoformat()
        feature = CO.feature_of(item, self.data["source"], today)
        self.assertIsNotNone(feature, "in force today")
        props = feature["properties"]
        self.assertEqual(props["otype"], "prohibition")
        self.assertEqual(props["label"], "No motor vehicles")
        self.assertEqual(props["ways"], sorted(MASTILES_WAYS))
        # Permanent: no seasonal or experimental form, no end, begun.
        self.assertNotIn("oform", props)
        self.assertNotIn("end", props)
        self.assertLessEqual(props["start"], today)
        # Both pieces drawn: three lines, two from the east piece.
        self.assertEqual(feature["geometry"]["type"], "MultiLineString")
        self.assertEqual(len(feature["geometry"]["coordinates"]), 3)


class TheCommittedOutput(unittest.TestCase):
    """tro/council/order-register.json, which the order build reads."""

    @classmethod
    def setUpClass(cls):
        cls.out = _committed()
        cls.items = dict((i["id"], i) for i in cls.out["items"])
        cls.held = dict((p["id"], p["why"]) for p in cls.out["review"])

    def test_it_was_built_from_this_register(self):
        self.assertEqual(self.out["records"], len(_orders()))

    def test_mastiles_is_published_closed_or_held_because_u808_moved(self):
        if MASTILES in self.held:
            self.assertIn(GONE, self.held[MASTILES])
            return
        item = self.items.get(MASTILES)
        self.assertIsNotNone(item, "Mastiles Lane is published")
        self.assertEqual(item["ways"], sorted(MASTILES_WAYS))
        self.assertEqual(item["vehicles"], "motor_vehicles")
        self.assertEqual(item["on"], "ucr")
        self.assertEqual(CO.label_for(item)[1], "prohibition")
        today = datetime.date.today().isoformat()
        feature = CO.feature_of(item, self.out["source"], today)
        self.assertIsNotNone(feature, "drawn today")
        self.assertEqual(feature["properties"]["otype"], "prohibition")
        self.assertEqual(feature["properties"]["label"], "No motor vehicles")
        self.assertLessEqual(feature["properties"]["start"], today)

    def test_every_published_park_item_is_what_was_reviewed(self):
        today = datetime.date.today().isoformat()
        for oid, item in self.items.items():
            if not oid.startswith(_P):
                continue
            with self.subTest(oid):
                ways, on = PINNED[oid]
                self.assertEqual(item["ways"], ways)
                self.assertEqual(item.get("on"), on)
                self.assertEqual(item["vehicles"], "motor_vehicles")
                self.assertLessEqual(item.get("start") or "", today)
                feature = CO.feature_of(item, self.out["source"], today)
                self.assertIsNotNone(feature, "in force today")
                self.assertEqual(feature["properties"]["otype"],
                                 "prohibition")

    def test_every_park_order_is_published_or_held_with_a_reason(self):
        for o in _from_the_list(_orders()):
            with self.subTest(o["id"]):
                if o["status"] == "approved":
                    self.assertTrue(o["id"] in self.items or
                                    self.held.get(o["id"]))
                else:
                    self.assertNotIn(o["id"], self.items)
                    self.assertNotIn(o["id"], self.held)

    def test_every_pinned_park_order_that_is_not_held_is_published(self):
        for o in _from_the_list(_orders()):
            if o["status"] != "approved" or not o.get("ways"):
                continue
            with self.subTest(o["id"]):
                if o["id"] in self.held:
                    self.assertIn(GONE, self.held[o["id"]])
                else:
                    self.assertEqual(self.items[o["id"]]["ways"],
                                     sorted(o["ways"]))

    def test_the_park_is_named_as_a_source_when_its_orders_are_published(self):
        source = self.out["source"]
        mine = [i for i in self.out["items"]
                if i["id"].startswith("yorkshire-dales-national-park-")]
        if not mine:
            # Every pinned road gone at once: the other tests here say why.
            self.assertNotIn(PARK, source["name"])
            return
        self.assertIn(PARK, source["name"])
        # `authorities` holds the LANE authorities a source speaks for
        # (build_tro matches it to each council row's lane names); the
        # Park's ground in our lanes is North Yorkshire's.
        self.assertIn("North Yorkshire", source["authorities"])


if __name__ == "__main__":
    unittest.main(verbosity=1)

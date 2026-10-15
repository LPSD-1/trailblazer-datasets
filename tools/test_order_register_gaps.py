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

Nothing here reads the clock: every date is pinned, so the suite gives the
same answer on every day of the year (a red suite stops the lane refresh).
"""
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

# A day inside every order's season (1 October to 31 March), and one
# outside it by more than the 90 days the order build looks ahead.
IN_SEASON = "2026-12-01"
OUT_OF_SEASON = "2027-05-01"

DY_PAGE = ("https://www.derbyshire.gov.uk/leisure/countryside/access/"
           "rights-of-way/traffic-regulation-orders/permanent-traffic-"
           "regulation-orders-affecting-public-rights-of-way.aspx")
PARK = "Peak District National Park Authority"
_DY = "derbyshire-county-council/"


def _peak_title(lane):
    return PARK + ": traffic order on " + lane + "; terms not published"


# Every Peak order these gaps publish, by value: (ways, on, parish,
# path_as_written, local_name, where, title). "on" is "ucr" where every lane
# is a road, None for byways.
PEAK = {
    _DY + "outseats/boat-48-long-causeway": (sorted([
        "DY-48/1-b2643d891a", "DY-48/2-8ad2079a1f",
        "DY-48/3-6da9913800"]), None,
        "Outseats", "Byway Open to All Traffic 48", "Long Causeway",
        "Outseats Byway Open to All Traffic 48 (Long Causeway)",
        "Peak District National Park Authority: traffic order on Long "
        "Causeway; terms not published"),
    _DY + "chapel-en-le-frith/boat-144-and-edale-boat-16-chapel-gate": (
        sorted(["DY-144/1-641c22a110", "DY-144/2-a88b760795",
                "DY-16/1-b22e00a8ec", "DY-16/2-18c7ce1398",
                "DY-16/3-fe435e0302"]), None,
        "Chapel-en-le-Frith",
        "Byway Open to All Traffic 144 continuing as Edale - Byway Open to "
        "All Traffic 16", "Chapel Gate",
        "Chapel-en-le-Frith Byway Open to All Traffic 144 continuing as "
        "Edale - Byway Open to All Traffic 16 (Chapel Gate)",
        "Peak District National Park Authority: traffic order on Chapel "
        "Gate; terms not published"),
    _DY + "little-longstone/boat-14-leys-lane": (sorted([
        "DY-14/1-af5666a618", "DY-14/2-a725f04961",
        "DY-14/3-f07cfdbf2b", "DY-14/4-dfda9d8adf"]), None,
        "Little Longstone", "Byway Open to All Traffic 14", "Leys Lane",
        "Little Longstone Byway Open to All Traffic 14 (Leys Lane)",
        "Peak District National Park Authority: traffic order on Leys "
        "Lane; terms not published"),
    _DY + "monyash/boat-29-derby-lane": (sorted([
        "DY-29/1-0b814fecb5", "DY-29/2-93ac53e2f7"]), None,
        "Monyash", "Byway Open to All Traffic 29", "Derby Lane",
        "Monyash Byway Open to All Traffic 29 (Derby Lane)",
        "Peak District National Park Authority: traffic order on Derby "
        "Lane; terms not published"),
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
# Every order this change pins: {id: (ways, on)}.
PINNED = dict((oid, v[:2]) for oid, v in PEAK.items())
PINNED.update(PARTIAL)

LABEL = ("Traffic order made by the Peak District National Park Authority. "
         "The published list does not give its terms, so this lane may be "
         "restricted. Check the signs and the official order.")
_DROVERS_LOCAL = ("The Drovers Way; the motorcycle ban covers part of it, "
                  "and the official order says which part; the whole lane "
                  "is shown")
_DROVERS = ("Central Bedfordshire Council: motor vehicles other than "
            "motorcycles, and horse-drawn carriages, are banned on all of "
            "The Drovers Way. Motorcycles are banned on part of it, so the "
            "map shows the whole lane closed to motorcycles too")
# The council's list bans "Motor Vehicles (part), and Carriages": the
# carriage ban has no "(part)", so it covers the whole lane.
_WHEEL_LOCAL = ("Wheelbarrow Green Highway; the motor vehicle ban covers "
                "part of it, and the official order says which part; the "
                "whole lane is shown")
_WHEEL = ("Central Bedfordshire Council: horse-drawn carriages are banned on "
          "all of Wheelbarrow Green Highway, and motor vehicles other than "
          "motorcycles on part of it. This order does not ban motorcycles")
_ES = "East Sussex County Council: "
_ES30 = ("from 1 October to 31 March each year, motor vehicles other than "
         "motorcycles are banned on byway 30c and part of 30b. This order "
         "does not ban motorcycles")
_ES115 = ("from 1 October to 31 March each year, motor vehicles other than "
          "motorcycles are banned on part of byway 115. This order does not "
          "ban motorcycles")
_ES115_LOCAL = ("the order covers part of it, and the official order says "
                "which part; the whole lane is shown")
_ELMBRIDGE = ("Elmbridge Borough Council: all vehicles are banned on New "
              "Road, Claygate (byway 47), under a 1996 road closure order")
# What a rider reads for each partial order, and what it closes to whom:
# (vehicles, form, authority, where, title).
PARTIAL_TEXT = {
    "central-bedfordshire-council/dunstable/boat-4": (
        "motor_vehicles", "permanent", "Central Bedfordshire",
        "Dunstable BOAT 4 (%s)" % _DROVERS_LOCAL, _DROVERS),
    "central-bedfordshire-council/totternhoe/boat-12": (
        "motor_vehicles", "permanent", "Central Bedfordshire",
        "Totternhoe BOAT 12 (%s)" % _DROVERS_LOCAL, _DROVERS),
    "central-bedfordshire-council/totternhoe/boat-42": (
        "motor_vehicles_except_motorcycles", "permanent",
        "Central Bedfordshire",
        "Totternhoe BOAT 42 (%s)" % _WHEEL_LOCAL, _WHEEL),
    "central-bedfordshire-council/totternhoe/boat-45": (
        "motor_vehicles_except_motorcycles", "permanent",
        "Central Bedfordshire",
        "Totternhoe BOAT 45 (%s)" % _WHEEL_LOCAL, _WHEEL),
    "east-sussex-county-council/crowborough/byway-30c-and-b-part": (
        "motor_vehicles_except_motorcycles", "seasonal", "East Sussex",
        "Crowborough Byway 30b and 30c (the order covers 30c and part of "
        "30b; the whole lane is shown)", _ES + _ES30),
    "east-sussex-county-council/heathfield-and-waldron/byway-115-part-of": (
        "motor_vehicles_except_motorcycles", "seasonal", "East Sussex",
        "Heathfield and Waldron Byway 115 (%s)" % _ES115_LOCAL,
        _ES + _ES115),
    "surrey-county-council/esher-claygate/47": (
        "all_vehicles", "permanent", "Surrey", "Claygate BOAT 47 (New Road)",
        _ELMBRIDGE),
}

# Every order this change touches holds exactly these fields, and every
# field but id, ways, paths and season is pinned by value below. A name, a
# phone number or an address added to any of them fails here.
_PEAK_KEYS = {"id", "council", "authorities", "parish", "path_as_written",
              "local_name", "vehicles", "label", "form", "made_by",
              "restriction", "source_url", "evidence", "status",
              "review_note", "ways", "reviewed", "order_name"}
_LIST_KEYS = {"id", "council", "authorities", "parish", "path_as_written",
              "paths", "vehicles", "form", "grid_refs", "restriction",
              "source_url", "evidence", "status", "review_note", "ways",
              "reviewed", "local_name"}
KEYS = dict((oid, _PEAK_KEYS) for oid in PEAK)
KEYS[ROYCH] = _PEAK_KEYS - {"ways", "reviewed"}
for _oid in PARTIAL:
    KEYS[_oid] = _LIST_KEYS
for _oid in ("east-sussex-county-council/crowborough/byway-30c-and-b-part",
             "east-sussex-county-council/heathfield-and-waldron/"
             "byway-115-part-of"):
    KEYS[_oid] = _LIST_KEYS | {"season"}
KEYS["surrey-county-council/esher-claygate/47"] = (
    (_LIST_KEYS - {"grid_refs"}) | {"order_name", "order_date"})

_RULE = ("pinned 2026-10-14 on the owner's rule (14 Oct 2026): an order on "
         "part of a lane we carry closes the whole lane. %s, whole; the path "
         "reference names the same %s.")
_PEAK_EVIDENCE = ("Permanent TROs made by the Peak District National Park "
                  "Authority. The following routes within the Peak District "
                  "National Park are affected by TROs: ... ")
_PEAK_RESTRICTION = ("Traffic order made by the Peak District National Park "
                     "Authority; the published list does not give its terms")
_PEAK_NOTE = (
    "Derbyshire County Council's page lists this route under 'Permanent TROs "
    "made by the Peak District National Park Authority' and says it is "
    "affected by a TRO, but not what the order restricts or when it began; "
    "the park's own site, which has the orders, is on our blocked-hosts list "
    "(bot challenge), so the terms are not guessed: it is published as a "
    "restriction whose terms are not stated.")


def _peak_text(oid, evidence, reviewed):
    _w, _on, parish, path, local, _where, title = PEAK[oid]
    return {
        "council": "Derbyshire County Council", "authorities": ["Derbyshire"],
        "parish": parish, "path_as_written": path, "local_name": local,
        "vehicles": "other", "label": LABEL, "form": "permanent",
        "made_by": PARK, "restriction": _PEAK_RESTRICTION,
        "source_url": DY_PAGE, "evidence": _PEAK_EVIDENCE + evidence,
        "status": "approved", "review_note": _PEAK_NOTE,
        "reviewed": reviewed, "order_name": title}


_CB_URL = ("https://www.centralbedfordshire.gov.uk/leisure-and-countryside/"
           "countryside/rights-way/byways-open-all-traffic")
_CB_DROVERS_EVIDENCE = (
    "Dunstable BOAT 4, Totternhoe BOAT 12 , known as The Drovers Way – "
    "Motor Vehicles, Motor Cycles (part) and Carriages are permanently "
    "banned - LU6 2AP - Grid Reference TL 007 214 to SP 991 222 (Tring Road "
    "to BOAT 47). Length: 1930 metres.")
_CB_DROVERS_NOTE = (
    "The page bans motor vehicles and carriages on the whole of The Drovers "
    "Way (Dunstable BOAT 4 and Totternhoe BOAT 12, 1922 m together against "
    "the 1930 m it gives), and motorcycles on part of it without saying "
    "which part; by the owner's rule motorcycles are shown banned on the "
    "whole lane.")
_CB_WHEEL_EVIDENCE = (
    "Totternhoe BOAT 42 & 45 - Known as Wheelbarrow Green Highway - Motor "
    "Vehicles (part), and Carriages are permanently banned - LU6 2BT - Grid "
    "Reference SP 979 226 to SP 986 215 (BOAT 24 to BOAT 3) Length: 1140 "
    "metres.")
_CB_WHEEL_NOTE = (
    "The page bans carriages on the whole of Wheelbarrow Green Highway and "
    "'Motor Vehicles (part)', without saying which part; Totternhoe BOAT %s "
    "is one of its two byways (42 and 45 are 1147 m together against the "
    "1140 m the page gives). The same list names 'Motor Cycles' apart from "
    "'Motor Vehicles' for The Drovers Way, so here motorcycles are not "
    "banned.")
_ES_URL = ("https://www.eastsussex.gov.uk/leisure-tourism/discover-east-"
           "sussex/rights-of-way/east-sussex/seasonal-byway-restrictions")
_ES_EVIDENCE = (
    "The following byways are subject to seasonal restrictions between 1st "
    "October and 31st March each year ... Byways closed to vehicles "
    "(excluding motor and pedal cycles, quadricycles and horse drawn "
    "vehicles) ... Public Byway ")
_SU_TEXT = (
    "No vehicular traffic shall proceed in that part of Byway Open to All "
    "Traffic 47 (Esher) known as New Road Claygate from its junction with "
    "the B280 Fairoak Lane northwards for a distance of approximately 890 "
    "metres to the southern side of the junction of Holroyd Road with "
    "Coverts Road")


def _cb(parish, n, local, restriction, grids, evidence, note):
    return {
        "council": "Central Bedfordshire Council",
        "authorities": ["Central Bedfordshire"], "parish": parish,
        "path_as_written": "BOAT %s" % n, "grid_refs": grids,
        "restriction": restriction, "source_url": _CB_URL,
        "evidence": evidence, "status": "approved", "review_note": note,
        "local_name": local}


_DROVERS_GRIDS = ["TL 007 214", "SP 991 222"]
_WHEEL_GRIDS = ["SP 979 226", "SP 986 215"]
_CHAPEL = _DY + "chapel-en-le-frith/boat-144-and-edale-boat-16-chapel-gate"
TEXT = {
    _DY + "outseats/boat-48-long-causeway": _peak_text(
        _DY + "outseats/boat-48-long-causeway",
        "Long Causeway, Outseats - Byway Open to All Traffic 48",
        "pinned 2026-10-14: Outseats BOAT 48, all three of its sections in "
        "Derbyshire's byway data."),
    _CHAPEL: _peak_text(
        _CHAPEL,
        "Chapel Gate, Chapel-en-le-Frith - Byway Open to All Traffic 144 "
        "continuing as Edale - Byway Open to All Traffic 16",
        "pinned 2026-10-14: Chapel-en-le-Frith BOAT 144 (two sections) and "
        "Edale BOAT 16 (three sections), which meet end to end. The other "
        "parishes' BOAT 16s (Abney, Brough and Shatton, Stoney Middleton) "
        "are not this route and are not pinned."),
    _DY + "little-longstone/boat-14-leys-lane": _peak_text(
        _DY + "little-longstone/boat-14-leys-lane",
        "Leys Lane, Little Longstone - Byway Open to All Traffic 14",
        "pinned 2026-10-14: Little Longstone BOAT 14, all four of its "
        "sections."),
    _DY + "monyash/boat-29-derby-lane": _peak_text(
        _DY + "monyash/boat-29-derby-lane",
        "Derby Lane - Monyash Byway Open to All Traffic 29",
        "pinned 2026-10-14: Monyash BOAT 29, both sections. Eyam BOAT 29 is "
        "not this route and is not pinned."),
    ROYCH: {
        "council": "Derbyshire County Council", "authorities": ["Derbyshire"],
        "parish": "Chapel-en-le-Frith",
        "path_as_written": "The Roych - non-classified highway",
        "local_name": "The Roych", "vehicles": "other", "label": LABEL,
        "form": "permanent", "made_by": PARK,
        "restriction": _PEAK_RESTRICTION, "source_url": DY_PAGE,
        "evidence": _PEAK_EVIDENCE + "The Roych - non-classified highway in "
        "Chapel-en-le-Frith and Chinley, Buxworth and Brownside",
        "status": "approved",
        "review_note": _PEAK_NOTE + " The Roych is an unclassified road, and "
        "we carry no Derbyshire unsurfaced road layer, so the build holds it "
        "back until we do.",
        "order_name": _peak_title("The Roych")},
    "central-bedfordshire-council/dunstable/boat-4": _cb(
        "Dunstable", 4, _DROVERS_LOCAL, _DROVERS.split(": ", 1)[1],
        _DROVERS_GRIDS, _CB_DROVERS_EVIDENCE, _CB_DROVERS_NOTE),
    "central-bedfordshire-council/totternhoe/boat-12": _cb(
        "Totternhoe", 12, _DROVERS_LOCAL, _DROVERS.split(": ", 1)[1],
        _DROVERS_GRIDS, _CB_DROVERS_EVIDENCE, _CB_DROVERS_NOTE),
    "central-bedfordshire-council/totternhoe/boat-42": _cb(
        "Totternhoe", 42, _WHEEL_LOCAL, _WHEEL.split(": ", 1)[1],
        _WHEEL_GRIDS, _CB_WHEEL_EVIDENCE, _CB_WHEEL_NOTE % 42),
    "central-bedfordshire-council/totternhoe/boat-45": _cb(
        "Totternhoe", 45, _WHEEL_LOCAL, _WHEEL.split(": ", 1)[1],
        _WHEEL_GRIDS, _CB_WHEEL_EVIDENCE, _CB_WHEEL_NOTE % 45),
    "east-sussex-county-council/crowborough/byway-30c-and-b-part": {
        "council": "East Sussex County Council",
        "authorities": ["East Sussex"], "parish": "Crowborough",
        "path_as_written": "Byway 30b and 30c",
        "grid_refs": ["TQ 510 296", "TQ 510 294"], "restriction": _ES30,
        "source_url": _ES_URL,
        "evidence": _ES_EVIDENCE + "Crowborough 30c and b (part) – TQ "
        "510 296 to TQ 510 294",
        "status": "approved",
        "review_note": "The order covers Crowborough byway 30c and part of "
        "30b, TQ 510 296 to TQ 510 294 (about 200 m); our 30b is 195 m and "
        "30c 122 m.",
        "local_name": "the order covers 30c and part of 30b; the whole lane "
        "is shown"},
    "east-sussex-county-council/heathfield-and-waldron/byway-115-part-of": {
        "council": "East Sussex County Council",
        "authorities": ["East Sussex"], "parish": "Heathfield and Waldron",
        "path_as_written": "Byway 115",
        "grid_refs": ["TQ 564 118", "TQ 562 188"], "restriction": _ES115,
        "source_url": _ES_URL,
        "evidence": _ES_EVIDENCE + "Heathfield and Waldron 115 (part of) - "
        "TQ 564 118 to TQ 562 188",
        "status": "approved",
        "review_note": "The order covers part of Heathfield and Waldron byway "
        "115. Its second grid reference, TQ 562 188, is on our 426 m lane; "
        "the first, TQ 564 118, is 6.8 km away and looks mistyped, so the "
        "part is not placed. Checked 14 Oct 2026: we carry exactly one "
        "Heathfield and Waldron byway 115, TQ 562 188 is 12 m from it, and "
        "no East Sussex byway lies within 1.6 km of TQ 564 118 (TQ 564 188, "
        "a likely intended reading, is 94 m from it).",
        "local_name": _ES115_LOCAL},
    "surrey-county-council/esher-claygate/47": {
        "council": "Surrey County Council", "authorities": ["Surrey"],
        "parish": "Claygate", "path_as_written": "BOAT 47",
        "local_name": "New Road", "order_name": _ELMBRIDGE,
        "order_date": "1996", "restriction": _SU_TEXT + " Claygate.",
        "source_url": "https://www.surreycc.gov.uk/__data/assets/pdf_file/"
        "0019/225037/List-of-BOATs-and-TROs-April-2023.pdf",
        "evidence": _SU_TEXT, "status": "approved",
        "review_note": "The order is 'The Borough of Elmbridge (BOAT 47; New "
        "Road, Claygate) Road Closure order 1996'. The order covers about "
        "890 m of BOAT 47 (New Road, Claygate), from the B280 Fairoak Lane "
        "north to Holroyd Road. We carry BOAT 47 as Claygate 47 (175 m) and "
        "Esher 47 (717 m), 892 m end to end, so the part is in effect all "
        "of it."},
}
for _oid, (_ways, _on) in PARTIAL.items():
    TEXT[_oid]["reviewed"] = _RULE % (", ".join(_ways),
                                      "lanes" if len(_ways) > 1 else "lane")
for _oid, (_v, _form, _a, _where, _title) in PARTIAL_TEXT.items():
    TEXT[_oid]["vehicles"] = _v
    TEXT[_oid]["form"] = _form

# A second layer under the value pins: what a name or a number looks like.
_PERSONAL = [
    re.compile(r"@"),
    re.compile(r"(\+44|\(0\d{2,4}\)|\b0\d{2,4}[ -]?\d{3}[ -]?\d{3,4}\b|"
               r"\b07\d{3} ?\d{6}\b)"),
    re.compile(r"\b(mr|mrs|ms|miss|dr|cllr|councillor|officer)\b\.? [A-Z]",
               re.I),
]


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
                self.assertEqual(o["authorities"], ["Derbyshire"])
                self.assertEqual(o["made_by"], PARK)
                self.assertEqual(o["form"], "permanent")
                # The page names the routes, not the terms: never guessed.
                self.assertEqual(o["vehicles"], "other")
                self.assertEqual(o["label"], LABEL)
                self.assertNotIn("start", o)
                self.assertNotIn("end", o)
                self.assertIn(o["path_as_written"].split(" - ")[0],
                              o["evidence"])

    def test_each_park_order_names_its_lane_by_value(self):
        for oid, (_w, _on, parish, path, local, _where, title) in \
                PEAK.items():
            with self.subTest(oid):
                o = _order(oid)
                self.assertEqual(o["parish"], parish)
                self.assertEqual(o["path_as_written"], path)
                self.assertEqual(o["local_name"], local)
                self.assertEqual(o["order_name"], title)
                self.assertEqual(title, _peak_title(local))

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

    def test_each_order_holds_exactly_its_fields(self):
        self.assertEqual(set(KEYS), set(PINNED) | {ROYCH})
        for oid, keys in KEYS.items():
            with self.subTest(oid):
                self.assertEqual(set(_order(oid)), keys)

    def test_every_field_of_each_order_is_pinned_by_value(self):
        # Every field but id, ways, paths and season, which are pinned
        # elsewhere or hold no words. Nothing here can carry a name.
        self.assertEqual(set(TEXT), set(PINNED) | {ROYCH})
        for oid, want in TEXT.items():
            with self.subTest(oid):
                o = _order(oid)
                got = dict((k, v) for k, v in o.items()
                           if k not in ("id", "ways", "paths", "season"))
                self.assertEqual(got, want)

    def test_no_personal_data_in_the_new_entries(self):
        for oid in list(PINNED) + [ROYCH]:
            text = json.dumps(_order(oid), ensure_ascii=False)
            for pattern in _PERSONAL:
                with self.subTest(oid, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))

    def test_the_personal_data_layer_catches_what_it_is_for(self):
        for bad in ("Ring 01629 533190", "(01629) 533190", "+44 1629 533190",
                    "07700 900123", "Cllr Brown", "councillor Smith",
                    "Officer Jones", "a.b@example.org", "Mrs Smith"):
            with self.subTest(bad):
                self.assertTrue(any(p.search(bad) for p in _PERSONAL))
        for fine in ("TQ 510 296 to TQ 510 294", "SP 979 226",
                     "BOAT 47 (Esher)", "1930 metres", "order 1996"):
            with self.subTest(fine):
                self.assertFalse(any(p.search(fine) for p in _PERSONAL))


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
        # A pinned day inside the season: every order here is permanent
        # with no end, or seasonal from 1 October to 31 March.
        for oid in PINNED:
            item = self.items.get(oid)
            if item is None:
                continue
            with self.subTest(oid):
                self.assertNotIn("end", item)
                feature = CO.feature_of(item, self.out["source"], IN_SEASON)
                self.assertIsNotNone(feature, "drawn on " + IN_SEASON)
                self.assertEqual(feature["properties"]["ways"],
                                 PINNED[oid][0])

    def test_a_seasonal_order_is_not_drawn_out_of_its_season(self):
        # More than 90 days before 1 October the season is not yet drawn.
        seasonal = [oid for oid, t in PARTIAL_TEXT.items()
                    if t[1] == "seasonal"]
        self.assertEqual(len(seasonal), 2)
        for oid in seasonal:
            item = self.items.get(oid)
            if item is None:
                continue
            with self.subTest(oid):
                self.assertIsNone(
                    CO.feature_of(item, self.out["source"], OUT_OF_SEASON))

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
        self.assertIn("Derbyshire", self.out["source"]["authorities"])
        for oid, (_w, _on, _p, _path, _l, where, title) in PEAK.items():
            item = self.items.get(oid)
            if item is None:
                continue
            with self.subTest(oid):
                # The synthetic ways above take their authority from the
                # item, so it is pinned here by value.
                self.assertEqual(item["authority"], "Derbyshire")
                code, otype, label = CO.label_for(item)
                self.assertEqual(otype, "other")
                self.assertEqual(label, LABEL)
                self.assertNotIn("start", item)
                self.assertNotIn("end", item)
                self.assertEqual(item["where"], where)
                self.assertEqual(item["title"], title)
                self.assertEqual(item["ref"], title)

    def test_each_partial_order_reads_as_reviewed(self):
        for oid, text in PARTIAL_TEXT.items():
            vehicles, form, authority, where, title = text
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

    def test_no_title_is_cut_short(self):
        # The build cuts a title at 240 characters.
        for oid in PINNED:
            item = self.items.get(oid)
            if item is None:
                continue
            with self.subTest(oid):
                self.assertLess(len(item["title"]), 240)

    def test_the_elmbridge_order_is_named_by_its_council(self):
        item = self.items["surrey-county-council/esher-claygate/47"]
        self.assertEqual(item["ref"], _ELMBRIDGE)
        self.assertEqual(item["title"], _ELMBRIDGE)

    def test_exactly_these_orders_were_pinned_in_this_change(self):
        self.assertEqual(set(PINNED), {
            _DY + "outseats/boat-48-long-causeway",
            _CHAPEL,
            _DY + "little-longstone/boat-14-leys-lane",
            _DY + "monyash/boat-29-derby-lane",
            "central-bedfordshire-council/dunstable/boat-4",
            "central-bedfordshire-council/totternhoe/boat-12",
            "central-bedfordshire-council/totternhoe/boat-42",
            "central-bedfordshire-council/totternhoe/boat-45",
            "east-sussex-county-council/crowborough/byway-30c-and-b-part",
            "east-sussex-county-council/heathfield-and-waldron/"
            "byway-115-part-of",
            "surrey-county-council/esher-claygate/47"})
        self.assertEqual(set(PARTIAL_TEXT), set(PARTIAL))
        published = set(i["id"] for i in self.out["items"]) & set(PINNED)
        held = set(self.held) & set(PINNED)
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

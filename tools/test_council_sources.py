#!/usr/bin/env python3
"""Each council source is read the way it publishes, no personal data is
kept, and a failed read never blanks what was published.

    python tools/test_council_sources.py

No network: each source is answered by a stand-in serving records shaped as
the council's own service returned them on 7 October 2026 (trimmed). The
byways are synthetic, laid where those records' geometry is.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_sources as cs  # noqa: E402
from byway_match import Byways, Way  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402
from polite_http import FetchFailed, Refused  # noqa: E402


def _bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def bng_line(*points):
    return [list(p) for p in points]


def way_at(uid, authority, name, *bng):
    return Way(uid, authority, name,
               [[grid_to_wgs84(e, n) for e, n in bng]])


class Client(object):
    """get_json by URL substring; raises what it is told to."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []
        self.requests = 0

    def get_json(self, url):
        self.asked.append(url)
        for key, value in self.answers.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise FetchFailed("HTTP 404 for %s" % url)


def esri(attrs, *points):
    return {"attributes": attrs,
            "geometry": {"paths": [bng_line(*points)]} if points else None}


# ------------------------------------------------------------------ Dorset

DORSET = {"type": "FeatureCollection", "features": [
    {"type": "Feature",
     "geometry": {"type": "MultiLineString",
                  "coordinates": [[[372000, 105000], [372400, 105000]]]},
     "properties": {"route_code": "S51/5",
                    "status": "Byway open to all traffic",
                    "closure_start_date": "2025-09-10Z",
                    "closure_end_date": "2027-03-10Z",
                    "closure_is_indefinite": False,
                    "operationalstatus": "Closed",
                    "parish": "West Knighton CP",
                    "closure_notification_document":
                        "https://example.dorset/notice.pdf"}},
    {"type": "Feature",
     "geometry": {"type": "MultiLineString",
                  "coordinates": [[[334307, 92146], [334378, 92152]]]},
     "properties": {"route_code": "W2/2", "status": "Footpath",
                    "operationalstatus": "Closed",
                    "parish": "Lyme Regis CP"}},
]}


class Dorset(unittest.TestCase):
    def test_only_byways_are_read_and_dates_come_through(self):
        n, got = cs.read_dorset(Client({"route_closed": DORSET}))
        self.assertEqual(n, 2)
        self.assertEqual([g["ref"] for g in got], ["S51/5"])
        self.assertEqual((got[0]["start"], got[0]["end"]),
                         ("2025-09-10", "2027-03-10"))
        self.assertEqual(got[0]["vehicles"], "all_users")
        self.assertEqual(got[0]["url"], "https://example.dorset/notice.pdf")


# ------------------------------------------------------------------- Essex

ESSEX = {"features": [
    esri({"GlobalID": "g1", "Item_Type": "Prohibition", "Path_Type": "Byway",
          "Parish": "Black Notley", "Path_number": 9, "Category": "Seasonal",
          "Closure_Description": "Seasonal Prohibtion of Driving Order "
                                 "(1 November - 31 March)  Motorcycles "
                                 "exempted",
          "Valid_From": 847670400000,
          "File_Link": "https://example.essex/tro.pdf"},
         (575000, 220000), (575500, 220000)),
    esri({"GlobalID": "g2", "Item_Type": "Diversion", "Path_Type": "Byway",
          "Parish": "Debden", "Path_number": 37, "Category": "Temporary",
          "Closure_Description": "alternative route"},
         (550000, 233000), (551000, 233000)),
    esri({"GlobalID": "g3", "Item_Type": "Prohibition",
          "Path_Type": "Restricted Byway", "Parish": "Debden",
          "Path_number": 5, "Category": "Permanent"},
         (550000, 234000), (551000, 234000)),
]}


class Essex(unittest.TestCase):
    def test_a_diversion_is_never_read_as_a_closure(self):
        _n, got = cs.read_essex(Client({"Essex_PRoW": ESSEX}))
        self.assertEqual([g["id"] for g in got], ["g1"])

    def test_a_seasonal_motorcycles_exempt_order(self):
        _n, got = cs.read_essex(Client({"Essex_PRoW": ESSEX}))
        g = got[0]
        self.assertEqual(g["vehicles"], "motor_vehicles_except_motorcycles")
        self.assertEqual(g["form"], "seasonal")
        self.assertEqual(g["season"], {"from": "11-01", "to": "03-31"})
        self.assertEqual(g["start"], "1996-11-11")


# ---------------------------------------------------------- Northumberland

PERSONAL = ("DBrookes@northumberland.gov.uk", "David Brookes", "01670",
            "Courtyard Gardens", "Bellway", "Hanson")
NLAND_PTR = {"features": [esri({
    "OBJECTID": 1, "KEYID": "538/054", "TYPE": "PTR", "ORDER_TYPE": 1,
    "APPLICANT": "Northumberland County Council",
    "PURPOSE_REASON": "To prohibit the use of the route by mechanically "
                      "propelled vehicles.",
    "START_ORDER_DATE": 1064966400000,
    "DESC_EXISTING": "From the junction with Bridleway No 15 eastwards.",
    "CONTACT": "David Brookes  Tel: 01670 624134  Email:  "
               "DBrookes@northumberland.gov.uk"},
    (390000, 560000), (391000, 560000))]}
NLAND_TTR = {"features": [
    esri({"KEYID": "538/054", "TYPE": "TTR", "Start_date": "22nd July 2024",
          "End_date": "23rd December 2026",
          "Reason": "Closure requested by the owner of 1 Courtyard Gardens",
          "WhoBy": "Bellway Homes", "GlobalID": "t1",
          "Expiry_Date": 1797984000000},
         (390000, 560000), (390400, 560000))]}


class Northumberland(unittest.TestCase):
    def client(self):
        return Client({"PROW_RightsOfWayClosures": NLAND_PTR,
                       "MASTER_VIEW": NLAND_TTR})

    def test_permanent_and_temporary_orders(self):
        n, got = cs.read_northumberland(self.client())
        self.assertEqual(n, 2)
        perm = [g for g in got if g["form"] == "permanent"][0]
        temp = [g for g in got if g["form"] == "temporary"][0]
        self.assertEqual(perm["vehicles"], "motor_vehicles")
        self.assertEqual(perm["start"], "2003-10-01")
        self.assertEqual((temp["start"], temp["end"]),
                         ("2024-07-22", "2026-12-23"))

    def test_no_contact_applicant_or_householder_is_kept(self):
        byways = Byways([way_at("ND-054", "Northumberland",
                                "Byway open to all traffic (BOAT) 538 054",
                                (390000, 560000), (391000, 560000))])
        tmp = tempfile.mkdtemp()
        try:
            source = dict(cs.by_id()["northumberland-closures"])
            source["read"] = lambda _c: cs.read_northumberland(self.client())
            entry = cs.fetch_one(source, None, byways, tmp, "2026-10-07")
            self.assertTrue(entry["ok"], entry)
            text = _bytes(os.path.join(
                tmp, "northumberland-closures.json")).decode("utf-8")
            for word in PERSONAL:
                self.assertNotIn(word, text)
            self.assertEqual(entry["items"], 2)
        finally:
            shutil.rmtree(tmp)


# --------------------------------------------------------------- Lancashire


class Lancashire(unittest.TestCase):
    def test_names_and_emails_are_stripped_from_the_summary(self):
        data = {"features": [esri({
            "PATH_TYPE": "Byway Open to all Traffic", "PathTypeShort": "BT",
            "PATH_NUMBE": 348, "D_PARISH": "Rawtenstall", "DISTRICT": 14,
            "PARISH": 4, "PathRefLong": "BT1404348",
            "TempClosureSummary": "Temporary Closure (2163): Dangerous "
                                  "Surface - Tom Partridge Pendle Bc Contact"
                                  " tom@example.gov.uk (until December 2026)"},
            (380000, 422000), (380100, 422000))]}
        _n, got = cs.read_lancashire(Client({"Public_Rights_of_Way": data}))
        g = got[0]
        self.assertNotIn("Partridge", json.dumps(g))
        self.assertNotIn("@", json.dumps(g))
        self.assertEqual(g["end"], "2026-12-31")
        self.assertEqual(g["refs"], [("1404", "348")])


# ----------------------------------------------------------------- Suffolk


class Suffolk(unittest.TestCase):
    def read(self, conditions):
        data = {"features": [esri({
            "TRO_Ref": "PTRO001", "DM_Parish": "Acton", "Status": "Byway",
            "Route_No": "29", "Suffix": "0", "Conditions": conditions,
            "Link": "https://example.suffolk/ptro001.pdf"},
            (590000, 245000), (590300, 245000))]}
        return cs.read_suffolk_tros(Client({"ROWTRO_View": data}))[1][0]

    def test_a_season_about_horses_does_not_make_a_motor_ban_seasonal(self):
        g = self.read("Motor vehicles and horse-drawn vehicles prohibited all"
                      " year. No person shall drive, ride or lead a horse "
                      "between 1 October to 30 April.")
        self.assertEqual((g["vehicles"], g["form"]),
                         ("motor_vehicles", "permanent"))

    def test_a_winter_ban(self):
        g = self.read("Motor vehicles prohibited from 1 October to 30 April.")
        self.assertEqual(g["form"], "seasonal")
        self.assertEqual(g["season"], {"from": "10-01", "to": "04-30"})


# ------------------------------------------------------------ Hertfordshire


class Hertfordshire(unittest.TestCase):
    def test_a_specified_vehicles_order_is_never_published_as_a_ban(self):
        data = {"features": [
            esri({"PATHNAME": "TRING TOWN 028", "PARISH": "TRING TOWN",
                  "PATHNUMB": "028", "UNITID": "1", "OBJECTID": 1,
                  "PTROTYPE": "Prohibiting Use of Specified Vehicles",
                  "PTROYEAR": "1987"}, (492000, 211000), (492500, 211000)),
            esri({"PATHNAME": "KNEBWORTH 041", "PARISH": "KNEBWORTH",
                  "PATHNUMB": "041", "UNITID": "2", "OBJECTID": 2,
                  "PTROTYPE": "Prohibiting Use of Motor Vehicles",
                  "PTROYEAR": "2011"}, (523000, 221000), (523500, 221000)),
            esri({"PATHNAME": "ARDELEY 005", "PARISH": "ARDELEY",
                  "PATHNUMB": "005", "UNITID": "3", "OBJECTID": 3},
                 (532000, 227000), (532400, 227000)),
        ]}
        n, got = cs.read_hertfordshire(Client({"row/MapServer": data}))
        self.assertEqual(n, 3)
        by = dict((g["id"].split("|")[0], g) for g in got)
        self.assertEqual(sorted(by), ["1", "2"], "a path with no order")
        self.assertEqual(by["1"]["vehicles"], "other")
        self.assertIn("see the order", by["1"]["label"])
        self.assertEqual(by["2"]["vehicles"], "motor_vehicles")
        self.assertIn("2011", by["2"]["title"])


class WestBerkshire(unittest.TestCase):
    ROWS = [
        esri({"OBJECTID": 1, "Id": 3, "Routecode": "Beed/22/2",
              "TRO_title": "West Berkshire District Council (Byway Open to "
                           "All Traffic - Beedon 22 (part)) Prohibition of "
                           "motor vehicles or vehicles (except motorcycles)) "
                           "Order 2010",
              "Notes": "Prohibition of all motor vehicles except motorbikes "
                       "between 1st October and 31st May each year",
              "PermOrTemp": "Permanent", "Active": "y",
              "Start": 1287100800000, "Finish": 32503680000000},
             (450000, 175000), (450500, 175000)),
        esri({"OBJECTID": 2, "Id": None, "Routecode": "LAMB/2/2",
              "TRO_title": "https://one.network/?tmi=GB1. Gas works",
              "Notes": "https://one.network/?tmi=GB1.",
              "PermOrTemp": "Temp", "Active": "y"},
             (440000, 175000), (440100, 175000)),
        esri({"OBJECTID": 3, "Id": 36, "Routecode": "Buck/54a/1",
              "TRO_title": "WBDC temporary closure of public bridleway "
                           "Buck54a. For information contact Broadview "
                           "Farm on 0790 607 922",
              "Notes": "Demolition works", "PermOrTemp": "Temp",
              "Active": "y", "Start": 1620086400000,
              "Finish": 1856908800000},
             (445000, 172000), (445300, 172000)),
    ]

    def read(self):
        return cs.read_west_berkshire(Client({
            "PUBLIC_RIGHTS_OF_WAY_CLOSURES": {"features": self.ROWS}}))

    def test_the_seasonal_ban_is_seasonal_with_its_season(self):
        n, got = self.read()
        self.assertEqual(n, 3)
        beedon = [g for g in got if g["ref"] == "Beed/22/2"][0]
        self.assertEqual(beedon["vehicles"],
                         "motor_vehicles_except_motorcycles")
        self.assertEqual(beedon["form"], "seasonal")
        self.assertEqual(beedon["season"], {"from": "10-01", "to": "05-31"})
        self.assertEqual(beedon["refs"], [("Beed", "22")])

    def test_one_network_rows_are_not_taken(self):
        _n, got = self.read()
        self.assertFalse(any("one.network" in json.dumps(g) for g in got))

    def test_a_bridleway_closure_does_not_claim_a_byway(self):
        _n, got = self.read()
        buck = [g for g in got if g["ref"] == "Buck/54a/1"][0]
        self.assertNotIn("0790", buck["title"], "a phone number was kept")
        self.assertFalse(buck["claims_byway"])
        self.assertEqual(buck["form"], "temporary")


class IsleOfWight(unittest.TestCase):
    def test_closed_is_held_for_review_never_published(self):
        data = {"features": [
            esri({"OBJECTID": 7, "P_NUMBER": "S26", "COMMENT": "closed"},
                 (446000, 85000), (446400, 85000)),
            esri({"OBJECTID": 8, "P_NUMBER": "V60", "COMMENT": "surfaced"},
                 (450000, 85000), (450400, 85000))]}
        n, cands = cs.read_iow_comments(Client({"PublicRightsOfWay": data}))
        self.assertEqual((n, [c["ref"] for c in cands]), (2, ["S26"]))
        byways = Byways([way_at("IW-S26", "Isle of Wight",
                                "Byway open to all traffic (BOAT) S 26",
                                (446000, 85003), (446400, 85003))])
        items, _u, review = cs.match(cands, byways, "Isle of Wight")
        self.assertEqual(items, [])
        self.assertEqual(review[0]["ways"], ["IW-S26"])
        self.assertIn("confirm", review[0]["why"])


# ------------------------------------------------- devon, somerset (by ref)


class ByReference(unittest.TestCase):
    def test_devon_names_a_byway_by_parish_and_number(self):
        self.assertEqual(cs.devon_refs(
            "Stockland Byway 34 temporary closure", ""), [("Stockland", "34")])
        self.assertEqual(cs.devon_refs(
            "Hartland Bridleway 63 temporary closure", ""), [])
        self.assertEqual(cs.devon_refs(
            "x", "BYWAY OPEN TO ALL TRAFFIC No. 25, CHIVELSTONE"),
            [("Chivelstone", "25")])

    def test_a_restricted_byway_is_not_a_byway(self):
        self.assertEqual(cs.devon_refs(
            "Enmore Restricted Byway 19 temporary closure", ""), [])

    def test_somerset_reads_path_codes(self):
        data = [{"id": 1, "title": {"rendered":
                 "Temporary Closure of Part of WS 10/33/1"},
                 "acf": {"expiry_date": "01/12/2026", "type_of_work": ""},
                 "link": "https://example.somerset/1"}]
        client = Client({"row_closures": data, "/tro": []})
        _n, got = cs.read_somerset(client)
        self.assertEqual(got[0]["refs"], [("WS", "10/33/1")])
        self.assertEqual(got[0]["end"], "2026-12-01")
        byways = Byways([way_at("ST-10/33/1", "Somerset",
                                "Byway open to all traffic (BOAT) WS 10/33/1",
                                (330000, 145000), (330500, 145000))])
        items, unmatched, _review = cs.match(got, byways, "Somerset")
        self.assertEqual(items[0]["ways"], ["ST-10/33/1"])
        self.assertEqual(items[0]["match"], "reference")
        self.assertEqual(items[0]["geometry"]["type"], "LineString")


# ------------------------------------------------------- keep the last good


class KeepLastGood(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.byways = Byways([way_at(
            "DT-5", "Dorset", "Byway open to all traffic (BOAT) S51 5",
            (372000, 105000), (372400, 105000))])
        self.source = dict(cs.by_id()["dorset-closures"])
        self.path = os.path.join(self.tmp, "dorset-closures.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_with(self, answer):
        self.source["read"] = lambda c: cs.read_dorset(
            Client({"route_closed": answer}))
        return cs.fetch_one(self.source, None, self.byways, self.tmp,
                            "2026-10-07")

    def good(self):
        entry = self.run_with(DORSET)
        self.assertTrue(entry["ok"], entry)
        self.assertEqual(entry["items"], 1)
        return _bytes(self.path)

    def test_a_source_that_cannot_be_read_leaves_its_file_alone(self):
        before = self.good()
        for fault in (FetchFailed("HTTP 500"), Refused("HTTP 403")):
            entry = self.run_with(fault)
            self.assertFalse(entry["ok"])
            self.assertEqual(_bytes(self.path), before)

    def test_a_source_that_comes_back_empty_is_a_bad_read(self):
        before = self.good()
        entry = self.run_with({"features": []})
        self.assertFalse(entry["ok"])
        self.assertIn("kept the last good read", entry["error"])
        self.assertEqual(_bytes(self.path), before)

    def test_a_collapse_is_a_bad_read_and_a_dip_is_not(self):
        self.assertIsNone(cs.guard(None, 0))
        self.assertIsNone(cs.guard(30, 25))
        self.assertIsNotNone(cs.guard(30, 9))
        self.assertIsNone(cs.guard(4, 1), "a layer of four may fall to one")

    def test_an_unchanged_read_is_not_rewritten(self):
        self.good()
        entry = self.run_with(DORSET)
        self.assertFalse(entry["changed"])

    def test_the_run_fails_only_when_every_source_fails(self):
        # main() over a stand-in source list: one down, one fine -> 0;
        # both down -> 1.
        ok = dict(self.source, id="ok-src",
                  read=lambda c: cs.read_dorset(
                      Client({"route_closed": DORSET})))
        bad = dict(self.source, id="bad-src",
                   read=lambda c: (_ for _ in ()).throw(FetchFailed("down")))
        saved = (cs.SOURCES, cs.main.__globals__["SOURCES"])
        import byway_match
        real_load = byway_match.load_byways
        byway_match.load_byways = lambda: _Many(self.byways)
        try:
            # Quietly: its ::warning:: lines would otherwise reach the
            # Actions log as annotations about sources that do not exist.
            with contextlib.redirect_stdout(io.StringIO()):
                cs.SOURCES = [ok, bad]
                self.assertEqual(cs.main(["fetch", "--out", self.tmp]), 0)
                cs.SOURCES = [dict(bad), dict(bad, id="bad-2")]
                self.assertEqual(cs.main(["fetch", "--out", self.tmp]), 1)
        finally:
            cs.SOURCES = saved[0]
            byway_match.load_byways = real_load


class _Many(object):
    """Byways that report a full country, so main()'s sanity floor passes."""

    def __init__(self, inner):
        self.inner = inner

    def __len__(self):
        return 10000

    def __getattr__(self, name):
        return getattr(self.inner, name)


class Registry(unittest.TestCase):
    def test_west_berkshire_is_read_from_its_open_host_not_gis2(self):
        source = cs.by_id()["west-berkshire-closures"]
        self.assertNotIn("blocked", source)
        self.assertTrue(source["endpoint"].startswith(
            "https://gis.westberks.gov.uk/"))
        from polite_http import blocked, host_of
        self.assertFalse(blocked(host_of(source["endpoint"])))
        self.assertTrue(blocked("gis2.westberks.gov.uk"))
        self.assertTrue(blocked("www.westberks.gov.uk"))

    def test_every_source_is_credited_by_name_and_licence(self):
        for s in cs.SOURCES:
            self.assertTrue(s["name"] and s["licence"] and s["authority"],
                            s["id"])
            self.assertTrue(s["endpoint"].startswith("https://"), s["id"])


if __name__ == "__main__":
    unittest.main(verbosity=1)

#!/usr/bin/env python3
"""Ceredigion's live road closures are read for what they are: a closure is a
closure, a diversion route is never one, only "Live" records are read, the
dates are the council's own, a bad read never replaces a good one, and the
`applicant` field is never even asked for.

    python tools/test_ceredigion_closures.py

No network. tools/fixtures/ceredigion/road_closures_live_ctc.json is eight
features of the council's own GetFeature answer of 8 October 2026, unedited
apart from the collection's three counts, set to eight to match (the request
never asked for `applicant`, so it is absent). CE-13 is the one Ceredigion
byway a live feature touched that day, copied from containers/ways-wales.tbmap;
the other byway is synthetic, laid where a closure's own line is.
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ceredigion_closures as cc  # noqa: E402
import council_orders  # noqa: E402
import council_sources as cs  # noqa: E402
import polite_http  # noqa: E402
from byway_match import Byways, Way  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402
from polite_http import FetchFailed, Refused  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "ceredigion",
                       "road_closures_live_ctc.json")
DAY = "2026-10-08"

# Byway open to all traffic (BOAT) 68 13, as published on 8 October 2026.
# The live diversion route for closure 160/26 runs the whole of it.
CE13 = Way("CE-13-7a94505f28", "Ceredigion",
           "Byway open to all traffic (BOAT) 68 13",
           [[(-4.54567, 52.13373), (-4.54559, 52.13359), (-4.54548, 52.1335),
             (-4.54544, 52.13345), (-4.54544, 52.1332)]])


def fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def by_id(data, record_id):
    return next(f for f in data["features"]
                if f["properties"]["id"] == record_id)


def under(feature, uid="CE-SYN"):
    """A synthetic Ceredigion byway laid along a feature's own line."""
    lines = feature["geometry"]["coordinates"]
    if feature["geometry"]["type"] == "LineString":
        lines = [lines]
    return Way(uid, "Ceredigion", "Byway open to all traffic (BOAT) 99 1",
               [[grid_to_wgs84(e, n) for e, n in l] for l in lines])


class Client(object):
    """get_json by URL substring; raises what it is told to."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def get_json(self, url):
        self.asked.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def read(data=None):
    return cs.read_ceredigion(Client(fixture() if data is None else data))


def ids(candidates):
    return sorted(int(c["id"].split("|")[1]) for c in candidates)


# ------------------------------------------------------------------- dates


class Dates(unittest.TestCase):
    def test_the_councils_yyyymmdd_dates_come_through(self):
        _n, got = read()
        by = dict((int(c["id"].split("|")[1]), c) for c in got)
        self.assertEqual((by[281]["start"], by[281]["end"]),
                         ("2025-04-30", "2026-10-30"))
        # A one-day event closure: starts and ends on the same day.
        self.assertEqual((by[2189]["start"], by[2189]["end"]),
                         ("2026-10-31", "2026-10-31"))

    def test_a_closure_is_drawn_to_its_last_day_and_not_after(self):
        data = fixture()
        byways = Byways([under(by_id(data, 281))])
        _n, got = read(data)
        items, _u, _r = cs.match([c for c in got if c["id"] == "66/25|281"],
                                 byways, "Ceredigion")
        source = cs.by_id()["ceredigion-closures"]
        self.assertIsNotNone(council_orders.feature_of(
            dict(items[0]), source, "2026-10-30"))
        self.assertIsNone(council_orders.feature_of(
            dict(items[0]), source, "2026-10-31"))


    def test_the_council_line_finds_the_byway_and_ours_is_drawn(self):
        data = fixture()
        theirs = under(by_id(data, 281))
        # Our byway runs on past the council's line, so the two differ.
        lines = [list(l) for l in theirs.lines]
        lines[-1] = lines[-1] + [[lines[-1][-1][0] + 0.001,
                                  lines[-1][-1][1]]]
        byways = Byways([Way(theirs.uid, theirs.authority, theirs.name,
                             lines)])
        _n, got = read(data)
        items, _u, _r = cs.match([c for c in got if c["id"] == "66/25|281"],
                                 byways, "Ceredigion")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["geometry"],
                         cs.as_geometry(byways.geometry(items[0]["ways"])))
        self.assertNotIn("draw_ours", items[0])


# -------------------------------------------------------------- categories


class Categories(unittest.TestCase):
    def test_a_diversion_route_is_never_read_as_a_closure(self):
        n, got = read()
        self.assertEqual(n, 8)
        self.assertNotIn(282, ids(got))
        self.assertNotIn(1536, ids(got))

    def test_the_diversion_on_ce13_publishes_nothing_there(self):
        # 8 October 2026: the diversion route for closure 160/26 runs the
        # whole of byway 68 13. Read as a closure, it would shut a lane that
        # is the way round.
        _n, got = read()
        items, unmatched, review = cs.match(got, Byways([CE13]),
                                            "Ceredigion")
        self.assertEqual((items, unmatched, review), ([], [], []))

    def test_a_closure_is_a_closure_of_the_road_for_everyone(self):
        _n, got = read()
        c = next(c for c in got if c["id"] == "160/26|1534")
        self.assertEqual((c["vehicles"], c["form"]), ("all_users",
                                                      "temporary"))
        self.assertIn("Drainage replacement", c["title"])
        self.assertIn("24 awr/hours", c["title"])
        self.assertEqual(c["url"], cc.PUBLIC)
        incidental = next(c for c in got if c["id"] == "501/26|2189")
        self.assertEqual(incidental["vehicles"], "all_users")

    def test_access_only_is_a_restriction_never_a_full_closure(self):
        data = fixture()
        _n, got = read(data)
        c = next(c for c in got if c["id"] == "160/26|1535")
        self.assertEqual(c["vehicles"], "other")
        items, _u, _r = cs.match([c], Byways([under(by_id(data, 1535))]),
                                 "Ceredigion")
        feature = council_orders.feature_of(
            items[0], cs.by_id()["ceredigion-closures"], DAY)
        props = feature["properties"]
        self.assertEqual((props["code"], props["otype"], props["label"]),
                         ("councilRestriction", "other",
                          "Road closed except for access"))

    def test_other_is_held_for_review_never_published(self):
        # "Two-way traffic flow": traffic management, not a closure.
        data = fixture()
        _n, got = read(data)
        items, _u, review = cs.match(
            [c for c in got if c["id"] == "03/25|700"],
            Byways([under(by_id(data, 700))]), "Ceredigion")
        self.assertEqual(items, [])
        self.assertEqual(review[0]["ways"], ["CE-SYN"])


# ------------------------------------------------------------------ status


class Status(unittest.TestCase):
    def test_only_live_records_are_read(self):
        data = fixture()
        by_id(data, 281)["properties"]["status"] = "Expired"
        by_id(data, 1534)["properties"]["status"] = None
        n, got = read(data)
        self.assertEqual(n, 8, "every record is still counted as read")
        self.assertNotIn(281, ids(got))
        self.assertNotIn(1534, ids(got))
        self.assertIn(2187, ids(got))

    def test_live_is_read_whatever_its_case_or_spacing(self):
        data = fixture()
        by_id(data, 281)["properties"]["status"] = " LIVE "
        _n, got = read(data)
        self.assertIn(281, ids(got))


# ------------------------------------------------------- keep the last good


class KeepLastGood(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.byways = Byways([CE13, under(by_id(fixture(), 281))])
        self.source = dict(cs.by_id()["ceredigion-closures"])
        self.path = os.path.join(self.tmp, "ceredigion-closures.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_with(self, answer):
        self.source["read"] = lambda _c: cs.read_ceredigion(Client(answer))
        return cs.fetch_one(self.source, None, self.byways, self.tmp, DAY)

    def good(self):
        entry = self.run_with(fixture())
        self.assertTrue(entry["ok"], entry)
        self.assertEqual(entry["items"], 1)
        with open(self.path, "rb") as fh:
            return fh.read()

    def unchanged(self, before):
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_a_read_in_another_frame_is_refused(self):
        # Every line would fall outside the grid, and the file would be
        # rewritten with nothing on any byway.
        before = self.good()
        data = fixture()
        data["crs"] = {"type": "name",
                       "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}}
        for f in data["features"]:
            g = f["geometry"]
            lines = [g["coordinates"]] if g["type"] == "LineString" \
                else g["coordinates"]
            lines = [[list(grid_to_wgs84(e, n)) for e, n in l]
                     for l in lines]
            g["coordinates"] = lines[0] if g["type"] == "LineString" \
                else lines
        entry = self.run_with(data)
        self.assertFalse(entry["ok"], entry)
        self.assertIn("27700", entry["error"])
        self.unchanged(before)

    def test_a_cut_short_read_is_refused(self):
        before = self.good()
        data = fixture()
        data["features"] = data["features"][:3]
        data["numberMatched"], data["numberReturned"] = 127, 3
        entry = self.run_with(data)
        self.assertFalse(entry["ok"], entry)
        self.unchanged(before)

    def test_a_failed_or_empty_read_leaves_the_file_alone(self):
        before = self.good()
        empty = dict(fixture(), features=[], numberMatched=0,
                     numberReturned=0, totalFeatures=0)
        for answer in (FetchFailed("HTTP 500"), Refused("HTTP 403"), empty,
                       {"error": "not a collection"}):
            entry = self.run_with(answer)
            self.assertFalse(entry["ok"], answer)
            self.unchanged(before)


# --------------------------------------------------------------- applicant


class Opener(object):
    """The transport under a real PoliteClient: robots.txt is absent (404,
    no rules), GetFeature answers the fixture. Records URL and clock."""

    def __init__(self, clock, answer):
        self.clock = clock
        self.answer = answer
        self.calls = []

    def __call__(self, url, timeout, data=None):
        self.calls.append((url, self.clock(), data))
        if urllib.parse.urlsplit(url).path == "/robots.txt":
            return 404, {}, b""
        return 200, {}, json.dumps(self.answer).encode("utf-8")


class Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def polite(answer=None):
    clock = Clock()
    opener = Opener(clock, fixture() if answer is None else answer)
    client = polite_http.PoliteClient(opener=opener, sleep=clock.sleep,
                                      clock=clock, log=lambda *_: None,
                                      overrides={})
    return client, opener


class Applicant(unittest.TestCase):
    def test_the_request_names_its_fields_and_applicant_is_not_one(self):
        client, opener = polite()
        n, _got = cs.read_ceredigion(client)
        self.assertEqual(n, 8)
        urls = [url for url, _t, _d in opener.calls]
        for url in urls:
            self.assertNotIn("applicant", url.lower())
        feature = [u for u in urls if "GetFeature" in u]
        self.assertEqual(len(feature), 1, urls)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(
            feature[0]).query)
        self.assertEqual(query["propertyName"][0].split(","),
                         ["layer_type", "id", "reference", "status",
                          "category", "times", "road_closed_all_day",
                          "justification", "date_start", "date_end", "geom"])
        self.assertEqual(query["typeNames"], [cc.TYPE_NAME])
        self.assertEqual(query["outputFormat"], ["application/json"])
        self.assertEqual(query["srsName"], ["EPSG:27700"])
        self.assertTrue(all(d is None for _u, _t, d in opener.calls),
                        "GET only")

    def test_the_published_endpoint_never_asks_for_applicant(self):
        source = cs.by_id()["ceredigion-closures"]
        self.assertEqual(source["endpoint"], cc.GET_FEATURE)
        self.assertIn("propertyName=", source["endpoint"])
        self.assertNotIn("applicant", source["endpoint"].lower())

    def test_an_applicant_the_server_sends_anyway_is_not_kept(self):
        data = fixture()
        for f in data["features"]:
            f["properties"]["applicant"] = "Jane Example for Example Ltd"
        tmp = tempfile.mkdtemp()
        try:
            source = dict(cs.by_id()["ceredigion-closures"])
            source["read"] = lambda _c: cs.read_ceredigion(Client(data))
            byways = Byways([under(by_id(data, 281))])
            entry = cs.fetch_one(source, None, byways, tmp, DAY)
            self.assertEqual(entry["items"], 1, entry)
            with open(os.path.join(tmp, "ceredigion-closures.json"),
                      encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("Jane Example", text)
            self.assertNotIn("Example Ltd", text)
            self.assertNotIn("applicant", text.lower())
        finally:
            shutil.rmtree(tmp)

    def test_requests_to_the_council_are_at_least_4_5_s_apart(self):
        client, opener = polite()
        self.assertEqual(client.min_gap, 2.0, "the client main() makes")
        cs.read_ceredigion(client)
        times = [t for _u, t, _d in opener.calls]
        self.assertEqual(len(times), 2, "robots.txt, then GetFeature")
        self.assertGreaterEqual(times[1] - times[0], 4.5)
        self.assertEqual(client.min_gap, 2.0, "put back for other sources")


# ---------------------------------------------------------------- registry


class Registry(unittest.TestCase):
    def test_credited_to_the_council_and_said_to_be_outside_street_manager(
            self):
        source = cs.by_id()["ceredigion-closures"]
        self.assertEqual(source["authority"], "Ceredigion")
        self.assertTrue(source["name"].startswith("Ceredigion County Council"))
        self.assertEqual(source["kind"], "council-layer")
        self.assertIn("Street Manager", source["note"])
        self.assertIn("Wales", source["note"])
        self.assertNotIn("blocked", source)
        self.assertFalse(polite_http.blocked(polite_http.host_of(
            source["endpoint"])))


if __name__ == "__main__":
    unittest.main(verbosity=1)

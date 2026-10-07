#!/usr/bin/env python3
"""Definitive map applications about byways are published without a single
applicant's name or address, and a failed register never blanks them.

    python tools/test_dmmo_applications.py

No network: a stand-in client answering in the shape of the councils' own
layers (Devon's Schedule 14 register, 7 October 2026), with invented names in
the personal fields so a leak would be visible.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dmmo_applications as dm  # noqa: E402
from byway_match import Byways, Way  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402
from polite_http import Refused  # noqa: E402

NAME, ADDRESS = "Jane Applicant", "1 Example Cottage, EX1 1AA"


def devon_feature(ref, proposed, progress, path):
    return {"attributes": {
        "OBJECTID": 1, "Reference": ref, "Parish": "Loddiswell",
        "ClaimLocat": "Bridleway 11 Wood Lane", "InitialSta": "Bridleway",
        "ProposedSt": proposed, "Progress": progress,
        "Applicatio": 1131926400000,
        # A server that ignores outFields and sends everything anyway:
        "Applicant": NAME, "ApplicantA": ADDRESS, "ApplicantP": "EX1 1AA",
        "WebScanned": "https://devoncc.sharepoint.example/scan.pdf"},
        "geometry": {"paths": [path]}}


#: A byway 500 m east from (270000, 50000).
BYWAY = Way("DN-1", "Devon", "Byway open to all traffic (BOAT) X 1",
            [[grid_to_wgs84(270000, 50000), grid_to_wgs84(270500, 50000)]])
ON_IT = [[270000, 50002], [270500, 50002]]
ELSEWHERE = [[275000, 55000], [275400, 55000]]


class Client(object):
    def __init__(self, features=None, error=None):
        self.features, self.error, self.asked = features or [], error, []

    def get_json(self, url):
        self.asked.append(url)
        if self.error:
            raise self.error
        return {"features": self.features}


def only(source_id):
    return [s for s in dm.SOURCES if s["id"] == source_id]


class NoPersonalData(unittest.TestCase):
    def test_no_layer_asks_for_a_personal_field(self):
        for s in dm.SOURCES:
            for f in s.get("fields") or []:
                self.assertNotIn(f.lower(), dm.PERSONAL, s["id"])

    def test_the_request_names_its_fields_and_never_star(self):
        client = Client([devon_feature("DMR/Loddiswell 1", "Byway Open To "
                                       "All Traffic", "Undetermined",
                                       ON_IT)])
        dm._arcgis(client, only("devon-dmmo")[0])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(
            client.asked[0]).query)
        fields = query["outFields"][0].split(",")
        self.assertNotIn("*", fields)
        self.assertNotIn("Applicant", fields)

    def test_a_personal_field_sent_anyway_is_never_kept(self):
        client = Client([devon_feature("DMR/Loddiswell 1", "Byway Open To "
                                       "All Traffic", "Undetermined",
                                       ON_IT)])
        _n, apps = dm._arcgis(client, only("devon-dmmo")[0])
        text = json.dumps(apps)
        for leak in (NAME, ADDRESS, "EX1 1AA", "sharepoint"):
            self.assertNotIn(leak, text)

    def test_an_email_typed_into_a_description_is_removed(self):
        self.assertNotIn("@", dm._clean("Upgrade to BOAT - contact "
                                        "j.bloggs@example.org"))


class Reading(unittest.TestCase):
    def test_a_boat_claim_is_kept_and_a_footpath_claim_is_not(self):
        client = Client([
            devon_feature("DMR/Loddiswell 1", "Byway Open To All Traffic",
                          "Undetermined", ELSEWHERE),
            devon_feature("DMR/Loddiswell 2", "Footpath", "Undetermined",
                          ELSEWHERE)])
        _n, apps = dm._arcgis(client, only("devon-dmmo")[0])
        self.assertEqual([a["ref"] for a in apps], ["DMR/Loddiswell 1"])
        self.assertEqual(apps[0]["state"], "open")
        self.assertEqual(apps[0]["details"]["received"], "2005-11-14")

    def test_a_restricted_byway_alone_is_not_a_byway_claim(self):
        self.assertIsNone(dm._BOAT.search("Upgrade to Restricted Byway"))
        self.assertIsNotNone(dm._BOAT.search("Upgrade to BOAT"))
        self.assertIsNotNone(dm._BOAT.search("add a byway open to all "
                                             "traffic"))

    def test_derbyshire_stages(self):
        self.assertEqual(dm.derbyshire_state("Awaiting action"), "open")
        self.assertEqual(dm.derbyshire_state("Objections Received to "
                                             "Order"), "open")
        for done in ("Application Concluded", "Order Confirmed",
                     "Rejected by Committee - Insufficient evidence",
                     "Appeal to SoS dismissed", "Order not Confirmed"):
            self.assertEqual(dm.derbyshire_state(done), "determined", done)
        self.assertEqual(dm.derbyshire_state("Non-compliant Application"),
                         "incomplete")

    def test_segments_of_one_application_are_one_application(self):
        a = dm._app({"id": "x", "council": "C"}, "UR1", None, "BOAT", None,
                    "open", [[(0, 0), (1, 1)]])
        b = dm._app({"id": "x", "council": "C"}, "UR1", None, "BOAT", None,
                    "open", [[(1, 1), (2, 2)]])
        got = dm.group([a, b])
        self.assertEqual(len(got), 1)
        self.assertEqual(len(got[0]["lines"]), 2)


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = dm.SOURCES
        dm.SOURCES = only("devon-dmmo") + only("dorset-dmmo")

    def tearDown(self):
        dm.SOURCES = self.saved
        shutil.rmtree(self.tmp)

    def run_with(self, client):
        return dm.run(client, Byways([BYWAY]), self.tmp, "2026-10-07",
                      log=lambda *_: None)

    def published(self):
        with open(os.path.join(self.tmp, "dmmo-applications.json")) as fh:
            listing = json.load(fh)
        with open(os.path.join(self.tmp,
                               "dmmo-applications.geojson")) as fh:
            return listing, json.load(fh)

    def test_an_open_claim_on_a_byway_is_matched_and_drawn(self):
        self.run_with(Client([devon_feature(
            "DMR/Loddiswell 1", "Byway Open To All Traffic", "Undetermined",
            ON_IT)]))
        listing, geo = self.published()
        self.assertEqual(listing["open"], 1)
        props = geo["features"][0]["properties"]
        self.assertEqual(props["ways"], ["DN-1"])
        self.assertEqual(props["council"], "Devon County Council")
        self.assertIn("Devon County Council", geo["attribution"])

    def test_a_determined_claim_is_listed_but_not_drawn(self):
        self.run_with(Client([devon_feature(
            "DMR/Loddiswell 1", "Byway Open To All Traffic", "Determined",
            ON_IT)]))
        listing, geo = self.published()
        self.assertEqual((listing["applications"], listing["open"],
                          geo["features"]), (1, 0, []))

    def test_a_refused_register_keeps_what_it_published(self):
        feats = [devon_feature("DMR/L %d" % i, "Byway Open To All Traffic",
                               "Undetermined", ELSEWHERE) for i in range(6)]
        self.run_with(Client(feats))
        state, failed = self.run_with(Client(error=Refused("HTTP 403")))
        self.assertTrue(failed)
        listing, geo = self.published()
        self.assertEqual(len(geo["features"]), 6)
        self.assertFalse(state["devon-dmmo"]["ok"])
        self.assertEqual(state["devon-dmmo"]["failing_since"], "2026-10-07")

    def test_a_collapsed_read_keeps_what_it_published(self):
        feats = [devon_feature("DMR/L %d" % i, "Byway Open To All Traffic",
                               "Undetermined", ELSEWHERE) for i in range(6)]
        self.run_with(Client(feats))
        _s, failed = self.run_with(Client(feats[:1]))
        self.assertTrue(failed)
        _l, geo = self.published()
        self.assertEqual(len(geo["features"]), 6)

    def test_a_blocked_register_is_never_asked(self):
        client = Client([])
        state, _f = self.run_with(client)
        self.assertIn("blocked", state["dorset-dmmo"])
        self.assertFalse(any("dorset" in u for u in client.asked))


if __name__ == "__main__":
    unittest.main(verbosity=1)

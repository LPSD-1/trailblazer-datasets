#!/usr/bin/env python3
"""Files the owner commits to manual/<CODE>/ are read like an automated
snapshot, credited with how and when they came, and stop the automated
fetching for that authority.

    python tools/test_manual_inbox.py

No network: a temporary manual/ folder, synthetic byways.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_sources as cs  # noqa: E402
import manual_inbox as mi  # noqa: E402
from byway_match import Byways, Way  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: A West Berkshire byway, 500 m east from (450000, 170000).
BYWAY = Way("WB-1", "West Berkshire", "Byway open to all traffic (BOAT) "
            "Hampstead Norreys 11",
            [[grid_to_wgs84(450000, 170000), grid_to_wgs84(450500, 170000)]])

CLOSURES = {"type": "FeatureCollection", "features": [
    {"type": "Feature",
     "properties": {"REF": "WB/TTRO/26/14", "PATH": "Hampstead Norreys 11",
                    "FROM": "14/10/2026", "TO": "14/04/2027",
                    "WHAT": "Closed to all users for drainage works",
                    "Officer": "A. Person"},
     "geometry": {"type": "LineString", "coordinates": [
         [450000, 170003], [450500, 170003]]}}]}

MAPPING = {"id": "$REF", "ref": "$REF", "where": "$PATH",
           "title": "$WHAT", "start": "$FROM", "end": "$TO",
           "vehicles": "all_users", "form": "temporary"}


class Inbox(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "WB"))
        with open(os.path.join(self.root, "authorities.json"), "w") as fh:
            json.dump({"WB": {"authority": "West Berkshire"}}, fh)

    def tearDown(self):
        shutil.rmtree(self.root)

    def put(self, name, data, manifest=None):
        with open(os.path.join(self.root, "WB", name), "w") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))
        if manifest is not None:
            with open(os.path.join(self.root, "WB", "manifest.json"),
                      "w") as fh:
                json.dump(manifest, fh)

    def test_an_empty_folder_is_no_inbox(self):
        self.assertEqual(mi.inboxes(self.root), [])
        self.assertEqual(mi.automated_off(self.root), set())

    def test_a_file_stops_the_automated_fetching_unless_asked(self):
        self.put("prow-restrictions-2026-10-08.pdf", "%PDF-1.4")
        self.assertEqual(mi.automated_off(self.root), {"West Berkshire"})
        self.put("prow-restrictions-2026-10-08.pdf", "%PDF-1.4",
                 {"automated": "on"})
        self.assertEqual(mi.automated_off(self.root), set())

    def test_what_is_wrong_with_a_file_is_said_not_guessed(self):
        self.put("undated.pdf", "%PDF-1.4")
        self.put("closures.geojson", CLOSURES)
        self.put("notes.docx", "x")
        problems = mi.inboxes(self.root)[0]["problems"]
        self.assertTrue(any("undated.pdf: no date" in p for p in problems))
        self.assertTrue(any("closures.geojson: a map layer needs" in p
                            for p in problems))
        self.assertTrue(any("notes.docx: not a kind" in p for p in problems))

    def test_a_councils_layer_is_matched_and_credited(self):
        self.put("closures.geojson", CLOSURES, {"files": {
            "closures.geojson": {"how": "eir", "date": "2026-09-30",
                                 "title": "Rights of way closures",
                                 "layer": MAPPING}}})
        sources = cs.manual_sources(self.root)
        self.assertEqual([s["id"] for s in sources],
                         ["manual-wb-closures"])
        src = sources[0]
        self.assertEqual(src["kind"], "council-layer")
        self.assertEqual(src["supplied"],
                         "supplied by the council 2026-09-30")
        self.assertIn("West Berkshire", src["name"])
        records, cands = src["read"](None)
        self.assertEqual(records, 1)
        items, unmatched, _review = cs.match(cands, Byways([BYWAY]),
                                             "West Berkshire")
        self.assertEqual(unmatched, [])
        item = items[0]
        self.assertEqual((item["ways"], item["start"], item["end"],
                          item["vehicles"], item["form"]),
                         (["WB-1"], "2026-10-14", "2027-04-14", "all_users",
                          "temporary"))
        self.assertNotIn("A. Person", json.dumps(items),
                         "a field the mapping did not name was carried")

    def test_a_layer_in_wgs84_is_read_as_is(self):
        lon0, lat0 = grid_to_wgs84(450000, 170003)
        lon1, lat1 = grid_to_wgs84(450500, 170003)
        wgs = json.loads(json.dumps(CLOSURES))
        wgs["features"][0]["geometry"]["coordinates"] = [[lon0, lat0],
                                                         [lon1, lat1]]
        self.put("closures.geojson", wgs, {"files": {
            "closures.geojson": {"date": "2026-10-08", "layer": MAPPING}}})
        src = cs.manual_sources(self.root)[0]
        self.assertEqual(src["kind"], "council-page")
        _n, cands = src["read"](None)
        items, _u, _r = cs.match(cands, Byways([BYWAY]), "West Berkshire")
        self.assertEqual(items[0]["ways"], ["WB-1"])


class TheRealFolder(unittest.TestCase):
    def test_every_code_names_an_authority_and_the_readme_exists(self):
        table = mi.authority_table()
        self.assertGreaterEqual(len(table), 140)
        for code in ("CB", "HD", "IW", "WB", "PW", "DY", "DU"):
            self.assertIn(code, table)
        self.assertTrue(os.path.exists(os.path.join(ROOT, "manual",
                                                    "README.md")))


if __name__ == "__main__":
    unittest.main(verbosity=1)

#!/usr/bin/env python3
"""A council's own byway layer replaces rowmaps' only where it should, keeps
every unchanged way's id, and never blanks an authority.

    python tools/test_council_ways.py

No network. Synthetic lines near 52N 1W; a stand-in client; temporary
directories. The build_packages half needs `cryptography` (exit 2 without it,
as the repository's other pack tests do).
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_ways as cw  # noqa: E402
from polite_http import FetchFailed, Refused  # noqa: E402

try:
    import build_packages  # noqa: E402
except (ImportError, SystemExit):
    print("BLIND: build_packages needs cryptography (pip install "
          "cryptography)")
    sys.exit(2)

#: About 1 m of longitude and of latitude here.
DLON, DLAT = 1 / 68700.0, 1 / 110574.0


def line(x0, y0, x1, y1, n=5):
    """A straight line between two points given in metres from 52N 1W."""
    return [[round(-1.0 + (x0 + (x1 - x0) * k / float(n)) * DLON, 6),
             round(52.0 + (y0 + (y1 - y0) * k / float(n)) * DLAT, 6)]
            for k in range(n + 1)]


def rowmaps(name, coords):
    return {"type": "Feature",
            "properties": {"Name": name, "Description":
                           "BO|ZZ:1|0.300|none|", "Attribution": "x"},
            "geometry": {"type": "LineString", "coordinates": coords}}


def council(*ways):
    return {"source": {"council": "Zedshire County Council",
                       "attribution": "Source: Zedshire County Council."},
            "ways": [{"id": str(i), "parish": p, "number": n, "lines": ls}
                     for i, (p, n, ls) in enumerate(ways)]}


# Byway 1 runs east 600 m; byway 2 runs north 400 m, well apart.
B1 = line(0, 0, 600, 0)
B2 = line(2000, 0, 2000, 400)


class Merge(unittest.TestCase):
    def test_an_unchanged_way_is_kept_byte_for_byte(self):
        # The council's copy is 3 m off and drawn in other vertices.
        mine = rowmaps("ZZ|Ash|1", B1)
        got, report = cw.merge("ZZ", [mine], council(
            ("Ash", "1", [line(0, 3, 600, 3, n=17)])))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["geometry"], mine["geometry"])
        self.assertEqual(got[0]["properties"]["Name"], "ZZ|Ash|1")
        self.assertEqual(got[0]["properties"]["TB_council"],
                         "Zedshire County Council")
        self.assertEqual((report["kept"], report["dropped"],
                          report["added"]), (1, [], []))

    def test_a_way_the_council_no_longer_draws_is_dropped(self):
        got, report = cw.merge("ZZ", [rowmaps("ZZ|Ash|1", B1),
                                      rowmaps("ZZ|Ash|2", B2)],
                               council(("Ash", "1", [B1])))
        self.assertEqual([f["properties"]["Name"] for f in got],
                         ["ZZ|Ash|1"])
        self.assertEqual(report["dropped"], ["ZZ|Ash|2"])

    def test_a_way_rowmaps_lacks_is_added_with_the_councils_geometry(self):
        got, report = cw.merge("ZZ", [rowmaps("ZZ|Ash|1", B1)],
                               council(("Ash", "1", [B1]),
                                       ("Elm", "7", [B2])))
        self.assertEqual(report["added"], ["ZZ|Elm|7"])
        new = got[-1]
        self.assertEqual(new["properties"]["Name"], "ZZ|Elm|7")
        self.assertTrue(new["properties"]["Description"].startswith(
            "BO|ZZ:1|0.24"), new["properties"]["Description"])
        self.assertAlmostEqual(new["geometry"]["coordinates"][0][1],
                               B2[0][1], places=5)

    def test_a_parallel_lane_across_a_field_is_not_the_same_way(self):
        _got, report = cw.merge("ZZ", [rowmaps("ZZ|Ash|1", B1)],
                                council(("Ash", "1",
                                         [line(0, 60, 600, 60)])))
        self.assertEqual(report["kept"], 0)
        self.assertEqual(report["added"], ["ZZ|Ash|1"])

    def test_a_cut_back_way_is_replaced_by_what_is_left(self):
        # The council stopped up the eastern half: rowmaps' whole line is no
        # longer drawn, and the remaining 300 m comes from the council.
        got, report = cw.merge("ZZ", [rowmaps("ZZ|Ash|1", B1)],
                               council(("Ash", "1", [line(0, 0, 300, 0)])))
        self.assertEqual(report["dropped"], ["ZZ|Ash|1"])
        self.assertEqual(report["added"], ["ZZ|Ash|1"])
        self.assertLess(cw._length(got[0]["geometry"]["coordinates"]), 310)

    def test_an_extension_adds_only_the_new_part(self):
        got, report = cw.merge("ZZ", [rowmaps("ZZ|Ash|1", B1)],
                               council(("Ash", "1", [line(0, 0, 900, 0)])))
        self.assertEqual(report["kept"], 1)
        self.assertEqual(len(got), 2)
        extra = cw._length(got[1]["geometry"]["coordinates"])
        self.assertTrue(270 < extra < 300, extra)

    def test_a_junction_overshoot_is_not_a_new_byway(self):
        _got, report = cw.merge("ZZ", [rowmaps("ZZ|Ash|1", B1)],
                                council(("Ash", "1", [line(-40, 0, 600, 0)])))
        self.assertEqual(report["added"], [])


class Agrees(unittest.TestCase):
    def report(self, **kw):
        base = {"rowmaps": 100, "kept": 100, "council_ways": 100,
                "new_share": 0.0, "dropped": [], "added": []}
        base.update(kw)
        return base

    def test_a_close_match_is_used(self):
        self.assertIsNone(cw.agrees(self.report(kept=80, new_share=0.2)))

    def test_a_different_network_is_not(self):
        self.assertIn("only 50", cw.agrees(self.report(kept=50)))
        self.assertIn("not in rowmaps", cw.agrees(self.report(
            new_share=0.4)))
        self.assertIn("no byways", cw.agrees(self.report(council_ways=0)))


class BuildUsesIt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, code, data):
        with open(os.path.join(self.tmp, "%s.json" % code), "w") as fh:
            json.dump(data, fh)

    def test_no_council_file_means_rowmaps_untouched(self):
        mine = [rowmaps("ZZ|Ash|1", B1)]
        got, report = cw.byways_for("ZZ", mine, out_dir=self.tmp,
                                    log=lambda *_: None)
        self.assertIs(got, mine)
        self.assertIsNone(report)

    def test_a_disagreeing_council_file_is_not_used(self):
        mine = [rowmaps("ZZ|Ash|%d" % i, line(0, 100 * i, 600, 100 * i))
                for i in range(8)]
        self.write("ZZ", council(("Ash", "1", [B2])))
        said = []
        got, _r = cw.byways_for("ZZ", mine, out_dir=self.tmp,
                                log=said.append)
        self.assertIs(got, mine)
        self.assertTrue(said and said[0].startswith("::warning::"))

    def test_a_kept_way_keeps_its_lane_id_and_credits_the_council(self):
        mine = rowmaps("ZZ|Ash|1", B1)
        before = build_packages.normalise(copy.deepcopy(mine), "ZZ",
                                          "Zedshire", "byway_open_to_all_"
                                          "traffic")
        self.write("ZZ", council(("Ash", "1", [B1])))
        got, _r = cw.byways_for("ZZ", [mine], out_dir=self.tmp,
                                log=lambda *_: None)
        after = build_packages.normalise(got[0], "ZZ", "Zedshire",
                                         "byway_open_to_all_traffic")
        p0, p1 = before["properties"], after["properties"]
        self.assertEqual(p1["lane_uid"], p0["lane_uid"])
        self.assertEqual(p1["name"], p0["name"])
        self.assertEqual(p0["source"], "rowmaps:zedshire")
        self.assertEqual(p1["source"], "council:zedshire")
        self.assertEqual(p1["attribution"], "Source: Zedshire County "
                                            "Council.")
        self.assertNotIn("TB_council", p1)

    def test_a_rowmaps_way_still_says_rowmaps(self):
        p = build_packages.normalise(rowmaps("ZZ|Ash|1", B1), "ZZ",
                                     "Zedshire", "byway_open_to_all_traffic"
                                     )["properties"]
        self.assertEqual(p["attribution"], build_packages.OGL)


class References(unittest.TestCase):
    def test_each_councils_reference_reads_as_rowmaps_names_it(self):
        cases = [
            (cw._ref_cambridgeshire, {"name": "40/9", "parish": "Ely"},
             ("Ely", "9")),
            (cw._ref_central_beds, {"parish": "Potton", "path_no": "7"},
             ("POTTON", "7")),
            (cw._ref_wokingham, {"prowuid": "ARBO10"}, ("ARBO", "10")),
            (cw._ref_devon, {"Parish_Sta": "Aveton Gifford Byway 24"},
             ("Aveton Gifford", "24")),
            (cw._ref_cheshire_east, {"Routecode": "032/BY25/1",
                                     "Alias": "Audlem BY25"},
             ("Audlem", "BY25/1")),
            (cw._ref_cheshire_west, {"Routecode": "004/BY14/1"},
             ("004", "BY14/1")),
            (cw._ref_oxfordshire, {"RouteCode": "100/2/10"}, ("100", "2/10")),
            (cw._ref_lancashire, {"PathRefLong": "BT1404348"},
             ("1404", "348")),
            (cw._ref_northumberland, {"KEYID": "509/029"}, ("509", "029")),
            (cw._ref_east_sussex, {"Path_Name": "Alciston 11a"},
             ("Alciston", "11a")),
            (cw._ref_hertfordshire, {"PATHNAME": "BERKHAMSTED 040"},
             ("BERKHAMSTED", "040")),
            (cw._ref_essex, {"Parish": "Roxwell", "Central_As":
                             "PROW 230_73"}, ("Roxwell", "73")),
            (cw._ref_wiltshire, {"REF": "LACO24"}, ("LACO", "24")),
            (cw._ref_bracknell, {"Parish": "Winkfield",
                                 "UniqueID": "WIN BOAT16 "},
             ("WINKFIELD", "16")),
        ]
        for fn, attrs, want in cases:
            self.assertEqual(fn(attrs), want, fn.__name__)


GML = b"""<?xml version="1.0"?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs"
 xmlns:gml="http://www.opengis.net/gml" xmlns:ms="http://mapserver.gis.umn.edu/mapserver">
 <gml:featureMember><ms:RoW_Legal_Network_1 gml:id="RoW.344">
  <ms:msGeometry><gml:LineString srsName="EPSG:27700"><gml:posList
   srsDimension="2">521000 245000 521100 245050 521200 245060</gml:posList>
  </gml:LineString></ms:msGeometry>
  <ms:ogc_fid>344</ms:ogc_fid><ms:type>BOAT</ms:type>
  <ms:path_no>18</ms:path_no><ms:parish>DUNTON</ms:parish>
 </ms:RoW_Legal_Network_1></gml:featureMember>
</wfs:FeatureCollection>"""


class Gml(unittest.TestCase):
    def test_a_wfs_11_gml_answer_is_read(self):
        records, ways = cw.gml_ways(GML, cw._ref_central_beds)
        self.assertEqual(records, 1)
        self.assertEqual((ways[0]["parish"], ways[0]["number"]),
                         ("DUNTON", "18"))
        lon, lat = ways[0]["lines"][0][0]
        self.assertTrue(-0.3 < lon < -0.1 and 52.0 < lat < 52.2, (lon, lat))

    def test_an_exception_report_is_a_failure_not_an_empty_layer(self):
        with self.assertRaises(FetchFailed):
            cw.gml_ways(b"<ows:ExceptionReport xmlns:ows='x'><ows:Exception>"
                        b"bad</ows:Exception></ows:ExceptionReport>",
                        cw._ref_central_beds)


class Fetch(unittest.TestCase):
    LAYER = {"code": "ZZ", "council": "Zedshire County Council",
             "licence": "OGL-3.0", "url": "https://z.example/layer"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "cache")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_with(self, ways=None, error=None):
        def read(_client, _layer):
            if error:
                raise error
            return len(ways), ways
        layer = dict(self.LAYER, read=read)
        return cw.fetch_one(layer, None, self.tmp, "2026-10-07",
                            cache=self.cache)

    def way(self, i):
        return {"id": str(i), "parish": "Ash", "number": str(i),
                "lines": [line(0, 100 * i, 600, 100 * i)]}

    def saved(self):
        with open(os.path.join(self.tmp, "ZZ.json")) as fh:
            return json.load(fh)

    def test_a_good_read_is_written_with_its_credit(self):
        entry = self.run_with([self.way(i) for i in range(9)])
        self.assertTrue(entry["ok"])
        data = self.saved()
        self.assertEqual(len(data["ways"]), 9)
        self.assertIn("Open Government Licence", data["source"]
                      ["attribution"])
        self.assertIn("Zedshire County Council", data["source"]
                      ["attribution"])

    def test_a_failed_or_collapsed_read_keeps_the_last_good_file(self):
        self.run_with([self.way(i) for i in range(9)])
        for kw in ({"error": Refused("403")},
                   {"error": FetchFailed("HTTP 500")},
                   {"ways": []},
                   {"ways": [self.way(1), self.way(2)]}):
            entry = self.run_with(**kw)
            self.assertFalse(entry["ok"], kw)
            self.assertEqual(len(self.saved()["ways"]), 9, kw)

    def test_a_licence_left_unstated_is_said_so(self):
        layer = dict(self.LAYER, licence=None)
        self.assertIn("without stating a licence", cw.attribution(layer))
        self.assertNotIn("Open Government", cw.attribution(layer))

    def test_with_rowmaps_in_the_cache_the_merge_is_previewed(self):
        os.makedirs(os.path.join(self.cache, "ZZ"))
        with open(os.path.join(self.cache, "ZZ", cw.BOAT_FILE), "w") as fh:
            json.dump({"type": "FeatureCollection", "features": [
                rowmaps("ZZ|Ash|%d" % i, line(0, 100 * i, 600, 100 * i))
                for i in range(9)]}, fh)
        entry = self.run_with([self.way(i) for i in range(8)])
        self.assertEqual(entry["merge"]["kept"], 8)
        self.assertEqual(entry["merge"]["dropped"], ["ZZ|Ash|8"])
        self.assertTrue(entry["merge"]["used"])


class Layers(unittest.TestCase):
    def test_every_layer_is_a_read_the_guard_allows(self):
        for layer in cw.LAYERS:
            self.assertTrue(layer["url"].startswith("https://"), layer["code"])
            self.assertIn(layer["read"], (cw.read_arcgis, cw.read_wfs_json,
                                          cw.read_wfs_gml), layer["code"])
            if layer["read"] is cw.read_arcgis:
                self.assertTrue(layer["where"], layer["code"])
            else:
                self.assertEqual(layer["params"].get(
                    "request", layer["params"].get("REQUEST")), "GetFeature")

    def test_no_restricted_byway_filter_and_no_blocked_host(self):
        from polite_http import blocked, host_of
        for layer in cw.LAYERS:
            text = json.dumps([layer.get("where"), layer.get("filter")])
            self.assertNotIn("Restricted", text, layer["code"])
            self.assertFalse(blocked(host_of(layer["url"])), layer["code"])


if __name__ == "__main__":
    unittest.main(verbosity=1)

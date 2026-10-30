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
import re
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

    def write(self, code, data, last_ok=None):
        import datetime
        with open(os.path.join(self.tmp, "%s.json" % code), "w") as fh:
            json.dump(data, fh)
        with open(os.path.join(self.tmp, "status.json"), "w") as fh:
            json.dump({code: {"ok": True, "last_ok": last_ok or
                              datetime.date.today().isoformat()}}, fh)

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

    def test_a_cross_check_layer_stands_in_only_when_rowmaps_is_empty(self):
        data = council(("Ash", "1", [B1]))
        data["source"]["role"] = "cross-check"
        self.write("ZZ", data)
        mine = [rowmaps("ZZ|Ash|1", line(0, 3, 600, 3, n=9))]
        got, report = cw.byways_for("ZZ", mine, out_dir=self.tmp,
                                    log=lambda *_: None)
        self.assertIs(got, mine)
        got, _r = cw.byways_for("ZZ", [], out_dir=self.tmp,
                                log=lambda *_: None)
        self.assertEqual([f["properties"]["Name"] for f in got],
                         ["ZZ|Ash|1"])

    def test_a_council_file_a_month_old_is_not_used(self):
        # The layer has failed for a month; rowmaps may since have added
        # byways the old council file does not draw, and the merge would
        # drop them.
        mine = [rowmaps("ZZ|Ash|1", B1), rowmaps("ZZ|Ash|2", B2)]
        self.write("ZZ", council(("Ash", "1", [B1]), ("Ash", "2", [B2])),
                   last_ok="2026-09-08")
        said = []
        got, report = cw.byways_for("ZZ", mine, out_dir=self.tmp,
                                    log=said.append, today="2026-10-08")
        self.assertIs(got, mine, "a council file 30 days old was used")
        self.assertIsNone(report)
        self.assertIn("30 days ago", said[0])
        got, report = cw.byways_for("ZZ", mine, out_dir=self.tmp,
                                    log=lambda *_: None,
                                    today="2026-10-07")
        self.assertIsNot(got, mine, "a council file 29 days old was not "
                                    "used")
        self.assertIsNotNone(report)

    def test_with_nothing_from_rowmaps_an_old_file_still_stands_in(self):
        self.write("ZZ", council(("Ash", "1", [B1])), last_ok="2026-01-01")
        got, _r = cw.byways_for("ZZ", [], out_dir=self.tmp,
                                log=lambda *_: None, today="2026-10-08")
        self.assertEqual([f["properties"]["Name"] for f in got],
                         ["ZZ|Ash|1"])

    def test_a_council_file_with_no_recorded_read_is_not_used(self):
        mine = [rowmaps("ZZ|Ash|1", B1)]
        self.write("ZZ", council(("Ash", "1", [B1])))
        os.remove(os.path.join(self.tmp, "status.json"))
        got, _r = cw.byways_for("ZZ", mine, out_dir=self.tmp,
                                log=lambda *_: None, today="2026-10-08")
        self.assertIs(got, mine)

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
            (cw._ref_west_berkshire, {"RouteCode": "Beed/22/2"},
             ("BEED", "22")),
            (cw._ref_isle_of_wight, {"P_NUMBER": "GL48"}, ("GL", "48")),
            (cw._ref_hampshire, {"PARISH": "Kingsclere",
                                 "ROWCODE": "706"}, ("Kingsclere", "706")),
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

    def test_match_sees_each_records_attributes_and_keeps_only_its_picks(
            self):
        body = gml_layer("row_lines", [
            {"ogc_fid": "861", "feature": "Byway Open to All Traffic",
             "prow_id": "SED0557"},
            {"ogc_fid": "862", "feature": "Byway", "prow_id": "SED0558"},
            {"ogc_fid": "863", "feature": "Footpath", "prow_id": "SED0559"}])
        seen = []

        def match(a):
            seen.append((a.get("ogc_fid"), a.get("feature")))
            return a.get("feature") == "Byway Open to All Traffic"
        records, ways = cw.gml_ways(body, cw._ref_dudley, match)
        self.assertEqual(records, 3)
        self.assertEqual(seen, [("861", "Byway Open to All Traffic"),
                                ("862", "Byway"), ("863", "Footpath")])
        self.assertEqual([(w["id"], w["parish"], w["number"]) for w in ways],
                         [("row_lines.861", "SED", "0557")])

    def test_without_a_match_every_record_with_a_line_is_a_way(self):
        body = gml_layer("row_lines", [
            {"ogc_fid": "1", "feature": "Footpath", "prow_id": "SED1"},
            {"ogc_fid": "2", "feature": "Byway", "prow_id": "SED2"}])
        records, ways = cw.gml_ways(body, cw._ref_dudley)
        self.assertEqual((records, len(ways)), (2, 2))


def gml_layer(type_name, rows):
    """A WFS 1.1 GML answer as an iShare server gives it: one line per row,
    each row's fields as elements beside its geometry."""
    members = []
    for i, row in enumerate(rows):
        fields = "".join("<ms:%s>%s</ms:%s>" % (k, v, k)
                         for k, v in row.items())
        members.append(
            '<gml:featureMember><ms:%s gml:id="%s.%s">'
            '<ms:msGeometry><gml:LineString srsName="EPSG:27700">'
            '<gml:posList srsDimension="2">%d 291000 %d 291050'
            '</gml:posList></gml:LineString></ms:msGeometry>%s'
            '</ms:%s></gml:featureMember>'
            % (type_name, type_name, row.get("ogc_fid", i), 393000 + i * 500,
               393100 + i * 500, fields, type_name))
    return ('<?xml version="1.0"?><wfs:FeatureCollection '
            'xmlns:wfs="http://www.opengis.net/wfs" '
            'xmlns:gml="http://www.opengis.net/gml" '
            'xmlns:ms="http://mapserver.gis.umn.edu/mapserver">%s'
            '</wfs:FeatureCollection>' % "".join(members)).encode("utf-8")


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

    def test_a_reader_that_breaks_on_a_strange_answer_fails_that_read(self):
        # read_arcgis, read_wfs_json and gml_ways take a council's answer
        # apart themselves, so a malformed one can raise anything; that is
        # one failed read, never the end of the run.
        self.run_with([self.way(i) for i in range(9)])
        for error in (AttributeError("'list' object has no attribute 'get'"),
                      ValueError("could not convert string to float: 'x'"),
                      IndexError("list index out of range")):
            entry = self.run_with(error=error)
            self.assertFalse(entry["ok"], error)
            self.assertIn(type(error).__name__, entry["error"])
            self.assertIn(str(error), entry["error"])
            self.assertLessEqual(len(entry["error"]), 300)
            self.assertEqual(len(self.saved()["ways"]), 9, error)
        # A long message is cut at exactly 300 characters, its start kept.
        long_error = KeyError("k" * 1000)
        entry = self.run_with(error=long_error)
        self.assertFalse(entry["ok"])
        self.assertEqual(len(entry["error"]), 300)
        self.assertEqual(entry["error"], ("an unreadable answer (KeyError): "
                                          + str(long_error))[:300])
        self.assertEqual(len(self.saved()["ways"]), 9)
        refused = FetchFailed("HTTP 500 " + "r" * 1000)
        entry = self.run_with(error=refused)
        self.assertEqual(entry["error"], str(refused)[:300])
        self.assertEqual(len(entry["error"]), 300)

    def test_a_licence_left_unstated_is_said_so(self):
        layer = dict(self.LAYER, licence=None)
        self.assertIn("without stating a licence", cw.attribution(layer))
        self.assertNotIn("Open Government", cw.attribution(layer))

    def test_a_cross_check_layer_is_reported_as_not_used(self):
        # status.json said "used": true for Isle of Wight and Hampshire,
        # which the build never uses while rowmaps has a file.
        os.makedirs(os.path.join(self.cache, "ZZ"))
        with open(os.path.join(self.cache, "ZZ", cw.BOAT_FILE), "w") as fh:
            json.dump({"type": "FeatureCollection", "features": [
                rowmaps("ZZ|Ash|%d" % i, line(0, 100 * i, 600, 100 * i))
                for i in range(9)]}, fh)

        def read(_client, _layer):
            ways = [self.way(i) for i in range(9)]
            return len(ways), ways
        entry = cw.fetch_one(dict(self.LAYER, read=read, role="cross-check"),
                             None, self.tmp, "2026-10-07", cache=self.cache)
        self.assertFalse(entry["merge"]["used"])
        self.assertIn("cross-check", entry["merge"]["why_not"])
        self.assertEqual(entry["merge"]["kept"], 9)

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
                                          cw.read_wfs_gml, cw.read_file),
                          layer["code"])
            if layer["read"] is cw.read_arcgis:
                self.assertTrue(layer["where"], layer["code"])
            elif layer["read"] is cw.read_file:
                # A download: read whole, its byways picked out here.
                self.assertTrue(callable(layer["format"]), layer["code"])
                self.assertTrue(callable(layer["match"]), layer["code"])
            else:
                self.assertEqual(layer["params"].get(
                    "request", layer["params"].get("REQUEST")), "GetFeature")

    def test_no_personal_or_office_field_is_asked_for(self):
        for layer in cw.LAYERS:
            fields = (layer.get("fields") or "*").upper()
            self.assertNotIn("EMAIL", fields, layer["code"])
        self.assertEqual(cw.by_code()["HP"]["role"], "cross-check")
        self.assertEqual(cw.by_code()["IW"]["role"], "cross-check")

    def test_every_code_is_the_authority_rowmaps_files_it_under(self):
        # HA is Halton: Hampshire under it would have been published as
        # Halton's byways.
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "manual",
                "authorities.json")) as fh:
            table = json.load(fh)
        for layer in cw.LAYERS:
            name = table[layer["code"]]["authority"]
            self.assertIn(name.split()[0].lower(),
                          layer["council"].lower(), layer["code"])

    def test_no_restricted_byway_filter_and_no_blocked_host(self):
        from polite_http import blocked, host_of
        for layer in cw.LAYERS:
            text = json.dumps([layer.get("where"), layer.get("filter")])
            self.assertNotIn("Restricted", text, layer["code"])
            self.assertFalse(blocked(host_of(layer["url"])), layer["code"])


#: The councils moved off rowmaps.com onto their own downloads by the
#: owner's decision of 30 October 2026.
#: Calderdale's file is a fixed January 2022 snapshot, so it is only a
#: cross-check; Norfolk's stays on rowmaps, held back on its licence.
SWITCHED = ("BM", "DY", "DZ", "DU", "KL", "L1", "LD", "NY", "SN", "YY",
            "SU", "WP")
CROSS_CHECKS = ("CA", "HP", "IW")

#: Left on rowmaps.com: their councils' hosts refuse us. Powys's GeoServer
#: answered 403 on 30 October 2026; Dorset's refuses GitHub's runners
#: (HOME-COLLECTOR.md, measured 7 October 2026). A refusal is never asked
#: again from the runners.
NOT_SWITCHED = {"PW": "geo.powys.gov.uk", "DT": "gi.dorsetcouncil.gov.uk"}

#: A host name in running text: what a credit must never carry.
HOSTLIKE = re.compile(r"\b[\w-]+(\.[\w-]+)*\.(uk|org|com|net|io)\b", re.I)


def tro_full_names():
    import csv
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "tro_authorities.csv"), encoding="utf-8") as fh:
        return set(r["display_name"] for r in csv.DictReader(fh))


def cf_records(records):
    """A `format` that ignores the bytes and answers these records."""
    return lambda _body: [(dict(a), [list(l) for l in ls])
                          for a, ls in records]


class Stand(object):
    """A stand-in client: answers by URL substring, records each GET."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        for key, value in self.answers.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise FetchFailed("HTTP 404 for %s" % url)


class SwitchedCouncils(unittest.TestCase):
    def test_each_is_read_from_the_council_and_credits_it_with_a_licence(
            self):
        layers = cw.by_code()
        names = tro_full_names()
        for code in SWITCHED:
            layer = layers.get(code)
            self.assertIsNotNone(layer, code)
            self.assertNotIn("role", layer, code)
            self.assertEqual(layer["licence"], "OGL-3.0", code)
            credit = cw.attribution(layer)
            self.assertIn(layer["council"], credit, code)
            self.assertNotIn("rowmaps", credit.lower(), code)
            self.assertIsNone(HOSTLIKE.search(credit), (code, credit))
            self.assertNotIn("without stating a licence", credit, code)
            # build_status finds the layer (and so its licence) by the
            # council's name in the authority table.
            self.assertIn(layer["council"], names, code)
            self.assertEqual(cw.public(layer)["attribution"], credit)

    def test_the_licence_said_is_the_one_the_council_gives(self):
        layers = cw.by_code()
        for code in SWITCHED:
            self.assertIn("Open Government Licence v3.0",
                          cw.attribution(layers[code]), code)
        # Surrey asks for the Ordnance Survey's credit beside the OGL, its
        # year the file's own (SurreyCredit).
        self.assertTrue(callable(layers["SU"]["credit"]))

    def test_a_layers_own_credit_follows_whatever_its_licence(self):
        for licence in ("OGL-3.0", "OS-OpenData", None):
            layer = {"council": "Zedshire County Council",
                     "licence": licence, "credit": "Contains Z data."}
            credit = cw.attribution(layer)
            self.assertTrue(credit.endswith(" Contains Z data."), licence)
            self.assertIn("Zedshire County Council", credit, licence)

    def test_the_host_list_covers_every_layer_and_says_its_robots(self):
        from polite_http import blocked, host_of
        for layer in cw.LAYERS:
            host = host_of(layer["url"])
            self.assertIn(host, cw.HOSTS, layer["code"])
            self.assertIsNone(cw.unlisted(layer), layer["code"])
        for host, said in cw.HOSTS.items():
            self.assertIn("robots.txt", said, host)
            self.assertFalse(blocked(host), host)
        # and nothing more: a host no layer reads, or is sent to by the
        # council's own redirect, is not left listed.
        self.assertEqual(set(cw.HOSTS),
                         set(host_of(l["url"]) for l in cw.LAYERS)
                         | set(h for l in cw.LAYERS
                               for h in l.get("redirect_hosts") or []))

    def test_the_councils_that_refuse_us_stay_on_rowmaps(self):
        from polite_http import host_of
        layers = cw.by_code()
        for code, host in NOT_SWITCHED.items():
            self.assertFalse(code in layers, code)
            self.assertNotIn(host, cw.HOSTS, code)
            self.assertFalse([l for l in cw.LAYERS
                              if host_of(l["url"]) == host], code)

    def test_a_layer_on_an_unlisted_host_is_never_fetched(self):
        stray = {"code": "ZZ", "council": "Zedshire County Council",
                 "licence": "OGL-3.0", "url": "https://elsewhere.example/x",
                 "read": cw.read_file, "format": cf_records([]),
                 "match": lambda a: True, "ref": lambda a: ("", "")}
        self.assertIn("elsewhere.example", cw.unlisted(stray))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        stand = Stand({})
        saved = cw.LAYERS[:]
        cw.LAYERS[:] = [stray]
        try:
            status, failed = cw.fetch(out_dir=tmp, today="2026-10-30",
                                      client=stand)
        finally:
            cw.LAYERS[:] = saved
        self.assertEqual(stand.asked, [])
        self.assertFalse(status["ZZ"]["ok"])
        self.assertIn("elsewhere.example", status["ZZ"]["error"])
        self.assertTrue(failed)


FILE_LAYER = {"code": "ZZ", "council": "Zedshire County Council",
              "licence": "OGL-3.0", "url": "https://z.example/row.zip",
              "read": cw.read_file,
              "match": lambda a: a.get("STATUS") == "BOAT",
              "ref": lambda a: ("Ash", a.get("NO")), "oid": "ID"}


class ReadFile(unittest.TestCase):
    RECORDS = [({"STATUS": "BOAT", "NO": "1", "ID": "71"}, [B1]),
               ({"STATUS": "Footpath", "NO": "2", "ID": "72"}, [B2]),
               ({"STATUS": "BOAT", "NO": "3", "ID": ""}, [B2]),
               ({"STATUS": "BOAT", "NO": "4", "ID": "74"}, [])]

    def test_only_matching_records_with_lines_become_ways(self):
        layer = dict(FILE_LAYER, format=cf_records(self.RECORDS))
        stand = Stand({"row.zip": b"zip bytes"})
        records, ways = cw.read_file(stand, layer)
        self.assertEqual(records, 4)
        self.assertEqual(stand.asked, ["https://z.example/row.zip"])
        self.assertEqual([(w["id"], w["parish"], w["number"]) for w in ways],
                         [("71", "Ash", "1"), ("2", "Ash", "3")])
        self.assertEqual(ways[0]["lines"], [B1])

    def test_without_an_id_field_the_record_number_is_the_id(self):
        layer = dict(FILE_LAYER, format=cf_records(self.RECORDS))
        del layer["oid"]
        _n, ways = cw.read_file(Stand({"row.zip": b""}), layer)
        self.assertEqual([w["id"] for w in ways], ["0", "2"])

    def test_the_bytes_read_are_the_ones_handed_to_the_format(self):
        seen = []
        layer = dict(FILE_LAYER, format=lambda b: seen.append(b) or [])
        cw.read_file(Stand({"row.zip": b"the file"}), layer)
        self.assertEqual(seen, [b"the file"])

    PAGE = (b'<a href="/files/2026_09-prow-shape.zip">old</a>'
            b'<a href="https://z.example/files/2026_10-prow-shape.zip">'
            b'shape</a><a href="/files/2026_10-prow-tab.zip">tab</a>')

    def test_a_dated_file_is_found_on_the_councils_page(self):
        layer = dict(FILE_LAYER, url="https://z.example/dataset/prow",
                     find=r'href="([^"]*2026_10-prow-shape\.zip)"',
                     format=cf_records(self.RECORDS))
        stand = Stand({"/dataset/prow": self.PAGE,
                       "2026_10-prow-shape.zip": b"zip"})
        records, _w = cw.read_file(stand, layer)
        self.assertEqual(records, 4)
        self.assertEqual(stand.asked, [
            "https://z.example/dataset/prow",
            "https://z.example/files/2026_10-prow-shape.zip"])

    def test_a_relative_link_is_taken_against_the_page(self):
        layer = dict(FILE_LAYER, url="https://z.example/dataset/prow",
                     find=r'href="(/files/2026_10-prow-tab\.zip)"',
                     format=cf_records([]))
        stand = Stand({"/dataset/prow": self.PAGE, "prow-tab.zip": b""})
        cw.read_file(stand, layer)
        self.assertEqual(stand.asked[-1],
                         "https://z.example/files/2026_10-prow-tab.zip")

    def test_no_link_two_links_or_another_host_is_a_failure(self):
        for page, why in (
                (b"<p>moved</p>", "none"),
                (b'<a href="/f/a-prow-shape.zip"></a>'
                 b'<a href="/f/b-prow-shape.zip"></a>', "two"),
                (b'<a href="https://other.example/f/a-prow-shape.zip"></a>',
                 "another host"),
                (b'<a href="http://z.example/f/a-prow-shape.zip"></a>',
                 "not https")):
            layer = dict(FILE_LAYER, url="https://z.example/dataset/prow",
                         find=r'href="([^"]*-prow-shape\.zip)"',
                         format=cf_records([]))
            stand = Stand({"/dataset/prow": page, ".zip": b""})
            with self.assertRaises(FetchFailed, msg=why):
                cw.read_file(stand, layer)
            self.assertEqual(len(stand.asked), 1, why)

    def test_the_same_link_twice_is_one_link(self):
        page = (b'<a href="/f/a-prow-shape.zip">x</a>'
                b'<a href="/f/a-prow-shape.zip">again</a>')
        layer = dict(FILE_LAYER, url="https://z.example/dataset/prow",
                     find=r'href="([^"]*-prow-shape\.zip)"',
                     format=cf_records([]))
        stand = Stand({"/dataset/prow": page, ".zip": b""})
        cw.read_file(stand, layer)
        self.assertEqual(stand.asked[-1],
                         "https://z.example/f/a-prow-shape.zip")


class ReadEveryFewDays(unittest.TestCase):
    """A file the council replaces monthly or yearly is read weekly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        saved = cw.LAYERS[:]
        self.addCleanup(cw.LAYERS.__setitem__, slice(None), saved)
        cw.HOSTS["z.example"] = "robots.txt: a test host"
        self.addCleanup(cw.HOSTS.pop, "z.example")
        ways = [({"STATUS": "BOAT", "NO": str(i), "ID": str(i)},
                 [line(0, 100 * i, 600, 100 * i)]) for i in range(5)]
        cw.LAYERS[:] = [dict(FILE_LAYER, format=cf_records(ways),
                             every_days=7)]

    def fetch(self, today, answers=None):
        stand = Stand(answers or {"row.zip": b""})
        status, failed = cw.fetch(out_dir=self.tmp, today=today,
                                  client=stand)
        return stand, status, failed

    def test_read_once_then_not_again_until_a_week_has_passed(self):
        stand, status, _f = self.fetch("2026-10-01")
        self.assertEqual(len(stand.asked), 1)
        self.assertEqual(status["ZZ"]["last_ok"], "2026-10-01")
        for day in ("2026-10-02", "2026-10-07"):
            stand, status, _f = self.fetch(day)
            self.assertEqual(stand.asked, [], day)
            self.assertEqual(status["ZZ"]["last_ok"], "2026-10-01", day)
        stand, status, _f = self.fetch("2026-10-08")
        self.assertEqual(len(stand.asked), 1)
        self.assertEqual(status["ZZ"]["last_ok"], "2026-10-08")

    def test_a_failed_read_is_tried_again_the_next_day(self):
        self.fetch("2026-10-01")
        _s, status, failed = self.fetch(
            "2026-10-08", {"row.zip": Refused("HTTP 403")})
        self.assertFalse(status["ZZ"]["ok"])
        self.assertEqual(status["ZZ"]["last_ok"], "2026-10-01")
        self.assertIn("403", failed[0])
        stand, status, _f = self.fetch("2026-10-09")
        self.assertEqual(len(stand.asked), 1)
        self.assertTrue(status["ZZ"]["ok"])

    def test_a_layer_without_every_days_is_read_daily(self):
        layer = dict(cw.LAYERS[0])
        del layer["every_days"]
        cw.LAYERS[:] = [layer]
        self.fetch("2026-10-01")
        stand, _s, _f = self.fetch("2026-10-02")
        self.assertEqual(len(stand.asked), 1)


class NewReferences(unittest.TestCase):
    def test_each_new_councils_reference_reads_as_rowmaps_names_it(self):
        layers = cw.by_code()
        cases = {
            "BM": ({"RouteCode": "NMA/19/3"}, ("NMA", "19/3")),
            "DY": ({"PARISH": "Beeley", "ROUTECODE": "NE5/13/2"},
                   ("Beeley", "13/2")),
            "DZ": ({"prow_id": "SED0557"}, ("SED", "0557")),
            "DU": ({"PARISH": "Consett", "PROW_NO__F": "19"},
                   ("Consett", "19")),
            "KL": ({"RouteCode": "HOL/180/10"}, ("HOL", "180/10")),
            "L1": ({"NUMBER_": "523004"}, ("523", "004")),
            "LD": ({"LOCAL_AR_1": "MORLEY", "PROW_ID": "146"},
                   ("MORLEY", "146")),
            "NY": ({"ROUTECODE": "25.104/6/2"}, ("25.104", "6/2")),
            "SN": ({"row_number": "318"}, ("St Helens", "318")),
            "YY": ({"row": "138M"}, ("M", "138")),
            "SU": ({"LocArea": "Chelsham & Farleigh", "PROWNo": "9a"},
                   ("Chelsham & Farleigh", "9a")),
            "WP": ({"PROW_NO": "145"}, ("", "145")),
        }
        self.assertEqual(sorted(cases), sorted(SWITCHED))
        self.assertEqual(layers["CA"]["ref"]({"RouteCode": "07/98/2"}),
                         ("07", "98/2"))
        for code, (attrs, want) in cases.items():
            self.assertEqual(layers[code]["ref"](attrs), want, code)
        # Stockport's lettered paths, Lake District's six-digit numbers.
        self.assertEqual(layers["YY"]["ref"]({"row": "55(b)CG"}),
                         ("CG", "55(b)"))
        self.assertEqual(layers["L1"]["ref"]({"NUMBER_": "52"}), ("", "52"))


class WhatEachSwitchedLayerPicks(unittest.TestCase):
    """Each layer's choice against the council's own wording, as read on
    30 October 2026: byways open to all traffic only."""

    def test_byways_in_restricted_byways_footpaths_and_orders_out(self):
        layers = cw.by_code()
        cases = {
            "BM": ("StatusDesc", ["BOAT"], ["Restricted Byway", "Bridleway",
                                           "Footpath"]),
            "CA": ("StatusDesc", ["BOAT"], ["Restricted Byway", "Footpath"]),
            "DU": ("STATUS", ["BY"], ["RB", "BW", "FP"]),
            "KL": ("Status", ["BYWAY"], ["RESTRICTED BYWAY", "FOOTPATH"]),
            "L1": ("Type_Text", ["BOAT"], ["RB", "BW", "FP"]),
            "LD": ("PROW_STATU", ["BY"], ["RB", "BW", "FP"]),
            "NY": ("DESIGNATIO", ["BOAT"], ["Restricted Byways", "UUR",
                                           "Bridleway"]),
            "YY": ("status", ["Byway Open to All Traffic (BOAT)"],
                   ["Restricted Byway", "Footpath"]),
            "SU": ("Stat_Text", ["Byway"], ["Restricted Byway",
                                            "Bridleway"]),
            "WP": ("ALT_NAME", ["Byway open to all traffic (BOAT)"],
                   ["DMMO (add BOAT)", "Restricted Byway",
                    "DMMO (add Restricted Byway)"]),
        }
        for code, (field, yes, no) in cases.items():
            layer = layers[code]
            self.assertIs(layer["read"], cw.read_file, code)
            for v in yes:
                self.assertTrue(layer["match"]({field: v}), (code, v))
            for v in no + ["", None]:
                self.assertFalse(layer["match"]({field: v}), (code, v))
        self.assertEqual(layers["DY"]["filter"], ("PROW_STATU", "BOAT"))

    # Dudley and St Helens answer a server-side filter with HTTP 500 and
    # 404 (30 October 2026): their whole layer is read and the byways are
    # picked out here, by the field each council fills.
    WHOLE_LAYERS = {
        "DZ": ("feature", "prow_id", "SED0557", ("SED", "0557"),
               ["Byway", "Footpath", "Bridleway", "Cycle Path",
                "Permissive Path"]),
        "SN": ("row_classification", "row_number", "212",
               ("St Helens", "212"), ["Footpath", "Bridleway"]),
    }

    def test_dudley_and_st_helens_are_read_whole_and_picked_here(self):
        layers = cw.by_code()
        boat = "Byway Open to All Traffic"
        for code, (field, ref_field, ref, want, others) in \
                self.WHOLE_LAYERS.items():
            layer = layers[code]
            self.assertIs(layer["read"], cw.read_wfs_gml, code)
            self.assertNotIn("filter", layer, code)
            rows = [{"ogc_fid": "1", field: boat, ref_field: ref}] + [
                {"ogc_fid": str(i + 2), field: v, ref_field: "X%d" % i}
                for i, v in enumerate(others + [""])]
            stand = Stand({layer["url"]: gml_layer(
                layer["params"]["typeName"], rows)})
            records, ways = cw.read_wfs_gml(stand, layer)
            self.assertEqual(len(stand.asked), 1, code)
            self.assertTrue(stand.asked[0].startswith(layer["url"] + "&"),
                            code)
            self.assertNotIn("FILTER", stand.asked[0].upper(), code)
            self.assertIn("typeName=" + layer["params"]["typeName"],
                          stand.asked[0], code)
            self.assertEqual(records, len(rows), code)
            self.assertEqual([(w["parish"], w["number"]) for w in ways],
                             [want], code)

    def test_derbyshire_asks_only_for_the_fields_it_uses(self):
        props = cw.by_code()["DY"]["params"]["propertyName"]
        self.assertNotIn("EMAIL", props.upper())
        self.assertIn("the_geom", props)

    def test_whole_files_and_whole_layers_are_read_weekly_others_daily(self):
        for layer in cw.LAYERS:
            if layer["read"] is cw.read_file or \
                    layer["code"] in self.WHOLE_LAYERS:
                self.assertEqual(layer.get("every_days"), 7, layer["code"])
            else:
                self.assertIsNone(layer.get("every_days"), layer["code"])


# ------------------------------------------------- the review round, 30 Oct

import contextlib  # noqa: E402
import io  # noqa: E402
import sqlite3  # noqa: E402

import test_council_files as tcf  # noqa: E402


class Exact(Stand):
    """A stand-in client answering exact URLs only."""

    def get(self, url):
        self.asked.append(url)
        if url not in self.answers:
            raise FetchFailed("HTTP 404 for %s" % url)
        value = self.answers[url]
        if isinstance(value, Exception):
            raise value
        return value


#: Two lines in British National Grid, one near Stockport.
GRID = [(430000.0, 380000.0), (430100.0, 380050.0)]
STOCKPORT = [(390000.0, 390000.0), (390100.0, 390050.0)]


def shp_zip(fields, rows, name="R"):
    """A zipped shapefile: one line per row, every row on GRID."""
    return tcf.zipped({name + ".shp": tcf.shp([[GRID] for _r in rows]),
                       name + ".dbf": tcf.dbf(fields, rows),
                       name + ".prj": tcf.BNG_PRJ})


def geojson(rows, coords, crs=None):
    doc = {"type": "FeatureCollection",
           "features": [{"type": "Feature", "properties": r,
                         "geometry": {"type": "LineString",
                                      "coordinates": [list(p) for p in
                                                      coords]}}
                        for r in rows]}
    if crs:
        doc["crs"] = {"type": "name", "properties": {"name": crs}}
    return json.dumps(doc).encode("utf-8")


def surrey_gpkg(rows, last_change="2026-09-07T10:12:00Z"):
    """Surrey's GeoPackage, zipped as Surrey publishes it: columns id,
    geom, Stat_Text, LocArea, PROWNo; dated in gpkg_contents."""
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    os.close(fd)
    os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, "
                 "column_name TEXT, geometry_type_name TEXT, srs_id INT, "
                 "z INT, m INT)")
    conn.execute("INSERT INTO gpkg_geometry_columns VALUES "
                 "('RoW', 'geom', 'MULTILINESTRING', 27700, 0, 0)")
    conn.execute("CREATE TABLE gpkg_contents (table_name TEXT, "
                 "data_type TEXT, last_change TEXT)")
    if last_change is not None:
        conn.execute("INSERT INTO gpkg_contents VALUES ('RoW', 'features', "
                     "?)", (last_change,))
    conn.execute('CREATE TABLE "RoW" (id INTEGER PRIMARY KEY, geom BLOB, '
                 'Stat_Text TEXT, LocArea TEXT, PROWNo TEXT)')
    for i, (stat, area, no) in enumerate(rows, 1):
        conn.execute('INSERT INTO "RoW" VALUES (?, ?, ?, ?, ?)',
                     (i, tcf.gp(tcf.wkb_multi([GRID])), stat, area, no))
    conn.commit()
    conn.close()
    with open(path, "rb") as fh:
        data = fh.read()
    os.remove(path)
    return tcf.zipped({"RoW_OpenData_September_2026.gpkg": data})


NY_FILE = ("https://hub.datanorthyorkshire.org/dataset/"
           "5a7a0b1c-2d3e-4f50-8a6b-7c8d9e0f1a2b/resource/"
           "1919c0de-aaaa-4bbb-8ccc-dddddddddddd/download/"
           "2026_10-nyc-prow-shape.zip")
#: The dataset page's two download links, as the page wrote them on
#: 30 October 2026 (ids shortened there; any id fits the pattern).
NY_PAGE = ('<li><a class="resource-url-analytics" href="%s">Download</a>'
           '<a class="resource-url-analytics" href="%s">Download</a></li>'
           % (NY_FILE.replace("-shape.zip", "-tab.zip"), NY_FILE)
           ).encode("utf-8")
SU_FILE = ("https://www.surreycc.gov.uk/__data/assets/file/0011/267428/"
           "RoW_OpenData_September_2026.zip")
SU_PAGE = ('<p><a href="%s" class="scc-btn scc-btn--primary">Download '
           'Rights of Way open data (ZIP)</a></p>' % SU_FILE).encode("utf-8")


class EachSwitchedLayerEndToEnd(unittest.TestCase):
    """Every switched file layer, through its own format, match and ref,
    on a file shaped as the council publishes it: one byway, one other."""

    def run_layer(self, code, answers):
        layer = cw.by_code()[code]
        stand = Exact(answers)
        got = cw.read_file(stand, layer)
        return got[0], got[1], stand

    def shapefile(self, code, fields, boat, other, want_ref, want_id):
        layer = cw.by_code()[code]
        records, ways, _s = self.run_layer(
            code, {layer["url"]: shp_zip(fields, [boat, other])})
        self.assertEqual(records, 2, code)
        self.assertEqual([(w["id"], (w["parish"], w["number"]))
                          for w in ways], [(want_id, want_ref)], code)
        self.assertEqual(ways[0]["lines"], [tcf.wgs(GRID)], code)

    def test_buckinghamshire(self):
        self.shapefile("BM", [("StatusDesc", 20), ("RouteCode", 12),
                              ("MI_PRINX", 6)],
                       ("BOAT", "NMA/19/3", "501"),
                       ("Restricted Byway", "NMA/20/1", "502"),
                       ("NMA", "19/3"), "501")

    def test_durham(self):
        self.shapefile("DU", [("STATUS", 4), ("PARISH", 12),
                              ("PROW_NO__F", 6), ("CRMID", 6)],
                       ("BY", "Consett", "19", "77"),
                       ("RB", "Consett", "20", "78"),
                       ("Consett", "19"), "77")

    def test_kirklees(self):
        self.shapefile("KL", [("Status", 20), ("RouteCode", 12)],
                       ("BYWAY", "HOL/180/10"),
                       ("RESTRICTED BYWAY", "HOL/181/1"),
                       ("HOL", "180/10"), "HOL/180/10")

    def test_lake_district(self):
        self.shapefile("L1", [("Type_Text", 6), ("NUMBER_", 8)],
                       ("BOAT", "523004"), ("RB", "523005"),
                       ("523", "004"), "523004")

    def test_leeds(self):
        self.shapefile("LD", [("PROW_STATU", 4), ("LOCAL_AR_1", 12),
                              ("PROW_ID", 6), ("OBJECTID", 6)],
                       ("BY", "MORLEY", "146", "9"),
                       ("FP", "MORLEY", "147", "10"),
                       ("MORLEY", "146"), "9")

    def test_telford(self):
        self.shapefile("WP", [("ALT_NAME", 40), ("PROW_NO", 6),
                              ("OBJECTID", 6)],
                       ("Byway open to all traffic (BOAT)", "145", "3"),
                       ("DMMO (add BOAT)", "146", "4"),
                       ("", "145"), "3")

    def test_north_yorkshire_finds_this_months_shapefile(self):
        layer = cw.by_code()["NY"]
        blob = shp_zip([("DESIGNATIO", 20), ("ROUTECODE", 12)],
                       [("BOAT", "25.104/6/2"), ("Bridleway", "25.104/7/1")])
        records, ways, stand = self.run_layer(
            "NY", {layer["url"]: NY_PAGE, NY_FILE: blob})
        self.assertEqual(stand.asked, [layer["url"], NY_FILE])
        self.assertEqual(records, 2)
        self.assertEqual([(w["id"], w["parish"], w["number"]) for w in ways],
                         [("25.104/6/2", "25.104", "6/2")])

    def test_the_find_patterns_take_only_the_real_link(self):
        ny = re.findall(cw.by_code()["NY"]["find"], NY_PAGE.decode())
        self.assertEqual(ny, [NY_FILE])
        su = re.findall(cw.by_code()["SU"]["find"], SU_PAGE.decode())
        self.assertEqual(su, [SU_FILE])

    def test_calderdale_is_read_as_degrees_whatever_its_crs_says(self):
        layer = cw.by_code()["CA"]
        pts = [(-1.95, 53.72), (-1.951, 53.721)]
        blob = geojson([{"StatusDesc": "BOAT", "RouteCode": "07/98/2",
                         "fid": 4},
                        {"StatusDesc": "Footpath", "RouteCode": "07/1/1",
                         "fid": 5}], pts, crs="EPSG:27700")
        records, ways, _s = self.run_layer("CA", {layer["url"]: blob})
        self.assertEqual(records, 2)
        self.assertEqual([(w["id"], w["parish"], w["number"]) for w in ways],
                         [("4", "07", "98/2")])
        self.assertEqual(ways[0]["lines"], [pts])

    def test_stockport_is_read_as_the_national_grid(self):
        layer = cw.by_code()["YY"]
        blob = geojson([{"status": "Byway Open to All Traffic (BOAT)",
                         "row": "138M", "ogc_fid": 12},
                        {"status": "Restricted Byway", "row": "139M",
                         "ogc_fid": 13}], STOCKPORT)
        records, ways, _s = self.run_layer("YY", {layer["url"]: blob})
        self.assertEqual(records, 2)
        self.assertEqual([(w["id"], w["parish"], w["number"]) for w in ways],
                         [("12", "M", "138")])
        self.assertEqual(ways[0]["lines"], [tcf.wgs(STOCKPORT)])
        lon, lat = ways[0]["lines"][0][0]
        self.assertTrue(-2.2 < lon < -2.1 and 53.35 < lat < 53.45,
                        (lon, lat))

    def test_surrey_finds_its_file_and_dates_its_credit(self):
        layer = cw.by_code()["SU"]
        blob = surrey_gpkg([("Byway", "Chelsham & Farleigh", "9a"),
                            ("Restricted Byway", "Tatsfield", "1")])
        records, ways, stand = self.run_layer(
            "SU", {layer["url"]: SU_PAGE, SU_FILE: blob})
        self.assertEqual(stand.asked, [layer["url"], SU_FILE])
        self.assertEqual(records, 2)
        self.assertEqual([(w["id"], w["parish"], w["number"]) for w in ways],
                         [("1", "Chelsham & Farleigh", "9a")])
        self.assertEqual(ways[0]["lines"], [tcf.wgs(GRID)])

    def test_every_switched_file_layer_is_covered_here(self):
        covered = {"BM", "DU", "KL", "L1", "LD", "WP", "NY", "CA", "YY",
                   "SU"}
        self.assertEqual(covered, set(l["code"] for l in cw.LAYERS
                                      if l["read"] is cw.read_file))


class SurreyCredit(unittest.TestCase):
    """Surrey's Ordnance Survey credit carries the year of the file's own
    date (gpkg_contents last_change), never today's."""

    OS_2026 = "Contains OS data © Crown copyright and database right 2026."

    def fetch(self, blob):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        layer = cw.by_code()["SU"]
        stand = Exact({layer["url"]: SU_PAGE, SU_FILE: blob})
        entry = cw.fetch_one(layer, stand, tmp, "2026-10-30", cache=tmp)
        return entry, cw.read_json(os.path.join(tmp, "SU.json"))

    def test_the_credit_is_dated_from_the_file(self):
        entry, saved = self.fetch(surrey_gpkg([("Byway", "Ash", "1")]))
        self.assertTrue(entry["ok"], entry)
        source = saved["source"]
        self.assertEqual(source["credit"], self.OS_2026)
        self.assertEqual(
            source["attribution"],
            cw.OGL + " Source: Surrey County Council's public rights of way "
            "layer, as published by the council. " + self.OS_2026)
        for way in saved["ways"]:
            self.assertEqual(way["parish"], "Ash")

    def test_another_years_file_says_that_year(self):
        _e, saved = self.fetch(surrey_gpkg([("Byway", "Ash", "1")],
                                           "2027-01-04T00:00:00Z"))
        self.assertTrue(saved["source"]["credit"].endswith(
            "database right 2027."))

    def test_a_file_with_no_date_is_not_used(self):
        entry, saved = self.fetch(surrey_gpkg([("Byway", "Ash", "1")],
                                              None))
        self.assertFalse(entry["ok"])
        self.assertIn("date", entry["error"])
        self.assertIsNone(saved)


class ReadFileRefusesWhatItCannotRead(unittest.TestCase):
    """A file that makes the format, the match or the ref throw is a failed
    read of that council, never a crash of the whole run."""

    def layer(self, **kw):
        records = [({"STATUS": "BOAT", "NO": "1", "ID": "71"}, [B1])]
        return dict(dict(FILE_LAYER, format=cf_records(records)), **kw)

    def boom(self, error):
        def raise_it(*_a):
            raise error
        return raise_it

    def test_format_match_and_ref_errors_are_failed_reads(self):
        for part, error in (("format", ValueError("bad bytes")),
                            ("match", KeyError("STATUS")),
                            ("ref", AttributeError("no strip"))):
            layer = self.layer(**{part: self.boom(error)})
            with self.assertRaises(FetchFailed, msg=part) as caught:
                cw.read_file(Stand({"row.zip": b""}), layer)
            self.assertIn(type(error).__name__, str(caught.exception), part)

    def test_a_refusal_is_still_a_refusal(self):
        with self.assertRaises(Refused):
            cw.read_file(Stand({"row.zip": Refused("HTTP 403")}),
                         self.layer())

    def test_the_run_goes_on_to_the_next_council_and_saves_status(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        cw.HOSTS["z.example"] = "robots.txt: a test host"
        self.addCleanup(cw.HOSTS.pop, "z.example")
        saved = cw.LAYERS[:]
        self.addCleanup(cw.LAYERS.__setitem__, slice(None), saved)
        ways = [({"STATUS": "BOAT", "NO": str(i), "ID": str(i)},
                 [line(0, 100 * i, 600, 100 * i)]) for i in range(3)]
        cw.LAYERS[:] = [
            dict(self.layer(), code="ZA", ref=self.boom(TypeError("x"))),
            dict(FILE_LAYER, code="ZB", format=cf_records(ways))]
        status, failed = cw.fetch(out_dir=tmp, today="2026-10-30",
                                  client=Stand({"row.zip": b""}))
        self.assertFalse(status["ZA"]["ok"])
        self.assertIn("TypeError", status["ZA"]["error"])
        self.assertTrue(status["ZB"]["ok"])
        on_disk = cw.read_json(os.path.join(tmp, "status.json"))
        self.assertEqual(sorted(on_disk), ["ZA", "ZB"])
        self.assertTrue(on_disk["ZB"]["ok"])
        self.assertTrue(os.path.exists(os.path.join(tmp, "ZB.json")))
        self.assertTrue(any(l.startswith("ZA ") for l in failed))


class TheReviewRoundsDecisions(unittest.TestCase):
    def test_norfolk_stays_on_rowmaps_held_back_on_its_licence(self):
        self.assertNotIn("NK", cw.by_code())
        self.assertNotIn("maps.norfolk.gov.uk", cw.HOSTS)

    def test_calderdale_and_the_old_layers_are_cross_checks(self):
        layers = cw.by_code()
        for code in CROSS_CHECKS:
            self.assertEqual(layers[code].get("role"), "cross-check", code)
            self.assertEqual(cw.public(layers[code])["role"], "cross-check")
        self.assertFalse(set(CROSS_CHECKS) & set(SWITCHED))

    def test_dudleys_reference_reader_is_named_for_dudley(self):
        self.assertIs(cw.by_code()["DZ"]["ref"], cw._ref_dudley)
        self.assertFalse(hasattr(cw, "_ref_wokingham_like"))

    def test_the_credit_says_the_council_published_it(self):
        layer = {"council": "Zedshire County Council", "licence": None}
        self.assertEqual(
            cw.attribution(layer),
            "Source: Zedshire County Council's public rights of way layer, "
            "as published by the council. The council publishes it without "
            "stating a licence.")
        self.assertEqual(
            cw.attribution(dict(layer, licence="OGL-3.0")),
            cw.OGL + " Source: Zedshire County Council's public rights of "
            "way layer, as published by the council.")


class HostPaths(unittest.TestCase):
    """The shared S3 host is read only inside Stockport's own bucket, and
    Buckinghamshire's media host only under /documents/."""

    S3 = "s3-eu-west-1.amazonaws.com"
    MEDIA = "media.buckinghamshire.gov.uk"

    def test_the_s3_host_is_pinned_to_stockports_bucket(self):
        self.assertEqual(cw.HOST_PATHS, {self.S3: "/smbc-opendata/",
                                         self.MEDIA: "/documents/"})
        self.assertIsNone(cw.unlisted(cw.by_code()["YY"]))
        why = cw.unlisted({"url": "https://%s/another-bucket/x.json"
                           % self.S3})
        self.assertIn("/smbc-opendata/", why)

    def test_every_pinned_host_is_a_listed_host(self):
        self.assertTrue(set(cw.HOST_PATHS) <= set(cw.HOSTS))

    def test_the_client_is_kept_to_the_listed_hosts_and_paths(self):
        allow = cw.client_allow()
        self.assertEqual(set(allow), set(cw.HOSTS))
        self.assertEqual(allow[self.S3], "/smbc-opendata/")
        self.assertIsNone(allow["datamillnorth.org"])

    def bm_client(self, location):
        """The real client, kept to client_allow(), over a stand-in network
        where Buckinghamshire's file answers 302 to `location` (measured 30
        October 2026: the www host sends it to the media host)."""
        from test_polite_http import real_client
        bm = cw.by_code()["BM"]
        return bm["url"], real_client({
            "https://www.buckinghamshire.gov.uk/robots.txt": (
                200, {}, b"User-agent: *\nAllow: /\n"),
            bm["url"]: (302, {"Location": location}, b""),
            "https://%s/robots.txt" % self.MEDIA: (403, {}, b""),
            "https://%s/documents/ROW_2025.zip" % self.MEDIA: (
                200, {}, b"the file"),
            "https://elsewhere.example/robots.txt": (403, {}, b""),
            "https://elsewhere.example/documents/ROW_2025.zip": (
                200, {}, b"not the council's")}, allow=cw.client_allow())

    def test_buckinghamshires_file_is_followed_to_its_media_host(self):
        media = "https://%s/documents/ROW_2025.zip" % self.MEDIA
        url, (client, transport) = self.bm_client(media)
        self.assertEqual(client.get(url), b"the file")
        self.assertIn(media, [u for u, _h in transport.asked])
        # Only /documents/ on the media host.
        for other in ("https://%s/images/x.zip" % self.MEDIA,
                      "https://%s/documents/../x.zip" % self.MEDIA):
            with self.assertRaises(Refused, msg=other):
                client.get(other)

    def test_a_redirect_to_a_host_not_listed_is_refused(self):
        url, (client, transport) = self.bm_client(
            "https://elsewhere.example/documents/ROW_2025.zip")
        with self.assertRaises(Refused):
            client.get(url)
        self.assertFalse([u for u, _h in transport.asked
                          if "elsewhere.example" in u])


class FetchOnlySaysWhyNothingWasRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        saved = cw.LAYERS[:]
        self.addCleanup(cw.LAYERS.__setitem__, slice(None), saved)
        cw.HOSTS["z.example"] = "robots.txt: a test host"
        self.addCleanup(cw.HOSTS.pop, "z.example")
        ways = [({"STATUS": "BOAT", "NO": str(i), "ID": str(i)},
                 [line(0, 100 * i, 600, 100 * i)]) for i in range(3)]
        cw.LAYERS[:] = [dict(FILE_LAYER, format=cf_records(ways),
                             every_days=7),
                        dict(FILE_LAYER, code="ZY", format=cf_records(ways),
                             every_days=7)]
        self.made = []
        real = cw.polite_http.PoliteClient

        def client(**kw):
            self.made.append(kw)
            return Stand({"row.zip": b""})
        cw.polite_http.PoliteClient = client
        self.addCleanup(setattr, cw.polite_http, "PoliteClient", real)

    def main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cw.main(["fetch", "--out", self.tmp] + list(args))
        return rc, out.getvalue()

    def test_a_layer_not_yet_due_is_named_with_its_last_read(self):
        self.main("--today", "2026-10-01")
        rc, said = self.main("--only", "ZZ", "--today", "2026-10-03")
        self.assertEqual(rc, 0)
        lines = [l for l in said.splitlines() if l.startswith("ZZ ")]
        self.assertEqual(len(lines), 1, said)
        self.assertIn("2026-10-01", lines[0])
        self.assertIn("every 7 days", lines[0])
        self.assertIn("2026-10-08", lines[0])
        self.assertNotIn("ZY", said)

    def test_the_run_reads_through_a_client_kept_to_the_listed_hosts(self):
        self.main("--today", "2026-10-01")
        self.assertEqual(self.made, [{"allow": cw.client_allow()}])
        self.assertIn("z.example", self.made[0]["allow"])


class BywayCreditsReachRiders(unittest.TestCase):
    """The manifest lists the councils whose own byways a build used
    (bywaySources), and neither a pack nor the manifest calls a layer
    published without a licence Open Government Licence."""

    SOURCES = [
        {"code": "BM", "council": "Buckinghamshire Council",
         "licence": "OGL-3.0", "url": "https://b.example/x.zip",
         "attribution": cw.OGL + " Source: Buckinghamshire Council's public "
                        "rights of way layer, as published by the council."},
        {"code": "DN", "council": "Devon County Council", "licence": None,
         "url": "https://d.example/0",
         "attribution": "Source: Devon County Council's public rights of way "
                        "layer, as published by the council. The council "
                        "publishes it without stating a licence."},
        {"code": "SU", "council": "Surrey County Council",
         "licence": "OGL-3.0", "url": "https://s.example/x.zip",
         "credit": "Contains OS data © Crown copyright and database "
                   "right 2026.",
         "attribution": "Surrey's."},
        {"code": "HP", "council": "Hampshire County Council",
         "licence": None, "url": "https://h.example/0", "attribution": "H."},
    ]

    def lane(self, code, council):
        return {"properties": {"authorityCode": code,
                               "source": ("council:x" if council
                                          else "rowmaps:x")}}

    def test_sources_are_counted_by_the_ways_they_gave(self):
        lanes = [self.lane("BM", True), self.lane("BM", True),
                 self.lane("DN", True), self.lane("SU", False),
                 self.lane("HP", False)]
        got = build_packages.byway_sources_in(self.SOURCES, lanes)
        self.assertEqual([(s["code"], s["count"]) for s in got],
                         [("BM", 2), ("DN", 1)])
        self.assertEqual(got[0]["attribution"],
                         self.SOURCES[0]["attribution"])

    def test_no_council_byways_leaves_licence_and_credit_as_they_were(self):
        P = build_packages
        self.assertEqual(P.dataset_licence([], []), "OGL-3.0")
        self.assertEqual(P.dataset_licence([]), "OGL-3.0")
        self.assertEqual(P.rights_of_way_attribution([]), P.OGL)

    def test_a_layer_without_a_licence_is_not_called_ogl(self):
        P = build_packages
        used = [dict(s, count=3) for s in self.SOURCES[:3]]
        licence = P.dataset_licence([], used)
        self.assertNotEqual(licence, "OGL-3.0")
        self.assertIn("Devon County Council", licence)
        self.assertIn("without a stated licence", licence)
        self.assertNotIn("Buckinghamshire", licence)
        # Every layer OGL: the whole build is.
        ogl_only = [used[0], used[2]]
        self.assertEqual(P.dataset_licence([], ogl_only), "OGL-3.0")
        said = P.rights_of_way_attribution(used)
        for s in used:
            self.assertIn(s["attribution"], said)
        self.assertTrue(said.endswith(" Other rights of way: " + P.OGL),
                        said)
        # One plain sentence before the councils' own credits, each of
        # which begins "Source:" or with the licence - never a label run
        # into another label.
        self.assertEqual(said, "Some byways are read from the councils' own "
                         "layers. " + " ".join(s["attribution"] for s in used)
                         + " Other rights of way: " + P.OGL)
        self.assertNotIn(": Source:", said)
        self.assertNotIn("publications", said)
        self.assertIn("via rowmaps.com", said)

    def test_a_pack_with_council_byways_credits_each_council(self):
        P = build_packages
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        key = b"k" * 32
        real_dist = P.dist_dir
        P.dist_dir = lambda: tmp
        self.addCleanup(setattr, P, "dist_dir", real_dist)

        def feature(uid, source):
            return {"type": "Feature",
                    "properties": {"lane_uid": uid, "authorityCode": "DN",
                                   "source": source},
                    "geometry": {"type": "LineString",
                                 "coordinates": [[-1, 52], [-1.01, 52]]}}

        import build_map_container
        path = os.path.join(tmp, "packages", "ways-gb-x.tbpack")

        def sealed(features, **kw):
            P.write_package("ways", "gb-x", "X", None,
                            copy.deepcopy(features), key,
                            "2026-10-30T00:00:00Z", **kw)
            with open(path, "rb") as fh:
                return fh.read(), build_map_container.unpack(path, key)

        plain = [feature("a", "rowmaps:devon")]
        before, body = sealed(plain)
        self.assertEqual(body["attribution"], P.OGL)
        # Sources, but no council byway in this pack: the same bytes.
        self.assertEqual(sealed(plain, byway_sources=self.SOURCES)[0],
                         before)
        _b, body = sealed([feature("a", "council:devon")],
                          byway_sources=self.SOURCES)
        devon = self.SOURCES[1]
        self.assertEqual(body["attribution"], P.rights_of_way_attribution(
            [dict(devon, count=1)]))
        self.assertNotIn("Buckinghamshire", body["attribution"])
        self.assertNotIn("bywaySources", body)

    def test_the_manifest_lists_the_layers_and_is_unchanged_without(self):
        P = build_packages
        none = P.manifest_credit([], [], [])
        self.assertEqual(list(none.items()), [
            ("attribution", P.OGL), ("licence", "OGL-3.0"),
            ("source", "Local highway authority definitive maps via "
                       "rowmaps.com")])
        used = [dict(s, count=2) for s in self.SOURCES[:2]]
        got = P.manifest_credit([], ["Devon County Council",
                                     "Buckinghamshire Council"], used)
        self.assertEqual(list(got), ["attribution", "licence", "source",
                                     "councilLayers", "bywaySources"])
        self.assertEqual(got["bywaySources"], used)
        self.assertEqual(got["attribution"],
                         P.rights_of_way_attribution(used))
        self.assertEqual(got["licence"], P.dataset_licence([], used))
        self.assertTrue(got["source"].endswith(
            "; byways for 2 authorities from the councils' own "
            "layers"))
        self.assertEqual(got["councilLayers"], ["Buckinghamshire Council",
                                                "Devon County Council"])

    def test_one_council_is_named_in_the_singular(self):
        P = build_packages
        devon = [dict(self.SOURCES[1], count=2)]
        got = P.manifest_credit([], ["Devon County Council"], devon)
        self.assertEqual(got["source"], "Local highway authority definitive "
                         "maps via rowmaps.com; byways for 1 authority from "
                         "the council's own layer")
        self.assertTrue(got["attribution"].startswith(
            "Some byways are read from the council's own layer. "),
            got["attribution"])
        self.assertIn("except the byways of Devon County Council, read from "
                      "the council's own layer, which are published",
                      got["licence"])
        for said in got.values():
            self.assertNotIn("councils' own", str(said))

    def test_the_licence_names_no_decision_maker(self):
        P = build_packages
        licence = P.dataset_licence(
            [{"code": "DN", "council": "Devon County Council", "count": 4,
              "licence": None}])
        self.assertIn("the unsurfaced unclassified roads of Devon County "
                      "Council are council highway records published "
                      "without a stated licence, on the decision of 8 "
                      "October 2026, credited to each council (ucrSources)",
                      licence)
        self.assertNotIn("owner", licence)

    def test_a_pack_with_roads_and_council_byways_credits_both(self):
        P = build_packages
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        real_dist = P.dist_dir
        P.dist_dir = lambda: tmp
        self.addCleanup(setattr, P, "dist_dir", real_dist)
        import build_map_container

        def feature(uid, code, source):
            return {"type": "Feature",
                    "properties": {"lane_uid": uid, "authorityCode": code,
                                   "source": source},
                    "geometry": {"type": "LineString",
                                 "coordinates": [[-1, 52], [-1.01, 52]]}}
        ucr_sources = [{"code": "NY", "council": "North Yorkshire Council",
                        "licence": "OGL-3.0", "attribution": "NY roads."}]
        P.write_package("ways", "gb-x", "X", None,
                        [feature("a", "DN", "council:devon")], b"k" * 32,
                        "2026-10-30T00:00:00Z",
                        ucrs=[feature("u", "NY", "council:ny")],
                        ucr_sources=ucr_sources, byway_sources=self.SOURCES)
        body = build_map_container.unpack(
            os.path.join(tmp, "packages", "ways-gb-x.tbpack"), b"k" * 32)
        self.assertEqual(body["attribution"], P.dataset_attribution(
            P.rights_of_way_attribution([dict(self.SOURCES[1], count=1)]),
            [dict(ucr_sources[0], count=1)]))

    def test_the_build_records_the_source_of_each_layer_it_used(self):
        P = build_packages
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        cache, out = os.path.join(tmp, "cache"), os.path.join(tmp, "out")
        os.makedirs(os.path.join(cache, "ZZ"))
        os.makedirs(os.path.join(cache, "ZY"))
        for code in ("ZZ", "ZY"):
            with open(os.path.join(cache, code, P.COUNCIL_LAYER_TYPE
                                   + ".json"), "w") as fh:
                json.dump({"features": [rowmaps("%s|Ash|1" % code, B1)]},
                          fh)
        data = council(("Ash", "1", [B1]))
        data["source"] = {"code": "ZZ", "council": "Zedshire County Council",
                          "licence": None, "attribution": "Z."}
        os.makedirs(out)
        with open(os.path.join(out, "ZZ.json"), "w") as fh:
            json.dump(data, fh)
        import datetime
        with open(os.path.join(out, "status.json"), "w") as fh:
            json.dump({"ZZ": {"ok": True, "last_ok":
                              datetime.date.today().isoformat()}}, fh)
        real = (P.cache_dir, cw.OUT, P.COUNCIL_LAYERS_USED[:],
                P.COUNCIL_SOURCES_USED[:])

        def restore():
            P.cache_dir, cw.OUT = real[0], real[1]
            P.COUNCIL_LAYERS_USED[:] = real[2]
            P.COUNCIL_SOURCES_USED[:] = real[3]
        self.addCleanup(restore)
        P.cache_dir, cw.OUT = (lambda: cache), out
        P.COUNCIL_LAYERS_USED[:] = []
        P.COUNCIL_SOURCES_USED[:] = []
        with contextlib.redirect_stdout(io.StringIO()):
            by_type = P.load_all({"ZZ": "Zedshire", "ZY": "Wyeshire"})
        self.assertEqual(P.COUNCIL_SOURCES_USED, [data["source"]])
        lanes = by_type[P.COUNCIL_LAYER_TYPE]
        got = P.byway_sources_in(P.COUNCIL_SOURCES_USED, lanes)
        self.assertEqual(got, [dict(data["source"], count=1)])
        self.assertIn("without a stated licence",
                      P.dataset_licence([], got))


class ReadmeSaysWhatIsRead(unittest.TestCase):
    """The public README's account of the sources agrees with the code."""

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "README.md")
        with open(path, encoding="utf-8") as fh:
            self.text = " ".join(fh.read().split())

    def test_the_opening_credit_names_every_kind_of_source(self):
        self.assertNotIn("Every package here is built from", self.text)
        self.assertIn("> Source: local highway authority definitive maps, "
                      "via rowmaps.com, and > councils' own layers as "
                      "credited in the lane data.", self.text)

    def test_west_berkshire_names_the_host_read_and_the_one_not(self):
        import polite_http
        read, not_read = "gis.westberks.gov.uk", "gis2.westberks.gov.uk"
        self.assertIn(read, cw.HOSTS)
        self.assertIn(not_read, polite_http.ROBOTS_HOSTS)
        said = re.findall(r"West Berkshire's (?:byways are|closures layer "
                          r"is) read from the council's GIS host `([^`]+)`, "
                          r"which has no robots.txt[;.] (?:its|Its) older GIS "
                          r"host, `([^`]+)`, whose robots.txt disallows "
                          r"automated access, is not read", self.text)
        self.assertEqual(said, [(read, not_read)] * 2)

    def test_kent_dartmoor_and_exmoor_are_said_plainly(self):
        self.assertIn("Kent's, Dartmoor's and Exmoor's own layers are not "
                      "read at present.", self.text)
        self.assertNotIn("licences that do not allow this use", self.text)

    def test_each_public_correction_is_there(self):
        for said in (
                # The roads' layers, not the byway layers, state no licence.
                "No licence is stated for these unsurfaced-road layers; see",
                "Except as noted below, where a council publishes its own "
                "byway layer,",
                # The collector reads closures, not byways.
                "the closures of a council whose servers do not answer "
                "GitHub's shared runners (Dorset, Powys) are read from one "
                "fixed, honestly named collector server",
                "until we have confirmed the terms of the licence its file "
                "states.",
                "Powys's map server did not respond to our request on 30 "
                "October 2026, so Powys's byways stay on rowmaps.com. (The "
                "collector server reads Powys's closures only; that is a "
                "separate pipeline.)"):
            self.assertIn(said, self.text)

    def test_no_decision_is_put_on_the_owner_for_the_roads(self):
        self.assertNotIn("the owner decided", self.text)
        self.assertNotIn("on the owner's decision of 8 October", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=1)

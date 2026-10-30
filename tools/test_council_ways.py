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
        records, ways = cw.gml_ways(body, cw._ref_wokingham_like, match)
        self.assertEqual(records, 3)
        self.assertEqual(seen, [("861", "Byway Open to All Traffic"),
                                ("862", "Byway"), ("863", "Footpath")])
        self.assertEqual([(w["id"], w["parish"], w["number"]) for w in ways],
                         [("row_lines.861", "SED", "0557")])

    def test_without_a_match_every_record_with_a_line_is_a_way(self):
        body = gml_layer("row_lines", [
            {"ogc_fid": "1", "feature": "Footpath", "prow_id": "SED1"},
            {"ogc_fid": "2", "feature": "Byway", "prow_id": "SED2"}])
        records, ways = cw.gml_ways(body, cw._ref_wokingham_like)
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
SWITCHED = ("BM", "CA", "DY", "DZ", "DU", "KL", "L1", "LD", "NK", "NY",
            "SN", "YY", "SU", "WP")

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
            self.assertIn(layer["licence"], ("OGL-3.0", "OS-OpenData"), code)
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
        # Norfolk's file itself says the OS Open Data Licence (data.gov.uk
        # says CC BY): the file is credited.
        norfolk = cw.attribution(layers["NK"])
        self.assertIn("OS OpenData Licence", norfolk)
        self.assertNotIn("Creative Commons", norfolk)
        self.assertNotIn("Open Government", norfolk)
        self.assertIn("Norfolk County Council", norfolk)
        for code in SWITCHED:
            if code != "NK":
                self.assertIn("Open Government Licence v3.0",
                              cw.attribution(layers[code]), code)
        # Surrey asks for the Ordnance Survey's credit beside the OGL.
        self.assertIn("Ordnance Survey", cw.attribution(layers["SU"]))

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
        # and nothing more: a host no layer reads is not left listed.
        self.assertEqual(set(cw.HOSTS),
                         set(host_of(l["url"]) for l in cw.LAYERS))

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
            "CA": ({"RouteCode": "07/98/2"}, ("07", "98/2")),
            "DY": ({"PARISH": "Beeley", "ROUTECODE": "NE5/13/2"},
                   ("Beeley", "13/2")),
            "DZ": ({"prow_id": "SED0557"}, ("SED", "0557")),
            "DU": ({"PARISH": "Consett", "PROW_NO__F": "19"},
                   ("Consett", "19")),
            "KL": ({"RouteCode": "HOL/180/10"}, ("HOL", "180/10")),
            "L1": ({"NUMBER_": "523004"}, ("523", "004")),
            "LD": ({"LOCAL_AR_1": "MORLEY", "PROW_ID": "146"},
                   ("MORLEY", "146")),
            "NK": ({"name": "Burston and Shimpling BOAT36"},
                   ("Burston and Shimpling", "BOAT36")),
            "NY": ({"ROUTECODE": "25.104/6/2"}, ("25.104", "6/2")),
            "SN": ({"row_number": "318"}, ("St Helens", "318")),
            "YY": ({"row": "138M"}, ("M", "138")),
            "SU": ({"LocArea": "Chelsham & Farleigh", "PROWNo": "9a"},
                   ("Chelsham & Farleigh", "9a")),
            "WP": ({"PROW_NO": "145"}, ("", "145")),
        }
        self.assertEqual(sorted(cases), sorted(SWITCHED))
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
            "NK": ("folder", ["Byway Open to All Traffic"],
                   ["Restricted Byway", "Bridleway", "Footpath"]),
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


if __name__ == "__main__":
    unittest.main(verbosity=1)

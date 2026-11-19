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


import test_council_files as tcf  # noqa: E402  (the file builders)
from polite_http import FetchFailed  # noqa: E402

#: The byway's line, in each projection a council might send it in.
LINE_BNG = [(450000.0, 170003.0), (450500.0, 170003.0)]
LINE_WGS = [grid_to_wgs84(*p) for p in LINE_BNG]


def _feature(coords, kind="LineString", props=None):
    return {"type": "Feature", "properties": props or {"REF": "R1"},
            "geometry": {"type": kind, "coordinates": coords}}


def _collection(*features, **extra):
    data = {"type": "FeatureCollection", "features": list(features)}
    data.update(extra)
    return data


class _Box(unittest.TestCase):
    """A temporary manual/ with one authority, WB."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "WB"))
        with open(os.path.join(self.root, "authorities.json"), "w") as fh:
            json.dump({"WB": {"authority": "West Berkshire"}}, fh)

    def tearDown(self):
        shutil.rmtree(self.root)

    def clear(self):
        for old in os.listdir(os.path.join(self.root, "WB")):
            os.remove(os.path.join(self.root, "WB", old))

    def put(self, name, data, meta=None, manifest_text=None):
        """`data`: bytes, text or JSON. `meta`: the file's manifest entry
        (with a layer mapping on the REF property, but for a PDF, unless it
        says otherwise)."""
        if isinstance(data, (dict, list)):
            data = json.dumps(data)
        if isinstance(data, str):
            data = data.encode("utf-8")
        with open(os.path.join(self.root, "WB", name), "wb") as fh:
            fh.write(data)
        if manifest_text is None:
            entry = {"how": "eir", "date": "2026-09-30"}
            if not name.endswith(".pdf"):
                entry["layer"] = {"id": "$REF", "vehicles": "all_users",
                                  "form": "temporary"}
            entry.update(meta or {})
            manifest_text = json.dumps({"files": {name: entry}})
        with open(os.path.join(self.root, "WB", "manifest.json"), "w",
                  encoding="utf-8") as fh:
            fh.write(manifest_text)

    def box(self):
        boxes = mi.inboxes(self.root)
        self.assertEqual(len(boxes), 1)
        return boxes[0]

    def read(self):
        """(records, candidates) from the one layer, the inbox clean."""
        self.assertEqual(self.box()["problems"], [])
        sources = cs.manual_sources(self.root)
        self.assertEqual(len(sources), 1)
        return sources[0]["read"](None)

    def refused(self, *words):
        """The one layer refused as FetchFailed (never another exception),
        the message naming the file and each of `words`."""
        sources = cs.manual_sources(self.root)
        self.assertEqual(len(sources), 1)
        with self.assertRaises(FetchFailed) as got:
            sources[0]["read"](None)
        message = str(got.exception)
        self.assertIn("manual/WB/", message)
        for w in words:
            self.assertIn(w, message)
        return message

    def matched(self):
        _n, cands = self.read()
        items, unmatched, _r = cs.match(cands, Byways([BYWAY]),
                                        "West Berkshire")
        self.assertEqual(unmatched, [])
        self.assertEqual([i["ways"] for i in items], [["WB-1"]])
        return items


class LayerFormats(_Box):
    """A council's reply is as likely a zipped shapefile, a GeoPackage or a
    KML as GeoJSON: each is read by council_files' own readers, under the
    same projection gate council_ways uses."""

    def shapefile(self, prj=tcf.BNG_PRJ, line=LINE_BNG):
        return tcf.zipped({"closures.shp": tcf.shp([[line]]),
                           "closures.shx": b"",
                           "closures.dbf": tcf.dbf([("REF", 10)],
                                                   [("R1",)]),
                           "closures.prj": prj})

    def test_a_zipped_shapefile_in_bng_is_matched(self):
        self.put("closures-2026-09-30.zip", self.shapefile())
        items = self.matched()
        self.assertTrue(items[0]["id"].endswith("|R1"), items[0]["id"])

    def test_a_zipped_shapefile_in_wgs84_is_matched(self):
        self.put("closures.zip", self.shapefile(tcf.WGS_PRJ, LINE_WGS))
        self.matched()

    def test_a_shapefile_in_a_projection_the_gate_does_not_know_is_refused(
            self):
        self.put("closures.zip", self.shapefile(tcf.MERC_PRJ))
        self.refused("projection")

    def gpkg(self, srs=27700, line=LINE_BNG):
        return tcf.gpkg([(tcf.gp(tcf.wkb_line(line)), "Closed", "R1")],
                        srs=srs)

    def test_a_geopackage_plain_or_zipped_is_matched(self):
        for name, blob in (("closures.gpkg", self.gpkg()),
                           ("closures.zip", tcf.zipped(
                               {"c/closures.gpkg": self.gpkg()})),
                           ("closures.gpkg", self.gpkg(4326, LINE_WGS))):
            with self.subTest(name=name):
                self.clear()
                self.put(name, blob, {"layer": {"id": "$PROWNo",
                                                "vehicles": "all_users"}})
                items = self.matched()
                self.assertTrue(items[0]["id"].endswith("|R1"))

    def test_a_geopackage_in_another_srs_is_refused(self):
        self.put("closures.gpkg", self.gpkg(3857))
        self.refused("SRS 3857")

    def kml(self, coords=None):
        coords = coords or " ".join("%r,%r,0" % p for p in LINE_WGS)
        return ('<?xml version="1.0" encoding="UTF-8"?><kml xmlns='
                '"http://www.opengis.net/kml/2.2"><Document><Placemark>'
                '<name>R1</name><LineString><coordinates>%s</coordinates>'
                '</LineString></Placemark></Document></kml>' % coords)

    def test_a_kml_or_kmz_is_matched(self):
        for name, blob in (("closures.kml", self.kml()),
                           ("closures.kmz", tcf.zipped(
                               {"doc.kml": self.kml()}))):
            with self.subTest(name=name):
                self.clear()
                self.put(name, blob, {"layer": {"id": "$name",
                                                "vehicles": "all_users"}})
                items = self.matched()
                self.assertTrue(items[0]["id"].endswith("|R1"))

    def test_a_kml_of_points_only_is_refused_not_left_unmatched(self):
        self.put("closures.kml", self.kml("%r,%r,0" % LINE_WGS[0]).replace(
            "LineString", "Point"), {"layer": {"id": "$name"}})
        self.refused("no lines")

    def test_a_zip_holding_no_layer_is_refused(self):
        self.put("closures.zip", tcf.zipped({"letter.pdf": b"%PDF-1.4"}))
        self.refused("zip")

    def test_a_damaged_zip_is_refused(self):
        self.put("closures.zip", self.shapefile()[:60])
        self.refused()

    def test_a_spreadsheet_is_a_loud_problem_saying_convert_it(self):
        for name in ("closures.xlsx", "closures.xls", "closures.ods"):
            with self.subTest(name=name):
                self.clear()
                self.put(name, b"PK\x03\x04")
                box = self.box()
                self.assertEqual(box["docs"], [])
                self.assertTrue(any(name in p and "convert" in p and
                                    "GeoJSON" in p for p in box["problems"]),
                                box["problems"])

    def test_a_csv_given_a_layer_mapping_is_told_to_convert(self):
        self.put("closures-2026-09-30.csv", "REF,E,N\nR1,450000,170003\n")
        box = self.box()
        self.assertEqual(box["docs"], [])
        self.assertTrue(any("closures-2026-09-30.csv" in p and "convert" in p
                            and "GeoJSON" in p for p in box["problems"]),
                        box["problems"])
        self.assertEqual(cs.manual_sources(self.root), [])

    def test_a_csv_without_a_mapping_is_still_a_document(self):
        self.put("orders-2026-09-30.csv", "Order,Date\nX,2026\n",
                 manifest_text="{}")
        box = self.box()
        self.assertEqual(box["problems"], [])
        self.assertEqual([d["kind"] for d in box["docs"]], ["document"])

    def test_a_loose_shapefile_part_is_told_to_zip_it(self):
        self.put("closures.shp", tcf.shp([[LINE_BNG]]))
        box = self.box()
        self.assertEqual(box["docs"], [])
        self.assertTrue(any("closures.shp" in p and ".zip" in p
                            for p in box["problems"]), box["problems"])


class MalformedLayer(_Box):
    """One bad file is a FetchFailed for that source alone: never a
    TypeError that stops every council's run, never a NaN drawn."""

    def test_each_malformed_feature_is_refused_as_fetchfailed(self):
        good = list(LINE_BNG[1])
        cases = {
            "a null coordinate": _feature([[None, 170003.0], good]),
            "an empty-string coordinate": _feature([["", 170003.0], good]),
            "a coordinate as text": _feature([["450000", "170003"], good]),
            "a NaN": _feature([[float("nan"), 170003.0], good]),
            "an infinity": _feature([[float("inf"), 170003.0], good]),
            "a true for a number": _feature([[True, 170003.0], good]),
            "a one-number point": _feature([[450000.0], good]),
            "a point as a number": _feature([450000.0, good]),
            "null coordinates": _feature(None),
            "properties as a list": dict(_feature([list(LINE_BNG[0]), good]),
                                         properties=["x"]),
        }
        for why, feature in cases.items():
            with self.subTest(why=why):
                self.put("closures.geojson", _collection(feature))
                self.refused()
        for why, data in (("a null feature", _collection(None)),
                          ("a feature as text", _collection("x")),
                          ("features not a list",
                           {"type": "FeatureCollection", "features": {}}),
                          ("not JSON", "{"), ("a list", [1])):
            with self.subTest(why=why):
                self.put("closures.geojson", data)
                self.refused()

    def test_a_malformed_reply_keeps_the_last_good_copy(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)
        self.put("closures.geojson", _collection(_feature(
            [list(p) for p in LINE_BNG])))
        source = cs.manual_sources(self.root)[0]
        byways = Byways([BYWAY])
        first = cs.fetch_one(source, None, byways, out, "2026-11-18")
        self.assertTrue(first["ok"], first)
        path = os.path.join(out, "%s.json" % source["id"])
        with open(path, "rb") as fh:
            before = fh.read()
        self.put("closures.geojson", _collection(_feature(
            [[None, 170003.0], list(LINE_BNG[1])])))
        source = cs.manual_sources(self.root)[0]
        entry = cs.fetch_one(source, None, byways, out, "2026-11-19")
        self.assertFalse(entry["ok"])
        self.assertIn("manual/WB/closures.geojson", entry["error"])
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)


class LayerProjection(_Box):
    """Coordinates are taken only inside England and Wales, in British
    National Grid or in longitude and latitude; anything else is said, not
    read as nothing."""

    def one(self, coords, kind="LineString", meta=None, **extra):
        self.put("closures.geojson", _collection(_feature(coords, kind),
                                                 **extra), meta)

    def test_web_mercator_is_refused(self):
        self.one([[-145000.0, 6705000.0], [-144500.0, 6705000.0]])
        self.refused("England and Wales")

    def test_swapped_axes_are_refused(self):
        self.one([[lat, lon] for lon, lat in LINE_WGS])
        self.refused("England and Wales")

    def test_bng_outside_england_and_wales_is_refused(self):
        for coords in ([[450000.0, 970000.0], [450500.0, 970000.0]],
                       [[900000.0, 170000.0], [900500.0, 170000.0]],
                       [[-5000.0, 170000.0], [-4500.0, 170000.0]]):
            with self.subTest(coords=coords):
                self.one(coords)
                self.refused("England and Wales")

    def test_a_layer_mixing_projections_is_refused(self):
        self.put("closures.geojson", _collection(
            _feature([list(p) for p in LINE_BNG]),
            _feature([list(p) for p in LINE_WGS])))
        self.refused("England and Wales")

    def test_polygons_and_points_are_refused_not_left_unmatched(self):
        ring = [list(p) for p in LINE_BNG] + [[450500.0, 170500.0],
                                              list(LINE_BNG[0])]
        for kind, coords in (("Polygon", [ring]),
                             ("MultiPolygon", [[ring]]),
                             ("Point", list(LINE_BNG[0])),
                             ("MultiPoint", [list(p) for p in LINE_BNG])):
            with self.subTest(kind=kind):
                self.one(coords, kind)
                self.refused(kind, "lines")

    def test_a_layer_with_no_line_at_all_is_refused(self):
        self.put("closures.geojson", _collection(
            {"type": "Feature", "properties": {"REF": "R1"},
             "geometry": None}))
        self.refused("no lines")

    def test_both_projections_inside_the_bounds_are_read(self):
        for coords in ([list(p) for p in LINE_BNG],
                       [list(p) for p in LINE_WGS],
                       [list(p) + [12.5] for p in LINE_BNG]):
            with self.subTest(coords=coords[0]):
                self.one(coords)
                self.matched()

    def test_a_multilinestring_is_read(self):
        self.one([[list(p) for p in LINE_BNG]], "MultiLineString")
        self.matched()

    def test_a_declared_crs_that_matches_is_read(self):
        for crs, coords in (("urn:ogc:def:crs:EPSG::27700", LINE_BNG),
                            ("EPSG:27700", LINE_BNG),
                            ("urn:ogc:def:crs:OGC:1.3:CRS84", LINE_WGS),
                            ("EPSG:4326", LINE_WGS)):
            with self.subTest(crs=crs):
                self.one([list(p) for p in coords],
                         crs={"type": "name", "properties": {"name": crs}})
                self.matched()

    def test_a_declared_crs_the_reader_does_not_know_is_refused(self):
        self.one([list(p) for p in LINE_BNG],
                 crs={"type": "name",
                      "properties": {"name": "urn:ogc:def:crs:EPSG::3857"}})
        self.refused("3857")

    def test_a_declared_crs_that_the_coordinates_contradict_is_refused(self):
        # Calderdale's layer says EPSG:27700 over coordinates in degrees.
        self.one([list(p) for p in LINE_WGS],
                 crs={"type": "name",
                      "properties": {"name": "EPSG:27700"}})
        self.refused("England and Wales")

    def test_the_manifest_crs_overrules_the_files_own(self):
        for crs in ("wgs84", "WGS84 "):
            with self.subTest(crs=crs):
                self.one([list(p) for p in LINE_WGS], meta={"crs": crs},
                         crs={"type": "name",
                              "properties": {"name": "EPSG:27700"}})
                self.matched()

    def test_a_manifest_crs_the_coordinates_contradict_is_refused(self):
        self.one([list(p) for p in LINE_WGS], meta={"crs": "bng"})
        self.refused("England and Wales")

    def test_a_manifest_crs_must_be_bng_or_wgs84(self):
        for crs in ("EPSG:3857", 27700, ""):
            with self.subTest(crs=crs):
                self.one([list(p) for p in LINE_BNG], meta={"crs": crs})
                box = self.box()
                self.assertEqual(box["docs"], [])
                self.assertTrue(any("crs" in p for p in box["problems"]),
                                box["problems"])


class Manifest(_Box):
    """What the manifest says is checked, never silently replaced by a
    default."""

    def test_an_unreadable_manifest_is_a_loud_problem(self):
        for text in ("{bad", "[1, 2]", '"text"', '{"files": [1]}',
                     '{"files": null}'):
            with self.subTest(text=text):
                # A dated PDF beside it, which would otherwise be read and
                # credited "saved by hand".
                with open(os.path.join(self.root, "WB",
                                       "orders-2026-09-30.pdf"), "w") as fh:
                    fh.write("%PDF-1.4")
                self.put("closures-2026-09-30.geojson", CLOSURES,
                         manifest_text=text)
                self.assertEqual(mi.documents_by_id(self.root), {})
                self.assertEqual(cs.manual_sources(self.root), [])
                box = self.box()
                self.assertTrue(any("manifest.json unreadable" in p
                                    for p in box["problems"]),
                                box["problems"])
                self.assertEqual(box["docs"], [],
                                 "a file was credited without its manifest")
                self.assertEqual(mi.automated_off(self.root),
                                 {"West Berkshire"})

    def test_no_manifest_at_all_is_not_a_problem(self):
        self.put("x-2026-09-30.pdf", "%PDF", manifest_text="{}")
        os.remove(os.path.join(self.root, "WB", "manifest.json"))
        box = self.box()
        self.assertEqual(box["problems"], [])
        self.assertEqual(box["docs"][0]["how"], "saved by hand")

    def test_a_files_entry_that_is_not_an_object_is_a_problem(self):
        self.put("x-2026-09-30.pdf", "%PDF",
                 manifest_text='{"files": {"x-2026-09-30.pdf": "eir"}}')
        box = self.box()
        self.assertEqual(box["docs"], [])
        self.assertTrue(any("x-2026-09-30.pdf" in p for p in
                            box["problems"]), box["problems"])

    def test_a_layer_mapping_that_is_not_an_object_is_a_problem(self):
        self.put("closures.geojson", CLOSURES, {"layer": "$REF"})
        box = self.box()
        self.assertEqual(box["docs"], [])
        self.assertTrue(any("layer" in p for p in box["problems"]),
                        box["problems"])

    def test_how_eir_and_foi_are_supplied_by_the_council_in_any_case(self):
        for how in ("eir", "EIR", " eir ", "foi", "FOI", "Foi\n",
                    "council"):
            with self.subTest(how=how):
                self.put("x-2026-09-30.pdf", "%PDF", {"how": how})
                box = self.box()
                self.assertEqual(box["problems"], [])
                self.assertEqual(box["docs"][0]["how"],
                                 "supplied by the council")

    def test_how_hand_or_none_is_saved_by_hand(self):
        for meta in ({"how": "hand"}, {"how": "Browser"}, {}):
            with self.subTest(meta=meta):
                self.put("x-2026-09-30.pdf", "%PDF",
                         manifest_text=json.dumps({"files": {
                             "x-2026-09-30.pdf": meta}}))
                box = self.box()
                self.assertEqual(box["problems"], [])
                self.assertEqual(box["docs"][0]["how"], "saved by hand")

    def test_an_unknown_how_is_refused_loudly(self):
        for how in ("fio", "email", "", 3, None, ["eir"]):
            with self.subTest(how=how):
                self.put("x-2026-09-30.pdf", "%PDF", {"how": how})
                box = self.box()
                self.assertEqual(box["docs"], [])
                self.assertTrue(any("x-2026-09-30.pdf" in p and "how" in p
                                    for p in box["problems"]),
                                box["problems"])

    def test_a_date_must_be_iso(self):
        for date in ("30/09/2026", "2026-9-30", "2026-02-30", "20260930",
                     20260930, "2026-09-30T10:00", " 2026-09-30", "",
                     None):
            with self.subTest(date=date):
                self.put("x.pdf", "%PDF", {"date": date})
                box = self.box()
                self.assertEqual(box["docs"], [])
                self.assertTrue(any("x.pdf" in p and "date" in p
                                    for p in box["problems"]),
                                box["problems"])
        self.put("x.pdf", "%PDF", {"date": "2026-09-30"})
        box = self.box()
        self.assertEqual(box["problems"], [])
        self.assertEqual(box["docs"][0]["date"], "2026-09-30")

    def test_an_impossible_date_in_the_file_name_is_no_date(self):
        self.put("x-2026-13-45.pdf", "%PDF", manifest_text="{}")
        box = self.box()
        self.assertTrue(any("no date" in p for p in box["problems"]),
                        box["problems"])
        self.assertIsNone(box["docs"][0]["date"])


class SameStem(_Box):
    """Two layers whose names differ only by extension (or by what the id
    drops) would be one source id, the second overwriting the first: both
    are a problem, and neither is read."""

    def put_two(self, first, second):
        layer = {"how": "eir", "date": "2026-09-30",
                 "layer": {"id": "$REF", "vehicles": "all_users"}}
        for name in (first, second):
            with open(os.path.join(self.root, "WB", name), "w") as fh:
                fh.write(json.dumps(CLOSURES))
        with open(os.path.join(self.root, "WB", "manifest.json"), "w") as fh:
            json.dump({"files": {first: layer, second: layer}}, fh)

    def test_a_same_stem_pair_is_a_problem_and_neither_is_read(self):
        for pair in (("closures.zip", "closures.kml"),
                     ("closures.geojson", "closures.json"),
                     ("Closures.geojson", "closures.kmz"),
                     ("road closures.geojson", "road-closures.json")):
            with self.subTest(pair=pair):
                self.clear()
                self.put_two(*pair)
                box = self.box()
                self.assertEqual(box["docs"], [])
                self.assertEqual(cs.manual_sources(self.root), [])
                for name in pair:
                    self.assertTrue(any(name in p and "same" in p
                                        for p in box["problems"]),
                                    (name, box["problems"]))

    def test_a_document_beside_a_layer_of_its_stem_is_no_clash(self):
        self.put("closures.geojson", CLOSURES)
        with open(os.path.join(self.root, "WB", "closures.pdf"), "w") as fh:
            fh.write("%PDF")
        manifest = os.path.join(self.root, "WB", "manifest.json")
        with open(manifest) as fh:
            data = json.load(fh)
        data["files"]["closures.pdf"] = {"how": "eir", "date": "2026-09-30"}
        with open(manifest, "w") as fh:
            json.dump(data, fh)
        box = self.box()
        self.assertEqual(box["problems"], [])
        self.assertEqual(len(box["docs"]), 2)

    def test_a_single_layers_id_is_unchanged(self):
        self.put("Road Closures 2026-09-30.geojson", CLOSURES)
        self.assertEqual([s["id"] for s in cs.manual_sources(self.root)],
                         ["manual-wb-road-closures-2026-09-30"])


#: Just inside and just outside each edge of England and Wales, in each
#: projection: (edge, inside point, outside point).
BNG_EDGES = (("west", (60001.0, 170000.0), (59999.0, 170000.0)),
             ("east", (669999.0, 170000.0), (670001.0, 170000.0)),
             ("south", (450000.0, 1.0), (450000.0, -1.0)),
             ("north", (450000.0, 669999.0), (450000.0, 670001.0)))
WGS_EDGES = (("west", (-6.4999, 52.0), (-6.5001, 52.0)),
             ("east", (1.7999, 52.0), (1.8001, 52.0)),
             ("south", (-1.0, 49.8001), (-1.0, 49.7999)),
             ("north", (-1.0, 55.8999), (-1.0, 55.9001)))


class Bounds(_Box):
    """The edges of England and Wales are where they are said to be, for
    GeoJSON in both projections and for a council_files format (KML)."""

    def geojson(self, probe, near):
        self.clear()
        self.put("closures.geojson", _collection(_feature(
            [list(near), list(probe)])))
        return cs.manual_sources(self.root)[0]["read"]

    def kml(self, probe, near):
        self.clear()
        coords = " ".join("%r,%r,0" % p for p in (near, probe))
        self.put("closures.kml", LayerFormats.kml(None, coords),
                 {"layer": {"id": "$name"}})
        return cs.manual_sources(self.root)[0]["read"]

    def check(self, make, edges, near):
        for edge, inside, outside in edges:
            with self.subTest(edge=edge, side="inside"):
                records, _c = make(inside, near)(None)
                self.assertEqual(records, 1)
            with self.subTest(edge=edge, side="outside"):
                with self.assertRaises(FetchFailed) as got:
                    make(outside, near)(None)
                self.assertIn("England and Wales", str(got.exception))

    def test_bng_edges_in_geojson(self):
        self.check(self.geojson, BNG_EDGES, (450000.0, 170000.0))

    def test_wgs84_edges_in_geojson(self):
        self.check(self.geojson, WGS_EDGES, (-1.0, 52.0))

    def test_wgs84_edges_in_kml(self):
        self.check(self.kml, WGS_EDGES, (-1.0, 52.0))


class NullCrs(_Box):
    """`"crs": null`, in the manifest or in the file, is no crs stated."""

    def test_a_null_crs_in_the_manifest_is_none(self):
        self.put("closures.geojson", _collection(_feature(
            [list(p) for p in LINE_BNG])), {"crs": None})
        self.matched()

    def test_a_null_crs_in_the_file_is_none(self):
        self.put("closures.geojson", _collection(_feature(
            [list(p) for p in LINE_WGS]), crs=None))
        self.matched()


class TheRealFolder(unittest.TestCase):
    def test_every_code_names_an_authority_and_the_readme_exists(self):
        table = mi.authority_table()
        self.assertGreaterEqual(len(table), 140)
        for code in ("CB", "HD", "IW", "WB", "PW", "DY", "DU"):
            self.assertIn(code, table)
        self.assertTrue(os.path.exists(os.path.join(ROOT, "manual",
                                                    "README.md")))

    def test_the_readme_names_every_format_and_how_the_code_takes(self):
        with open(os.path.join(ROOT, "manual", "README.md"),
                  encoding="utf-8") as fh:
            readme = fh.read()
        for word in (list(mi.LAYERS) + sorted(mi.HOW) + list(mi.CRS) +
                     [".xlsx", ".xls", ".ods", ".shp"]):
            self.assertIn("`%s`" % word, readme, word)
        # Where a problem is said, and which requests a reply answers.
        flat = " ".join(readme.split())
        for words in ("Freedom of Information (FOI)",
                      "Problems are listed on every council orders run"):
            self.assertIn(words, flat)

    def test_the_readme_starts_with_steps_and_redacts_every_reply(self):
        with open(os.path.join(ROOT, "manual", "README.md"),
                  encoding="utf-8") as fh:
            readme = fh.read()
        flat = " ".join(readme.split())
        self.assertLess(readme.index("## Steps"),
                        readme.index("## Where each file goes"))
        steps = readme[readme.index("## Steps"):
                       readme.index("## Where each file goes")]
        for n in range(1, 6):
            self.assertIn("\n%d. " % n, steps)
        for words in ("manual/authorities.json", "pull request", "main",
                      "The checks do not look for personal data",
                      "step 2 is still yours"):
            self.assertIn(words, " ".join(steps.split()))
        # This repository is public: the rule applies to every file, so it
        # is a bullet of its own under "What to drop".
        drop = readme[readme.index("## What to drop"):
                      readme.index("## manifest.json")]
        self.assertIn("- **Before you commit a council's reply, remove the "
                      "requester's name, address and email from it.**",
                      " ".join(drop.split()))
        for words in ("**What happens to a document.** The next order "
                      "register check (`order-register.yml`",
                      "python tools/order_register.py accept",
                      "do not respond to requests from GitHub's servers",
                      "the coordinate system (crs)",
                      "A value starting with `$`", "`council`:", "`title`:"):
            self.assertIn(words, flat)
        self.assertNotIn("refuse GitHub's servers", flat)

    def test_the_tools_own_account_names_the_formats_and_the_problems(self):
        flat = " ".join(mi.__doc__.split())
        for words in ("a zipped shapefile, a GeoPackage, KML or KMZ",
                      "warned on every council-orders run"):
            self.assertIn(words, flat)


if __name__ == "__main__":
    unittest.main(verbosity=1)

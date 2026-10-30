#!/usr/bin/env python3
"""The council file readers: zipped shapefiles, GeoPackages, KML/KMZ and
GeoJSON, read into (attributes, WGS84 lines) - or refused, never guessed.

    python tools/test_council_files.py

No network. Every file is built here, byte by byte, from known values.
"""
import io
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_files as cf  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402
from polite_http import FetchFailed  # noqa: E402

BNG_PRJ = ('PROJCS["British_National_Grid",GEOGCS["GCS_OSGB_1936",'
           'DATUM["D_OSGB_1936",SPHEROID["Airy_1830",6377563.396,'
           '299.3249646]],PRIMEM["Greenwich",0.0],UNIT["Degree",'
           '0.0174532925199433]],PROJECTION["Transverse_Mercator"]]')
WGS_PRJ = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID['
           '"WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],'
           'UNIT["Degree",0.0174532925199433]]')
MERC_PRJ = ('PROJCS["WGS_1984_Web_Mercator_Auxiliary_Sphere",GEOGCS['
            '"GCS_WGS_1984"]]')

A = [(430000.0, 380000.0), (430100.0, 380050.0), (430200.0, 380060.0)]
B1 = [(431000.0, 381000.0), (431050.0, 381010.0)]
B2 = [(431200.0, 381100.0), (431260.0, 381150.0), (431300.0, 381190.0)]


def wgs(points):
    return [tuple(round(v, 6) for v in grid_to_wgs84(*p)) for p in points]


# ------------------------------------------------------------- shapefiles

def shp(records, kind=3, z=False):
    """A .shp of polyline records; each record a list of parts or None."""
    body = b""
    for i, parts in enumerate(records, 1):
        if parts is None:
            rec = struct.pack("<i", 0)
        else:
            pts = [p for part in parts for p in part]
            starts, at = [], 0
            for part in parts:
                starts.append(at)
                at += len(part)
            rec = struct.pack("<i4d2i", kind, 0, 0, 0, 0, len(parts),
                              len(pts))
            rec += struct.pack("<%di" % len(parts), *starts)
            rec += b"".join(struct.pack("<2d", *p) for p in pts)
            if z:
                rec += struct.pack("<2d", 0, 0) + b"\0" * 8 * len(pts)
        body += struct.pack(">2i", i, len(rec) // 2) + rec
    header = struct.pack(">i5ii", 9994, 0, 0, 0, 0, 0, (100 + len(body)) // 2)
    header += struct.pack("<2i8d", 1000, kind, 0, 0, 0, 0, 0, 0, 0, 0)
    return header + body


def dbf(fields, rows, deleted=(), encoding="utf-8"):
    """A .dbf of character fields; `deleted` lists row numbers marked
    deleted."""
    widths = [w for _n, w in fields]
    header_len = 32 + 32 * len(fields) + 1
    size = 1 + sum(widths)
    out = struct.pack("<4BIHH20x", 3, 126, 10, 30, len(rows), header_len,
                      size)
    for name, width in fields:
        out += name.encode("ascii").ljust(11, b"\0") + b"C" + b"\0" * 4 \
            + bytes([width, 0]) + b"\0" * 14
    out += b"\r"
    for i, row in enumerate(rows):
        out += b"*" if i in deleted else b" "
        for (name, width), value in zip(fields, row):
            out += value.encode(encoding).ljust(width, b" ")[:width]
    return out + b"\x1a"


def zipped(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


FIELDS = [("STATUS", 12), ("REF", 10)]


class Shapefile(unittest.TestCase):
    def test_lines_attributes_and_parts_are_read_in_bng(self):
        blob = zipped({
            "row/ROW.shp": shp([[A], [B1, B2]]),
            "row/ROW.dbf": dbf(FIELDS, [("BOAT", "ACL/1/1"),
                                        ("Footpath", "Bürs/2")]),
            "row/ROW.prj": BNG_PRJ, "row/ROW.cpg": "UTF-8"})
        got = cf.shapefile_records(blob)
        self.assertEqual([a for a, _l in got],
                         [{"STATUS": "BOAT", "REF": "ACL/1/1"},
                          {"STATUS": "Footpath", "REF": "Bürs/2"}])
        self.assertEqual(got[0][1], [wgs(A)])
        self.assertEqual(got[1][1], [wgs(B1), wgs(B2)])

    def test_without_a_cpg_the_text_is_latin_1(self):
        blob = zipped({"R.shp": shp([[A]]),
                       "R.dbf": dbf(FIELDS, [("BOAT", "Bürs")],
                                    encoding="latin-1"),
                       "R.prj": BNG_PRJ})
        self.assertEqual(cf.shapefile_records(blob)[0][0]["REF"], "Bürs")

    def test_polyline_z_is_read_as_its_x_and_y(self):
        blob = zipped({"R.shp": shp([[A]], kind=13, z=True),
                       "R.dbf": dbf(FIELDS, [("BOAT", "1")]),
                       "R.prj": BNG_PRJ})
        self.assertEqual(cf.shapefile_records(blob)[0][1], [wgs(A)])

    def test_a_deleted_record_is_skipped_and_the_rest_stay_aligned(self):
        blob = zipped({"R.shp": shp([[A], [B1], [B2]]),
                       "R.dbf": dbf(FIELDS, [("BOAT", "1"), ("BOAT", "2"),
                                             ("BOAT", "3")], deleted=(1,)),
                       "R.prj": BNG_PRJ})
        got = cf.shapefile_records(blob)
        self.assertEqual([(a["REF"], l) for a, l in got],
                         [("1", [wgs(A)]), ("3", [wgs(B2)])])

    def test_a_null_shape_has_no_lines(self):
        blob = zipped({"R.shp": shp([None, [A]]),
                       "R.dbf": dbf(FIELDS, [("BOAT", "1"), ("BOAT", "2")]),
                       "R.prj": BNG_PRJ})
        self.assertEqual([l for _a, l in cf.shapefile_records(blob)],
                         [[], [wgs(A)]])

    def test_a_wgs84_file_is_taken_as_longitude_and_latitude(self):
        pts = [(-1.5, 53.25), (-1.4999, 53.2501)]
        blob = zipped({"R.shp": shp([[pts]]),
                       "R.dbf": dbf(FIELDS, [("BOAT", "1")]),
                       "R.prj": WGS_PRJ})
        self.assertEqual(cf.shapefile_records(blob)[0][1], [pts])

    def test_a_projection_it_cannot_name_is_refused(self):
        for prj in (MERC_PRJ, "", "nonsense"):
            blob = zipped({"R.shp": shp([[A]]),
                           "R.dbf": dbf(FIELDS, [("BOAT", "1")]),
                           "R.prj": prj})
            with self.assertRaises(FetchFailed, msg=prj):
                cf.shapefile_records(blob)

    def test_points_and_mismatched_files_are_refused(self):
        cases = {
            "points": {"R.shp": shp([[A]], kind=1),
                       "R.dbf": dbf(FIELDS, [("BOAT", "1")]),
                       "R.prj": BNG_PRJ},
            "two shapes, one row": {"R.shp": shp([[A], [B1]]),
                                    "R.dbf": dbf(FIELDS, [("BOAT", "1")]),
                                    "R.prj": BNG_PRJ},
            "no prj": {"R.shp": shp([[A]]),
                       "R.dbf": dbf(FIELDS, [("BOAT", "1")])},
            "two shapefiles": {"R.shp": shp([[A]]), "S.shp": shp([[A]]),
                               "R.dbf": dbf(FIELDS, [("BOAT", "1")]),
                               "R.prj": BNG_PRJ},
        }
        for why, files in cases.items():
            with self.assertRaises(FetchFailed, msg=why):
                cf.shapefile_records(zipped(files))
        with self.assertRaises(FetchFailed):
            cf.shapefile_records(b"<html>not a zip</html>")

    def test_a_named_shapefile_is_picked_from_several(self):
        blob = zipped({"R.shp": shp([[A]]), "S.shp": shp([[B1]]),
                       "S.dbf": dbf(FIELDS, [("BOAT", "s")]),
                       "S.prj": BNG_PRJ})
        self.assertEqual(cf.shapefile_records(blob, stem="s")[0][1],
                         [wgs(B1)])


# ------------------------------------------------------------- GeoPackage

def wkb_line(points, z=False, order="<"):
    flag = 1 if order == "<" else 0
    kind = 1002 if z else 2
    out = bytes([flag]) + struct.pack(order + "I", kind) + \
        struct.pack(order + "I", len(points))
    for p in points:
        out += struct.pack(order + ("3d" if z else "2d"),
                           *(tuple(p) + ((7.0,) if z else ())))
    return out


def wkb_multi(lines, z=False):
    out = struct.pack("<BI", 1, 1005 if z else 5) + \
        struct.pack("<I", len(lines))
    return out + b"".join(wkb_line(l, z) for l in lines)


def gp(wkb, envelope=1, empty=False):
    flags = 0x01 | (envelope << 1) | (0x10 if empty else 0)
    env = b"\0" * (0, 32, 48, 48, 64)[envelope]
    return b"GP" + bytes([0, flags]) + struct.pack("<i", 27700) + env + wkb


def gpkg(rows, srs=27700, tables=1):
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    os.close(fd)
    os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, "
                 "column_name TEXT, geometry_type_name TEXT, srs_id INT, "
                 "z INT, m INT)")
    for t in range(tables):
        name = "row" if t == 0 else "other%d" % t
        conn.execute('CREATE TABLE "%s" (fid INTEGER PRIMARY KEY, geom BLOB, '
                     'Stat_Text TEXT, PROWNo TEXT)' % name)
        conn.execute("INSERT INTO gpkg_geometry_columns VALUES "
                     "(?, 'geom', 'MULTILINESTRING', ?, 0, 0)", (name, srs))
        for i, (geom, stat, no) in enumerate(rows, 1):
            conn.execute('INSERT INTO "%s" VALUES (?, ?, ?, ?)' % name,
                         (i, geom, stat, no))
    conn.commit()
    conn.close()
    with open(path, "rb") as fh:
        data = fh.read()
    os.remove(path)
    return data


class GeoPackage(unittest.TestCase):
    def test_multi_lines_with_an_envelope_and_z_are_read(self):
        data = gpkg([(gp(wkb_multi([A, B1], z=True)), "Byway", "7"),
                     (gp(wkb_line(B2)), "Footpath", "8")])
        got = cf.gpkg_records(data)
        self.assertEqual(got[0][0], {"fid": "1", "Stat_Text": "Byway",
                                     "PROWNo": "7"})
        self.assertEqual(got[0][1], [wgs(A), wgs(B1)])
        self.assertEqual(got[1][1], [wgs(B2)])

    def test_each_envelope_size_and_byte_order_is_read(self):
        for envelope in (0, 1, 2, 3, 4):
            data = gpkg([(gp(wkb_line(B2), envelope=envelope), "Byway",
                          "1")])
            self.assertEqual(cf.gpkg_records(data)[0][1], [wgs(B2)],
                             envelope)
        data = gpkg([(gp(wkb_line(B2, order=">")), "Byway", "1")])
        self.assertEqual(cf.gpkg_records(data)[0][1], [wgs(B2)])

    def test_zipped_and_empty_and_null_geometries(self):
        data = gpkg([(gp(b"", empty=True), "Byway", "1"),
                     (None, "Byway", "2"),
                     (gp(wkb_line(A)), "Byway", "3")])
        got = cf.gpkg_records(zipped({"RoW.gpkg": data, "notes.pdf": b"%"}))
        self.assertEqual([l for _a, l in got], [[], [], [wgs(A)]])

    def test_an_unknown_srs_or_two_tables_or_a_polygon_is_refused(self):
        with self.assertRaises(FetchFailed):
            cf.gpkg_records(gpkg([(gp(wkb_line(A)), "Byway", "1")],
                                 srs=3857))
        with self.assertRaises(FetchFailed):
            cf.gpkg_records(gpkg([(gp(wkb_line(A)), "Byway", "1")],
                                 tables=2))
        polygon = struct.pack("<BII", 1, 3, 0)
        with self.assertRaises(FetchFailed):
            cf.gpkg_records(gpkg([(gp(polygon), "Byway", "1")]))
        with self.assertRaises(FetchFailed):
            cf.gpkg_records(b"SQLite format 3\0 but not really")

    def test_a_wgs84_package_is_taken_as_longitude_and_latitude(self):
        pts = [(-0.5, 51.25), (-0.4999, 51.2501)]
        data = gpkg([(gp(wkb_line(pts)), "Byway", "1")], srs=4326)
        self.assertEqual(cf.gpkg_records(data)[0][1], [pts])


# -------------------------------------------------------------------- KML

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
 <name>Norfolk Rights of Way</name>
 <Folder><name>Bridleway</name>
  <Placemark><name>Stiffkey BR16</name>
   <LineString><coordinates>0.9235,52.9567,0 0.9236,52.9568,0
   </coordinates></LineString></Placemark>
 </Folder>
 <Folder><name>Byway Open to All Traffic</name>
  <Placemark><name>Roudham BOAT18</name>
   <ExtendedData><SchemaData><SimpleData name="NE_UNIQUE">1R/18</SimpleData>
   </SchemaData><Data name="WIDTH"><value>4</value></Data></ExtendedData>
   <MultiGeometry>
    <LineString><coordinates>0.80,52.45 0.81,52.46</coordinates></LineString>
    <LineString><coordinates>0.82,52.47 0.83,52.48</coordinates></LineString>
   </MultiGeometry></Placemark>
  <Folder><name>Inner</name>
   <Placemark><name>Inner BOAT1</name><LineString><coordinates>
    1.0,52.0 1.001,52.001</coordinates></LineString></Placemark>
  </Folder>
  <Placemark><name>After BOAT2</name><LineString><coordinates>
   1.1,52.1 1.101,52.101</coordinates></LineString></Placemark>
 </Folder>
</Document></kml>"""


class Kml(unittest.TestCase):
    def test_placemarks_carry_their_folder_name_and_data(self):
        got = cf.kml_records(KML.encode("utf-8"))
        self.assertEqual([(a["folder"], a["name"]) for a, _l in got],
                         [("Bridleway", "Stiffkey BR16"),
                          ("Byway Open to All Traffic", "Roudham BOAT18"),
                          ("Inner", "Inner BOAT1"),
                          ("Byway Open to All Traffic", "After BOAT2")])
        self.assertEqual(got[1][0]["NE_UNIQUE"], "1R/18")
        self.assertEqual(got[1][0]["WIDTH"], "4")
        self.assertEqual(got[0][1], [[(0.9235, 52.9567), (0.9236, 52.9568)]])
        self.assertEqual(got[1][1], [[(0.8, 52.45), (0.81, 52.46)],
                                     [(0.82, 52.47), (0.83, 52.48)]])

    def test_a_kmz_is_unzipped_and_bad_xml_refused(self):
        got = cf.kml_records(zipped({"a/b/rights.kml": KML}))
        self.assertEqual(len(got), 4)
        with self.assertRaises(FetchFailed):
            cf.kml_records(b"<kml><Placemark>")
        with self.assertRaises(FetchFailed):
            cf.kml_records(zipped({"a.kml": KML, "b.kml": KML}))


# ---------------------------------------------------------------- GeoJSON

class GeoJson(unittest.TestCase):
    def test_the_layers_stated_projection_is_used_not_the_files_crs(self):
        pts = [[-2.07, 53.79], [-2.0701, 53.7901]]
        doc = {"type": "FeatureCollection",
               "crs": {"type": "name", "properties": {"name": "EPSG:27700"}},
               "features": [{"type": "Feature",
                             "properties": {"StatusDesc": "BOAT", "n": None},
                             "geometry": {"type": "LineString",
                                          "coordinates": pts}}]}
        blob = json.dumps(doc).encode("utf-8")
        got = cf.geojson_records(blob, "wgs84")
        self.assertEqual(got, [({"StatusDesc": "BOAT", "n": ""},
                                [[tuple(p) for p in pts]])])
        # Read as the grid it says it is, degrees are no place in Britain.
        self.assertEqual(cf.geojson_records(blob, "bng")[0][1], [])

    def test_grid_multilines_are_converted(self):
        doc = {"features": [{"properties": {}, "geometry": {
            "type": "MultiLineString", "coordinates": [A, B1]}},
            {"properties": {}, "geometry": None}]}
        got = cf.geojson_records(json.dumps(doc).encode(), "bng")
        self.assertEqual([l for _a, l in got], [[wgs(A), wgs(B1)], []])

    def test_bad_input_is_refused(self):
        with self.assertRaises(FetchFailed):
            cf.geojson_records(b"{}", "bng")
        with self.assertRaises(FetchFailed):
            cf.geojson_records(b"not json", "bng")
        with self.assertRaises(FetchFailed):
            cf.geojson_records(b'{"features": []}', "mercator")


if __name__ == "__main__":
    unittest.main(verbosity=1)

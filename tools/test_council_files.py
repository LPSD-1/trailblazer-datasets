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
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_files as cf  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402
from polite_http import FetchFailed  # noqa: E402

#: Every council shapefile read on 30 October 2026 carried this .prj, as
#: ArcGIS writes it.
BNG_PRJ = ('PROJCS["British_National_Grid",GEOGCS["GCS_OSGB_1936",'
           'DATUM["D_OSGB_1936",SPHEROID["Airy_1830",6377563.396,'
           '299.3249646]],PRIMEM["Greenwich",0.0],UNIT["Degree",'
           '0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
           'PARAMETER["False_Easting",400000.0],'
           'PARAMETER["False_Northing",-100000.0],'
           'PARAMETER["Central_Meridian",-2.0],'
           'PARAMETER["Scale_Factor",0.9996012717],'
           'PARAMETER["Latitude_Of_Origin",49.0],UNIT["Meter",1.0]]')
#: The same grid as EPSG's WKT 1 spells it (GDAL, QGIS).
EPSG_PARAMS = ('PROJECTION["Transverse_Mercator"],'
               'PARAMETER["latitude_of_origin",49],'
               'PARAMETER["central_meridian",-2],'
               'PARAMETER["scale_factor",0.9996012717],'
               'PARAMETER["false_easting",400000],'
               'PARAMETER["false_northing",-100000],'
               'UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
               'AXIS["Easting",EAST],AXIS["Northing",NORTH]')
EPSG_GEOGCS = ('GEOGCS["OSGB 1936",DATUM["OSGB_1936",SPHEROID["Airy 1830",'
               '6377563.396,299.3249646,AUTHORITY["EPSG","7001"]],'
               'AUTHORITY["EPSG","6277"]],PRIMEM["Greenwich",0,'
               'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,'
               'AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4277"]]')
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


def gpkg(rows, srs=27700, tables=1, last_change=None):
    """A GeoPackage of `tables` feature tables; with `last_change`, its
    gpkg_contents dates each table so (a dict: table -> date)."""
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    os.close(fd)
    os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, "
                 "column_name TEXT, geometry_type_name TEXT, srs_id INT, "
                 "z INT, m INT)")
    if last_change is not None:
        conn.execute("CREATE TABLE gpkg_contents (table_name TEXT, "
                     "data_type TEXT, last_change TEXT)")
        for table, when in last_change.items():
            conn.execute("INSERT INTO gpkg_contents VALUES (?, 'features', "
                         "?)", (table, when))
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


# ------------------------------------------------------------ projections

def epsg_bng(name, geogcs=EPSG_GEOGCS, params=EPSG_PARAMS,
             authority='AUTHORITY["EPSG","27700"]'):
    tail = "," + authority if authority else ""
    return 'PROJCS["%s",%s,%s%s]' % (name, geogcs, params, tail)


class Projections(unittest.TestCase):
    """Only the British National Grid itself is read as grid: its name or
    its EPSG code, on the OSGB 1936 datum, with the grid's own origin."""

    def test_each_spelling_of_the_national_grid_is_taken(self):
        spellings = {
            "ArcGIS": BNG_PRJ,
            "EPSG, before 2020": epsg_bng("OSGB 1936 / British National Grid"),
            "EPSG, since 2020": epsg_bng("OSGB36 / British National Grid"),
            "by its EPSG code alone": epsg_bng("unnamed"),
            "named, with no code": epsg_bng("British National Grid",
                                            authority=None),
            "space before the code": epsg_bng(
                "unnamed", authority='AUTHORITY[ "EPSG" , "27700" ]'),
        }
        for why, prj in spellings.items():
            self.assertEqual(cf._projection(prj), "bng", why)
            self.assertEqual(cf._projection("\n  " + prj.lower()), "bng",
                             why + ", in lower case")

    def test_osgb36_that_is_not_the_national_grid_is_refused(self):
        not_bng = {
            "another grid on OSGB 1936": epsg_bng(
                "OSGB 1936 / Custom Grid", authority=None),
            "another grid's code": epsg_bng(
                "OSGB 1936 / Custom Grid",
                authority='AUTHORITY["EPSG","27701"]'),
            "27700 as a number, not the code": epsg_bng(
                "Custom", authority=None,
                params=EPSG_PARAMS + ',PARAMETER["x",27700]'),
            "the grid's code on an inner node only": epsg_bng(
                "Custom", authority=None, geogcs=EPSG_GEOGCS.replace(
                    '"4277"', '"27700"')),
            "the name, another false easting": BNG_PRJ.replace(
                "400000.0", "0.0"),
            "the name, another meridian": BNG_PRJ.replace(
                '"Central_Meridian",-2.0', '"Central_Meridian",-3.0'),
            "the name, no origin given": epsg_bng(
                "British National Grid", params='PROJECTION['
                '"Transverse_Mercator"]', authority=None),
            "the name, on WGS 84": epsg_bng(
                "OSGB 1936 / British National Grid", geogcs=(
                    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",'
                    '6378137,298.257223563]],UNIT["degree",'
                    '0.0174532925199433]]'), authority=None),
            "the name, another projection": BNG_PRJ.replace(
                "Transverse_Mercator", "Lambert_Conformal_Conic"),
            "the name, UTM's scale factor (a near miss)": BNG_PRJ.replace(
                "0.9996012717", "0.9996"),
        }
        for why, prj in not_bng.items():
            with self.assertRaises(FetchFailed, msg=why):
                cf._projection(prj)

    def test_wgs84_degrees_are_taken_and_anything_else_refused(self):
        self.assertEqual(cf._projection(WGS_PRJ), "wgs84")
        for prj in ('GEOGCS["GCS_OSGB_1936"]', "", None, MERC_PRJ):
            with self.assertRaises(FetchFailed, msg=prj):
                cf._projection(prj)


# ------------------------------------------------- shapefile Z and M, more

def poly(kind, parts, *extra):
    """One polyline record of shape `kind`; each of `extra` is a list of
    values (Z, then M) written as its range and its array."""
    pts = [p for part in parts for p in part]
    starts, at = [], 0
    for part in parts:
        starts.append(at)
        at += len(part)
    rec = struct.pack("<i4d2i", kind, 0, 0, 0, 0, len(parts), len(pts))
    rec += struct.pack("<%di" % len(parts), *starts)
    rec += b"".join(struct.pack("<2d", *p) for p in pts)
    for values in extra:
        rec += struct.pack("<2d", min(values), max(values))
        rec += struct.pack("<%dd" % len(values), *values)
    return rec


def shp_of(recs, kind=3, declared=None):
    """A .shp of ready-made records; `declared` overrides the file length
    its header gives (in bytes)."""
    body = b"".join(struct.pack(">2i", i, len(r) // 2) + r
                    for i, r in enumerate(recs, 1))
    length = declared if declared is not None else 100 + len(body)
    header = struct.pack(">i5ii", 9994, 0, 0, 0, 0, 0, length // 2)
    header += struct.pack("<2i8d", 1000, kind, 0, 0, 0, 0, 0, 0, 0, 0)
    return header + body


def one_file(shp_bytes, rows, prj=None):
    return zipped({"R.shp": shp_bytes,
                   "R.dbf": dbf(FIELDS, [("BOAT", str(i))
                                         for i in range(rows)]),
                   "R.prj": prj or BNG_PRJ})


class ShapefileZAndM(unittest.TestCase):
    def test_a_polyline_m_is_read_as_its_x_and_y(self):
        blob = one_file(shp_of([poly(23, [A, B1], [5.0, 6.0, 7.0, 8.0,
                                                   9.0])], kind=23), 1)
        self.assertEqual(cf.shapefile_records(blob)[0][1],
                         [wgs(A), wgs(B1)])

    def test_a_polyline_z_with_its_m_is_read_as_its_x_and_y(self):
        blob = one_file(shp_of([poly(13, [B2], [1.0, 2.0, 3.0],
                                     [7.0, 8.0, 9.0]),
                                poly(13, [A], [4.0, 5.0, 6.0])], kind=13), 2)
        self.assertEqual([l for _a, l in cf.shapefile_records(blob)],
                         [[wgs(B2)], [wgs(A)]])

    def test_a_shape_of_another_kind_among_lines_is_refused(self):
        polygon = poly(5, [A + [A[0]]])
        multipoint = struct.pack("<i4di", 8, 0, 0, 0, 0, 1) + \
            struct.pack("<2d", *A[0])
        for why, rec in (("polygon", polygon), ("multipoint", multipoint)):
            blob = one_file(shp_of([poly(3, [A]), rec]), 2)
            with self.assertRaises(FetchFailed, msg=why) as cm:
                cf.shapefile_records(blob)
            self.assertIn("among lines", str(cm.exception), why)


class Truncated(unittest.TestCase):
    """A file cut short or corrupted is a failed read (the last good file
    is kept), never an exception that stops the run, and never a smaller
    network taken as whole."""

    def assertRefused(self, fn, *args, **kw):
        why = kw.pop("why", "")
        try:
            fn(*args)
        except FetchFailed:
            return
        except Exception as e:  # noqa: BLE001 - the point of the test
            self.fail("%s: %s, not FetchFailed: %s"
                      % (why, type(e).__name__, e))
        self.fail("%s: read without complaint" % why)

    def test_a_shapefile_cut_inside_a_record(self):
        whole = shp_of([poly(3, [A]), poly(3, [B1, B2])])
        for cut in (len(whole) - 1, len(whole) - 20, len(whole) - 60, 101):
            blob = one_file(whole[:cut], 2)
            self.assertRefused(cf.shapefile_records, blob, why=cut)

    def test_a_shapefile_missing_whole_records_its_header_counts(self):
        whole = shp_of([poly(3, [A]), poly(3, [B1])])
        cut = shp_of([poly(3, [A])], declared=len(whole))
        self.assertRefused(cf.shapefile_records, one_file(cut, 1))
        # The same records with their own length are a whole file.
        self.assertEqual(len(cf.shapefile_records(
            one_file(shp_of([poly(3, [A])]), 1))), 1)

    def test_a_dbf_holding_fewer_records_than_its_header_says(self):
        table = dbf(FIELDS, [("BOAT", "1"), ("BOAT", "2")])
        size = 1 + sum(w for _n, w in FIELDS)
        blob = zipped({"R.shp": shp_of([poly(3, [A])]),
                       "R.dbf": table[:-1 - size] + b"\x1a",
                       "R.prj": BNG_PRJ})
        self.assertRefused(cf.shapefile_records, blob)
        self.assertRefused(cf.dbf_records, table[:20])

    def test_a_zip_cut_short_or_corrupted(self):
        whole = one_file(shp_of([poly(3, [A])]), 1)
        self.assertRefused(cf.shapefile_records, whole[:len(whole) // 2],
                           why="cut")
        buf = io.BytesIO()
        member = shp_of([poly(3, [A, B1, B2])])
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            z.writestr("R.shp", member)
            z.writestr("R.dbf", dbf(FIELDS, [("BOAT", "1")]))
            z.writestr("R.prj", BNG_PRJ)
        data = bytearray(buf.getvalue())
        at = bytes(data).index(member) + 150
        data[at] ^= 0xFF
        self.assertRefused(cf.shapefile_records, bytes(data), why="CRC")
        gpkg_zip = zipped({"RoW.gpkg": gpkg([(gp(wkb_line(A)), "Byway",
                                              "1")])})
        self.assertRefused(cf.gpkg_records, gpkg_zip[:len(gpkg_zip) - 30],
                           why="gpkg zip cut")
        self.assertRefused(cf.kml_records, zipped({"a.kml": KML})[:60],
                           why="kmz cut")

    def test_a_geopackage_geometry_cut_short(self):
        wkb = wkb_line(A)
        for cut in (1, 5, 9, len(wkb) - 1):
            data = gpkg([(gp(wkb[:cut]), "Byway", "1")])
            self.assertRefused(cf.gpkg_records, data, why=cut)
        for short in (b"GP", b"GP\0", b"GP\0\x03", b"GP\0\x01\0\0"):
            data = gpkg([(short, "Byway", "1")])
            self.assertRefused(cf.gpkg_records, data, why=short)
        self.assertRefused(cf.gpkg_records, gpkg([(b"XY", "Byway", "1")]))

    def test_a_geometry_blob_with_no_wkb_after_its_header(self):
        # gpkg_geometry itself: a header with nothing after it, and an
        # envelope said to be longer than what follows the header.
        wkb = wkb_line(A)
        for flags, blob in (
                ("no envelope, header only", b"GP\0\x00" + b"\0" * 4),
                ("32-byte envelope, header only", b"GP\0\x02" + b"\0" * 4),
                ("32-byte envelope cut at 16", b"GP\0\x02" + b"\0" * 20),
                ("32-byte envelope, nothing after", b"GP\0\x02" + b"\0" * 36),
                ("64-byte envelope cut at 48", b"GP\0\x08" + b"\0" * 52),
                ("a header cut at 4", b"GP\0\x00")):
            with self.assertRaises(FetchFailed, msg=flags):
                cf.gpkg_geometry(blob)
        # The smallest that is read: one byte of WKB after each header.
        self.assertEqual(cf.gpkg_geometry(b"GP\0\x00" + b"\0" * 4 + b"W"),
                         b"W")
        self.assertEqual(cf.gpkg_geometry(b"GP\0\x02" + b"\0" * 36 + b"W"),
                         b"W")
        self.assertEqual(cf.gpkg_geometry(gp(wkb)), wkb)

    def test_a_text_point_is_refused_not_read(self):
        # float("12") is 12.0, so a point written as text would be read as
        # one; a file that quotes its coordinates is not a file we know.
        for point in (["12", "34"], [12.0, "34"], ("12", 34), [True, 34.0],
                      [None, 1.0]):
            with self.assertRaises(FetchFailed, msg=point):
                cf._xy(point)
        self.assertEqual(cf._xy([12, 34.5, 7]), (12.0, 34.5))
        self.assertEqual(cf._xy((430000.0, 380000.0)), (430000.0, 380000.0))
        data = json.dumps({"features": [{"geometry": {
            "type": "LineString",
            "coordinates": [["-1.0", "52.0"], ["-1.01", "52.0"]]}}]})
        self.assertRefused(cf.geojson_records, data.encode(), "wgs84")

    def test_a_geopackage_cut_short(self):
        data = gpkg([(gp(wkb_line(A)), "Byway", str(i)) for i in range(50)])
        for cut in (len(data) // 2, 1024, 100):
            self.assertRefused(cf.gpkg_records, data[:cut], why=cut)

    def test_kml_coordinates_that_are_not_numbers(self):
        for coords in ("a,b c,d", "0.80, 52.45 0.81, 52.46", "0.80,52.45 ,",
                       "0.80,52.45 x,52.46"):
            doc = ('<kml><Placemark><name>x</name><LineString><coordinates>'
                   '%s</coordinates></LineString></Placemark></kml>' % coords)
            self.assertRefused(cf.kml_records, doc.encode(), why=coords)

    def test_geojson_coordinates_and_features_that_are_not_lines(self):
        def doc(feature):
            return json.dumps({"features": [feature]}).encode()
        bad = {
            "words": {"geometry": {"type": "LineString",
                                   "coordinates": [["a", "b"], [1, 2]]}},
            "a number": {"geometry": {"type": "LineString",
                                      "coordinates": 5}},
            "too deep": {"geometry": {"type": "LineString",
                                      "coordinates": [[[1, 2]], [[3, 4]]]}},
            "a point for a part": {"geometry": {"type": "MultiLineString",
                                                "coordinates": [[1, 2]]}},
            "a point of one number": {"geometry": {
                "type": "LineString", "coordinates": [[1.0], [2.0, 3.0]]}},
            "a point of none": {"geometry": {
                "type": "LineString", "coordinates": [[], [2.0, 3.0]]}},
            "properties a list": {"properties": [1], "geometry": None},
            "a feature that is text": "feature",
            "a geometry that is text": {"geometry": "LineString"},
        }
        for why, feature in bad.items():
            for projection in ("wgs84", "bng"):
                self.assertRefused(cf.geojson_records, doc(feature),
                                   projection, why=(why, projection))
        self.assertRefused(cf.geojson_records, b'{"features": 5}', "bng")
        self.assertRefused(cf.geojson_records, b"\xff\xfe", "bng")

    def test_deep_nesting(self):
        deep = b'{"features": [' + b"[" * 100000 + b"]" * 100000 + b"]}"
        self.assertRefused(cf.geojson_records, deep, "wgs84", why="json")
        # A MultiLineString inside a MultiLineString, very deep.
        wkb = struct.pack("<BII", 1, 5, 1) * 50000 + wkb_line(A)
        self.assertRefused(cf.gpkg_records, gpkg([(gp(wkb), "Byway", "1")]),
                           why="wkb")
        kml = (b"<kml>" + b"<Folder>" * 50000 + b"</Folder>" * 50000 +
               b"</kml>")
        try:
            self.assertEqual(cf.kml_records(kml), [])
        except FetchFailed:
            pass


# ------------------------------------------------------ WKB, every variant

def wkb_points(points, dims, order):
    return b"".join(struct.pack(order + "%dd" % dims,
                                *(tuple(p) + (7.0, 8.0)[:dims - 2]))
                    for p in points)


def wkb_of(kind, points, dims=2, order="<", srid=None):
    flag = 1 if order == "<" else 0
    out = bytes([flag]) + struct.pack(order + "I", kind)
    if srid is not None:
        out += struct.pack(order + "I", srid)
    return out + struct.pack(order + "I", len(points)) + \
        wkb_points(points, dims, order)


def wkb_multi_of(kind, children, order="<"):
    flag = 1 if order == "<" else 0
    return bytes([flag]) + struct.pack(order + "II", kind, len(children)) \
        + b"".join(children)


class WkbVariants(unittest.TestCase):
    """ISO WKB as GeoPackage stores it - 2D, Z, M and ZM, either byte
    order - and the old 2.5D flag GDAL once wrote for Z. The PostGIS-only
    EWKB flags for M and an SRID never appear in a GeoPackage (its SRID is
    in the geometry header) and are refused."""

    def read(self, wkb):
        return cf.wkb_lines(wkb)

    def test_iso_z_m_and_zm_lines_and_multilines(self):
        for base, dims in ((0, 2), (1000, 3), (2000, 3), (3000, 4)):
            for order in ("<", ">"):
                line = wkb_of(base + 2, A, dims, order)
                self.assertEqual(self.read(line), [A], (base, order))
                multi = wkb_multi_of(base + 5, [
                    wkb_of(base + 2, A, dims, order),
                    wkb_of(base + 2, B1, dims, order)], order)
                self.assertEqual(self.read(multi), [A, B1], (base, order))

    def test_the_25d_flag_is_z(self):
        z = 0x80000000
        self.assertEqual(self.read(wkb_of(z | 2, B2, 3)), [B2])
        self.assertEqual(self.read(wkb_of(z | 2, B2, 3, ">")), [B2])
        multi = wkb_multi_of(z | 5, [wkb_of(z | 2, A, 3),
                                     wkb_of(z | 2, B1, 3)])
        self.assertEqual(self.read(multi), [A, B1])

    def test_a_big_endian_multiline_of_either_childs_order(self):
        multi = wkb_multi_of(5, [wkb_of(2, A, 2, ">"), wkb_of(2, B1, 2, "<"),
                                 wkb_of(2, B2, 2, ">")], ">")
        self.assertEqual(self.read(multi), [A, B1, B2])
        data = gpkg([(gp(multi), "Byway", "1")])
        self.assertEqual(cf.gpkg_records(data)[0][1],
                         [wgs(A), wgs(B1), wgs(B2)])

    def test_ewkb_m_and_srid_flags_are_refused(self):
        for why, wkb in (
                ("M flag", wkb_of(0x40000002, A, 3)),
                ("ZM flags", wkb_of(0xC0000002, A, 4)),
                ("SRID flag", wkb_of(0x20000002, A, 2, srid=27700)),
                ("SRID flag on a multi", wkb_multi_of(0x20000005, []))):
            with self.assertRaises(FetchFailed, msg=why):
                self.read(wkb)

    def test_a_multi_holds_only_lines(self):
        for why, child in (("a multi", wkb_multi_of(5, [wkb_of(2, A)])),
                           ("a point", struct.pack("<BI2d", 1, 1, 1, 2)),
                           ("a polygon", struct.pack("<BII", 1, 3, 0))):
            with self.assertRaises(FetchFailed, msg=why):
                self.read(wkb_multi_of(5, [wkb_of(2, A), child]))

    def test_points_and_polygons_are_refused(self):
        for kind in (1, 3, 4, 6, 7, 1001, 3003):
            with self.assertRaises(FetchFailed, msg=kind):
                self.read(wkb_of(kind, A))

    def test_an_empty_line_and_an_empty_multi(self):
        self.assertEqual(self.read(wkb_of(2, [])), [[]])
        self.assertEqual(self.read(wkb_multi_of(5, [])), [])


# --------------------------------------------------------- the WGS84 guard

class Wgs84Guard(unittest.TestCase):
    def test_grid_numbers_read_as_degrees_give_no_line(self):
        doc = {"features": [{"properties": {}, "geometry": {
            "type": "LineString", "coordinates": A}}]}
        self.assertEqual(cf.geojson_records(json.dumps(doc).encode(),
                                            "wgs84"), [({}, [])])
        blob = one_file(shp_of([poly(3, [A])]), 1, prj=WGS_PRJ)
        self.assertEqual(cf.shapefile_records(blob)[0][1], [])

    def test_a_point_out_of_range_on_either_axis_is_dropped(self):
        pts = [(-1.5, 53.25), (181.0, 53.0), (-181.0, 53.0), (-1.4, 90.5),
               (-1.4, -90.5), (-1.4, 53.3), (180.0, 90.0), (-180.0, -90.0)]
        self.assertEqual(cf._lonlat(pts), [(-1.5, 53.25), (-1.4, 53.3),
                                           (180.0, 90.0), (-180.0, -90.0)])

    def test_points_are_rounded_and_repeats_dropped(self):
        pts = [(-1.123456789, 53.987654321), (-1.1234571, 53.9876539),
               (-1.2, 53.9), (-1.2, 53.9), (-1.2, 53.9000001), (-1.3, 54.0),
               (-1.2, 53.9)]
        self.assertEqual(cf._lonlat(pts), [(-1.123457, 53.987654),
                                           (-1.2, 53.9), (-1.3, 54.0),
                                           (-1.2, 53.9)])
        self.assertEqual(cf._lonlat([(1.0, 2.0, 3.0), (1.5, 2.5, 0, 9)]),
                         [(1.0, 2.0), (1.5, 2.5)])

    def test_a_line_left_with_one_point_is_dropped(self):
        self.assertEqual(cf._to_wgs("wgs84", [[(-1.2, 53.9), (-1.2, 53.9)],
                                              [(-1.2, 53.9), (-1.3, 54.0)]]),
                         [[(-1.2, 53.9), (-1.3, 54.0)]])


# ------------------------------------------------ a GeoPackage's own date

class GeoPackageDate(unittest.TestCase):
    """The year in an Ordnance Survey credit comes from the data's own
    date, never the day we read it."""

    def test_the_year_is_the_feature_tables_last_change(self):
        data = gpkg([(gp(wkb_line(A)), "Byway", "1")], last_change={
            "row": "2026-09-07T16:01:19.136Z", "notes": "2019-01-01"})
        self.assertEqual(cf.gpkg_year(data), "2026")
        self.assertEqual(cf.gpkg_year(zipped({"RoW.gpkg": data})), "2026")
        data = gpkg([(gp(wkb_line(A)), "Byway", "1")], last_change={
            "row": "2027-01-02T00:00:00Z"})
        self.assertEqual(cf.gpkg_year(data), "2027")

    def test_no_date_is_none_never_a_guess(self):
        row = [(gp(wkb_line(A)), "Byway", "1")]
        for why, data in (
                ("no contents table", gpkg(row)),
                ("no row for the table", gpkg(row, last_change={
                    "other": "2026-09-07T16:01:19Z"})),
                ("an empty date", gpkg(row, last_change={"row": ""})),
                ("not a date", gpkg(row, last_change={"row": "recently"})),
                ("a null date", gpkg(row, last_change={"row": None}))):
            self.assertIsNone(cf.gpkg_year(data), why)
        with self.assertRaises(FetchFailed):
            cf.gpkg_year(b"not a geopackage")


class _Caps(unittest.TestCase):
    """Run with the module's caps set small, so a synthetic bomb of a few
    kilobytes stands in for one of gigabytes."""

    CAPS = {}

    def setUp(self):
        saved = dict((k, getattr(cf, k)) for k in self.CAPS)
        self.addCleanup(lambda: [setattr(cf, k, v) for k, v in saved.items()])
        for k, v in self.CAPS.items():
            setattr(cf, k, v)


def _deflated(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _shapefile_zip(pad=b""):
    return _deflated({"r.shp": shp([[A]]), "r.shx": b"",
                      "r.dbf": dbf(FIELDS, [("Byway", "1")]),
                      "r.prj": BNG_PRJ, "pad.txt": pad})


class ZipBomb(_Caps):
    """A zip whose members inflate past the cap is refused before anything
    is inflated: one hostile download never stops the run."""

    CAPS = {"MAX_UNZIPPED": 20000}

    def test_each_reader_refuses_a_zip_inflating_past_the_cap(self):
        pad = b"\0" * 30000           # deflates to a few dozen bytes
        kml = KML.encode("utf-8")
        for why, read, blob in (
                ("shapefile", cf.shapefile_records, _shapefile_zip(pad)),
                ("geopackage", cf.gpkg_records, _deflated(
                    {"r.gpkg": gpkg([(gp(wkb_line(A)), "B", "1")]),
                     "pad.txt": pad})),
                ("geopackage year", cf.gpkg_year, _deflated(
                    {"r.gpkg": gpkg([(gp(wkb_line(A)), "B", "1")]),
                     "pad.txt": pad})),
                ("kmz", cf.kml_records, _deflated({"doc.kml": kml,
                                                   "pad.txt": pad}))):
            with self.subTest(why=why):
                self.assertLess(len(blob), 20000)
                with self.assertRaises(FetchFailed) as got:
                    read(blob)
                self.assertIn("too big", str(got.exception))

    def test_one_member_past_the_cap_is_refused_even_alone(self):
        blob = _deflated({"doc.kml": KML.encode("utf-8") +
                          b" " * 30000})
        with self.assertRaises(FetchFailed) as got:
            cf.kml_records(blob)
        self.assertIn("too big", str(got.exception))

    def test_a_member_that_lies_about_its_size_is_cut_off(self):
        # The size a zip's directory states is the sender's to write: the
        # read itself stops at the cap.
        blob = bytearray(_deflated({"doc.kml": KML.encode("utf-8") +
                                    b" " * 30000}))
        for header, off in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
            at = blob.find(header)
            blob[at + off:at + off + 4] = struct.pack("<I", 100)
        with self.assertRaises(FetchFailed):
            cf.kml_records(bytes(blob))

    def test_a_zip_inside_the_cap_is_read(self):
        self.assertEqual(len(cf.shapefile_records(_shapefile_zip())), 1)
        self.assertEqual(len(cf.kml_records(_deflated(
            {"doc.kml": KML.encode("utf-8")}))), 4)


def _gpkg_view(sql):
    """A GeoPackage whose one 'feature table' is the view `sql`."""
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    os.close(fd)
    os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, "
                 "column_name TEXT, geometry_type_name TEXT, srs_id INT, "
                 "z INT, m INT)")
    conn.execute("INSERT INTO gpkg_geometry_columns VALUES "
                 "('row', 'geom', 'LINESTRING', 27700, 0, 0)")
    conn.execute("CREATE VIEW row AS " + sql)
    conn.commit()
    conn.close()
    with open(path, "rb") as fh:
        data = fh.read()
    os.remove(path)
    return data


#: A view that never ends.
_FOREVER = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) "
            "SELECT i AS fid, NULL AS geom FROM n")


class GeoPackageBudget(_Caps):
    """A GeoPackage is a database the sender wrote: its feature table must
    be a real table, and a read is held to a budget of work and rows."""

    CAPS = {"MAX_GPKG_ROWS": 5}

    def test_a_feature_table_that_is_a_view_is_refused(self):
        for read in (cf.gpkg_records, cf.gpkg_year):
            with self.subTest(read=read.__name__):
                with self.assertRaises(FetchFailed) as got:
                    read(_gpkg_view(_FOREVER))
                self.assertIn("not a table", str(got.exception))

    def test_too_many_rows_are_refused(self):
        rows = [(gp(wkb_line(A)), "B", str(i)) for i in range(6)]
        with self.assertRaises(FetchFailed) as got:
            cf.gpkg_records(gpkg(rows))
        self.assertIn("rows", str(got.exception))
        self.assertEqual(len(cf.gpkg_records(gpkg(rows[:5]))), 5)



class GeoPackageSteps(_Caps):
    """The budget of work: a read that needs more is interrupted, as a
    FetchFailed, and one inside it is read whole."""

    CAPS = {"GPKG_STEPS": 3}

    def test_work_past_the_budget_is_interrupted(self):
        rows = [(gp(wkb_line(A)), "B", str(i)) for i in range(5000)]
        data = gpkg(rows)
        with self.assertRaises(FetchFailed) as got:
            cf.gpkg_records(data)
        self.assertIn("interrupted", str(got.exception))
        cf.GPKG_STEPS = 100000
        self.assertEqual(len(cf.gpkg_records(data)), 5000)


def _gpkg_extra(column_sql, rows=1, text="x"):
    """A one-table GeoPackage whose table has the extra column
    `column_sql`, and `rows` rows with PROWNo `text`."""
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    os.close(fd)
    os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, "
                 "column_name TEXT, geometry_type_name TEXT, srs_id INT, "
                 "z INT, m INT)")
    conn.execute("INSERT INTO gpkg_geometry_columns VALUES "
                 "('row', 'geom', 'LINESTRING', 27700, 0, 0)")
    conn.execute("CREATE TABLE row (fid INTEGER PRIMARY KEY, geom BLOB, "
                 "PROWNo TEXT%s)" % (", " + column_sql if column_sql else ""))
    for i in range(rows):
        conn.execute("INSERT INTO row (fid, geom, PROWNo) VALUES (?, ?, ?)",
                     (i + 1, gp(wkb_line(A)), text))
    conn.commit()
    conn.close()
    with open(path, "rb") as fh:
        data = fh.read()
    os.remove(path)
    return data


class GeoPackageValues(_Caps):
    """A value is held to a size before it is read: a generated column, or
    one big value, cannot make a small file fill the memory."""

    CAPS = {"MAX_GPKG_VALUE": 1000, "MAX_GPKG_BYTES": 5000}

    def test_a_generated_column_is_refused(self):
        for sql in ("g BLOB AS (zeroblob(100000000)) VIRTUAL",
                    "g BLOB AS (zeroblob(10)) STORED"):
            with self.subTest(sql=sql):
                for read in (cf.gpkg_records, cf.gpkg_year):
                    with self.assertRaises(FetchFailed) as got:
                        read(_gpkg_extra(sql))
                    self.assertIn("generated", str(got.exception))

    def test_one_value_past_the_cap_is_refused(self):
        with self.assertRaises(FetchFailed) as got:
            cf.gpkg_records(_gpkg_extra(None, text="x" * 1001))
        self.assertIn("bytes", str(got.exception))
        self.assertEqual(len(cf.gpkg_records(_gpkg_extra(
            None, text="x" * 900))), 1)

    def test_values_past_the_total_are_refused(self):
        with self.assertRaises(FetchFailed) as got:
            cf.gpkg_records(_gpkg_extra(None, rows=8, text="x" * 900))
        self.assertIn("bytes", str(got.exception))
        self.assertEqual(len(cf.gpkg_records(_gpkg_extra(
            None, rows=4, text="x" * 900))), 4)


class DbfHeader(unittest.TestCase):
    """A .dbf header's record count and size are checked against the file
    before a record is read: a tiny file cannot claim millions."""

    def header(self, data, count=None, size=None):
        data = bytearray(data)
        c, h, s = struct.unpack("<IHH", bytes(data[4:12]))
        data[4:12] = struct.pack("<IHH", c if count is None else count, h,
                                 s if size is None else size)
        return bytes(data)

    def test_more_records_than_the_file_holds_are_refused(self):
        good = dbf(FIELDS, [("Byway", "1")])
        for count, size in ((3000000, 0), (2, None), (3000000, None)):
            with self.subTest(count=count, size=size):
                with self.assertRaises(FetchFailed):
                    cf.dbf_records(self.header(good, count, size))

    def test_a_record_shorter_than_its_fields_is_refused(self):
        good = dbf(FIELDS, [("Byway", "1"), ("BOAT", "2")])
        with self.assertRaises(FetchFailed) as got:
            cf.dbf_records(self.header(good, size=5))
        self.assertIn("record", str(got.exception))
        self.assertEqual(len(cf.dbf_records(good)), 2)

    def test_the_record_count_is_checked_to_the_byte(self):
        # A .dbf need not end in its 0x1A byte: header + count * size is
        # the whole file, and one byte less is refused.
        rows = [("Byway", "1"), ("BOAT", "2"), ("ORPA", "3")]
        good = dbf(FIELDS, rows)
        self.assertEqual(len(cf.dbf_records(good[:-1])), 3)
        with self.assertRaises(FetchFailed) as got:
            cf.dbf_records(good[:-2])
        self.assertIn("ends before", str(got.exception))
        for count in (2, 3):
            with self.subTest(count=count):
                self.assertEqual(len(cf.dbf_records(
                    self.header(good, count))), count)
        with self.assertRaises(FetchFailed):
            cf.dbf_records(self.header(good, 4))


def _gpkg_one_leaf():
    """A GeoPackage whose table's interior root page has every child
    pointer aimed at one leaf: the same rows over and over, the corruption
    that turns a pre-pass into a copy of gigabytes."""
    data = bytearray(_gpkg_extra(None, rows=20, text="x" * 1500))
    size = struct.unpack(">H", bytes(data[16:18]))[0]
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    with os.fdopen(fd, "wb") as fh:
        fh.write(bytes(data))
    conn = sqlite3.connect(path)
    root = conn.execute("SELECT rootpage FROM sqlite_master "
                        "WHERE name = 'row'").fetchone()[0]
    conn.close()
    os.remove(path)
    base = (root - 1) * size
    assert data[base] == 5, "the table's root is not an interior page"
    cells = struct.unpack(">H", bytes(data[base + 3:base + 5]))[0]
    offsets = [struct.unpack(">H", bytes(data[base + 12 + 2 * i:
                                             base + 14 + 2 * i]))[0]
               for i in range(cells)]
    leaf = bytes(data[base + offsets[0]:base + offsets[0] + 4])
    for off in offsets:
        data[base + off:base + off + 4] = leaf
    data[base + 8:base + 12] = leaf
    return bytes(data)


class GeoPackageIntegrity(unittest.TestCase):
    """A corrupt GeoPackage is refused by SQLite's own quick check before
    any query on its feature table."""

    def test_pages_referenced_twice_are_refused(self):
        bad = _gpkg_one_leaf()
        for read in (cf.gpkg_records, cf.gpkg_year):
            with self.subTest(read=read.__name__):
                with self.assertRaises(FetchFailed) as got:
                    read(bad)
                self.assertIn("integrity", str(got.exception))

    def test_a_sound_file_passes_the_check(self):
        self.assertEqual(len(cf.gpkg_records(_gpkg_extra(
            None, rows=20, text="x" * 1500))), 20)


#: A value as costly as SQLite's length limit allows nothing like: 16 MB
#: made in C, outside the budget, every time one row is checked.
_COSTLY = "printf('%.*c', 16000000 + fid % 2, fid)"


def _gpkg_costly(column_sql, where, rows=1000):
    """A GeoPackage with `rows` rows in the feature table 'row' and in
    another table 'other', and `column_sql` added to the table `where`.
    The column goes into the schema after the rows are written, so the
    fixture is cheap to make however costly the column is to compute."""
    data = _gpkg_extra(None, rows=rows)
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE other (fid INTEGER PRIMARY KEY, note TEXT)")
    conn.executemany("INSERT INTO other VALUES (?, 'x')",
                     [(i + 1,) for i in range(rows)])
    conn.commit()
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?",
                       (where,)).fetchone()[0]
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET sql = ? WHERE name = ?",
                 (sql[:-1] + ", " + column_sql + ")", where))
    conn.commit()
    conn.close()
    with open(path, "rb") as fh:
        data = fh.read()
    os.remove(path)
    return data


class GeoPackageCostlyColumns(unittest.TestCase):
    """A generated column in ANY table is refused before quick_check, which
    computes every NOT NULL generated value in C, outside the budget: a
    small file could otherwise run past the job's timeout."""

    def test_a_costly_generated_column_is_refused_fast(self):
        column = "g TEXT AS (%s) VIRTUAL NOT NULL" % _COSTLY
        for where in ("row", "other"):
            data = _gpkg_costly(column, where)
            for read in (cf.gpkg_records, cf.gpkg_year):
                with self.subTest(where=where, read=read.__name__):
                    t = time.monotonic()
                    with self.assertRaises(FetchFailed) as got:
                        read(data)
                    self.assertLess(time.monotonic() - t, 2.0)
                    self.assertIn("generated", str(got.exception))
                    self.assertIn("%s.g" % where, str(got.exception))

    def test_a_costly_check_is_not_run_on_a_read_only_file(self):
        # Measured: neither trusted_schema off (printf is innocuous) nor
        # the length limit stops this; opening the file read-only does,
        # as quick_check then skips CHECK constraints.
        data = _gpkg_costly("CHECK (length(%s) > 0)" % _COSTLY, "other")
        t = time.monotonic()
        self.assertEqual(len(cf.gpkg_records(data)), 1000)
        self.assertLess(time.monotonic() - t, 2.0)

    def test_virtual_tables_are_still_read(self):
        # Real GeoPackages carry an R*Tree virtual table and its shadow
        # tables, and may carry a virtual table of a module this SQLite
        # lacks (quick_check skips both): neither is refused.
        data = _gpkg_extra(None, rows=3)
        fd, path = tempfile.mkstemp(suffix=".gpkg")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        conn = sqlite3.connect(path)
        conn.execute("CREATE VIRTUAL TABLE rtree_row_geom USING "
                     "rtree(id, minx, maxx, miny, maxy)")
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute("INSERT INTO sqlite_master VALUES ('table', 'vx', "
                     "'vx', 0, 'create  virtual table vx USING "
                     "nosuchmodule(a)')")
        conn.commit()
        conn.close()
        with open(path, "rb") as fh:
            data = fh.read()
        os.remove(path)
        self.assertEqual(len(cf.gpkg_records(data)), 3)


class ShippedCaps(unittest.TestCase):
    """The caps as shipped, not rebound small: each holds at its real
    value."""

    MIB = 1024 * 1024

    def test_the_caps_have_their_shipped_values(self):
        self.assertEqual(cf.MAX_UNZIPPED, 100 * self.MIB)
        self.assertEqual(cf.MAX_GPKG_VALUE, 16 * self.MIB)
        # Too slow to build a real 100 MiB GeoPackage: the value is pinned.
        self.assertEqual(cf.MAX_GPKG_BYTES, 100 * self.MIB)

    def stating(self, size):
        """A one-member zip whose directory states `size` bytes."""
        blob = bytearray(_deflated({"doc.kml": KML.encode("utf-8")}))
        at = blob.find(b"PK\x01\x02")
        blob[at + 24:at + 28] = struct.pack("<I", size)
        return bytes(blob)

    def test_a_zip_stating_just_over_100_mib_is_refused(self):
        with self.assertRaises(FetchFailed) as got:
            cf._zip(self.stating(100 * self.MIB + 1))
        self.assertIn("too big", str(got.exception))
        z = cf._zip(self.stating(100 * self.MIB))
        self.assertEqual(z.infolist()[0].file_size, 100 * self.MIB)

    def test_a_value_just_over_16_mib_is_refused(self):
        with self.assertRaises(FetchFailed) as got:
            cf.gpkg_records(_gpkg_extra(None, text="x" * (16 * self.MIB + 1)))
        self.assertIn("bytes", str(got.exception))
        got = cf.gpkg_records(_gpkg_extra(None, text="x" * (16 * self.MIB)))
        self.assertEqual(len(got), 1)


class TheModulesAccount(unittest.TestCase):
    def test_the_docstring_names_each_budget(self):
        flat = " ".join(cf.__doc__.split())
        for words in ("A HOSTILE FILE IS HELD TO A BUDGET", "MAX_UNZIPPED",
                      "trusted_schema off", "GPKG_STEPS", "MAX_GPKG_ROWS",
                      "not a view",
                      "a generated or hidden column in any table is refused "
                      "before anything else runs",
                      "quick_check makes every one",
                      "opened read-only (so the check runs no CHECK "
                      "constraint)"):
            self.assertIn(words, flat)


class MacosxSidecars(unittest.TestCase):
    """A zip made on a Mac carries __MACOSX/ copies of each file: they are
    not a second KML or GeoPackage."""

    def test_kmz_and_gpkg_zips_skip_macosx(self):
        kml = KML.encode("utf-8")
        self.assertEqual(len(cf.kml_records(zipped(
            {"doc.kml": kml, "__MACOSX/._doc.kml": b"\0\5\26\7"}))), 4)
        data = gpkg([(gp(wkb_line(A)), "B", "1")])
        self.assertEqual(len(cf.gpkg_records(zipped(
            {"r.gpkg": data, "__MACOSX/._r.gpkg": b"\0\5\26\7"}))), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)

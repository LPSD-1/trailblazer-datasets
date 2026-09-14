"""The module that actually builds the pack.

It was the only untested one in the publishing path. `test_tro.py` stops at
the WKT string and never converts it, and `test_tro_merge.py` covers a module
the build does not import - so `parse_wkt`, `to_wgs84`, `geojson`,
`corpus_date` and the publication floor had nothing holding them.
"""
import json
import os
import tempfile
import unittest

import build_tro


class ParseWkt(unittest.TestCase):
    def test_a_point(self):
        kind, coords = build_tro.parse_wkt("SRID=27700;POINT(400000 300000)")
        self.assertEqual(kind, "POINT")
        self.assertEqual(coords, [(400000.0, 300000.0)])

    def test_a_line(self):
        kind, coords = build_tro.parse_wkt(
            "SRID=27700;LINESTRING(400000 300000, 400100 300100)")
        self.assertEqual(kind, "LINESTRING")
        self.assertEqual(len(coords), 2)

    def test_a_z_geometry_is_read_not_dropped(self):
        # `POINT Z (...)` is valid WKT and the service is free to start
        # sending it. The regex wanted a bare word before the bracket, so
        # every such order vanished from the pack with nothing logged.
        kind, coords = build_tro.parse_wkt(
            "SRID=27700;POINT Z (400000 300000 12)")
        self.assertEqual(kind, "POINT")
        self.assertEqual(coords[0][0], 400000.0)
        self.assertEqual(coords[0][1], 300000.0)

    def test_lowercase_srid(self):
        kind, _ = build_tro.parse_wkt(
            "srid=27700;linestring(400000 300000, 400100 300100)")
        self.assertEqual(kind, "LINESTRING")

    def test_a_multilinestring_keeps_its_parts_apart(self):
        # Flattened into one line, two closed stretches a mile apart get
        # joined by a straight line drawn along roads that are open.
        kind, parts = build_tro.parse_wkt(
            "SRID=27700;MULTILINESTRING((400000 300000, 400100 300100),"
            "(410000 310000, 410100 310100))")
        self.assertEqual(kind, "MULTILINESTRING")
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[0]), 2)

    def test_nonsense_is_nothing(self):
        self.assertEqual(build_tro.parse_wkt("not wkt at all"), (None, []))
        self.assertEqual(build_tro.parse_wkt(None), (None, []))
        self.assertEqual(build_tro.parse_wkt(""), (None, []))


class GridDomain(unittest.TestCase):
    """The check that keeps orders out of the sea.

    Testing the EASTING AND NORTHING, not the converted degrees. (0, 0) - the
    commonest missing value there is - converts to a point in the Celtic Sea
    about 130 km southwest of Land's End, which is comfortably inside any
    box drawn round the British Isles.
    """

    def test_a_real_place_is_in(self):
        self.assertTrue(build_tro.in_grid(400000, 300000))

    def test_the_origin_is_out(self):
        self.assertFalse(build_tro.in_grid(0, 0))

    def test_negatives_are_out(self):
        self.assertFalse(build_tro.in_grid(-50000, -50000))

    def test_shetland_is_in(self):
        # The far north of the grid, and a legitimate place to close a road.
        self.assertTrue(build_tro.in_grid(450000, 1200000))

    def test_far_outside_is_out(self):
        self.assertFalse(build_tro.in_grid(9000000, 9000000))


class Geojson(unittest.TestCase):
    def _feature(self, wkt, **kw):
        base = {
            "code": "miscRoadClosure",
            "label": "Road closed",
            "wkt": wkt,
            "name": "An order",
            "where": "Hooton Lane",
            "start": "2026-09-01",
            "end": None,
        }
        base.update(kw)
        return base

    def test_a_line_survives(self):
        got = build_tro.geojson(
            self._feature("SRID=27700;LINESTRING(400000 300000, 400100 300100)"),
            "dtro-1")
        self.assertIsNotNone(got)
        self.assertEqual(got["geometry"]["type"], "LineString")
        self.assertEqual(len(got["geometry"]["coordinates"]), 2)

    def test_a_junk_vertex_does_not_drag_the_line_into_the_atlantic(self):
        # A 130 m closure near Sheffield with one (0, 0) in the middle became
        # a 400 km V out to sea and back, and everything downstream that reads
        # the bounding box then covered half of England.
        got = build_tro.geojson(
            self._feature("SRID=27700;LINESTRING(430000 435000, 0 0, "
                          "430100 435100)"),
            "dtro-2")
        self.assertIsNotNone(got)
        lons = [c[0] for c in got["geometry"]["coordinates"]]
        self.assertTrue(all(lon > -3 for lon in lons),
                        "a vertex in the Celtic Sea survived: %r" % (lons,))

    def test_an_order_with_no_usable_geometry_is_dropped(self):
        self.assertIsNone(
            build_tro.geojson(self._feature("SRID=27700;POINT(0 0)"), "d"))

    def test_the_uid_changes_when_the_order_does(self):
        # The comment beside the seed claims "an order that has not changed
        # keeps its id and one that has gets a new one". It was seeded from
        # (ref, code, where, FIRST coordinate) only, so amending the dates or
        # extending the geometry by a hundred kilometres kept the same id.
        line = "SRID=27700;LINESTRING(400000 300000, 400100 300100)"
        longer = "SRID=27700;LINESTRING(400000 300000, 500000 400000)"
        a = build_tro.geojson(self._feature(line), "d")
        b = build_tro.geojson(self._feature(line, end="2027-01-01"), "d")
        c = build_tro.geojson(self._feature(longer), "d")
        self.assertNotEqual(a["properties"]["tro_uid"],
                            b["properties"]["tro_uid"])
        self.assertNotEqual(a["properties"]["tro_uid"],
                            c["properties"]["tro_uid"])

    def test_and_does_not_change_when_it_does_not(self):
        # The other half. Without this the floor above is satisfied by making
        # every id random, which would re-download the world every cut.
        line = "SRID=27700;LINESTRING(400000 300000, 400100 300100)"
        a = build_tro.geojson(self._feature(line), "d")
        b = build_tro.geojson(self._feature(line), "d")
        self.assertEqual(a["properties"]["tro_uid"], b["properties"]["tro_uid"])

    def test_two_different_closures_at_one_junction_are_two_orders(self):
        line = "SRID=27700;LINESTRING(400000 300000, 400100 300100)"
        other = "SRID=27700;LINESTRING(400000 300000, 399000 299000)"
        a = build_tro.geojson(self._feature(line, ref=None, where=None), "d")
        b = build_tro.geojson(self._feature(other, ref=None, where=None), "d")
        self.assertNotEqual(a["properties"]["tro_uid"],
                            b["properties"]["tro_uid"])


class CorpusDate(unittest.TestCase):
    def test_from_the_filename(self):
        self.assertEqual(
            build_tro.corpus_date("/x/dtros_20260906_010013.csv"), "2026-09-06")

    def test_without_one_it_falls_back_rather_than_throwing(self):
        self.assertRegex(build_tro.corpus_date("/x/dtros_all.csv"),
                         r"^\d{4}-\d{2}-\d{2}$")


class PreviousCount(unittest.TestCase):
    def test_reads_what_the_last_run_published(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "index.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"packs": [{"id": "gb-tro", "features": 34021}]}, fh)
            self.assertEqual(build_tro.previous_count(path), 34021)

    def test_an_index_from_before_the_count_existed_is_no_floor(self):
        # Rather than a floor of zero that silently passes, or a crash on the
        # first run after this shipped.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "index.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"packs": [{"id": "gb-tro", "bytes": 3751859}]}, fh)
            self.assertEqual(build_tro.previous_count(path), 0)

    def test_a_missing_index_is_no_floor(self):
        self.assertEqual(build_tro.previous_count("/nowhere/index.json"), 0)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Street Manager closures reach a byway only through its USRN, only where
the activity is, and only when they shut the way.

    python tools/test_street_manager.py

No network. A tiny GeoPackage built in a temporary file in the shape of OS
Open USRN; activity events in the shape of the Street Manager archive
(September 2026), trimmed.
"""
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import street_manager as sm  # noqa: E402
from byway_match import Byways, Way  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402
from polite_http import FetchFailed  # noqa: E402


def way(uid, *bng):
    return Way(uid, "Lancashire", "Byway open to all traffic (BOAT) X 1",
               [[grid_to_wgs84(e, n) for e, n in bng]])


# A byway 600 m east from (380000, 430000).
BYWAY = way("LA-1", (380000, 430000), (380600, 430000))


def gp_line(points, z=True):
    """A GeoPackage blob: header, xyz envelope, WKB LineString Z."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    env = struct.pack("<6d", min(xs), max(xs), min(ys), max(ys), 0, 0)
    header = b"GP" + bytes([0, 0b101]) + struct.pack("<i", 27700) + env
    kind = 1002 if z else 2
    wkb = struct.pack("<BII", 1, kind, len(points))
    for x, y in points:
        wkb += struct.pack("<ddd", x, y, 0.0) if z else \
            struct.pack("<dd", x, y)
    return header + wkb


def make_gpkg(path, streets):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE openUSRN (id INTEGER PRIMARY KEY, geometry "
               "BLOB, usrn INTEGER, street_type TEXT)")
    db.execute("CREATE VIRTUAL TABLE rtree_openUSRN_geometry USING "
               "rtree(id, minx, maxx, miny, maxy)")
    for i, (usrn, pts) in enumerate(streets, 1):
        db.execute("INSERT INTO openUSRN VALUES (?,?,?,?)",
                   (i, gp_line(pts), usrn, "Officially Described Street"))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        db.execute("INSERT INTO rtree_openUSRN_geometry VALUES (?,?,?,?,?)",
                   (i, min(xs), max(xs), min(ys), max(ys)))
    db.commit()
    db.close()


class Gpkg(unittest.TestCase):
    def test_a_3d_linestring_blob_is_read(self):
        self.assertEqual(sm.gpkg_lines(gp_line([(1, 2), (3, 4)])),
                         [[(1.0, 2.0), (3.0, 4.0)]])

    def test_a_multilinestring_is_every_part(self):
        parts = [struct.pack("<BII", 1, 2, 2) + struct.pack("<4d", 0, 0, 1, 1),
                 struct.pack("<BII", 1, 2, 2) + struct.pack("<4d", 5, 5, 6, 6)]
        wkb = struct.pack("<BII", 1, 5, 2) + b"".join(parts)
        self.assertEqual(len(sm.wkb_lines(wkb)), 2)


class Usrn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.gpkg = os.path.join(self.tmp, "u.gpkg")
        make_gpkg(self.gpkg, [
            # The byway's own street, drawn 3 m off.
            (111, [(380000, 430003), (380300, 430003), (380600, 430003)]),
            # A road crossing it.
            (222, [(380300, 429500), (380300, 430500)]),
            # A road 200 m away, parallel.
            (333, [(380000, 430200), (380600, 430200)]),
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_the_street_it_runs_along_and_not_the_one_it_crosses(self):
        conn = sqlite3.connect(self.gpkg)
        try:
            self.assertEqual(sm.usrns_of(BYWAY, conn), ["111"])
        finally:
            conn.close()

    def test_a_short_byway_does_not_take_the_road_crossing_its_end(self):
        # 60 m long: the crossing road covers 24 m of it, 40%, but that is
        # a junction, not the byway's street.
        short = way("LA-2", (380270, 430000), (380330, 430000))
        conn = sqlite3.connect(self.gpkg)
        try:
            self.assertNotIn("222", sm.usrns_of(short, conn))
        finally:
            conn.close()

    def test_the_table_lists_byways_by_usrn(self):
        table = sm.build_usrn_table(Byways([BYWAY]), self.gpkg,
                                    log=lambda *_: None)
        self.assertEqual(table["usrns"], {"111": ["LA-1"]})
        self.assertEqual(table["matched"], 1)


def event(arn, when, **o):
    base = {"activity_reference_number": arn, "usrn": "111",
            "street_name": "TOWNELEY HOLMES ROAD",
            "activity_coordinates": "POINT(380300 430000)",
            "activity_name": "Burnley bonfire\n- whole road",
            "activity_type": "other",
            "activity_type_details": "Temporary Traffic Regulation Notice",
            "start_date": "2026-11-05T00:00:00.000Z",
            "end_date": "2026-11-05T00:00:00.000Z",
            "traffic_management_type": "road_closure", "cancelled": "No",
            "highway_authority": "LANCASHIRE COUNTY COUNCIL"}
    base.update(o)
    return {"event_time": when, "object_data": base, "object_reference": arn}


def archive(*events):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, e in enumerate(events):
            z.writestr("%s_%d.json" % (e["object_reference"], i),
                       json.dumps(e))
    return buf.getvalue()


TABLE = {"111": ["LA-1"]}
WAYS = {"LA-1": BYWAY}


class Activities(unittest.TestCase):
    def items(self, *events, today="2026-10-07"):
        acts = sm.newest_activities([archive(*events)])
        return sm.items_for(acts, TABLE, WAYS, today)

    def test_a_road_closure_on_the_byways_usrn_is_published(self):
        got = self.items(event("ARN-1", "2026-09-01"))
        self.assertEqual(len(got), 1)
        item = got[0]
        self.assertEqual((item["ways"], item["start"], item["end"],
                          item["form"], item["vehicles"]),
                         (["LA-1"], "2026-11-05", "2026-11-05", "temporary",
                          "all_users"))
        self.assertEqual(item["title"],
                         "Temporary closure: Burnley bonfire - whole road")
        self.assertIn("Lancashire County Council", item["source_name"])

    def test_only_the_newest_version_of_an_activity_counts(self):
        # Newest first in the archive: order in the zip means nothing.
        got = self.items(event("ARN-1", "2026-09-20", cancelled="Yes"),
                         event("ARN-1", "2026-09-01"))
        self.assertEqual(got, [])

    def test_skips_and_scaffolding_are_not_closures(self):
        got = self.items(event("ARN-2", "2026-09-01", activity_type="skips",
                               activity_type_details=None,
                               activity_name="Skip",
                               traffic_management_type="no_carriageway_"
                                                       "incursion"))
        self.assertEqual(got, [])

    def test_an_ended_activity_is_not_published(self):
        self.assertEqual(self.items(event("ARN-1", "2026-09-01"),
                                    today="2026-11-06"), [])

    def test_a_closure_miles_along_the_same_street_is_not_this_byways(self):
        far = event("ARN-3", "2026-09-01",
                    activity_coordinates="POINT(383000 430000)")
        self.assertEqual(self.items(far), [])

    def test_another_streets_closure_is_not_this_byways(self):
        self.assertEqual(self.items(event("ARN-4", "2026-09-01",
                                          usrn="999")), [])


class Fetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.usrn = os.path.join(self.tmp, "byway-usrn.json")
        with open(self.usrn, "w") as fh:
            json.dump({"usrns": TABLE, "matched": 1}, fh)
        self.out = os.path.join(self.tmp, "orders", "street-manager.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_months_are_read_and_a_missing_one_is_not_fatal(self):
        asked = []

        class Client(object):
            def get(self, url):
                asked.append(url)
                if url.endswith("/09.zip"):
                    return archive(event("ARN-1", "2026-09-01"))
                raise FetchFailed("HTTP 404 for %s" % url)

        data = sm.fetch(Client(), "2026-10-07", 2, usrn_file=self.usrn,
                        out=self.out, byways=Byways([BYWAY]),
                        log=lambda *_: None)
        self.assertEqual([u.rsplit("/", 2)[-2:] for u in asked],
                         [["2026", "09.zip"], ["2026", "08.zip"]])
        self.assertEqual(data["source"]["kind"], "street-manager")
        self.assertEqual(data["source"]["authorities"], ["Lancashire"])
        with open(self.out) as fh:
            self.assertEqual(len(json.load(fh)["items"]), 1)

    def test_nothing_readable_leaves_the_last_file_alone(self):
        class Down(object):
            def get(self, url):
                raise FetchFailed("HTTP 503")

        with self.assertRaises(FetchFailed):
            sm.fetch(Down(), "2026-10-07", 2, usrn_file=self.usrn,
                     out=self.out, byways=Byways([BYWAY]),
                     log=lambda *_: None)
        self.assertFalse(os.path.exists(self.out))

    def test_the_order_build_reads_what_this_writes(self):
        import council_orders
        data = sm.fetch(
            type("C", (), {"get": lambda self, url: archive(
                event("ARN-1", "2026-09-01"))})(),
            "2026-10-07", 1, usrn_file=self.usrn, out=self.out,
            byways=Byways([BYWAY]), log=lambda *_: None)
        feature = council_orders.feature_of(data["items"][0], data["source"],
                                            "2026-11-05")
        self.assertIsNotNone(feature)
        props = feature["properties"]
        self.assertEqual(props["source"], "street-manager")
        self.assertEqual(props["start"], "2026-11-05")
        self.assertEqual(council_orders.PRECEDENCE["street-manager"],
                         max(council_orders.PRECEDENCE.values()))


if __name__ == "__main__":
    unittest.main(verbosity=1)

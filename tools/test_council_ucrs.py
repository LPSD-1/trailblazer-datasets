#!/usr/bin/env python3
"""Unsurfaced unclassified roads: read from the council by its own rules,
kept last-good, published as their own class in their own table and tile
layer - never where an app before 119 would draw them red - and matched by
the closures pipeline like any byway.

    python tools/test_council_ucrs.py

No network. Real fixtures: tools/fixtures/devon-ucr/cat12-subset.json is a
subset of Devon County Council's CAT 12 layer as its /query answers (British
National Grid), and closure-notices.json four of Devon's closure pages, both
read 8 October 2026. The build half needs `cryptography` (exit 2 without it,
as the repository's other pack tests do).
"""
import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import council_ucrs as cu  # noqa: E402
import local_rules as lr  # noqa: E402
from polite_http import FetchFailed, blocked, check_read_only, host_of  # noqa

try:
    import build_packages as P  # noqa: E402
    import build_map_container as B  # noqa: E402
    import build_containers as C  # noqa: E402
except (ImportError, SystemExit):
    print("BLIND: the build half needs cryptography (pip install "
          "cryptography)")
    sys.exit(2)

import byway_match as BM  # noqa: E402
import check_containers as CC  # noqa: E402
import council_orders as CO  # noqa: E402
import council_sources as CS  # noqa: E402
from test_mvt import decode_tile  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures", "devon-ucr")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


LAYER = cu.by_code()["DN"]
FEATURES = _fixture("cat12-subset.json")["features"]
SOURCE = dict(cu.public(LAYER), since="2026-10-08")


def routes():
    return cu.routes_of(copy.deepcopy(FEATURES), LAYER["rules"])


def route(parish, number):
    return next(r for r in routes()
                if (r["parish"], r["number"]) == (parish, number))


def ucr_lane(parish, number):
    return P.normalise_ucr(route(parish, number), SOURCE, "DN", "Devon")


def boat(uid, lines, authority="Devon", name=None):
    return {"type": "Feature",
            "properties": {"lane_uid": uid, "class": "boat",
                           "name": name or "Byway open to all traffic "
                                           "(BOAT) Abbotsham 3",
                           "authority": authority, "county": authority,
                           "authorityCode": "DN", "legal_tier": "statutory",
                           "source": "rowmaps:devon", "source_date":
                           "2026-10-08", "motorbike_ok": 1,
                           "fourxfour_ok": 1, "access_reason": "a byway",
                           "access_evidence": "statutory",
                           "rowType": "byway_open_to_all_traffic",
                           "lengthKm": 0.5},
            "geometry": {"type": "LineString", "coordinates": lines[0]}
            if len(lines) == 1 else {"type": "MultiLineString",
                                     "coordinates": lines}}


# A byway a few kilometres from Abbotsham 301, nowhere near any fixture road.
FAR_BOAT = boat("DN-3-0000000001", [[[-4.20, 51.00], [-4.19, 51.005]]])


class ReadingRules(unittest.TestCase):
    """Devon's rule, applied to the council's own records."""

    def test_one_route_per_parish_and_number_and_nothing_else(self):
        got = [(r["parish"], r["number"]) for r in routes()]
        self.assertEqual(got, [
            ("Abbotsham", "301"), ("Berry Pomeroy", "309"),
            ("Brixton", "303"), ("Coffinswell", "303"), ("Cotleigh", "305"),
            ("Dean Prior", "304"), ("Ermington", "306"),
            ("Littlehempston", "307")])

    def test_a_record_of_another_status_is_not_a_road(self):
        # OBJECTID 999999 is the fixture's one synthetic record: a BOAT.
        ids = [o for r in routes() for o in r["objectids"]]
        self.assertNotIn(999999, ids)

    def test_status_is_read_without_regard_to_case(self):
        # Ermington 306 is spelt 'Uucr' in the layer, one row of 1,143.
        self.assertEqual(route("Ermington", "306")["objectids"], [337])

    def test_a_uuct_record_is_a_road_too(self):
        # Littlehempston 307: one section 'uUCR', one 'uUCT'.
        self.assertEqual(route("Littlehempston", "307")["objectids"],
                         [522, 523])

    def test_every_section_of_a_route_is_one_route(self):
        r = route("Brixton", "303")
        self.assertEqual(r["objectids"], [129, 130, 131])
        self.assertEqual(len(r["lines"]), 3)

    def test_the_councils_name_reads_as_a_rider_would_say_it(self):
        self.assertEqual(route("Abbotsham", "301")["name"], "Rocky Lane")
        # The first section's name; the two after it have none.
        self.assertEqual(route("Brixton", "303")["name"], "Brixton Coombe")
        self.assertEqual(route("Littlehempston", "307")["name"],
                         "Bittam's Lane")
        self.assertEqual(route("Cotleigh", "305")["name"],
                         "By ALAN BRIGHT'S SAWMILL")

    def test_unnamed_and_blank_are_no_name(self):
        self.assertEqual(route("Dean Prior", "304")["name"], "")
        self.assertEqual(route("Berry Pomeroy", "309")["name"], "")

    def test_capitals_become_title_case_keeping_the_apostrophe_s_small(self):
        self.assertEqual(cu.road_name("BITTAM'S LANE"), "Bittam's Lane")
        self.assertEqual(cu.road_name("LEY-HILL LANE"), "Ley-Hill Lane")
        self.assertEqual(cu.road_name("UNNAMED", ("UNNAMED",)), "")
        self.assertEqual(cu.road_name("  ", ("",)), "")

    def test_lines_are_wgs84_in_devon(self):
        for r in routes():
            for line in r["lines"]:
                for lon, lat in line:
                    self.assertTrue(-4.8 < lon < -2.8 and 50.1 < lat < 51.3,
                                    (r["parish"], lon, lat))


class _Client(object):
    """Answers an ArcGIS /query from the fixture, in two pages."""

    def __init__(self, features):
        self.features = features
        self.urls = []

    def get_json(self, url):
        check_read_only(url)
        self.urls.append(url)
        offset = int(url.split("resultOffset=")[1].split("&")[0])
        page = self.features[offset:offset + 7]
        return {"features": copy.deepcopy(page),
                "exceededTransferLimit": offset + 7 < len(self.features)}


class Fetch(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.layer = dict(LAYER, min_records=5)

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def _fetch(self, read, today="2026-10-08"):
        cu.UCR_LAYERS[:] = [self.layer]
        try:
            return cu.fetch(out_dir=self.out, today=today, client=object(),
                            read=read)
        finally:
            cu.UCR_LAYERS[:] = [LAYER]

    def _held(self):
        with open(os.path.join(self.out, "DN.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_real_reader_pages_through_query_with_the_rule(self):
        client = _Client(FEATURES)
        records, got = cu.read_layer(client, LAYER)
        self.assertEqual(records, len(FEATURES))
        self.assertEqual(len(got), 8)
        self.assertEqual(len(client.urls), 2)
        self.assertTrue(all("/MapServer/5/query?" in u for u in client.urls))
        self.assertIn("Status+IN+%28%27uUCR%27%2C%27uUCT%27%29",
                      client.urls[0])
        self.assertNotIn("Comments", client.urls[0])

    def test_a_good_read_is_written_with_its_source(self):
        status, failed = self._fetch(lambda c, l: (12, routes()))
        self.assertEqual(failed, [])
        held = self._held()
        self.assertEqual(len(held["routes"]), 8)
        self.assertEqual(held["source"]["council"], "Devon County Council")
        self.assertIn("8 October 2026", held["source"]["licence_note"])
        self.assertIsNone(held["source"]["licence"])
        self.assertEqual(status["DN"]["last_ok"], "2026-10-08")

    def test_an_unchanged_read_keeps_its_since_date(self):
        self._fetch(lambda c, l: (12, routes()), today="2026-10-08")
        status, _ = self._fetch(lambda c, l: (12, routes()),
                                today="2026-10-20")
        self.assertEqual(self._held()["since"], "2026-10-08")
        self.assertEqual(status["DN"]["last_ok"], "2026-10-20")
        self.assertFalse(status["DN"]["changed"])

    def test_a_changed_read_moves_its_since_date(self):
        self._fetch(lambda c, l: (12, routes()), today="2026-10-08")
        fewer = routes()[:-1]
        self._fetch(lambda c, l: (11, fewer), today="2026-10-20")
        self.assertEqual(self._held()["since"], "2026-10-20")

    def test_a_failed_read_keeps_the_last_good_file(self):
        self._fetch(lambda c, l: (12, routes()))

        def down(c, l):
            raise FetchFailed("HTTP 503")
        status, failed = self._fetch(down, today="2026-10-09")
        self.assertEqual(len(self._held()["routes"]), 8)
        self.assertFalse(status["DN"]["ok"])
        self.assertEqual(status["DN"]["last_ok"], "2026-10-08")
        self.assertEqual(status["DN"]["failing_since"], "2026-10-09")
        self.assertIn("503", failed[0])

    def test_a_read_that_collapses_is_refused(self):
        self._fetch(lambda c, l: (12, routes()))
        status, failed = self._fetch(lambda c, l: (12, routes()[:4]),
                                     today="2026-10-09")
        self.assertEqual(len(self._held()["routes"]), 8)
        self.assertIn("a bad read", status["DN"]["error"])

    def test_a_read_under_the_councils_floor_is_refused_first_time_too(self):
        status, _ = self._fetch(lambda c, l: (3, routes()[:2]))
        self.assertFalse(os.path.exists(os.path.join(self.out, "DN.json")))
        self.assertIn("broken read", status["DN"]["error"])

    def test_a_read_with_no_roads_is_refused(self):
        status, _ = self._fetch(lambda c, l: (12, []))
        self.assertFalse(status["DN"]["ok"])


class Layers(unittest.TestCase):
    def test_every_layer_has_what_the_reader_needs(self):
        for layer in cu.UCR_LAYERS:
            for key in ("code", "council", "authority", "what", "url",
                        "where", "rules", "min_records"):
                self.assertIn(key, layer, layer["code"])
            for key in ("status", "parish", "number"):
                self.assertIn(key, layer["rules"], layer["code"])
            self.assertTrue(layer.get("licence") or layer.get("licence_note"),
                            "%s: say what the licence is, or that there is "
                            "none and why it is published" % layer["code"])

    def test_no_layer_is_asked_of_a_blocked_host_or_for_a_write(self):
        for layer in cu.UCR_LAYERS:
            self.assertFalse(blocked(host_of(layer["url"])), layer["code"])
            check_read_only(layer["url"] + "/query?where=1%3D1")

    def test_no_officer_name_is_asked_for(self):
        # Devon's Comments field reads "Unsurfaced - <officer's name>".
        for layer in cu.UCR_LAYERS:
            self.assertNotIn("COMMENT", layer["fields"].upper())

    def test_the_devon_layer_is_credited_and_its_licence_decision_dated(self):
        said = cu.attribution(LAYER)
        self.assertIn("Devon County Council", said)
        self.assertIn("8 October 2026", said)
        self.assertIn("without stating a licence", said)


class Build(unittest.TestCase):
    def test_a_route_is_one_lane_in_the_ucr_class(self):
        lane = ucr_lane("Brixton", "303")
        p = lane["properties"]
        self.assertEqual(lane["geometry"]["type"], "MultiLineString")
        self.assertEqual(len(lane["geometry"]["coordinates"]), 3)
        self.assertEqual(p["class"], "ucr")
        self.assertEqual(p["legal_tier"], "highway_record")
        self.assertEqual(p["access_evidence"], "highway_record")
        self.assertEqual(p["source"], "highway-records:devon-county-council")
        self.assertEqual((p["motorbike_ok"], p["fourxfour_ok"]), (1, 1))
        self.assertEqual(p["authority"], "Devon")
        self.assertIn("Devon County Council's highway records",
                      p["access_reason"])
        self.assertIn("check the signs and any traffic orders",
                      p["access_reason"])
        self.assertIn("Devon County Council", p["attribution"])
        self.assertTrue(p["lane_uid"].startswith("DN-UCR-brixton-303-"))
        self.assertGreater(p["lengthKm"], 0.1)

    def test_a_named_road_is_named_with_its_reference(self):
        self.assertEqual(ucr_lane("Abbotsham", "301")["properties"]["name"],
                         "Rocky Lane (Abbotsham UCR 301)")
        self.assertEqual(ucr_lane("Dean Prior", "304")["properties"]["name"],
                         "Unsurfaced unclassified road (UCR) Dean Prior 304")

    def test_the_id_follows_the_geometry_and_nothing_else(self):
        a, b = ucr_lane("Abbotsham", "301"), ucr_lane("Abbotsham", "301")
        self.assertEqual(a["properties"]["lane_uid"],
                         b["properties"]["lane_uid"])
        moved = route("Abbotsham", "301")
        moved["lines"][0][0][0] += 0.0001
        c = P.normalise_ucr(moved, SOURCE, "DN", "Devon")
        self.assertNotEqual(a["properties"]["lane_uid"],
                            c["properties"]["lane_uid"])

    def test_a_road_lying_on_a_byway_is_left_to_the_byway(self):
        out = tempfile.mkdtemp()
        try:
            self._hold(out)
            on = boat("DN-9-0000000009",
                      route("Abbotsham", "301")["lines"])
            lanes, sources, report = P.ucr_lanes(
                {"DN": "Devon"}, [on, FAR_BOAT], out_dir=out,
                today="2026-10-08", log=lambda *a: None)
            names = [l["properties"]["name"] for l in lanes]
            self.assertNotIn("Rocky Lane (Abbotsham UCR 301)", names)
            self.assertEqual(report["on_byway"],
                             ["Rocky Lane (Abbotsham UCR 301)"])
            self.assertEqual(len(lanes), 7)
            self.assertEqual(sources[0]["count"], 7)
            self.assertEqual(sources[0]["since"], "2026-10-08")
        finally:
            shutil.rmtree(out, ignore_errors=True)

    @staticmethod
    def _hold(out):
        with open(os.path.join(out, "DN.json"), "w") as fh:
            json.dump({"source": cu.public(LAYER), "since": "2026-10-08",
                       "routes": routes()}, fh)
        with open(os.path.join(out, "status.json"), "w") as fh:
            json.dump({"DN": {"last_ok": "2026-10-08"}}, fh)


class Packs(unittest.TestCase):
    """A UCR is sealed in `ucrFeatures`, never in `features`."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._dist = P.dist_dir
        P.dist_dir = lambda: self.tmp
        self.key = os.urandom(32)

    def tearDown(self):
        P.dist_dir = self._dist
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open(self, entry):
        return B.unpack(os.path.join(self.tmp, entry["file"]), self.key)

    def test_roads_ride_in_their_own_member(self):
        roads = [ucr_lane("Abbotsham", "301"), ucr_lane("Brixton", "303")]
        entry = P.write_package("ways", "south-west", "South West", None,
                                [copy.deepcopy(FAR_BOAT)], self.key,
                                "2026-10-08T00:00:00Z", ucrs=roads,
                                ucr_sources=[SOURCE])
        body = self._open(entry)
        self.assertEqual([f["properties"]["class"] for f in body["features"]],
                         ["boat"])
        self.assertEqual([f["properties"]["class"]
                          for f in body["ucrFeatures"]], ["ucr", "ucr"])
        self.assertEqual(body["ucrSources"][0]["count"], 2)
        self.assertEqual(entry["ucrCount"], 2)
        self.assertEqual(entry["laneCount"], 1)
        for f in body["ucrFeatures"]:
            self.assertEqual(f["properties"]["source_date"], "2026-10-08")

    def test_a_pack_without_roads_is_sealed_as_it_always_was(self):
        a = P.write_package("ways", "south-west", "South West", None,
                            [copy.deepcopy(FAR_BOAT)], self.key,
                            "2026-10-08T00:00:00Z")
        body = self._open(a)
        self.assertNotIn("ucrFeatures", body)
        self.assertNotIn("ucrSources", body)
        self.assertNotIn("ucrCount", a)

    def test_a_road_and_a_byway_sharing_an_id_is_refused(self):
        road = ucr_lane("Abbotsham", "301")
        twin = copy.deepcopy(FAR_BOAT)
        twin["properties"]["lane_uid"] = road["properties"]["lane_uid"]
        with self.assertRaises(SystemExit):
            P.write_package("ways", "south-west", "South West", None, [twin],
                            self.key, "2026-10-08T00:00:00Z", ucrs=[road])

    def test_each_road_goes_with_its_authoritys_piece(self):
        other = copy.deepcopy(FAR_BOAT)
        other["properties"]["authority"] = "Cornwall"
        parts = [("Cornwall", [other]), ("Devon", [copy.deepcopy(FAR_BOAT)])]
        got = P.attach_ucrs(parts, [ucr_lane("Abbotsham", "301")])
        self.assertEqual([len(g) for g in got], [0, 1])


def _area(path, features, ucrs, kind="area", rules=None, sources=None):
    zooms = B.AREA_ZOOMS if kind == "area" else (8, 10)
    B.write_container(path, features, kind, zooms, "2026-10-08T00:00:00Z",
                      ucrs=ucrs, ucr_sources=sources, local_rules=rules)


def _layers(path):
    db = sqlite3.connect(path)
    try:
        out = {}
        for (blob,) in db.execute("SELECT tile_data FROM tiles"):
            for layer in decode_tile(blob):
                for f in layer["features"]:
                    out.setdefault(layer["name"], set()).add(
                        (f["props"].get("class"), f["props"].get("lane_uid")))
        return out
    finally:
        db.close()


class Containers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.roads = [ucr_lane("Abbotsham", "301"), ucr_lane("Brixton", "303")]
        self.path = os.path.join(self.tmp, "ways-south-west.tbmap")
        _area(self.path, [copy.deepcopy(FAR_BOAT)], self.roads,
              sources=[dict(SOURCE, count=2)])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self):
        return sqlite3.connect(self.path)

    def test_roads_are_rows_of_their_own_table_and_never_of_ways(self):
        db = self._db()
        self.assertEqual(db.execute("SELECT way_class, COUNT(*) FROM ways "
                                    "GROUP BY 1").fetchall(), [("boat", 1)])
        self.assertEqual(db.execute(
            "SELECT way_class, legal_tier, source, motorbike_ok, "
            "fourxfour_ok FROM ucr_ways ORDER BY way_uid").fetchall(),
            [("ucr", "highway_record", "highway-records:devon-county-council",
              1, 1)] * 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM ucr_ways_bbox")
                         .fetchone()[0], 2)

    def test_the_two_tables_have_the_same_columns_in_the_same_order(self):
        db = self._db()
        cols = lambda t: [r[1:3] for r in db.execute(  # noqa: E731
            "PRAGMA table_info(%s)" % t)]
        self.assertEqual(cols("ways"), cols("ucr_ways"))

    def test_one_id_space_rowid_box_and_tile(self):
        db = self._db()
        ucr_ids = {r[0] for r in db.execute("SELECT rowid FROM ucr_ways")}
        boat_ids = {r[0] for r in db.execute("SELECT rowid FROM ways")}
        self.assertFalse(ucr_ids & boat_ids)
        self.assertEqual(ucr_ids, {B.stable_id(f["properties"]["lane_uid"])
                                   for f in self.roads})

    def test_roads_are_drawn_in_layer_ucr_and_byways_in_lanes(self):
        got = _layers(self.path)
        self.assertEqual({c for c, _ in got["lanes"]}, {"boat"})
        self.assertEqual({c for c, _ in got["ucr"]}, {"ucr"})
        self.assertEqual({u for _, u in got["ucr"]},
                         {f["properties"]["lane_uid"] for f in self.roads})

    def test_the_counts_an_older_app_reads_are_the_byways_alone(self):
        meta = dict(self._db().execute("SELECT key, value FROM meta"))
        self.assertEqual(meta["lane_count"], "1")
        self.assertEqual(meta["way_count"], "1")
        self.assertEqual(json.loads(meta["class_counts"]), {"boat": 1})
        self.assertEqual(json.loads(meta["legal_tier_counts"]),
                         {"statutory": 1})
        self.assertEqual(meta["ucr_count"], "2")
        sources = json.loads(meta["ucr_sources"])
        self.assertEqual(sources[0]["council"], "Devon County Council")
        self.assertEqual(sources[0]["since"], "2026-10-08")

    def test_bounds_take_in_the_roads(self):
        meta = dict(self._db().execute("SELECT key, value FROM meta"))
        west = float(meta["bounds"].split(",")[0])
        self.assertLess(west, -4.25)

    def test_a_container_without_roads_has_no_trace_of_them(self):
        path = os.path.join(self.tmp, "plain.tbmap")
        _area(path, [copy.deepcopy(FAR_BOAT)], None)
        db = sqlite3.connect(path)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        meta = dict(db.execute("SELECT key, value FROM meta"))
        self.assertNotIn("ucr_ways", names)
        self.assertNotIn("ucr_count", meta)
        self.assertNotIn("local_rules", meta)
        self.assertEqual(set(_layers(path)), {"lanes"})

    def test_the_overview_draws_roads_in_their_layer_and_holds_no_rows(self):
        path = os.path.join(self.tmp, "ways-overview.tbmap")
        _area(path, [copy.deepcopy(FAR_BOAT)], self.roads, kind="overview")
        db = sqlite3.connect(path)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("ucr_ways", names)
        self.assertEqual(dict(db.execute("SELECT key, value FROM meta"))
                         ["ucr_count"], "2")
        self.assertEqual({c for c, _ in _layers(path)["ucr"]}, {"ucr"})

    def test_the_gate_passes_it(self):
        problems = []
        meta, found = CC.check_container(self.path, problems)
        CC.check_agreement([(self.path, found)], problems)
        self.assertEqual(problems, [])

    def test_the_gate_refuses_a_road_where_an_older_app_would_draw_it(self):
        # A UCR built as a byway: a row of `ways`, drawn in layer `lanes`.
        path = os.path.join(self.tmp, "wrong.tbmap")
        _area(path, [copy.deepcopy(FAR_BOAT), ucr_lane("Abbotsham", "301")],
              None)
        problems = []
        CC.check_container(path, problems)
        text = " ".join(problems)
        self.assertIn("unsurfaced road(s) in `ways`", text)
        self.assertIn("in the wrong layer", text)


class Matching(unittest.TestCase):
    """The closures pipeline sees UCRs, and keeps them apart from byways."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.road = ucr_lane("Abbotsham", "301")
        path = os.path.join(self.tmp, "ways-south-west.tbmap")
        _area(path, [copy.deepcopy(FAR_BOAT)], [self.road])
        _area(os.path.join(self.tmp, "ways-overview.tbmap"),
              [copy.deepcopy(FAR_BOAT)], [self.road], kind="overview")
        self.pattern = os.path.join(self.tmp, "ways-*.tbmap")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_matcher_reads_the_roads(self):
        byways = BM.load_byways(self.pattern)
        uid = self.road["properties"]["lane_uid"]
        self.assertIn(uid, byways.ways)
        self.assertEqual(byways.ways[uid].way_class, "ucr")
        self.assertEqual((byways.ways[uid].parish, byways.ways[uid].number),
                         ("abbotsham", "301"))
        self.assertEqual(byways.ways["DN-3-0000000001"].way_class, "boat")

    def test_a_closure_drawn_along_a_road_matches_it_by_geometry(self):
        byways = BM.load_byways(self.pattern)
        line = route("Abbotsham", "301")["lines"][0]
        got = byways.match_geometry([line[: len(line) // 2 + 1]],
                                    authorities={"Devon"})
        self.assertEqual([u for u, _s, _m in got],
                         [self.road["properties"]["lane_uid"]])

    def test_definitive_map_processes_do_not_see_roads(self):
        byways = BM.load_byways(self.pattern, include_ucr=False)
        self.assertEqual(list(byways.ways), ["DN-3-0000000001"])

    def test_a_road_reference_matches_only_roads(self):
        ways = [BM.Way("DN-UCR-1", "Devon",
                       "Unsurfaced unclassified road (UCR) Bere Ferrers 306",
                       [[(-4.17, 50.45), (-4.16, 50.45)]], "ucr"),
                BM.Way("DN-306-a", "Devon",
                       "Byway open to all traffic (BOAT) Bere Ferrers 306",
                       [[(-4.15, 50.45), (-4.14, 50.45)]], "boat")]
        byways = BM.Byways(ways)
        self.assertEqual(byways.match_ref("Devon", "Bere Ferrers", "306",
                                          "ucr"), ["DN-UCR-1"])
        self.assertEqual(byways.match_ref("Devon", "Bere Ferrers", "306"),
                         ["DN-306-a"])

    def test_a_road_name_reads_as_its_council_reference(self):
        for name, want in (
                ("Rocky Lane (Abbotsham UCR 301)", ("abbotsham", "301")),
                ("Unsurfaced unclassified road (UCR) Dean Prior 304",
                 ("deanprior", "304")),
                ("Byway open to all traffic (BOAT) Debden 75",
                 ("debden", "75"))):
            self.assertEqual(BM.split_name(name), want, name)
        self.assertEqual(BM.norm_number("uUCR 306"), "306")
        self.assertEqual(BM.norm_number("uUCR306"), "306")


class DevonNotices(unittest.TestCase):
    """Devon's own closure notices for its unsurfaced roads (8 Oct 2026)."""

    PAGES = _fixture("closure-notices.json")["pages"]

    def _read(self):
        pages = self.PAGES

        class Client(object):
            def get_json(self, url):
                return copy.deepcopy(pages)
        return CS.read_devon(Client())

    def test_the_three_road_closures_are_read_with_their_references(self):
        records, items = self._read()
        self.assertEqual(records, 4)
        got = dict((i["ref"], i["refs"]) for i in items)
        self.assertEqual(got, {
            "bere-ferrers-uucr306-temporary-closure":
                [("Bere Ferrers", "306", "ucr")],
            "offwell-uucr301-and-303-temporary-closure":
                [("Offwell", "301", "ucr"), ("Offwell", "303", "ucr")],
            "newton-poppleford-harpford-uucr308-temporary-closure":
                [("Newton Poppleford & Harpford", "308", "ucr")]})

    def test_a_section_14_orders_dates_are_read_from_its_words(self):
        _n, items = self._read()
        bere = next(i for i in items if "bere" in i["ref"])
        self.assertEqual((bere["start"], bere["end"]),
                         ("2026-01-13", "2027-07-17"))
        offwell = next(i for i in items if "offwell" in i["ref"])
        self.assertEqual(offwell["end"], "2028-01-18")

    def test_matched_to_the_roads_and_labelled_road_closed(self):
        _n, items = self._read()
        roads = [BM.Way("DN-UCR-bere-ferrers-306-x", "Devon",
                        "Unsurfaced unclassified road (UCR) Bere Ferrers 306",
                        [[(-4.17, 50.45), (-4.16, 50.45)]], "ucr"),
                 BM.Way("DN-UCR-offwell-301-x", "Devon",
                        "Unsurfaced unclassified road (UCR) Offwell 301",
                        [[(-3.17, 50.79), (-3.16, 50.79)]], "ucr"),
                 BM.Way("DN-UCR-newton-poppleford-and-harpford-308-x",
                        "Devon", "Unsurfaced unclassified road (UCR) "
                        "Newton Poppleford and Harpford 308",
                        [[(-3.28, 50.69), (-3.27, 50.69)]], "ucr"),
                 # A byway of the same parish and number: never closed by a
                 # notice about the road.
                 BM.Way("DN-306-b", "Devon", "Byway open to all traffic "
                        "(BOAT) Bere Ferrers 306",
                        [[(-4.10, 50.45), (-4.09, 50.45)]], "boat")]
        got, unmatched, _review = CS.match(items, BM.Byways(roads), "Devon")
        by_ref = dict((i["ref"], i["ways"]) for i in got)
        self.assertEqual(by_ref["bere-ferrers-uucr306-temporary-closure"],
                         ["DN-UCR-bere-ferrers-306-x"])
        self.assertEqual(
            by_ref["newton-poppleford-harpford-uucr308-temporary-closure"],
            ["DN-UCR-newton-poppleford-and-harpford-308-x"])
        self.assertEqual(unmatched, [])
        for item in got:
            self.assertEqual(item["on"], "ucr")
            self.assertEqual(CO.label_for(item)[2], "Road closed")

    def test_a_byway_closure_keeps_its_byway_label(self):
        self.assertEqual(CO.label_for({"vehicles": "all_users"})[2],
                         "Byway closed")


class LocalRules(unittest.TestCase):
    GOOD = {"id": "dn-test", "kind": "guidance", "effect": "info",
            "title": "t", "summary": "s",
            "applies_to": {"authorities": ["Devon"]},
            "source": {"publisher": "Devon County Council",
                       "url": "https://www.devon.gov.uk/prow/",
                       "checked": "2026-10-08"}}

    def _bad(self, **change):
        rule = copy.deepcopy(self.GOOD)
        for key, value in change.items():
            if value is None:
                rule.pop(key)
            else:
                rule[key] = value
        with self.assertRaises(lr.Invalid):
            lr.validate({"format": 1, "rules": [rule]})

    def test_the_published_file_is_valid_and_cites_every_rule(self):
        rules = lr.load()
        self.assertGreaterEqual(len(rules), 1)
        for r in rules:
            self.assertTrue(r["source"]["url"].startswith("https://"))

    def test_a_rule_must_cite_a_page_say_where_and_mean_something(self):
        self._bad(source=None)
        self._bad(applies_to={})
        self._bad(applies_to={"county": ["Devon"]})
        self._bad(effect="ban")
        self._bad(kind="rumour")
        self._bad(id="Has Spaces")
        self._bad(season={"from": "10-01"})
        self._bad(applies_to={"areas": [[1, 2, 0, 3]]})

    def test_a_page_on_a_blocked_host_cannot_have_been_read(self):
        self._bad(source={"publisher": "Dartmoor National Park Authority",
                          "url": "https://www.dartmoor.gov.uk/x",
                          "checked": "2026-10-08"})

    def test_a_container_carries_only_the_rules_that_could_apply(self):
        rules = lr.validate({"format": 1, "rules": [
            dict(self.GOOD, id="a-devon"),
            dict(self.GOOD, id="b-cumbria",
                 applies_to={"authorities": ["Cumbria"]}),
            dict(self.GOOD, id="c-box", applies_to={
                "areas": [[-4.3, 50.9, -4.2, 51.1]]}),
            dict(self.GOOD, id="d-far-box", applies_to={
                "areas": [[1.0, 52.0, 1.1, 52.1]]}),
            dict(self.GOOD, id="e-roads-only", applies_to={
                "authorities": ["Devon"], "way_classes": ["ucr"]})]})
        here = lr.for_container(rules, "-4.30,50.90,-3.00,51.20", ["Devon"],
                                ["boat", "ucr"])
        self.assertEqual([r["id"] for r in here],
                         ["a-devon", "c-box", "e-roads-only"])
        byways_only = lr.for_container(rules, "-4.30,50.90,-3.00,51.20",
                                       ["Devon"], ["boat"])
        self.assertNotIn("e-roads-only", [r["id"] for r in byways_only])

    def test_build_containers_writes_the_rules_that_apply_into_meta(self):
        tmp = tempfile.mkdtemp()
        dist, P.dist_dir = P.dist_dir, (lambda: tmp)
        try:
            key = os.urandom(32)
            entry = P.write_package(
                "ways", "south-west", "South West", None,
                [copy.deepcopy(FAR_BOAT)], key, "2026-10-08T00:00:00Z",
                ucrs=[ucr_lane("Abbotsham", "301")], ucr_sources=[SOURCE])
            manifest = os.path.join(tmp, "manifest.json")
            with open(manifest, "w") as fh:
                json.dump({"generated": "2026-10-08T00:00:00Z",
                           "dataset": "ways", "packages": [entry]}, fh)
            rules = lr.validate({"format": 1, "rules": [
                self.GOOD, dict(self.GOOD, id="x-cumbria", applies_to={
                    "authorities": ["Cumbria"]})]})
            out = C.build_all(manifest, os.path.join(tmp, "c"), key,
                              root=tmp, rules=rules)
            area = os.path.join(tmp, "c", "ways-south-west.tbmap")
            meta = dict(sqlite3.connect(area).execute(
                "SELECT key, value FROM meta"))
            self.assertEqual([r["id"] for r in json.loads(
                meta["local_rules"])["rules"]], ["dn-test"])
            self.assertEqual(meta["ucr_count"], "1")
            self.assertEqual(out["containers"][0]["ucrCount"], 1)
            # And a build that asks for no rules writes none.
            C.build_all(manifest, os.path.join(tmp, "d"), key, root=tmp)
            meta = dict(sqlite3.connect(os.path.join(
                tmp, "d", "ways-south-west.tbmap")).execute(
                "SELECT key, value FROM meta"))
            self.assertNotIn("local_rules", meta)
        finally:
            P.dist_dir = dist
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)

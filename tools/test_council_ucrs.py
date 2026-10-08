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
import re
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
import council_ways  # noqa: E402
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


def path(uid, lines, row_type="footpath", code="DN", authority="Devon"):
    """A definitive-map way as normalise() makes one, for the NERC test."""
    f = boat(uid, lines, authority=authority)
    f["properties"].update({"rowType": row_type, "authorityCode": code,
                            "class": row_type, "motorbike_ok": 0,
                            "fourxfour_ok": 0})
    return f


# A Devon footpath nowhere near any fixture road: Devon's definitive map is
# in the build, so its roads can be tested (and none of them is on it).
FAR_PATH = path("DN-1-0000000001", [[[-4.10, 50.90], [-4.09, 50.905]]])


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
        # Mixed case, with words in capitals: those words read as words.
        self.assertEqual(route("Cotleigh", "305")["name"],
                         "By Alan Bright's Sawmill")

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
        every = list(cu.UCR_LAYERS)
        cu.UCR_LAYERS[:] = [self.layer]
        try:
            return cu.fetch(out_dir=self.out, today=today, client=object(),
                            read=read)
        finally:
            cu.UCR_LAYERS[:] = every

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

    def test_the_councils_are_asked_at_most_once_every_4_5_seconds(self):
        made = []

        class Polite(object):
            def __init__(self, **kw):
                made.append(kw)
        real = cu.polite_http.PoliteClient
        cu.polite_http.PoliteClient = Polite
        every = list(cu.UCR_LAYERS)
        cu.UCR_LAYERS[:] = [self.layer]
        try:
            cu.fetch(out_dir=self.out, today="2026-10-08",
                     read=lambda c, l: (12, routes()))
        finally:
            cu.polite_http.PoliteClient = real
            cu.UCR_LAYERS[:] = every
        self.assertGreaterEqual(made[0]["min_gap"], 4.5)

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
        # Devon's Comments field reads "Unsurfaced - <officer's name>";
        # Northumberland's layer has Creator, Editor, AGENT_NAME and
        # OWNER_NAME; free-text descriptions can name anybody.
        for layer in cu.UCR_LAYERS:
            for field in layer["fields"].upper().split(","):
                for word in ("COMMENT", "CREATOR", "EDITOR", "AGENT",
                             "OWNER", "OFFICER", "CONTACT", "APPLICANT",
                             "DESCRI", "REMARK", "NOTE"):
                    self.assertNotIn(word, field, layer["code"])
            self.assertNotEqual(layer["fields"].strip(), "*")

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
        self.assertEqual(p["lane_uid"], "DN-UCR-brixton-303")
        self.assertGreater(p["lengthKm"], 0.1)

    def test_a_named_road_is_named_with_its_reference(self):
        self.assertEqual(ucr_lane("Abbotsham", "301")["properties"]["name"],
                         "Rocky Lane (Abbotsham UCR 301)")
        self.assertEqual(ucr_lane("Dean Prior", "304")["properties"]["name"],
                         "Unsurfaced unclassified road (UCR) Dean Prior 304")

    def test_the_id_is_the_councils_reference_and_survives_an_edit(self):
        # A rider's star stays on the road when Devon re-draws a section,
        # adds one or drops one (review, 8 Oct 2026: the id used to hash
        # every line).
        a = ucr_lane("Abbotsham", "301")
        self.assertEqual(a["properties"]["lane_uid"], "DN-UCR-abbotsham-301")
        moved = route("Abbotsham", "301")
        moved["lines"][0][0][0] += 0.0001
        moved["lines"].append([[-4.25, 51.02], [-4.249, 51.021]])
        c = P.normalise_ucr(moved, SOURCE, "DN", "Devon")
        self.assertEqual(c["properties"]["lane_uid"], "DN-UCR-abbotsham-301")
        self.assertEqual(P.ucr_uid("DN", "Newton Poppleford & Harpford",
                                   "308A"),
                         "DN-UCR-newton-poppleford-harpford-308a")
        self.assertEqual(P.ucr_uid("NY", "", "U2686"), "NY-UCR-u2686")

    def test_only_two_routes_that_collide_get_a_disambiguator(self):
        out = tempfile.mkdtemp()
        try:
            one, two = route("Abbotsham", "301"), route("Brixton", "303")
            # "Abbotsham" and "ABBOTSHAM." slug alike: one id, two roads.
            two = dict(two, parish="ABBOTSHAM.", number="301")
            others = [r for r in routes() if r["number"] not in ("301",)
                      or r["parish"] != "Abbotsham"]
            others = [r for r in others if (r["parish"], r["number"]) !=
                      ("Brixton", "303")]
            self._hold(out, [one, two] + others)
            lanes, _s, _r = P.ucr_lanes(
                {"DN": "Devon"}, [FAR_BOAT], [FAR_PATH], out_dir=out,
                today="2026-10-08", log=lambda *a: None)
            ids = sorted(l["properties"]["lane_uid"] for l in lanes)
            self.assertEqual(len(ids), len(set(ids)))
            twins = [i for i in ids if i.startswith("DN-UCR-abbotsham-301")]
            self.assertEqual(len(twins), 2)
            self.assertTrue(all(len(i) > len("DN-UCR-abbotsham-301")
                                for i in twins))
            self.assertIn("DN-UCR-dean-prior-304", ids)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_a_road_lying_on_a_byway_is_left_to_the_byway(self):
        out = tempfile.mkdtemp()
        try:
            self._hold(out)
            on = boat("DN-9-0000000009",
                      route("Abbotsham", "301")["lines"])
            lanes, sources, report = P.ucr_lanes(
                {"DN": "Devon"}, [on, FAR_BOAT], [FAR_PATH], out_dir=out,
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
    def _hold(out, held=None):
        with open(os.path.join(out, "DN.json"), "w") as fh:
            json.dump({"source": cu.public(LAYER), "since": "2026-10-08",
                       "routes": routes() if held is None else held}, fh)
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

    def test_a_closure_matched_to_a_byway_and_a_road_is_not_road_only(self):
        # "on": "ucr" only where EVERY matched way is a road (review nit,
        # 8 Oct 2026): a notice naming Offwell UCR 301 and Byway 5 closes a
        # byway too, and keeps the byway's words.
        ways = BM.Byways([
            BM.Way("DN-UCR-offwell-301", "Devon",
                   "Unsurfaced unclassified road (UCR) Offwell 301",
                   [[(-3.17, 50.79), (-3.16, 50.79)]], "ucr"),
            BM.Way("DN-5-b", "Devon",
                   "Byway open to all traffic (BOAT) Offwell 5",
                   [[(-3.15, 50.79), (-3.14, 50.79)]], "boat")])
        both = {"id": "mixed", "ref": "mixed", "vehicles": "all_users",
                "form": "temporary", "start": "2026-10-01",
                "refs": [("Offwell", "301", "ucr"), ("Offwell", "5", "boat")]}
        road = dict(both, id="road", ref="road",
                    refs=[("Offwell", "301", "ucr")])
        got, unmatched, _r = CS.match([both, road], ways, "Devon")
        by_id = dict((i["id"], i) for i in got)
        self.assertEqual(unmatched, [])
        self.assertEqual(sorted(by_id["mixed"]["ways"]),
                         ["DN-5-b", "DN-UCR-offwell-301"])
        self.assertNotIn("on", by_id["mixed"])
        self.assertEqual(CO.label_for(by_id["mixed"])[2], "Byway closed")
        self.assertEqual(by_id["road"]["on"], "ucr")
        self.assertEqual(CO.label_for(by_id["road"])[2], "Road closed")


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

    def test_the_dales_rules_cover_the_park_and_not_beyond(self):
        rules = dict((r["id"], r) for r in lr.load())
        ring = rules["ydnp-green-lane-driving-and-trail-riding"][
            "applies_to"]["polygon"]

        def inside(lon, lat):
            hit = False
            for i in range(len(ring)):
                (x1, y1), (x2, y2) = ring[i - 1], ring[i]
                if (y1 > lat) != (y2 > lat) and                         lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                    hit = not hit
            return hit
        self.assertTrue(inside(-2.149, 54.072))      # Malham
        self.assertTrue(inside(-2.197, 54.304))      # Hawes
        self.assertFalse(inside(-1.524, 54.137))     # Ripon
        self.assertFalse(inside(-0.92, 54.30))       # the North York Moors

    def test_the_ridgeway_rule_names_roads_the_build_publishes(self):
        rule = next(r for r in lr.load()
                    if r["id"] == "on-roads-meeting-the-ridgeway")
        layer = cu.by_code()["ON"]
        held = cu.held(codes={"ON"}, today="2026-10-08",
                       log=lambda *a: None)[0]
        ids = set(P.normalise_ucr(r, held[0], "ON", "Oxfordshire")
                  ["properties"]["lane_uid"] for r in held[2])
        self.assertEqual(layer["authority"], "Oxfordshire")
        for uid in rule["applies_to"]["way_uids"]:
            self.assertIn(uid, ids)

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



# ------------------------------------------------------------------ review

class Names(unittest.TestCase):
    """Placeholders, numbers and references are no name; the lane falls back
    to "Unsurfaced unclassified road (UCR) <parish> <number>" (review of
    8 Oct 2026: DN.json had 28 routes named "Unknown", 3 "Track" and one
    "204uUCR301")."""

    def test_placeholders_are_no_name(self):
        for raw in ("Unknown", "UNKNOWN", "Track", "UNNAMED Track",
                    "Un-named lane", "?", "N/A", "  "):
            self.assertEqual(cu.road_name(raw), "", raw)

    def test_numbers_and_references_are_no_name(self):
        for raw in ("301", "204uUCR301", "U2686", "D95", "01G200/05",
                    "uUCR 306"):
            self.assertEqual(cu.road_name(raw), "", raw)

    def test_real_names_are_kept(self):
        self.assertEqual(cu.road_name("GREEN LANE"), "Green Lane")
        self.assertEqual(cu.road_name("Back Lane"), "Back Lane")
        self.assertEqual(cu.road_name("UNNAMED ROAD ADJACENT SPARSHOLT FIELD"),
                         "Unnamed Road Adjacent Sparsholt Field")
        self.assertEqual(cu.road_name("A30 link"), "A30 link")

    def test_capitals_in_a_mixed_name_read_as_words_abbreviations_kept(self):
        self.assertEqual(cu.road_name("Track to IVEDON HOUSE"),
                         "Track to Ivedon House")
        self.assertEqual(cu.road_name("Lane by RAF Chivenor"),
                         "Lane by RAF Chivenor")

    def test_a_placeholder_named_road_falls_back_to_its_reference(self):
        r = dict(route("Abbotsham", "301"), name=cu.road_name("Unknown"))
        lane = P.normalise_ucr(r, SOURCE, "DN", "Devon")
        self.assertEqual(lane["properties"]["name"],
                         "Unsurfaced unclassified road (UCR) Abbotsham 301")

    def test_the_published_devon_file_holds_no_placeholder_name(self):
        held = cu.held(codes={"DN"}, today="2026-10-08",
                       log=lambda *a: None)
        for _s, _d, rs in held:
            for r in rs:
                self.assertFalse(r["name"] and cu._is_placeholder(r["name"]),
                                 (r["parish"], r["number"], r["name"]))


class Paging(unittest.TestCase):
    def test_the_read_is_ordered_by_the_object_id(self):
        client = _Client(FEATURES)
        cu.read_layer(client, LAYER)
        self.assertTrue(all("orderByFields=OBJECTID" in u
                            for u in client.urls), client.urls[0])

    def test_a_record_two_pages_both_return_is_read_once(self):
        doubled = copy.deepcopy(FEATURES) + copy.deepcopy(FEATURES[:3])
        records, got = cu.read_layer(_Client(doubled), LAYER)
        self.assertEqual(records, len(FEATURES))
        ids = [o for r in got for o in r["objectids"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(got, routes())


# ------------------------------------------------------------------ NERC

COUNCIL_FIXTURES = os.path.join(HERE, "fixtures", "council-ucrs")


def _council_fixture(name):
    with open(os.path.join(COUNCIL_FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _nerc_paths():
    """The real rowmaps paths on the fixture sections, normalise()d as the
    build makes them."""
    out = []
    for p in _council_fixture("nerc-paths.json")["paths"]:
        f = P.normalise(p["feature"], p["code"], p["code"], p["row_type"])
        if f is not None:
            out.append(f)
    return out


def _lanes_for(code, held_routes, paths, today="2026-10-08"):
    """ucr_lanes over one council's routes, held as the build holds them."""
    out = tempfile.mkdtemp()
    try:
        layer = cu.by_code()[code]
        with open(os.path.join(out, "%s.json" % code), "w") as fh:
            json.dump({"source": cu.public(layer), "since": today,
                       "routes": held_routes}, fh)
        with open(os.path.join(out, "status.json"), "w") as fh:
            json.dump({code: {"last_ok": today}}, fh)
        return P.ucr_lanes({code: layer["authority"]}, [], paths,
                           out_dir=out, today=today, log=lambda *a: None)
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _council_routes(code):
    layer = cu.by_code()[code]
    return cu.routes_of(_council_fixture("%s.json" % code.lower())
                        ["features"], layer["rules"])


def _route_with(code, oid):
    """The fixture route holding object id `oid`, and that section's lines."""
    layer = cu.by_code()[code]
    key = layer["rules"].get("oid") or "OBJECTID"
    feats = _council_fixture("%s.json" % code.lower())["features"]
    f = next(f for f in feats if f["attributes"][key] == oid)
    lines = [[[round(p[0], 5), round(p[1], 5)] for p in l]
             for l in CS.esri_lines(f["geometry"])]
    r = next(r for r in _council_routes(code) if oid in r["objectids"])
    return r, lines


class Nerc(unittest.TestCase):
    """A UCR section the definitive map also records as a footpath,
    bridleway or restricted byway has no motor rights (NERC 2006 s67) and is
    not drawn. Real sections, real rowmaps paths."""

    PATHS = None

    @classmethod
    def setUpClass(cls):
        cls.PATHS = _nerc_paths()

    def _dropped(self, code, oid):
        r, section = _route_with(code, oid)
        lanes, _s, report = _lanes_for(code, _council_routes(code),
                                       self.PATHS)
        drawn = [l for lane in lanes if lane["properties"]["authorityCode"]
                 == code for l in P.lines_of(lane)]
        return section, drawn, report, r

    def test_a_section_on_a_path_is_not_drawn_in_every_council(self):
        cases = {"NY": 767, "NK": 1709, "LL": 1, "ND": 555, "EY": 4072,
                 "ON": 3027, "SU": 4837}
        for code, oid in cases.items():
            section, drawn, report, r = self._dropped(code, oid)
            for line in section:
                self.assertNotIn(line, drawn, (code, oid))
            self.assertTrue(any(c == code for c, *_ in report["on_path"]),
                            code)
            self.assertGreater(report["per_council"][code]
                               ["on_path_sections"], 0, code)

    def test_a_section_partly_along_a_path_is_kept_and_listed(self):
        for code, oid in {"NY": 196, "NK": 940, "LL": 564, "ND": 1241,
                          "EY": 4295, "ON": 13802, "SU": 27497}.items():
            section, drawn, report, r = self._dropped(code, oid)
            for line in section:
                self.assertIn(line, drawn, (code, oid))

    def test_the_other_sections_of_the_road_are_kept(self):
        # Lincolnshire 01G200: section /05 lies on a footpath, /10 does not.
        _s, drawn, _r, r = self._dropped("LL", 1)
        _r2, other = _route_with("LL", 2)
        self.assertIn(other[0], drawn)

    def _bere_ferrers(self, paths):
        """Devon's Bere Ferrers 307: three sections, the 424 m one wholly on
        a public footpath. -> (lanes, report, the footpath section)."""
        bf = _council_fixture("dn-nerc-route.json")["route"]
        on = max(bf["lines"], key=lambda l: council_ways._length(
            [tuple(p) for p in l]))
        lanes, _s, report = _lanes_for("DN", [bf], paths)
        return lanes, report, on, bf

    def test_devons_bere_ferrers_307_footpath_section_is_not_drawn(self):
        lanes, report, on, bf = self._bere_ferrers(self.PATHS)
        self.assertEqual(len(bf["lines"]), 3)
        self.assertEqual(len(lanes), 1)
        self.assertNotIn(on, P.lines_of(lanes[0]))
        self.assertEqual(len(P.lines_of(lanes[0])), 2)
        self.assertEqual([(c, k, m) for c, _n, k, _sh, m in report["on_path"]],
                         [("DN", "footpath", 424)])
        # The road keeps its id with a section gone.
        self.assertEqual(lanes[0]["properties"]["lane_uid"],
                         "DN-UCR-bere-ferrers-307")

    def test_a_path_crossing_a_road_is_not_a_path_on_it(self):
        r = route("Abbotsham", "301")
        line = r["lines"][0]
        mid = line[len(line) // 2]
        across = path("DN-1-x", [[[mid[0] - 0.0003, mid[1] + 0.0003],
                                  [mid[0] + 0.0003, mid[1] - 0.0003]]])
        lanes, _s, report = _lanes_for("DN", [r], [across])
        self.assertEqual(len(lanes), 1)
        self.assertEqual(report["on_path"], [])

    def test_a_council_with_no_definitive_map_in_the_build_is_held_back(self):
        # Only another council's paths: Devon's roads cannot be tested, and
        # are not drawn unchecked.
        lanes, sources, report = _lanes_for(
            "DN", routes(), [path("SM-1-x", [[[-3.0, 51.0], [-3.0, 51.01]]],
                                  code="SM", authority="Somerset")])
        self.assertEqual(lanes, [])
        self.assertEqual(report["unchecked"], ["DN"])
        self.assertEqual(sources, [])

    def test_a_neighbours_path_counts(self):
        # A road on its county boundary may lie on the neighbour's path: the
        # paths of every authority are tested, not the council's alone.
        theirs = [dict(f, properties=dict(f["properties"],
                                          authorityCode="CO"))
                  for f in self.PATHS]
        lanes, report, on, _bf = self._bere_ferrers(theirs + [FAR_PATH])
        self.assertNotIn(on, P.lines_of(lanes[0]))


# --------------------------------------------------------------- councils

class Councils(unittest.TestCase):
    """Each council's rule over a small real subset of its own /query
    answer (tools/fixtures/council-ucrs/<code>.json, read 8 October 2026)."""

    def test_north_yorkshire_routes_are_u_roads_sections_joined(self):
        got = dict((r["number"], r) for r in _council_routes("NY"))
        self.assertEqual(got["U2686"]["objectids"], [2, 4])
        self.assertEqual(len(got["U2686"]["lines"]), 2)
        self.assertTrue(all(re.match(r"^U\d+$", n) for n in got), got.keys())
        self.assertTrue(all(r["parish"] == "" and r["name"] == ""
                            for r in got.values()))
        lane = P.normalise_ucr(got["U2686"], cu.public(cu.by_code()["NY"]),
                               "NY", "North Yorkshire")
        self.assertEqual(lane["properties"]["name"],
                         "Unsurfaced unclassified road (UCR) U2686")
        self.assertEqual(lane["properties"]["lane_uid"], "NY-UCR-u2686")
        self.assertEqual(lane["properties"]["source"],
                         "highway-records:north-yorkshire-council")

    def test_norfolk_named_by_parish_and_road_number(self):
        got = _council_routes("NK")
        k = next(r for r in got if r["objectids"] == [97])
        self.assertEqual((k["parish"], k["number"], k["name"]),
                         ("South Walsham", "59433", "Kingfisher Lane"))
        multi = next(r for r in got if 25616 in r["objectids"])
        self.assertEqual(multi["objectids"], [25616, 25617, 25618])

    def test_lincolnshire_road_is_the_asset_id_before_the_section(self):
        got = _council_routes("LL")
        r = next(r for r in got if 1 in r["objectids"])
        self.assertEqual((r["parish"], r["number"], r["objectids"]),
                         ("Scotton", "01G200", [1, 2]))
        self.assertEqual(r["name"], "Scotton Road")

    def test_northumberland_leaves_out_footpath_and_restricted_sections(self):
        feats = _council_fixture("nd.json")["features"]
        held = set(o for r in _council_routes("ND") for o in r["objectids"])
        for f in feats:
            a = f["attributes"]
            if a["DIVISION_N"] in ("FP", "RH"):
                self.assertNotIn(a["OBJECTID"], held)
            else:
                self.assertIn(a["OBJECTID"], held)
        self.assertTrue(any(f["attributes"]["DIVISION_N"] in ("FP", "RH")
                            for f in feats), "the fixture must hold one")

    def test_east_riding_only_all_vehicles_and_council_maintained(self):
        feats = _council_fixture("ey.json")["features"]
        held = set(o for r in _council_routes("EY") for o in r["objectids"])
        for f in feats:
            a = f["attributes"]
            ok = (a["dedication"] == "All Vehicles"
                  and a["customer_n"].startswith("HW: ERYC")
                  and (a["netwok_pri"] == "GREEN LANE"
                       or a["category_n"] == "6 Unmetalled"))
            self.assertEqual(a["FID"] in held, ok, a)
        r = next(r for r in _council_routes("EY") if 823 in r["objectids"])
        self.assertEqual((r["parish"], r["number"], r["objectids"]),
                         ("Bempton", "45907415", [823, 826]))
        self.assertEqual(r["name"], "Green Lane Track Off Spring Lane")

    def test_oxfordshire_unmetalled_only_and_padding_read_away(self):
        feats = _council_fixture("on.json")["features"]
        held = set(o for r in _council_routes("ON") for o in r["objectids"])
        for f in feats:
            a = f["attributes"]
            self.assertEqual(a["OBJECTID"] in held,
                             a["STREET_SURF"].strip() == "Unmetalled", a)
        r = next(r for r in _council_routes("ON") if 3027 in r["objectids"])
        self.assertEqual((r["parish"], r["number"]),
                         ("Kidmore End", "36204511"))
        q = [r for r in _council_routes("ON") if r["parish"] == "?"]
        self.assertEqual(q, [], "'?' is no place")

    def test_surrey_unclassified_only(self):
        feats = _council_fixture("su.json")["features"]
        held = set(o for r in _council_routes("SU") for o in r["objectids"])
        for f in feats:
            a = f["attributes"]
            self.assertEqual(a["OBJECTID"] in held,
                             a["road_type"] == "Unclassified", a)
        r = next(r for r in _council_routes("SU") if 475 in r["objectids"])
        self.assertEqual((r["number"], r["name"]),
                         ("D262", "Honeysuckle Bottom"))

    def test_every_fixture_is_read_by_the_councils_own_query(self):
        # The where clause the reader sends is the rule the fixture obeys:
        # every record the rule keeps would have been asked for.
        for code in ("NY", "NK", "LL", "ND", "EY", "ON", "SU"):
            layer = cu.by_code()[code]
            client = _Client(_council_fixture("%s.json" % code.lower())
                             ["features"])
            records, got = cu.read_layer(client, layer)
            self.assertTrue(got, code)
            oid = layer["rules"].get("oid") or "OBJECTID"
            self.assertIn("orderByFields=" + oid, client.urls[0])
            for u in client.urls:
                check_read_only(u)
                self.assertTrue(u.startswith(layer["url"] + "/query?"), u)


class Licence(unittest.TestCase):
    def test_every_council_is_credited_and_its_decision_recorded(self):
        for layer in cu.UCR_LAYERS:
            said = cu.attribution(layer)
            self.assertIn(layer["council"], said, layer["code"])
            self.assertIn("8 October 2026", said, layer["code"])
            self.assertIn("take it down if the council objects", said,
                          layer["code"])
            self.assertEqual(layer["decided"], "2026-10-08")

    def test_suffolk_herefordshire_and_worcestershire_are_not_read(self):
        codes = set(l["code"] for l in cu.UCR_LAYERS)
        self.assertEqual(codes, {"DN", "NY", "NK", "LL", "ND", "EY", "ON",
                                 "SU"})
        for l in cu.UCR_LAYERS:
            self.assertNotIn("suffolk", l["url"].lower())
            self.assertNotIn("herefordshire", l["url"].lower())
            self.assertNotIn("worcestershire", l["url"].lower())

    def test_the_manifest_licence_is_not_ogl_over_the_councils_records(self):
        self.assertEqual(P.dataset_licence([]), "OGL-3.0")
        used = [dict(cu.public(cu.by_code()[c]), count=1)
                for c in ("DN", "NY")]
        said = P.dataset_licence(used)
        self.assertTrue(said.startswith("OGL-3.0 for the rights of way"))
        self.assertIn("Devon County Council and North Yorkshire Council",
                      said)
        self.assertIn("without a stated licence", said)
        self.assertIn("8 October 2026", said)
        att = P.dataset_attribution("OGL.", used)
        self.assertTrue(att.startswith("Rights of way: OGL."))
        self.assertIn("North Yorkshire Council", att)
        # No roads: the attribution an older build wrote, byte for byte.
        self.assertEqual(P.dataset_attribution("OGL.", []), "OGL.")


if __name__ == "__main__":
    unittest.main(verbosity=1)

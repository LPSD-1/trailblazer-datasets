#!/usr/bin/env python3
"""What `osm_attributes.py` must never do.

WHAT THIS IS GUARDING. The schema says NULL means unknown and unknown is never
'no'. That rule is one `or 0` away from being broken silently: a way with no
width tag that comes out as `width_m: 0` is a way the 4x4 filter hides from a
rider who could have driven it, and nothing downstream can tell the difference
between "OSM says 0" and "OSM says nothing". Half the checks here exist to make
that substitution fail loudly.

The other half is the matcher. Geometry matching is the part that can be
confidently wrong: a lane matched to the road it crosses takes that road's
surface, its width and its bollards, and every one of those is evidence about
somewhere else. The fixtures below are the shapes that break it - a crossing
road, a parallel road, a lane carried by five OSM ways end to end, and a decoy
shifted 500 m north.

The PBF reader is checked by building a .osm.pbf byte by byte in this file and
reading it back. There is no fixture to download and no network here.

    python test_osm_attributes.py
"""
import io
import json
import math
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import osm_attributes as oa            # noqa: E402

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

LAT0 = 53.0
LON0 = -1.70
M_LON = oa.M_PER_DEG_LAT * math.cos(math.radians(LAT0))


def east(length_m, start_m=0.0, lat=LAT0, lon=LON0, n=6):
    """A straight west-to-east line of `length_m`, starting `start_m` east."""
    return [(lat, lon + (start_m + length_m * i / (n - 1)) / M_LON)
            for i in range(n)]


def north(length_m, at_m=0.0, lat=LAT0, lon=LON0, n=4):
    """A straight south-to-north line crossing the east lines at `at_m`."""
    return [(lat - length_m / 2 / oa.M_PER_DEG_LAT
             + length_m * i / (n - 1) / oa.M_PER_DEG_LAT,
             lon + at_m / M_LON) for i in range(n)]


def our(coords, uid="DY:1"):
    return [{"uid": uid, "name": "test", "county": "Testshire",
             "authority": "DY", "coords": coords}]


def osm(way_id, coords, **tags):
    return {"id": way_id, "tags": dict(tags), "coords": coords}


def barrier(kind, lat, lon, **tags):
    return {"kind": kind, "lat": lat, "lon": lon,
            "tags": dict(tags, barrier=kind)}


def row_of(our_ways, osm_ways, barriers=()):
    rows, stats = oa.run(our_ways, osm_ways, list(barriers))
    return rows[0], stats


# --------------------------------------------------------------------------
# the honesty rule
# --------------------------------------------------------------------------

def test_an_unmatched_way_is_unknown_and_not_narrow():
    # Nothing in OSM anywhere near it.
    row, _s = row_of(our(east(400)), [osm(1, east(400, start_m=5000))])
    for col in ("surface", "smoothness", "tracktype", "width_m",
                "min_width_m", "barriers"):
        check("unmatched way: %s is None" % col, row[col] is None,
              "got %r - a way we could not find is unknown, not narrow "
              "and not clear" % (row[col],))
    check("unmatched way reports its cover", row["match_cover"] < 0.6,
          "cover %r" % row["match_cover"])


def test_a_matched_way_with_no_width_tag_has_no_width():
    row, _s = row_of(our(east(400)),
                     [osm(1, east(400), highway="track", surface="gravel")])
    check("matched way is matched", row["osm_way_ids"] == [1],
          "got %r" % (row["osm_way_ids"],))
    check("surface is read", row["surface"] == "gravel")
    check("width_m is None when OSM does not say",
          row["width_m"] is None, "got %r" % (row["width_m"],))
    check("min_width_m is None when OSM does not say",
          row["min_width_m"] is None, "got %r" % (row["min_width_m"],))
    check("barriers is [] - none mapped, which is not none there",
          row["barriers"] == [], "got %r" % (row["barriers"],))


def test_width_parsing_refuses_to_guess():
    cases = [("3", 3.0), ("3 m", 3.0), ("2.5m", 2.5), ("10'", 3.048),
             ("6 ft", 1.829), ("8'6\"", 2.591), ("2,5", None),
             ("narrow", None), ("", None), (None, None), ("0", None),
             ("999", None), ("~3", None)]
    for raw, want in cases:
        got = oa.parse_width(raw)
        if want is None:
            check("parse_width(%r) is None" % (raw,), got is None,
                  "got %r - an unparseable width is unknown, not a number"
                  % (got,))
        else:
            check("parse_width(%r) == %s" % (raw, want),
                  got is not None and abs(got - want) < 0.01, "got %r" % got)


def test_an_unmeasured_barrier_gap_is_not_a_default_gap():
    check("a bollard with no maxwidth has no gap",
          oa.barrier_gap({"barrier": "bollard"}) is None)
    check("a bollard with maxwidth has one",
          oa.barrier_gap({"barrier": "bollard", "maxwidth": "1.2"}) == 1.2)


def test_the_tool_writes_no_access_column():
    # Step 1.3 reports evidence. Whether a 4x4 may use a lane is decided
    # elsewhere, with that evidence in hand and its provenance recorded -
    # never as a side effect of reading a tag.
    row, _s = row_of(our(east(400)),
                     [osm(1, east(400), highway="track", surface="gravel",
                          width="1.2")])
    for col in ("motorbike_ok", "fourxfour_ok", "access_reason",
                "access_evidence", "way_class", "legal_tier"):
        check("no row carries %s" % col, col not in row,
              "got %r" % (row.get(col),))
    src = io.open(os.path.join(HERE, "osm_attributes.py"),
                  encoding="utf-8").read()
    body = src.split('"""', 2)[2]          # past the module docstring
    for col in ("motorbike_ok", "fourxfour_ok"):
        check("the code below the docstring never mentions %s" % col,
              col not in body)


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def test_a_crossing_road_is_not_a_match():
    # The classic false positive: a road crossing our lane passes within
    # centimetres of it at the junction. Distance alone matches it.
    row, _s = row_of(our(east(600)),
                     [osm(9, north(600, at_m=300), highway="unclassified",
                          surface="asphalt", width="6")])
    check("the crossing road is not matched", not row["osm_way_ids"],
          "matched %r" % (row["osm_way_ids"],))
    check("and it lends us neither its surface nor its width",
          row["surface"] is None and row["width_m"] is None,
          "surface=%r width=%r" % (row["surface"], row["width_m"]))


def test_a_short_lane_crossed_by_a_road_needs_the_bearing_test():
    # The 600 m case above is thrown out by the coverage floor before bearing
    # is consulted. This one is not: our lane is 40 m, so all three of its
    # samples are within 20 m of the road crossing it, coverage is 100%, and
    # the ONLY thing standing between a rider and that road's tarmac, its 6 m
    # width and its bollards is the bearing test.
    row, _s = row_of(our(east(40)),
                     [osm(9, north(600, at_m=20), highway="unclassified",
                          surface="asphalt", width="6")])
    check("a 40 m lane is not the road that crosses it",
          not row["osm_way_ids"], "matched %r" % (row["osm_way_ids"],))
    check("and takes neither its surface nor its width",
          row["surface"] is None and row["width_m"] is None,
          "surface=%r width=%r" % (row["surface"], row["width_m"]))


def test_a_lane_split_into_five_osm_ways_is_one_match():
    parts = [osm(10 + i, east(120, start_m=120 * i), highway="track",
                 surface="gravel") for i in range(5)]
    row, _s = row_of(our(east(600)), parts)
    check("all five parts are matched", len(row["osm_way_ids"]) == 5,
          "matched %r" % (row["osm_way_ids"],))
    check("cover is complete", row["match_cover"] > 0.95,
          "cover %r" % row["match_cover"])
    check("surface comes through", row["surface"] == "gravel")


def test_a_parallel_road_forty_metres_away_is_not_a_match():
    off = 40.0 / oa.M_PER_DEG_LAT
    row, _s = row_of(our(east(600)),
                     [osm(7, [(lat + off, lon) for lat, lon in east(600)],
                          highway="unclassified", width="5")])
    check("a road 40 m away is outside the corridor",
          not row["osm_way_ids"], "matched %r" % (row["osm_way_ids"],))


def test_the_decoy_shift_moves_a_way_off_its_match():
    ways = our(east(600))
    osm_ways = [osm(1, east(600), highway="track", surface="gravel")]
    row, _s = row_of(ways, osm_ways)
    check("the way matches where it is", row["osm_way_ids"] == [1])
    row2, _s2 = row_of(oa.shift_ways(ways, 500.0), osm_ways)
    check("and does not 500 m north of it", not row2["osm_way_ids"],
          "matched %r" % (row2["osm_way_ids"],))


def test_partial_cover_below_the_floor_is_not_a_match():
    # Half a lane found is not a lane found: its attributes describe the half
    # somebody mapped, and we would publish them as the whole.
    row, _s = row_of(our(east(1000)),
                     [osm(1, east(300), highway="track", surface="gravel")])
    check("30%% cover is not a match", not row["osm_way_ids"],
          "cover %r matched %r" % (row["match_cover"], row["osm_way_ids"]))
    check("and carries no attributes", row["surface"] is None)


# --------------------------------------------------------------------------
# attributes off the match
# --------------------------------------------------------------------------

def test_a_barrier_on_the_way_is_carried_and_one_off_it_is_not():
    coords = east(600)
    on_lat, on_lon = coords[2]
    off_lat = on_lat + 200.0 / oa.M_PER_DEG_LAT
    row, _s = row_of(
        our(coords), [osm(1, coords, highway="track")],
        [barrier("bollard", on_lat, on_lon),
         barrier("gate", off_lat, on_lon)])
    kinds = [b["kind"] for b in row["barriers"]]
    check("the bollard on the line is carried", kinds == ["bollard"],
          "got %r" % (kinds,))


def test_a_barrier_further_along_the_same_osm_way_is_not_ours():
    # The OSM way runs 1,200 m; ours is the first 600 m of it. A bollard at
    # 900 m is on that way and not on this lane, and carrying it here would
    # put a hard exclusion on a lane that has no barrier on it at all.
    ours_line = east(600)
    long_way = east(1200)
    far_lat, far_lon = east(1200, n=5)[3]          # 900 m along
    row, _s = row_of(our(ours_line), [osm(1, long_way, highway="track")],
                     [barrier("bollard", far_lat, far_lon)])
    check("the lane matched the long way", row["osm_way_ids"] == [1],
          "got %r" % (row["osm_way_ids"],))
    check("the bollard 300 m past the end is not on this lane",
          row["barriers"] == [], "got %r" % (row["barriers"],))


def test_a_barrier_gap_can_be_the_narrowest_point():
    coords = east(600)
    lat, lon = coords[2]
    row, _s = row_of(our(coords), [osm(1, coords, highway="track", width="4")],
                     [barrier("bollard", lat, lon, maxwidth="1.5")])
    check("width_m is the way's width", row["width_m"] == 4.0,
          "got %r" % (row["width_m"],))
    check("min_width_m is the barrier gap", row["min_width_m"] == 1.5,
          "got %r" % (row["min_width_m"],))


def test_maxwidth_and_width_take_the_smaller():
    row, _s = row_of(our(east(400)),
                     [osm(1, east(400), highway="track", width="4",
                          maxwidth="2")])
    check("the narrower of width and maxwidth wins", row["width_m"] == 2.0,
          "got %r" % (row["width_m"],))


def test_the_dominant_surface_wins_by_length_not_by_count():
    parts = [osm(1, east(500), highway="track", surface="gravel"),
             osm(2, east(100, start_m=500), highway="track",
                 surface="asphalt")]
    row, _s = row_of(our(east(600)), parts)
    check("500 m of gravel beats 100 m of asphalt",
          row["surface"] == "gravel", "got %r" % (row["surface"],))


# --------------------------------------------------------------------------
# coverage arithmetic, which is what the gate is read from
# --------------------------------------------------------------------------

def test_coverage_counts_unknowns_as_unknown():
    rows = [{"match_cover": 1.0, "osm_way_ids": [1], "surface": "gravel",
             "smoothness": None, "tracktype": None, "width_m": None,
             "min_width_m": None, "barriers": []},
            {"match_cover": 0.0, "osm_way_ids": [], "surface": None,
             "smoothness": None, "tracktype": None, "width_m": None,
             "min_width_m": None, "barriers": None}]
    cov = oa.coverage(rows)
    check("matched is 50%", abs(cov["matched_pct"] - 50.0) < 1e-9,
          "got %r" % cov["matched_pct"])
    check("width is 0%", cov["width_pct"] == 0.0)
    check("any physical is 50%", abs(cov["any_physical_pct"] - 50.0) < 1e-9,
          "got %r" % cov["any_physical_pct"])
    check("no way carries a barrier", cov["with_barrier_pct"] == 0.0)


def test_a_way_we_cannot_use_is_named_not_dropped():
    # Kent publishes KT|AT|248 as a LineString holding one coordinate. It
    # cannot be matched to anything, and a run that quietly reports 800 ways
    # where the council published 801 is one nobody will ever reconcile.
    import tempfile
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "properties": {"Name": "KT|AT|248", "Description": "BO|KT:1|0.1"},
         "geometry": {"type": "LineString", "coordinates": [[0.81, 51.08]]}},
        {"type": "Feature",
         "properties": {"Name": "KT|AT|249", "Description": "BO|KT:2|0.4"},
         "geometry": {"type": "LineString",
                      "coordinates": [[lon, lat] for lat, lon in east(400)]}},
        {"type": "Feature",
         "properties": {"Name": "KT|AT|250", "Description": "BO|KT:3|0.4"},
         "geometry": {"type": "Point", "coordinates": [0.8, 51.1]}}]}
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "ways.json")
    with io.open(path, "w", encoding="utf8") as fh:
        fh.write(json.dumps(doc))
    ways, skipped = oa.load_our_ways(path, "Kent")
    check("the usable way is read", len(ways) == 1, "got %d" % len(ways))
    check("its uid is the council's own id",
          ways and ways[0]["uid"] == "KT:2",
          "got %r" % (ways[0]["uid"] if ways else None))
    check("both unusable features are named", len(skipped) == 2,
          "got %r" % (skipped,))
    check("and the one-point line says why",
          any("248" in s and "1 coordinate" in s for s in skipped),
          "got %r" % (skipped,))
    os.remove(path)
    os.rmdir(tmp)


def test_the_designation_basis_admits_what_it_counts():
    # step 0.1's regex is unanchored, so `restricted_byway` matches `byway`.
    tags = ([{"designation": "byway_open_to_all_traffic", "width": "3"}] * 2 +
            [{"designation": "restricted_byway"}] * 6)
    basis = oa.designation_basis(tags)
    check("all eight are counted on 0.1's basis", basis["osm_byways"] == 8,
          "got %r" % basis["osm_byways"])
    check("six of them are restricted byways",
          basis["restricted_byways_included"] == 6,
          "got %r" % basis["restricted_byways_included"])
    check("width is 25% on 0.1's basis",
          abs(basis["width_pct"] - 25.0) < 1e-9, "got %r" % basis["width_pct"])
    check("and 100% on BOATs alone",
          abs(basis["boat_only"]["width_pct"] - 100.0) < 1e-9,
          "got %r" % basis["boat_only"]["width_pct"])


# --------------------------------------------------------------------------
# the PBF reader, against bytes built here
# --------------------------------------------------------------------------

def _v(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _zz(n):
    return (n << 1) ^ (n >> 63) if n < 0 else (n << 1)


def _fld(no, payload):
    return _v(no << 3 | 2) + _v(len(payload)) + payload


def _int_fld(no, value):
    return _v(no << 3 | 0) + _v(value)


def _packed_ints(values):
    return b"".join(_v(v) for v in values)


def build_pbf(path, nodes, ways, strings):
    """A minimal but real .osm.pbf: one OSMHeader blob and one OSMData blob.

    nodes: [(id, lat, lon, [(key_sid, val_sid)])]  ways: [(id, [kv], [refs])]
    """
    table = _fld(1, b"".join(_fld(1, s.encode("utf8")) for s in strings))

    ids, lats, lons, kv = [], [], [], []
    last = [0, 0, 0]
    for nid, lat, lon, tags in nodes:
        rlat = int(round(lat * 1e7))
        rlon = int(round(lon * 1e7))
        ids.append(_zz(nid - last[0]))
        lats.append(_zz(rlat - last[1]))
        lons.append(_zz(rlon - last[2]))
        last = [nid, rlat, rlon]
        for k, val in tags:
            kv.extend([k, val])
        kv.append(0)
    dense = (_fld(1, _packed_ints(ids)) + _fld(8, _packed_ints(lats)) +
             _fld(9, _packed_ints(lons)) + _fld(10, _packed_ints(kv)))
    group = _fld(2, dense)

    for wid, tags, refs in ways:
        body = _int_fld(1, wid)
        body += _fld(2, _packed_ints([k for k, _v2 in tags]))
        body += _fld(3, _packed_ints([v2 for _k, v2 in tags]))
        deltas = []
        prev = 0
        for r in refs:
            deltas.append(_zz(r - prev))
            prev = r
        body += _fld(8, _packed_ints(deltas))
        group += _fld(3, body)

    # granularity 100 means raw values are 100 nanodegrees, i.e. 1e-7 degrees
    block = table + _fld(2, group) + _int_fld(17, 100)

    out = bytearray()
    for btype, payload in ((b"OSMHeader", b""), (b"OSMData", block)):
        comp = zlib.compress(payload)
        blob = _fld(3, comp) + _int_fld(2, len(payload))
        header = _fld(1, btype) + _int_fld(3, len(blob))
        out += struct.pack(">I", len(header)) + header + blob
    with open(path, "wb") as fh:
        fh.write(bytes(out))


def test_the_pbf_reader_reads_a_pbf():
    import tempfile
    strings = ["", "highway", "track", "surface", "gravel", "barrier",
               "bollard", "width", "3"]
    sid = {s: i for i, s in enumerate(strings)}
    coords = east(400, n=4)
    nodes = []
    for i, (lat, lon) in enumerate(coords, start=1):
        tags = []
        if i == 2:
            tags = [(sid["barrier"], sid["bollard"])]
        nodes.append((i, lat, lon, tags))
    ways = [(500, [(sid["highway"], sid["track"]),
                   (sid["surface"], sid["gravel"]),
                   (sid["width"], sid["3"])], [1, 2, 3, 4])]

    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "fixture.osm.pbf")
    build_pbf(path, nodes, ways, strings)

    bbox = (LAT0 - 0.05, LON0 - 0.05, LAT0 + 0.05, LON0 + 0.05)
    got_ways, got_barriers = oa.read_extract(path, bbox)
    check("one way read from the pbf", len(got_ways) == 1,
          "got %d" % len(got_ways))
    if got_ways:
        w = got_ways[0]
        check("its id survived", w["id"] == 500, "got %r" % w["id"])
        check("its tags survived",
              w["tags"] == {"highway": "track", "surface": "gravel",
                            "width": "3"}, "got %r" % (w["tags"],))
        check("all four node positions survived", len(w["coords"]) == 4,
              "got %d" % len(w["coords"]))
        if len(w["coords"]) == 4:
            dlat = abs(w["coords"][0][0] - coords[0][0])
            dlon = abs(w["coords"][0][1] - coords[0][1])
            check("delta and zigzag decoding put it in the right place",
                  dlat < 1e-6 and dlon < 1e-6,
                  "off by %.8f, %.8f" % (dlat, dlon))
    check("the barrier node was read", len(got_barriers) == 1,
          "got %d" % len(got_barriers))
    if got_barriers:
        check("with its kind", got_barriers[0]["kind"] == "bollard",
              "got %r" % got_barriers[0]["kind"])

    # And the whole way round: read the same file through the real path.
    rows, _stats = oa.run(our(coords), got_ways, got_barriers)
    check("the way built from a pbf matches our way",
          rows[0]["osm_way_ids"] == [500], "got %r" % (rows[0]["osm_way_ids"],))
    check("and carries the tags that were in the file",
          rows[0]["surface"] == "gravel" and rows[0]["width_m"] == 3.0,
          "got %r" % (rows[0],))

    designations = oa.extract_designations(path)
    check("a track with no designation is not counted as a byway",
          designations == [], "got %r" % (designations,))

    os.remove(path)
    os.rmdir(tmp)


def test_a_bbox_that_excludes_the_way_returns_nothing():
    import tempfile
    strings = ["", "highway", "track"]
    sid = {s: i for i, s in enumerate(strings)}
    coords = east(400, n=3)
    nodes = [(i, lat, lon, []) for i, (lat, lon) in enumerate(coords, 1)]
    ways = [(500, [(sid["highway"], sid["track"])], [1, 2, 3])]
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "fixture.osm.pbf")
    build_pbf(path, nodes, ways, strings)
    got, _b = oa.read_extract(path, (LAT0 + 1, LON0, LAT0 + 2, LON0 + 1))
    check("a way outside the bbox is not returned", got == [],
          "got %d ways" % len(got))
    os.remove(path)
    os.rmdir(tmp)


# --------------------------------------------------------------------------
# Overpass: the 504 that cost step 0.1 three counties
# --------------------------------------------------------------------------

class _Fake504(object):
    """urlopen that fails with 504 `fails` times, then answers."""

    def __init__(self, fails, payload=None):
        self.fails = fails
        self.calls = 0
        self.payload = payload if payload is not None else {"elements": []}

    def __call__(self, req, timeout=None):
        self.calls += 1
        if self.calls <= self.fails:
            import urllib.error
            raise urllib.error.HTTPError(
                "url", 504, "Gateway Timeout", {}, None)
        body = json.dumps(self.payload).encode()

        class _R(object):
            def read(self_inner):
                return body

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return _R()


def _no_sleep(_s):
    return None


def test_a_504_is_retried_not_lost():
    import tempfile
    import urllib.request
    real_open, real_sleep = urllib.request.urlopen, oa.time.sleep
    cache = tempfile.mkdtemp()
    try:
        fake = _Fake504(2, {"elements": [
            {"type": "way", "id": 1, "tags": {"highway": "track"},
             "geometry": [{"lat": LAT0, "lon": LON0},
                          {"lat": LAT0, "lon": LON0 + 0.001}]}]})
        urllib.request.urlopen = fake
        oa.time.sleep = _no_sleep
        els = oa.overpass_tile((53.0, -1.7, 53.05, -1.65), cache)
        check("two 504s did not lose the tile", len(els) == 1,
              "got %r" % (els,))
        check("it took three attempts", fake.calls == 3,
              "made %d calls" % fake.calls)

        # and the answer is cached, so a re-run costs nothing
        before = fake.calls
        oa.overpass_tile((53.0, -1.7, 53.05, -1.65), cache)
        check("the second read comes from the cache", fake.calls == before,
              "made %d more calls" % (fake.calls - before))
    finally:
        urllib.request.urlopen = real_open
        oa.time.sleep = real_sleep
        for f in os.listdir(cache):
            os.remove(os.path.join(cache, f))
        os.rmdir(cache)


def test_a_tile_that_keeps_failing_is_split_before_it_is_abandoned():
    import tempfile
    import urllib.request
    real_open, real_sleep = urllib.request.urlopen, oa.time.sleep
    cache = tempfile.mkdtemp()
    try:
        fake = _Fake504(4)          # four failures: the whole first round
        urllib.request.urlopen = fake
        oa.time.sleep = _no_sleep
        els = oa.overpass_tile((53.0, -1.7, 53.05, -1.65), cache)
        check("the split eventually answered", els == [], "got %r" % (els,))
        check("it split rather than giving up after four tries",
              fake.calls > 4, "made only %d calls" % fake.calls)
    finally:
        urllib.request.urlopen = real_open
        oa.time.sleep = real_sleep
        for f in os.listdir(cache):
            os.remove(os.path.join(cache, f))
        os.rmdir(cache)


def test_only_the_tiles_our_ways_touch_are_queried():
    ways = our(east(400))
    tiles = oa.tiles_covering(ways, 0.05)
    check("one short lane needs one or two tiles", 1 <= len(tiles) <= 2,
          "got %d" % len(tiles))


# --------------------------------------------------------------------------

def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            try:
                fn()
            except Exception as exc:              # noqa: BLE001
                # A test that raises has still found something. Reporting it
                # as a failure beats a traceback that hides the other 70.
                _failed.append("%s raised %s: %s"
                               % (fn.__name__, type(exc).__name__, exc))
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d checks" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

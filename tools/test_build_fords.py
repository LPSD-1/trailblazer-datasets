#!/usr/bin/env python3
"""Fords, offline.

The geometry fixtures are packed with the REAL `build_map_container
.pack_geometry` rather than a hand-rolled byte string, so `unpack_geometry`
here is tested against the encoder that actually writes containers. A decoder
tested against its own idea of the format is a decoder that agrees with itself.

Overpass is driven through an injected opener; nothing here touches the network.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_fords as F            # noqa: E402
import build_map_container as BMC  # noqa: E402
import build_pois as PO            # noqa: E402

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


WAYS_DDL = """
CREATE TABLE ways (
  way_uid TEXT PRIMARY KEY, way_class TEXT NOT NULL, authority TEXT NOT NULL,
  legal_tier TEXT NOT NULL, source TEXT NOT NULL, source_date TEXT NOT NULL,
  surface TEXT, tracktype TEXT, motorbike_ok INTEGER NOT NULL,
  fourxfour_ok INTEGER NOT NULL, access_reason TEXT NOT NULL,
  access_evidence TEXT NOT NULL, length_m REAL NOT NULL, geometry BLOB NOT NULL
);
CREATE VIRTUAL TABLE ways_bbox USING rtree(id, min_lon, max_lon,
                                           min_lat, max_lat);
"""


def container(path, ways):
    """ways: (uid, [(lon, lat), ...])."""
    db = sqlite3.connect(path)
    db.executescript(WAYS_DDL)
    for rowid, (uid, line) in enumerate(ways, start=1):
        db.execute(
            "INSERT INTO ways (rowid, way_uid, way_class, authority,"
            " legal_tier, source, source_date, motorbike_ok, fourxfour_ok,"
            " access_reason, access_evidence, length_m, geometry)"
            " VALUES (?,?,'boat','Derbyshire','statutory','rowmaps:x',"
            "'2026-01-01',1,1,'BOAT','statutory',500.0,?)",
            (rowid, uid, BMC.pack_geometry([line])))
        lons = [p[0] for p in line]
        lats = [p[1] for p in line]
        db.execute("INSERT INTO ways_bbox VALUES (?,?,?,?,?)",
                   (rowid, min(lons), max(lons), min(lats), max(lats)))
    db.commit()
    db.close()
    return path


def gauge(sid, lat, lon, low=None, high=None, river=None):
    return {"id": sid, "label": sid, "lat": lat, "lon": lon, "river": river,
            "catchment": None, "typical_low_m": low, "typical_high_m": high,
            "measures": ["%s-level" % sid]}


def node(osm_id, lat, lon, **tags):
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon,
            "tags": tags}


# -------------------------------------------------------------- geometry

def test_geometry_round_trips_through_the_real_packer():
    line = [(-1.6000000, 53.0000000), (-1.5990000, 53.0010000),
            (-1.5980000, 53.0005000)]
    back = F.unpack_geometry(BMC.pack_geometry([line]))
    check("one line comes back", len(back) == 1, repr(back))
    check("with every point", len(back[0]) == 3, repr(back[0]))
    worst = max(max(abs(a - b) for a, b in zip(p, q))
                for p, q in zip(line, back[0]))
    check("to the packer's own precision", worst < 1e-7, "worst %g" % worst)


def test_several_lines_round_trip():
    lines = [[(-1.6, 53.0), (-1.59, 53.0)], [(-1.5, 52.9), (-1.49, 52.9)]]
    back = F.unpack_geometry(BMC.pack_geometry(lines))
    check("both lines come back", len(back) == 2, repr(back))


def test_empty_geometry_is_no_lines_not_a_crash():
    check("an empty blob decodes to nothing", F.unpack_geometry(b"") == [])


def test_distance_to_a_line_is_measured_in_metres_not_degrees():
    """A degree of longitude at 53N is ~0.6 of a degree of latitude. Measuring
    in degrees makes an east-west offset look 1.6x further than the same
    distance north-south, which at a 25 m tolerance is the difference between
    finding a ford and losing it."""
    line = [[(-1.60, 53.00), (-1.60, 53.01)]]     # a north-south line
    east = F.distance_to_lines(53.005, -1.5997, line)   # ~20 m east
    line2 = [[(-1.60, 53.00), (-1.59, 53.00)]]    # an east-west line
    north = F.distance_to_lines(53.00018, -1.595, line2)  # ~20 m north
    check("an offset east measures about 20 m", 15 < east < 26,
          "%.1f m" % east)
    check("the same offset north measures about the same", 15 < north < 26,
          "%.1f m" % north)
    check("and the two agree within a few metres", abs(east - north) < 6,
          "%.1f vs %.1f" % (east, north))


# ------------------------------------------------------------ ford_of

def test_a_ford_node_becomes_a_ford():
    """PREMISE for every rejection test below."""
    got = F.ford_of(node(11, 53.0, -1.6, ford="yes", name="Milldale"),
                    "2026-09-24")
    check("it is a ford", got is not None, repr(got))
    check("with a stable uid", got["ford_uid"] == "osm:n11", repr(got))
    check("its name", got["name"] == "Milldale", repr(got))
    check("and the date it was read, per spec 6.4's honesty rule",
          got["source_date"] == "2026-09-24", repr(got))


def test_an_untagged_node_is_not_a_ford():
    check("no ford tag, no ford",
          F.ford_of(node(11, 53.0, -1.6, highway="track"), "x") is None)


def test_the_ford_value_is_kept_verbatim_and_not_flattened():
    """`stepping_stones` is a footpath crossing and `boat` is not a crossing at
    all for a motorcycle. Flattening every value to ford=true would tell a
    rider on a trail bike that a stepping-stone crossing is a ford they can
    ride, which is how a bike ends up in a river."""
    for value in ("yes", "stepping_stones", "boat", "intermittent"):
        got = F.ford_of(node(1, 53.0, -1.6, ford=value), "x")
        check("%s survives as itself" % value, got["ford_tag"] == value,
              repr(got))


def test_a_ford_way_uses_its_centre():
    """Overpass returns `center` for a way; a ford tagged on the way itself
    (a section of road through a stream) has no lat/lon of its own."""
    got = F.ford_of({"type": "way", "id": 99,
                     "center": {"lat": 53.0, "lon": -1.6},
                     "tags": {"ford": "yes"}}, "x")
    check("a ford way is placed at its centre",
          got is not None and got["lat"] == 53.0, repr(got))
    check("and its uid says it is a way", got["ford_uid"] == "osm:w99",
          repr(got))


def test_a_ford_with_no_position_is_dropped():
    check("a ford we cannot place is not carried",
          F.ford_of({"type": "node", "id": 1, "tags": {"ford": "yes"}},
                    "x") is None)


# ------------------------------------------------------- matching to ways

def _db(tmp, ways):
    return sqlite3.connect(container(os.path.join(tmp, "c.tbmap"), ways))


def test_a_ford_on_a_way_finds_that_way():
    """PREMISE for the two rejection tests below."""
    tmp = tempfile.mkdtemp()
    try:
        db = _db(tmp, [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        rowid, metres = F.nearest_way(db, "ways", "ways_bbox", 53.00002, -1.595)
        db.close()
        check("the ford is on way 1", rowid == 1, repr(rowid))
        check("within a couple of metres", metres is not None and metres < 5,
              repr(metres))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_ford_beyond_the_tolerance_is_on_no_way():
    """NULL, not the nearest thing. A ford 200 m from a byway is a ford on
    something else, and claiming it would put a river crossing on a lane that
    does not cross one."""
    tmp = tempfile.mkdtemp()
    try:
        db = _db(tmp, [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        rowid, metres = F.nearest_way(db, "ways", "ways_bbox", 53.0020, -1.595)
        db.close()
        check("a ford 200 m away matches no way", rowid is None, repr(rowid))
        check("and no distance is offered", metres is None, repr(metres))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_bounding_box_alone_would_have_been_wrong():
    """The reason the geometry is decoded at all.

    An L-shaped way's bounding box covers ground the way never touches. A
    matcher using the r-tree box alone attaches the ford in the empty corner to
    that way; the geometry says it is 800 m from the line.
    """
    tmp = tempfile.mkdtemp()
    try:
        elbow = [(-1.60, 53.00), (-1.60, 53.01), (-1.59, 53.01)]
        db = _db(tmp, [("w1", elbow)])
        # The corner the L does not reach, but its box does.
        corner_lat, corner_lon = 53.0005, -1.5905
        inside_box = db.execute(
            "SELECT COUNT(*) FROM ways_bbox WHERE max_lon >= ? AND min_lon <= ?"
            " AND max_lat >= ? AND min_lat <= ?",
            (corner_lon, corner_lon, corner_lat, corner_lat)).fetchone()[0]
        rowid, _m = F.nearest_way(db, "ways", "ways_bbox", corner_lat,
                                  corner_lon)
        db.close()
        check("the r-tree box does contain the corner", inside_box == 1,
              inside_box)
        check("but the geometry refuses it", rowid is None, repr(rowid))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_nearer_of_two_ways_wins():
    tmp = tempfile.mkdtemp()
    try:
        db = _db(tmp, [("far", [(-1.60, 53.0002), (-1.59, 53.0002)]),
                       ("near", [(-1.60, 53.0000), (-1.59, 53.0000)])])
        rowid, _m = F.nearest_way(db, "ways", "ways_bbox", 53.00001, -1.595)
        db.close()
        check("the nearer line is chosen", rowid == 2, repr(rowid))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- the gauge

def test_a_ford_with_a_gauge_carries_the_distance():
    """PREMISE, and spec 9.6 G's whole requirement: the gauge, its distance and
    its timestamp."""
    tmp = tempfile.mkdtemp()
    try:
        db = _db(tmp, [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        fords = [F.ford_of(node(1, 53.00001, -1.595, ford="yes"), "2026-09-24")]
        rows, gauges, counts = F.build_rows(
            db, fords, [gauge("E1", 53.02, -1.595, low=0.2, high=1.0,
                              river="Dove")])
        db.close()
        check("the ford is tied to a gauge", rows[0]["gauge"] == 1,
              repr(rows[0]))
        check("and carries the metres to it",
              rows[0]["gauge_m"] is not None and 1800 < rows[0]["gauge_m"]
              < 2600, repr(rows[0]["gauge_m"]))
        check("the gauge's typical range travels, so 'above normal' is sayable",
              gauges[0]["typical_high_m"] == 1.0, repr(gauges[0]))
        check("and the river's name, which is what the rider reads",
              gauges[0]["river"] == "Dove", repr(gauges[0]))
        check("nothing was counted ungauged", counts["ungauged"] == 0,
              repr(counts))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_ford_with_no_gauge_in_range_carries_null_not_the_far_one():
    """Attaching a gauge 400 km away would have the app announce a river is
    high about a catchment it has never touched."""
    tmp = tempfile.mkdtemp()
    try:
        db = _db(tmp, [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        fords = [F.ford_of(node(1, 53.00001, -1.595, ford="yes"), "2026-09-24")]
        rows, gauges, counts = F.build_rows(db, fords,
                                            [gauge("FAR", 58.0, -4.0)])
        db.close()
        check("no gauge is attached", rows[0]["gauge"] is None, repr(rows[0]))
        check("and no distance is invented", rows[0]["gauge_m"] is None,
              repr(rows[0]))
        check("it is counted", counts["ungauged"] == 1, repr(counts))
        check("and no gauge row is carried", gauges == [], repr(gauges))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_only_the_gauges_used_are_carried():
    tmp = tempfile.mkdtemp()
    try:
        db = _db(tmp, [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        fords = [F.ford_of(node(1, 53.00001, -1.595, ford="yes"), "2026-09-24")]
        stations = [gauge("USED", 53.01, -1.595)] + [
            gauge("KENT%d" % i, 51.2, 1.0 + i * 0.01) for i in range(20)]
        _rows, gauges, _c = F.build_rows(db, fords, stations)
        db.close()
        check("one gauge is carried", len(gauges) == 1, repr(gauges))
        check("the near one", gauges[0]["station_id"] == "USED",
              repr(gauges[0]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- level_state

def test_a_river_above_normal_is_reachable():
    """PREMISE for the unknown cases."""
    state, delta = F.level_state(1.8, 0.2, 1.0)
    check("it is above", state == "above", state)
    check("by 0.8 m - the sentence spec 9.6 G asks for",
          abs(delta - 0.8) < 1e-9, repr(delta))


def test_no_reading_is_unknown_and_never_normal():
    check("a silent gauge is unknown", F.level_state(None, 0.2, 1.0)[0] ==
          "unknown")
    check("and offers no delta", F.level_state(None, 0.2, 1.0)[1] is None)


def test_no_typical_range_is_unknown_and_never_normal():
    """Hundreds of EA level-only sites have no stageScale. Reading an absent
    typical high as 0.0 would call every one of them a flood; reading the state
    as 'normal' would reassure a rider about a crossing nobody has measured."""
    state, delta = F.level_state(1.5, None, None)
    check("a level with no normal to compare to is unknown", state == "unknown",
          state)
    check("and no delta is invented", delta is None, repr(delta))


def test_below_and_normal_measure_against_the_same_reference():
    """A field that silently changed its reference point between states would
    be read wrong by the first person to use it and never noticed, because both
    answers look plausible."""
    _s, below = F.level_state(0.1, 0.2, 1.0)
    _s2, normal = F.level_state(0.5, 0.2, 1.0)
    check("below is measured against the typical high",
          abs(below - (0.1 - 1.0)) < 1e-9, repr(below))
    check("and so is normal", abs(normal - (0.5 - 1.0)) < 1e-9, repr(normal))
    check("below is below", F.level_state(0.1, 0.2, 1.0)[0] == "below")
    check("and inside the range is normal",
          F.level_state(0.5, 0.2, 1.0)[0] == "normal")


def test_the_upper_boundary_is_stated_both_ways():
    check("exactly the typical high is normal, not above",
          F.level_state(1.0, 0.2, 1.0)[0] == "normal")
    check("a centimetre over is above",
          F.level_state(1.01, 0.2, 1.0)[0] == "above")


def test_no_typical_low_does_not_stop_above_being_said():
    """Many stations publish a high and no low. Requiring both would throw away
    the half that matters - a ford is impassable when the river is HIGH."""
    check("a high alone still says above",
          F.level_state(1.8, None, 1.0)[0] == "above")
    check("and nothing is called below without a low",
          F.level_state(0.0, None, 1.0)[0] == "normal")


# ------------------------------------------------------------------ feed

def test_a_reporting_gauge_is_published_with_its_state():
    body = F.feed_body("midlands", [gauge("E1", 53.0, -1.6, low=0.2, high=1.0,
                                          river="Dove")],
                       {"E1": {"latest": 1.8,
                               "latest_at": "2026-09-24T11:45:00Z"}},
                       "2026-09-24T12:00:00Z")
    check("the gauge is published", "E1" in body["stations"], repr(body))
    check("with its state", body["stations"]["E1"]["state"] == "above",
          repr(body["stations"]["E1"]))
    check("its metres above normal",
          abs(body["stations"]["E1"]["vs_typical_high_m"] - 0.8) < 1e-9,
          repr(body["stations"]["E1"]))
    check("and the timestamp spec 9.6 G requires",
          body["stations"]["E1"]["at"] == "2026-09-24T11:45:00Z",
          repr(body["stations"]["E1"]))


def test_a_silent_gauge_is_omitted_rather_than_published_at_zero():
    """Publishing it at 0.0 m would put every silent gauge at the bottom of its
    range - 'the river is low, cross it'."""
    body = F.feed_body("midlands", [gauge("E1", 53.0, -1.6, low=0.2, high=1.0)],
                       {"E1": {"latest": None, "latest_at": None}},
                       "2026-09-24T12:00:00Z")
    check("the silent gauge is not published", body["stations"] == {},
          repr(body["stations"]))


def test_the_feed_can_be_cut_to_the_gauges_fords_actually_use():
    """MEASURED, 2026-09-24: an unfiltered level feed is ~190 kB per region and
    1.1 MB over the six, because a region sees 400-1,600 of England's ~3,600
    level sites and almost none of them speak for a ford. This runs on the fast
    clock, so an unfiltered feed is data a rider pays for four times a day and
    never reads."""
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "c.tbmap"),
                         [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        # PREMISE: with no ford_gauges table the answer is None, which the
        # caller must treat as "publish everything" rather than "publish
        # nothing".
        check("a container with no fords yet returns None, not an empty set",
              F.gauges_in_use(path) is None, repr(F.gauges_in_use(path)))
        db = sqlite3.connect(path)
        fords = [F.ford_of(node(1, 53.00001, -1.595, ford="yes"), "d")]
        rows, gauges, _c = F.build_rows(db, fords,
                                        [gauge("USED", 53.01, -1.595)])
        db.close()
        F.write_fords(path, rows, gauges)
        used = F.gauges_in_use(path)
        check("now it names the gauge in use", used == {"USED"}, repr(used))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_gauge_with_no_typical_range_is_published_as_unknown_not_dropped():
    """The reading is real and worth showing; only the comparison is missing.
    Dropping it would lose a measurement, and calling it normal would invent
    one."""
    body = F.feed_body("midlands", [gauge("E1", 53.0, -1.6)],
                       {"E1": {"latest": 1.5,
                               "latest_at": "2026-09-24T11:45:00Z"}},
                       "2026-09-24T12:00:00Z")
    check("the reading is published", body["stations"]["E1"]["m"] == 1.5,
          repr(body))
    check("but its state is unknown",
          body["stations"]["E1"]["state"] == "unknown", repr(body))
    check("and the typical range is null, not zero",
          body["stations"]["E1"]["typical_high_m"] is None, repr(body))


# ------------------------------------------------------------- the write

def test_written_fords_join_to_their_way_and_gauge():
    """The wiring check. A `fords` table nothing can join back to a way is the
    defect this codebase names as its most common: a complete feature nothing
    calls."""
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "c.tbmap"),
                         [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        db = sqlite3.connect(path)
        fords = [F.ford_of(node(1, 53.00001, -1.595, ford="yes",
                                name="Milldale"), "2026-09-24")]
        rows, gauges, _c = F.build_rows(db, fords,
                                        [gauge("E1", 53.01, -1.595, high=1.0)])
        db.close()
        F.write_fords(path, rows, gauges)
        db = sqlite3.connect(path)
        joined = db.execute(
            "SELECT w.way_uid, f.name, f.way_m, g.station_id, f.gauge_m"
            " FROM fords f JOIN ways w ON w.rowid = f.way_id"
            " JOIN ford_gauges g ON g.id = f.gauge").fetchall()
        boxed = db.execute("SELECT COUNT(*) FROM fords_bbox").fetchone()[0]
        db.close()
        check("the ford reaches its way and its gauge", len(joined) == 1,
              repr(joined))
        check("by name", joined and joined[0][1] == "Milldale", repr(joined))
        check("carrying both distances",
              joined and joined[0][2] is not None and joined[0][4] is not None,
              repr(joined))
        check("and it is in the r-tree, or no map query finds it",
              boxed == 1, boxed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_ford_on_no_way_is_not_carried_by_default():
    """A ford on a B-road is weight a green-laning rider downloads for
    nothing."""
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "c.tbmap"),
                         [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        db = sqlite3.connect(path)
        fords = [F.ford_of(node(1, 53.00001, -1.595, ford="yes"), "d"),
                 F.ford_of(node(2, 53.20000, -1.595, ford="yes"), "d")]
        rows, gauges, counts = F.build_rows(db, fords, [gauge("E1", 53.01,
                                                              -1.595)])
        db.close()
        check("one ford is on our network", counts["on_a_way"] == 1,
              repr(counts))
        check("and one is not", counts["off_our_network"] == 1, repr(counts))
        written = F.write_fords(path, rows, gauges)
        check("only the matched one is written", written == 1, written)
        kept = F.write_fords(path, rows, gauges, keep_off_network=True)
        check("unless asked for both", kept == 2, kept)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_only_gauges_the_written_fords_point_at_are_carried():
    """MEASURED on the real Midlands build, 2026-09-24: 2,709 fords in the box,
    230 on one of our ways, and 501 gauges interned - more than twice what the
    written rows point at. Every unreferenced gauge is a row a rider downloads
    and an id the live feed is then asked to carry."""
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "c.tbmap"),
                         [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        db = sqlite3.connect(path)
        fords = [F.ford_of(node(1, 53.00001, -1.595, ford="yes"), "d"),
                 F.ford_of(node(2, 53.30000, -1.595, ford="yes"), "d")]
        # Two gauges, one beside each ford, so each ford interns its own.
        rows, gauges, counts = F.build_rows(
            db, fords, [gauge("ONLANE", 53.005, -1.595),
                        gauge("OFFLANE", 53.305, -1.595)])
        db.close()
        check("both gauges were interned", counts["gauges"] == 2, repr(counts))
        check("but only one ford is on a way", counts["on_a_way"] == 1,
              repr(counts))
        F.write_fords(path, rows, gauges)
        db = sqlite3.connect(path)
        kept = [r[0] for r in db.execute("SELECT station_id FROM ford_gauges")]
        db.close()
        check("only the referenced gauge is written", kept == ["ONLANE"],
              repr(kept))
        # PREMISE the other way: asked to keep both fords, both gauges stay.
        F.write_fords(path, rows, gauges, keep_off_network=True)
        db = sqlite3.connect(path)
        kept = sorted(r[0] for r in
                      db.execute("SELECT station_id FROM ford_gauges"))
        db.close()
        check("and both are written when both fords are",
              kept == ["OFFLANE", "ONLANE"], repr(kept))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_rebuild_removes_a_gauge_that_is_no_longer_referenced():
    """`INSERT OR REPLACE` keyed on an id renumbered from 1 each build only
    overwrites as far as the NEW build reaches. A rebuild needing FEWER gauges
    - a ford deleted from OSM, a gauge the EA retired - leaves the surplus
    high-numbered rows in the container for ever, and the live feed is then
    asked for a station id nothing publishes any more. That reads to the app as
    a silent gauge, which is the one thing this feature must not fake.

    So the SHRINK is the case, not the swap: two gauges written, then one.
    """
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "c.tbmap"),
                         [("w1", [(-1.60, 53.0000), (-1.59, 53.0000)]),
                          ("w2", [(-1.60, 53.2000), (-1.59, 53.2000)])])
        both = [F.ford_of(node(1, 53.00001, -1.595, ford="yes"), "d"),
                F.ford_of(node(2, 53.20001, -1.595, ford="yes"), "d")]
        stations = [gauge("NEAR1", 53.005, -1.595),
                    gauge("NEAR2", 53.205, -1.595)]
        db = sqlite3.connect(path)
        rows_both, gauges_both, counts = F.build_rows(db, both, stations)
        rows_one, gauges_one, _c = F.build_rows(db, both[:1], stations)
        db.close()
        check("the first build needs two gauges", counts["gauges"] == 2,
              repr(counts))
        F.write_fords(path, rows_both, gauges_both)
        db = sqlite3.connect(path)
        first = sorted(r[0] for r in
                       db.execute("SELECT station_id FROM ford_gauges"))
        db.close()
        # PREMISE: both really were written, so a disappearance below is a
        # deletion rather than a write that never happened.
        check("both gauges were written", first == ["NEAR1", "NEAR2"],
              repr(first))
        F.write_fords(path, rows_one, gauges_one)
        db = sqlite3.connect(path)
        after = sorted(r[0] for r in
                       db.execute("SELECT station_id FROM ford_gauges"))
        db.close()
        check("the rebuild leaves only the gauge still in use",
              after == ["NEAR1"], repr(after))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_feed_publishes_everything_when_a_container_has_no_fords_yet():
    """`gauges_in_use` returns None for a container with no `ford_gauges`, and
    None must mean "publish the lot", not "publish nothing". Treating the two
    the same would empty the river feed for every region on the first run after
    a container rebuild, and an empty feed reads to the app as every gauge
    being silent."""
    tmp = tempfile.mkdtemp()
    try:
        no_fords = container(os.path.join(tmp, "a.tbmap"),
                             [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        stations = {"parameter": "level", "stations": [
            gauge("A", 53.0, -1.6, high=1.0), gauge("B", 53.1, -1.6,
                                                    high=1.0)]}
        readings = {"as_of": "2026-09-24T12:00:00Z", "stations": {
            "A": {"latest": 0.5, "latest_at": "2026-09-24T11:45:00Z"},
            "B": {"latest": 0.6, "latest_at": "2026-09-24T11:45:00Z"}}}
        for name, blob in (("st.json", stations), ("rd.json", readings)):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                json.dump(blob, fh)

        class Args(object):
            region = "midlands"
            stations = os.path.join(tmp, "st.json")
            readings = os.path.join(tmp, "rd.json")
            out = os.path.join(tmp, "out")
            container = no_fords

        F.do_feed(Args(), log=lambda _m: None)
        with open(os.path.join(Args.out, "midlands.json"),
                  encoding="utf-8") as fh:
            body = json.load(fh)
        check("both gauges are published when nothing has fords yet",
              sorted(body["stations"]) == ["A", "B"],
              repr(sorted(body["stations"])))

        # And with a ford pointing at one of them, only that one survives.
        with_fords = container(os.path.join(tmp, "b.tbmap"),
                               [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        db = sqlite3.connect(with_fords)
        fords = [F.ford_of(node(1, 53.00001, -1.595, ford="yes"), "d")]
        rows, gauges, _c = F.build_rows(db, fords,
                                        [gauge("A", 53.0, -1.6, high=1.0)])
        db.close()
        F.write_fords(with_fords, rows, gauges)
        Args.container = with_fords
        F.do_feed(Args(), log=lambda _m: None)
        with open(os.path.join(Args.out, "midlands.json"),
                  encoding="utf-8") as fh:
            body = json.load(fh)
        check("only the referenced gauge is published",
              sorted(body["stations"]) == ["A"], repr(sorted(body["stations"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _written_fords(tmp):
    path = container(os.path.join(tmp, "v.tbmap"),
                     [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
    db = sqlite3.connect(path)
    fords = [F.ford_of(node(1, 53.00001, -1.595, ford="yes"), "d")]
    rows, gauges, _c = F.build_rows(db, fords, [gauge("E1", 53.005, -1.595)])
    db.close()
    F.write_fords(path, rows, gauges)
    return path


def test_a_correctly_written_container_has_no_problems():
    """PREMISE. Every refusal below would pass over a verifier that complained
    about everything."""
    tmp = tempfile.mkdtemp()
    try:
        problems = F.verify_written(_written_fords(tmp))
        check("a freshly written container verifies", problems == [],
              repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_ford_with_no_rtree_row_is_refused():
    """A ford with no box is in the file and invisible to every map query -
    the exact fault `validate_container.py` was written after."""
    tmp = tempfile.mkdtemp()
    try:
        path = _written_fords(tmp)
        db = sqlite3.connect(path)
        db.execute("DELETE FROM fords_bbox")
        db.commit()
        db.close()
        problems = F.verify_written(path)
        check("the missing box is refused", problems, repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_spare_rtree_row_is_refused_too():
    """The other side of the count check, and the reason it is not redundant
    with the per-ford one: every ford has a box here, but there is a box with
    no ford. It draws a ford on the map that no record explains, and a tap on
    it finds nothing."""
    tmp = tempfile.mkdtemp()
    try:
        path = _written_fords(tmp)
        db = sqlite3.connect(path)
        db.execute("INSERT INTO fords_bbox VALUES (99,-1.6,-1.6,53.0,53.0)")
        db.commit()
        missing = db.execute(
            "SELECT COUNT(*) FROM fords f WHERE NOT EXISTS"
            " (SELECT 1 FROM fords_bbox b WHERE b.id = f.rowid)").fetchone()[0]
        db.close()
        # PREMISE: no ford is MISSING a box, so only the count check can catch
        # this one.
        check("every ford still has its own box", missing == 0, missing)
        problems = F.verify_written(path)
        check("the spare box is refused", problems, repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_build_refuses_to_finish_when_the_verifier_objects():
    """THE WIRING. A verifier the build does not act on is the same defect it
    was written to catch. Forced, because a correctly built container cannot
    produce a real objection - and a wiring test that can never fire is no
    test."""
    tmp = tempfile.mkdtemp()
    real = F.verify_written
    try:
        path = container(os.path.join(tmp, "c.tbmap"),
                         [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        west, south, east, north = PO.REGIONS["midlands"]
        lat, lon = (south + north) / 2.0, (west + east) / 2.0
        cache = os.path.join(tmp, "fords")
        os.makedirs(cache, exist_ok=True)
        with open(F.cache_path(cache, "midlands"), "w", encoding="utf-8") as fh:
            json.dump({"region": "midlands", "fetched_at": "2026-09-24",
                       "elements": [node(1, lat, lon, ford="yes")]}, fh)
        stations = os.path.join(tmp, "st.json")
        with open(stations, "w", encoding="utf-8") as fh:
            json.dump({"stations": [gauge("E1", lat, lon)]}, fh)

        class Args(object):
            region = "midlands"
            cache = os.path.join(tmp, "fords")
            container = path
            in_place = True
            keep = None
            keep_off_network = True
            report = None

        Args.stations = stations
        # PREMISE: with the real verifier this build succeeds.
        check("a good build succeeds",
              F.do_build(Args(), log=lambda _m: None) == 0)
        F.verify_written = lambda _p: ["a deliberate objection"]
        try:
            F.do_build(Args(), log=lambda _m: None)
            check("an objection stops the build", False, "no SystemExit")
        except SystemExit:
            check("an objection stops the build", True)
    finally:
        F.verify_written = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_ford_naming_a_way_that_is_not_here_is_refused():
    tmp = tempfile.mkdtemp()
    try:
        path = _written_fords(tmp)
        db = sqlite3.connect(path)
        db.execute("UPDATE fords SET way_id = 9999")
        db.commit()
        db.close()
        problems = F.verify_written(path)
        check("the dangling way reference is refused",
              any("way" in p for p in problems), repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_ford_naming_a_gauge_that_is_not_here_is_refused():
    tmp = tempfile.mkdtemp()
    try:
        path = _written_fords(tmp)
        db = sqlite3.connect(path)
        db.execute("DELETE FROM ford_gauges")
        db.commit()
        db.close()
        problems = F.verify_written(path)
        check("the dangling gauge reference is refused",
              any("ford_gauges" in p for p in problems), repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_gauge_without_its_distance_is_refused():
    """Spec 9.6 G requires the gauge, its distance and its timestamp. A gauge
    with no distance is a reading the app cannot qualify."""
    tmp = tempfile.mkdtemp()
    try:
        path = _written_fords(tmp)
        db = sqlite3.connect(path)
        db.execute("UPDATE fords SET gauge_m = NULL")
        db.commit()
        db.close()
        problems = F.verify_written(path)
        check("a gauge with no distance is refused",
              any("distance" in p for p in problems), repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_rebuilding_does_not_double_the_rtree():
    """`fords_bbox` has no primary key, so an INSERT OR REPLACE on `fords`
    would leave the old box behind - and a duplicated r-tree row makes one ford
    appear twice on the map."""
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "c.tbmap"),
                         [("w1", [(-1.60, 53.00), (-1.59, 53.00)])])
        db = sqlite3.connect(path)
        fords = [F.ford_of(node(1, 53.00001, -1.595, ford="yes"), "d")]
        rows, gauges, _c = F.build_rows(db, fords, [gauge("E1", 53.01, -1.595)])
        db.close()
        F.write_fords(path, rows, gauges)
        F.write_fords(path, rows, gauges)
        db = sqlite3.connect(path)
        boxes = db.execute("SELECT COUNT(*) FROM fords_bbox").fetchone()[0]
        fords_n = db.execute("SELECT COUNT(*) FROM fords").fetchone()[0]
        db.close()
        check("one ford after two writes", fords_n == 1, fords_n)
        check("and one box", boxes == 1, boxes)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------- fetch

class _Response(object):
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_fetch_writes_a_sorted_deduped_cache():
    """The box splitter overlaps on its seams, so the same ford comes back from
    two sub-boxes; and an unsorted cache rewrites on every fetch, republishing
    a container whose content did not change."""
    tmp = tempfile.mkdtemp()
    try:
        payload = {"elements": [node(9, 53.0, -1.6, ford="yes"),
                                node(2, 53.1, -1.5, ford="yes"),
                                node(9, 53.0, -1.6, ford="yes")]}

        def opener(_request, timeout=None):
            return _Response(payload)

        class Args(object):
            region = "midlands"
            bbox = None
            cache = os.path.join(tmp, "fords")
            refresh = False

        F.do_fetch(Args(), log=lambda _m: None, opener=opener, pause=0.0)
        with open(F.cache_path(Args.cache, "midlands"), encoding="utf-8") as fh:
            blob = json.load(fh)
        ids = [e["id"] for e in blob["elements"]]
        check("the duplicate is gone", len(ids) == 2, repr(ids))
        check("and the order is stable", ids == sorted(ids, key=lambda i:
                                                       "osm:n%d" % i),
              repr(ids))
        check("the date it was read is recorded", blob["fetched_at"],
              repr(blob))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_cache_is_clipped_to_the_region():
    """A split box is fetched by its own bounds and Overpass returns a way
    whose centre falls outside the box it was asked for. A ford in the next
    region is that region's to publish."""
    tmp = tempfile.mkdtemp()
    try:
        west, south, east, north = PO.REGIONS["midlands"]
        inside = ((south + north) / 2.0, (west + east) / 2.0)
        outside = (north + 2.0, (west + east) / 2.0)
        os.makedirs(os.path.join(tmp, "fords"), exist_ok=True)
        with open(F.cache_path(os.path.join(tmp, "fords"), "midlands"), "w",
                  encoding="utf-8") as fh:
            json.dump({"region": "midlands", "fetched_at": "2026-09-24",
                       "elements": [node(1, inside[0], inside[1], ford="yes"),
                                    node(2, outside[0], outside[1],
                                         ford="yes")]}, fh)
        fords, _when = F.load_cached(os.path.join(tmp, "fords"), "midlands",
                                     PO.REGIONS["midlands"])
        check("the inside ford is kept", len(fords) == 1, repr(fords))
        check("and it is the right one", fords[0]["ford_uid"] == "osm:n1",
              repr(fords))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_missing_cache_is_refused_rather_than_built_empty():
    """An empty ford table looks exactly like a region with no fords."""
    tmp = tempfile.mkdtemp()
    try:
        F.load_cached(os.path.join(tmp, "nope"), "midlands",
                      PO.REGIONS["midlands"])
        check("a missing cache is refused", False, "no SystemExit")
    except SystemExit:
        check("a missing cache is refused", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_selectors_cover_the_values_osm_actually_uses():
    check("ford=yes is fetched", ("ford", "yes") in F.FORD_SELECTORS)
    check("and stepping_stones, which is not rideable",
          ("ford", "stepping_stones") in F.FORD_SELECTORS)
    check("every selector is on the ford key",
          all(k == "ford" for k, _v in F.FORD_SELECTORS),
          repr(F.FORD_SELECTORS))


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    rc = F.selftest(log=lambda msg: _failed.append(msg) if "FAIL" in msg
                    else None)
    check("build_fords --selftest passes", rc == 0)
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d checks" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Wet-weather restraint, end to end, offline.

The container fixtures here are built to `docs/WAYS-SCHEMA.md` rather than
copied from a published file, because every published container is still the
pre-pivot `lanes` shape and carries no `surface` column at all - a suite built
only on those would exercise the "we know nothing" path and never the one that
ships.

BOTH SHAPES ARE TESTED. `test_a_pre_pivot_lanes_container_degrades_honestly`
pins what happens against what is actually on disk today: every way comes back
`unknown`, which is the true answer, and not `hard`.
"""
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_wet as W              # noqa: E402
import ea_flood as EA              # noqa: E402

_passed = 0
_failed = []

NOW_ISO = "2026-09-24T12:00:00Z"


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


# ------------------------------------------------------------- fixtures

WAYS_DDL = """
CREATE TABLE ways (
  way_uid TEXT PRIMARY KEY, way_class TEXT NOT NULL, designation TEXT,
  name TEXT, authority TEXT NOT NULL, county TEXT,
  legal_tier TEXT NOT NULL, source TEXT NOT NULL, source_date TEXT NOT NULL,
  surface TEXT, smoothness TEXT, tracktype TEXT, width_m REAL,
  min_width_m REAL, barriers TEXT, climb_m REAL, sustained_pct REAL,
  motorbike_ok INTEGER NOT NULL, fourxfour_ok INTEGER NOT NULL,
  access_reason TEXT NOT NULL, access_evidence TEXT NOT NULL,
  length_m REAL NOT NULL, geometry BLOB NOT NULL
);
CREATE VIRTUAL TABLE ways_bbox USING rtree(id, min_lon, max_lon,
                                           min_lat, max_lat);
"""

LANES_DDL = """
CREATE TABLE lanes (
  lane_uid TEXT PRIMARY KEY, lane_class TEXT, county TEXT, name TEXT,
  designation TEXT, description TEXT, authority TEXT, vehicle_access TEXT,
  length_m REAL, geometry BLOB
);
CREATE VIRTUAL TABLE lanes_bbox USING rtree(id, min_lon, max_lon,
                                            min_lat, max_lat);
"""


def ways_container(path, rows):
    """rows: (uid, surface, tracktype, lat, lon[, source_date])."""
    db = sqlite3.connect(path)
    db.executescript(WAYS_DDL)
    for rowid, row in enumerate(rows, start=1):
        uid, surface, tracktype, lat, lon = row[:5]
        source_date = row[5] if len(row) > 5 else "2026-01-01"
        db.execute(
            "INSERT INTO ways (rowid, way_uid, way_class, authority,"
            " legal_tier, source, source_date, surface, tracktype,"
            " motorbike_ok, fourxfour_ok, access_reason, access_evidence,"
            " length_m, geometry) VALUES (?,?,'boat','Derbyshire',"
            "'statutory','rowmaps:derbyshire',?,?,?,1,1,'BOAT','statutory',"
            "500.0,X'00')",
            (rowid, uid, source_date, surface, tracktype))
        db.execute("INSERT INTO ways_bbox VALUES (?,?,?,?,?)",
                   (rowid, lon, lon, lat, lat))
    db.commit()
    db.close()
    return path


def lanes_container(path, rows):
    db = sqlite3.connect(path)
    db.executescript(LANES_DDL)
    for rowid, (uid, lat, lon) in enumerate(rows, start=1):
        db.execute("INSERT INTO lanes (rowid, lane_uid, geometry, length_m)"
                   " VALUES (?,?,X'00',1.0)", (rowid, uid))
        db.execute("INSERT INTO lanes_bbox VALUES (?,?,?,?,?)",
                   (rowid, lon, lon, lat, lat))
    db.commit()
    db.close()
    return path


def gauge(sid, lat, lon, label=None):
    return {"id": sid, "label": label or sid, "lat": lat, "lon": lon,
            "river": None, "catchment": None, "typical_low_m": None,
            "typical_high_m": None, "measures": ["%s-rainfall" % sid]}


# -------------------------------------------------------- susceptibility

def test_each_surface_class_is_reachable():
    """PREMISE. Every 'this is not soft' assertion below would pass over a
    classifier that returned `unknown` for everything, so each of the three
    real classes is shown reachable first."""
    check("tarmac is hard", W.susceptibility("asphalt")[0] == "hard")
    check("gravel is firm", W.susceptibility("gravel")[0] == "firm")
    check("mud is soft", W.susceptibility("mud")[0] == "soft")
    check("and the basis says where it came from",
          W.susceptibility("mud")[1] == "surface")
    check("and carries the value verbatim, for the app to show",
          W.susceptibility("mud")[2] == "mud")


def test_an_absent_surface_is_unknown_and_never_hard():
    """The failure this guards is a way with no OSM attributes being advised
    on as if it were tarmac - silence read as a reassurance."""
    klass, basis, value = W.susceptibility(None, None)
    check("nothing known is unknown", klass == "unknown", klass)
    check("the basis says nothing was read", basis == "none", basis)
    check("and no value is invented", value is None, repr(value))
    check("an empty string is the same as absent",
          W.susceptibility("", "")[0] == "unknown")


def test_unpaved_is_not_promoted_to_firm():
    """`unpaved` says only 'not sealed' and spans fine gravel to peat. Calling
    it firm invents drainage the tag never claimed, on the commonest
    non-null surface value in the OSM track network."""
    check("unpaved alone is unknown", W.susceptibility("unpaved")[0] ==
          "unknown")
    check("but tracktype may narrow it",
          W.susceptibility("unpaved", "grade1") == ("hard", "tracktype",
                                                    "grade1"))


def test_tracktype_is_the_fallback_and_surface_wins_where_both_exist():
    check("tracktype answers when surface is silent",
          W.susceptibility(None, "grade5") == ("soft", "tracktype", "grade5"))
    check("surface wins over tracktype",
          W.susceptibility("asphalt", "grade5")[0] == "hard")
    check("and says so", W.susceptibility("asphalt", "grade5")[1] == "surface")


def test_an_unrecognised_surface_falls_through_to_tracktype():
    """OSM's long tail of surface values is mostly spellings of things
    tracktype already grades; dropping straight to `unknown` would throw away
    a real answer we hold."""
    check("an unknown surface string uses tracktype",
          W.susceptibility("mystery_surface", "grade4") ==
          ("soft", "tracktype", "grade4"))
    check("and is unknown when tracktype is silent too",
          W.susceptibility("mystery_surface", None)[0] == "unknown")


def test_case_and_whitespace_do_not_change_the_answer():
    check("upper case still classes",
          W.susceptibility(" ASPHALT ")[0] == "hard")


def test_every_class_in_bands_is_a_class_the_classifier_can_return():
    """A band keyed to a class nothing produces is advice nobody ever sees,
    and a class with no band is a KeyError on a rider's phone."""
    check("BANDS_MM covers exactly CLASSES",
          set(W.BANDS_MM) == set(W.CLASSES),
          "%r vs %r" % (sorted(W.BANDS_MM), sorted(W.CLASSES)))
    produced = set()
    for surface in list(W.SURFACE_CLASS) + [None, "nonsense"]:
        for tracktype in list(W.TRACKTYPE_CLASS) + [None]:
            produced.add(W.susceptibility(surface, tracktype)[0])
    check("and every class it names is producible", produced <= set(W.CLASSES),
          repr(produced - set(W.CLASSES)))


# --------------------------------------------------------------- verdict

def test_a_wet_soft_lane_is_reachable():
    """PREMISE for the None and hard cases below."""
    check("lots of rain on a soft lane is wet",
          W.verdict("soft", 40.0) == "wet")
    check("a little is damp", W.verdict("soft", 6.0) == "damp")
    check("none is dry", W.verdict("soft", 0.0) == "dry")


def test_no_reading_is_unknown_and_never_dry():
    """THE RULE. A gauge that stopped reporting looks exactly like a drought
    if a missing total reads as zero, and the direction of that error is a
    rider told a soft lane is fine after a week of rain."""
    check("no millimetres is unknown", W.verdict("soft", None) == "unknown")
    check("and unknown is not dry", W.verdict("soft", None) != "dry")


def test_a_hard_lane_gets_no_advice_at_all_and_no_reassurance():
    """'na' and not 'dry': advising restraint on a tarmac byway after rain
    would train riders to ignore the whole feature, and telling them it is dry
    would be a claim about grip we never made."""
    check("a hard lane in a deluge is not advised on",
          W.verdict("hard", 400.0) == "na")
    check("and is not told it is dry", W.verdict("hard", 0.0) == "na")


def test_an_unknown_surface_is_advised_as_if_soft():
    """Deliberate, and the direction matters: this is ADVICE, never a
    prohibition (spec 9.6 C), so the cautious side is the right one. `basis`
    travels with it so the app can say 'surface not recorded' rather than
    'this lane is mud'."""
    for mm in (0.0, 6.0, 40.0):
        check("unknown tracks soft at %.0f mm" % mm,
              W.verdict("unknown", mm) == W.verdict("soft", mm))


def test_the_band_edges_are_inclusive_on_the_wetter_side():
    damp, wet = W.BANDS_MM["soft"]
    check("exactly the damp edge is damp", W.verdict("soft", damp) == "damp")
    check("a hair under is dry", W.verdict("soft", damp - 0.01) == "dry")
    check("exactly the wet edge is wet", W.verdict("soft", wet) == "wet")
    check("a hair under is damp", W.verdict("soft", wet - 0.01) == "damp")


def test_a_firm_lane_needs_more_rain_than_a_soft_one():
    """If the two bands ever equalised, the whole 'crossed with surface' claim
    would be false and the feature would be a rainfall overlay - which spec
    9.6 C says explicitly we must not pass off as new."""
    check("firm needs more rain to read damp",
          W.BANDS_MM["firm"][0] > W.BANDS_MM["soft"][0])
    check("and more to read wet",
          W.BANDS_MM["firm"][1] > W.BANDS_MM["soft"][1])
    mm = W.BANDS_MM["soft"][1]
    check("the same rain reads differently on the two surfaces",
          W.verdict("soft", mm) != W.verdict("firm", mm),
          "%s vs %s" % (W.verdict("soft", mm), W.verdict("firm", mm)))


# ------------------------------------------------------------ containers

def test_a_ways_container_yields_its_surfaces():
    """PREMISE for the `lanes` degradation test."""
    tmp = tempfile.mkdtemp()
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"), [
            ("w1", "mud", None, 53.0, -1.6),
            ("w2", "asphalt", None, 53.1, -1.7)])
        db = sqlite3.connect(path)
        rows = W.read_ways(db)
        db.close()
        check("both ways came back", len(rows) == 2, repr(rows))
        check("the surface is read", rows[0][2] == "mud", repr(rows[0]))
        # 1e-5 degrees is ~1 m. SQLite's rtree stores 32-bit floats, so a
        # coordinate read back is never bit-identical to the one written; a
        # metre of slop against a gauge 12 km away is not a distinction.
        check("and the bbox centre places it",
              abs(rows[0][4] - 53.0) < 1e-5 and abs(rows[0][5] + 1.6) < 1e-5,
              repr(rows[0]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_pre_pivot_lanes_container_degrades_honestly():
    """Every published container on disk today is this shape. The right answer
    is 'we do not know', not 'hard' and not a crash - and this is the shape the
    tool will meet in production until the pivot's containers ship."""
    tmp = tempfile.mkdtemp()
    try:
        path = lanes_container(os.path.join(tmp, "l.tbmap"),
                               [("l1", 53.0, -1.6)])
        db = sqlite3.connect(path)
        rows = W.read_ways(db)
        db.close()
        check("the lane is found", len(rows) == 1, repr(rows))
        rows_out, _g, counts = W.assign(rows, [gauge("A", 53.0, -1.6)])
        check("its surface is unknown, not hard",
              rows_out[0]["susceptibility"] == "unknown", repr(rows_out[0]))
        check("and the counts say so", counts["by_class"]["unknown"] == 1,
              repr(counts))
        check("and nothing was claimed to come from a surface tag",
              counts["by_basis"]["surface"] == 0, repr(counts))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_container_with_no_record_table_is_refused():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "empty.tbmap")
        sqlite3.connect(path).execute("CREATE TABLE meta (k TEXT)")
        db = sqlite3.connect(path)
        try:
            W.read_ways(db)
            check("a container with no ways is refused", False, "no SystemExit")
        except SystemExit:
            check("a container with no ways is refused", True)
        finally:
            db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -------------------------------------------------------------- assigning

def test_a_way_with_no_gauge_within_range_carries_null_not_the_far_one():
    """Spec 9.6 G's rule, applied to rain: a gauge 200 km away is not an
    answer. NULL here becomes 'unknown' in `verdict`, which is the only honest
    reading."""
    rows = [(1, "w1", "mud", None, 53.0, -1.6)]
    # PREMISE: a near gauge IS attached, so a NULL is a decision.
    near, _g, _c = W.assign(rows, [gauge("NEAR", 53.01, -1.61)])
    check("a near gauge is attached", near[0]["gauge"] == 1, repr(near[0]))
    check("with a distance under a kilometre",
          near[0]["gauge_m"] < 2000, repr(near[0]))
    far, gauges, counts = W.assign(rows, [gauge("FAR", 58.0, -4.0)])
    check("a gauge 700 km away is not attached", far[0]["gauge"] is None,
          repr(far[0]))
    check("and no distance is invented", far[0]["gauge_m"] is None,
          repr(far[0]))
    check("and it is counted as ungauged", counts["ungauged"] == 1,
          repr(counts))
    check("and no gauge row is carried for it", gauges == [], repr(gauges))


def test_only_the_gauges_actually_used_are_carried():
    """A national station list run against a Welsh container must not put
    gauges in Kent into the Welsh download."""
    rows = [(1, "w1", "mud", None, 53.0, -1.6)]
    stations = [gauge("USED", 53.01, -1.61)] + [
        gauge("KENT%d" % i, 51.2 + i * 0.01, 1.0) for i in range(30)]
    _rows, gauges, counts = W.assign(rows, stations)
    check("one gauge is carried", len(gauges) == 1, repr(gauges))
    check("and it is the near one", gauges[0]["station_id"] == "USED",
          repr(gauges[0]))
    check("the count agrees", counts["gauges"] == 1, repr(counts))


def test_the_gauge_table_carries_what_the_app_must_show():
    """Spec 9.6 G: the gauge, its distance and its timestamp. The app cannot
    say 'the gauge at Ashbourne, 12 km away' from an id alone."""
    rows = [(1, "w1", "mud", None, 53.0, -1.6)]
    _r, gauges, _c = W.assign(rows, [gauge("E7050", 53.01, -1.61,
                                           label="Ashbourne")])
    check("the label travels", gauges[0]["label"] == "Ashbourne",
          repr(gauges[0]))
    check("and the position, so it can be drawn",
          gauges[0]["lat"] is not None and gauges[0]["lon"] is not None,
          repr(gauges[0]))


def test_assignment_is_deterministic():
    """Two builds of one container must give one file, or every rider
    re-downloads a region that did not change."""
    rows = [(i, "w%d" % i, "mud", None, 53.0 + i * 0.01, -1.6)
            for i in range(1, 40)]
    stations = [gauge("S%d" % i, 53.0 + i * 0.02, -1.6) for i in range(20)]
    first = W.assign(rows, stations)
    second = W.assign(rows, stations)
    check("the rows are identical", first[0] == second[0])
    check("and the gauge numbering is identical", first[1] == second[1])


def test_the_written_table_joins_back_to_the_record_by_rowid():
    """The whole point of keying on the rowid: the app already has it from
    `ways_bbox`. If this join ever broke, `way_wetness` would be a table
    nothing could reach - the defect this codebase names as its most common."""
    tmp = tempfile.mkdtemp()
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"), [
            ("w1", "mud", None, 53.0, -1.6),
            ("w2", "asphalt", None, 53.02, -1.62)])
        db = sqlite3.connect(path)
        rows = W.read_ways(db)
        db.close()
        assigned, gauges, _c = W.assign(rows, [gauge("A", 53.0, -1.6)])
        W.write_wetness(path, assigned, gauges)
        db = sqlite3.connect(path)
        joined = dict(db.execute(
            "SELECT w.way_uid, x.susceptibility FROM ways w"
            " JOIN way_wetness x ON x.id = w.rowid"))
        gauge_of = dict(db.execute(
            "SELECT w.way_uid, g.station_id FROM ways w"
            " JOIN way_wetness x ON x.id = w.rowid"
            " JOIN wet_gauges g ON g.id = x.gauge"))
        db.close()
        check("both ways join", len(joined) == 2, repr(joined))
        check("and carry the right class",
              joined == {"w1": "soft", "w2": "hard"}, repr(joined))
        check("and reach their gauge through wet_gauges",
              gauge_of == {"w1": "A", "w2": "A"}, repr(gauge_of))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _written(tmp, ways=(("w1", "mud", None, 53.0, -1.6),)):
    path = ways_container(os.path.join(tmp, "w.tbmap"), list(ways))
    db = sqlite3.connect(path)
    rows = W.read_ways(db)
    db.close()
    assigned, gauges, _c = W.assign(rows, [gauge("A", 53.0, -1.6)])
    W.write_wetness(path, assigned, gauges)
    return path


def test_a_correctly_written_container_has_no_problems():
    """PREMISE. Every refusal below would pass over a verifier that complained
    about everything, so a clean container must come back clean first."""
    tmp = tempfile.mkdtemp()
    try:
        problems = W.verify_written(_written(tmp))
        check("a freshly written container verifies", problems == [],
              repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_way_with_no_wetness_row_is_refused():
    """A way with no row reads as no advice, which on a screen is
    indistinguishable from 'this lane is fine'."""
    tmp = tempfile.mkdtemp()
    try:
        path = _written(tmp, [("w1", "mud", None, 53.0, -1.6),
                              ("w2", "mud", None, 53.01, -1.61)])
        db = sqlite3.connect(path)
        db.execute("DELETE FROM way_wetness WHERE id = 2")
        db.commit()
        db.close()
        problems = W.verify_written(path)
        check("the missing row is refused", problems, repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_row_pointing_at_no_record_is_refused():
    """The defect this codebase names as its most common: a table that is
    present, populated, the right size, and joins to nothing."""
    tmp = tempfile.mkdtemp()
    try:
        path = _written(tmp)
        db = sqlite3.connect(path)
        db.execute("UPDATE way_wetness SET id = 9999")
        db.commit()
        db.close()
        problems = W.verify_written(path)
        check("an unreachable row is refused",
              any("no record" in p for p in problems), repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_gauge_reference_that_resolves_to_nothing_is_refused():
    tmp = tempfile.mkdtemp()
    try:
        path = _written(tmp)
        db = sqlite3.connect(path)
        db.execute("DELETE FROM wet_gauges")
        db.commit()
        db.close()
        problems = W.verify_written(path)
        check("a dangling gauge is refused",
              any("wet_gauges" in p for p in problems), repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_distance_without_a_gauge_is_refused():
    """Showing a reading the app cannot qualify is the one thing spec 9.6 C and
    9.6 G both forbid."""
    tmp = tempfile.mkdtemp()
    try:
        path = _written(tmp)
        db = sqlite3.connect(path)
        db.execute("UPDATE way_wetness SET gauge = NULL")
        db.commit()
        db.close()
        problems = W.verify_written(path)
        check("a distance with no gauge is refused",
              any("distance" in p for p in problems), repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_susceptibility_the_app_has_no_band_for_is_refused():
    tmp = tempfile.mkdtemp()
    try:
        path = _written(tmp)
        db = sqlite3.connect(path)
        db.execute("UPDATE way_wetness SET susceptibility = 'boggy'")
        db.commit()
        db.close()
        problems = W.verify_written(path)
        check("an unknown class is refused",
              any("band" in p for p in problems), repr(problems))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_assign_refuses_to_finish_when_the_verifier_objects():
    """THE WIRING. A verifier the build does not act on is the same defect it
    was written to catch: a complete piece of work nothing calls. The verifier
    is forced to object here, because a correctly built container cannot
    produce a real objection - and a wiring test that can never fire is no
    test."""
    tmp = tempfile.mkdtemp()
    real = W.verify_written
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"),
                              [("w1", "mud", None, 53.0, -1.6)])
        stations = os.path.join(tmp, "st.json")
        with open(stations, "w", encoding="utf-8") as fh:
            json.dump({"stations": [gauge("A", 53.0, -1.6)]}, fh)

        class Args(object):
            container = path
            in_place = True
            keep = None
            report = None

        Args.stations = stations
        # PREMISE: with the real verifier, this build succeeds.
        check("a good build succeeds", W.do_assign(Args(),
                                                   log=lambda _m: None) == 0)
        W.verify_written = lambda _p: ["a deliberate objection"]
        try:
            W.do_assign(Args(), log=lambda _m: None)
            check("an objection stops the build", False, "no SystemExit")
        except SystemExit:
            check("an objection stops the build", True)
    finally:
        W.verify_written = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_writing_twice_does_not_double_the_rows():
    """`assign` runs on the fast clock against a container that may already
    carry the table."""
    tmp = tempfile.mkdtemp()
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"),
                              [("w1", "mud", None, 53.0, -1.6)])
        db = sqlite3.connect(path)
        rows = W.read_ways(db)
        db.close()
        assigned, gauges, _c = W.assign(rows, [gauge("A", 53.0, -1.6)])
        W.write_wetness(path, assigned, gauges)
        W.write_wetness(path, assigned, gauges)
        db = sqlite3.connect(path)
        count = db.execute("SELECT COUNT(*) FROM way_wetness").fetchone()[0]
        gauge_count = db.execute(
            "SELECT COUNT(*) FROM wet_gauges").fetchone()[0]
        db.close()
        check("one row per way after two writes", count == 1, count)
        check("and one gauge row", gauge_count == 1, gauge_count)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ feed

def _readings(**per):
    return {sid: dict({"mm_24h": None, "mm_48h": None, "latest": None,
                       "latest_at": NOW_ISO, "readings": 0}, **spec)
            for sid, spec in per.items()}


def test_a_reporting_gauge_is_in_the_feed():
    """PREMISE for the omission tests."""
    body = W.feed_body("midlands", [gauge("A", 53.0, -1.6)],
                       _readings(A={"mm_24h": 2.0, "mm_48h": 7.5,
                                    "readings": 96}), NOW_ISO)
    check("the gauge is published", "A" in body["stations"], repr(body))
    check("with its 48 hour total",
          body["stations"]["A"]["mm_48h"] == 7.5, repr(body))
    check("and the time of its last reading, so the app can age it",
          body["stations"]["A"]["at"] == NOW_ISO, repr(body))


def test_a_silent_gauge_is_omitted_rather_than_published_as_zero():
    """A gauge with no usable reading must be a MISS in the feed, so the app's
    join falls through to `verdict(mm=None)` = unknown. Publishing it with 0.0
    would turn a broken sensor into a fortnight of dry weather."""
    body = W.feed_body("midlands", [gauge("A", 53.0, -1.6)],
                       _readings(A={"mm_24h": None, "mm_48h": None}), NOW_ISO)
    check("the silent gauge is not in the feed", body["stations"] == {},
          repr(body["stations"]))


def test_a_gauge_with_no_entry_at_all_is_omitted():
    body = W.feed_body("midlands", [gauge("A", 53.0, -1.6)], {}, NOW_ISO)
    check("a gauge the readings never mentioned is omitted",
          body["stations"] == {}, repr(body["stations"]))


def test_a_real_zero_is_published():
    """The other side of the same rule: a gauge that genuinely recorded no rain
    is data, and dropping it would make a dry week look like an outage."""
    body = W.feed_body("midlands", [gauge("A", 53.0, -1.6)],
                       _readings(A={"mm_24h": 0.0, "mm_48h": 0.0,
                                    "readings": 96}), NOW_ISO)
    check("a measured zero is published",
          body["stations"].get("A", {}).get("mm_48h") == 0.0, repr(body))


def test_the_feed_carries_the_bands_so_the_app_cannot_drift():
    """If the thresholds lived only in Dart, a recalibration would need an app
    release and the two would disagree in the meantime."""
    body = W.feed_body("midlands", [], {}, NOW_ISO)
    check("the bands travel with the data",
          set(body["bands_mm"]) == set(W.CLASSES), repr(body["bands_mm"]))
    check("hard carries no band", body["bands_mm"]["hard"] is None,
          repr(body["bands_mm"]))
    check("soft carries two edges", len(body["bands_mm"]["soft"]) == 2,
          repr(body["bands_mm"]))


def test_the_feed_admits_it_is_uncalibrated():
    """BANDS_MM are a stated convention, not a measurement. The app must be
    able to say so rather than presenting them as science."""
    body = W.feed_body("midlands", [], {}, NOW_ISO)
    check("calibrated is false until `calibrate` has been run",
          body["calibrated"] is False, repr(body["calibrated"]))
    check("and the OGL attribution travels", "Open Government" in
          body["licence"] or "OGL" in body["licence"], body["licence"])


def test_the_feed_carries_a_staleness_bound():
    body = W.feed_body("midlands", [], {}, NOW_ISO)
    check("the app is told how long this may be shown",
          body["stale_after_h"] == W.STALE_AFTER_H, repr(body))
    check("which is spec 5.6's six hours", W.STALE_AFTER_H == 6)


def test_clipping_keeps_a_gauge_just_over_the_regional_line():
    """A border way is legitimately served by a gauge two miles the other side
    of the boundary. Clipping hard would leave it ungauged - reported as
    unknown - for no reason but a rectangle."""
    import build_pois as PO
    west, south, east, north = PO.REGIONS["midlands"]
    inside = gauge("IN", (south + north) / 2.0, (west + east) / 2.0)
    just_out = gauge("EDGE", north + 0.1, (west + east) / 2.0)
    far_out = gauge("FAR", north + 3.0, (west + east) / 2.0)
    kept = [s["id"] for s in W.clip([inside, just_out, far_out],
                                    PO.REGIONS["midlands"])]
    check("the inside gauge is kept", "IN" in kept, repr(kept))
    check("and one just over the line", "EDGE" in kept, repr(kept))
    check("but not one three degrees away", "FAR" not in kept, repr(kept))


def test_the_feed_is_small():
    """The reason this is a separate file rather than a container column: it
    must be cheap enough to fetch on the fast clock. 600 gauges is more than
    any region has."""
    stations = [gauge("S%04d" % i, 53.0, -1.6) for i in range(600)]
    body = W.feed_body("midlands", stations,
                       _readings(**{s["id"]: {"mm_24h": 1.25, "mm_48h": 3.5,
                                              "readings": 96}
                                    for s in stations}), NOW_ISO)
    size = len(json.dumps(body))
    check("600 gauges fit in 100 kB of JSON", size < 100000, "%d bytes" % size)


# ------------------------------------------------------------- calibrate

def test_rolling_totals_count_overlapping_windows():
    """A 48-hour total sampled once every 48 hours misses the storm that
    straddled a boundary - which is exactly the window a rider cares about."""
    start = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    samples = [(start + datetime.timedelta(hours=h), 1.0) for h in range(96)]
    stepped = W.rolling_totals(samples, window_h=48, step_h=6)
    coarse = W.rolling_totals(samples, window_h=48, step_h=48)
    check("a 6-hour step gives more windows than a 48-hour one",
          len(stepped) > len(coarse), "%d vs %d" % (len(stepped), len(coarse)))
    check("each window is about 48 mm at 1 mm/h",
          all(46 <= v <= 50 for v in stepped), repr(stepped[:4]))


def test_a_storm_inside_one_window_is_found():
    start = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    samples = [(start + datetime.timedelta(hours=h), 0.0) for h in range(200)]
    samples[100] = (samples[100][0], 30.0)
    totals = W.rolling_totals(samples, window_h=48, step_h=6)
    check("the storm shows in some window", max(totals) == 30.0, repr(max(totals)))
    check("and not in every one", min(totals) == 0.0, repr(min(totals)))


def test_no_samples_gives_no_windows_and_no_percentiles():
    """Empty in, empty out. A zero here would say 'it never rains', which is
    what an unmonitored station would then look like."""
    check("no samples, no windows", W.rolling_totals([]) == [])
    check("no values, no percentiles", W.percentiles([]) == {})
    check("no distribution, no band position",
          W.band_position(W.BANDS_MM, []) == {})


def test_percentiles_are_the_values_they_name():
    values = [float(i) for i in range(1, 101)]
    got = W.percentiles(values, points=(50, 90))
    check("the 50th of 1..100 is 50", got["50"] == 50.0, repr(got))
    check("the 90th is 90", got["90"] == 90.0, repr(got))


def test_band_position_says_where_a_band_sits():
    """The output that would let someone say the bands are wrong: if soft's
    'wet' edge is the 40th percentile, the app advises restraint two days in
    five and riders stop reading it."""
    values = [float(i) for i in range(100)]
    got = W.band_position({"soft": (25.0, 75.0), "hard": None}, values)
    check("hard, having no band, is absent", "hard" not in got, repr(got))
    check("the damp edge lands at the 25th percentile",
          abs(got["soft"]["damp"]["percentile"] - 25.0) < 0.5, repr(got))
    check("the wet edge at the 75th",
          abs(got["soft"]["wet"]["percentile"] - 75.0) < 0.5, repr(got))
    check("and the millimetres are carried beside it",
          got["soft"]["wet"]["mm"] == 75.0, repr(got))


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    rc = W.selftest(log=lambda msg: _failed.append(msg) if "FAIL" in msg
                    else None)
    check("build_wet --selftest passes", rc == 0)
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d checks" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

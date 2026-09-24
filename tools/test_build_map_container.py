"""The overview floor has to be one every zoom above it can live with.

A container built with floor F holds F..high, and tile size is NOT monotonic in
zoom: coalescing merges more the further out you go, so a z4 tile can be
smaller than the z5 tile above it.

`lowest_zoom_that_fits` measured the candidate zoom ALONE, so for a cyclist z4
fitted, 4 was returned, and the z5 tile the container also had to carry came
out at 535 kB against a 512 kB ceiling. The publish guard refused the build -
correctly - but the builder should never have offered it.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_map_container as B

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


def floor_for(sizes, low=4, high=10, ceiling=512):
    """Run the real chooser against a made-up size profile."""
    calls = []

    def fake(features, zoom, coalesced, on_tile, ids=None):
        calls.append(zoom)
        on_tile(zoom, 0, 0, b"x" * sizes[zoom], 1)

    real = B.build_tiles
    B.build_tiles = fake
    try:
        return B.lowest_zoom_that_fits([], low, high, max_tile=ceiling), calls
    finally:
        B.build_tiles = real


def test_a_zoom_above_the_floor_can_veto_it():
    # The measured failure: z4 fits, z5 does not, and the container holds both.
    got, _ = floor_for({4: 100, 5: 600, 6: 200, 7: 150, 8: 100, 9: 90, 10: 80})
    check("a fat zoom above the floor vetoes it", got == 6, "got %r" % got)


def test_the_easy_case_still_takes_the_lowest():
    # A motorcyclist's data fits everywhere, so it should go as low as allowed.
    got, _ = floor_for({z: 40 for z in range(4, 11)})
    check("all fitting means the lowest floor", got == 4, "got %r" % got)


def test_a_dataset_that_fits_nowhere_gets_no_overview():
    got, _ = floor_for({z: 9000 for z in range(4, 11)})
    check("too fat everywhere means no overview", got == 11, "got %r" % got)


def test_a_walker_lands_where_it_fits():
    # Dense data: nothing below 9 fits, so 9 it is.
    got, _ = floor_for({4: 9000, 5: 9000, 6: 9000, 7: 9000, 8: 9000,
                        9: 300, 10: 200})
    check("a dense set lands at the first workable floor", got == 9,
          "got %r" % got)


def test_each_zoom_is_cut_once():
    # The chooser used to re-cut the whole set per candidate. Measuring each
    # zoom once and picking from the measurements is the same answer for less.
    _, calls = floor_for({z: 40 for z in range(4, 11)})
    check("each zoom measured exactly once",
          sorted(calls) == list(range(4, 11)), "cut zooms %r" % sorted(calls))


# --------------------------------------------------------------------------
# docs/WAYS-SCHEMA.md - the table every phase-1 producer writes to
# --------------------------------------------------------------------------

#: The contract, column for column, in the order the schema states it. Copied
#: from the document rather than from the code, because a test that reads the
#: code cannot tell you the code is wrong.
WAYS_COLUMNS = [
    "way_uid", "way_class", "designation", "name", "authority", "county",
    "legal_tier", "source", "source_date",
    "surface", "smoothness", "tracktype", "width_m", "min_width_m", "barriers",
    "climb_m", "sustained_pct",
    "motorbike_ok", "fourxfour_ok", "access_reason", "access_evidence",
    "length_m", "geometry",
]

#: The ones OTHER steps fill in. 1.3 ingests the OSM attributes, 1.4 derives
#: climb and sustained gradient from our own DEM; both are joined later. This
#: build must leave them NULL, because NULL is the schema's "unknown" and
#: unknown is not "no" - a zero width would read as a way too narrow to ride.
JOINED_LATER = ["surface", "smoothness", "tracktype", "width_m", "min_width_m",
                "barriers", "climb_m", "sustained_pct"]


def _way(uid, way_class, moto, fourxfour, evidence="statutory",
         lon=-1.70, lat=52.50, authority="Derbyshire"):
    return {
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [(lon, lat), (lon + 0.01, lat + 0.01)]},
        "properties": {
            "lane_uid": uid, "class": way_class, "county": authority,
            "name": "%s %s" % (way_class, uid), "designation": way_class,
            "authority": authority, "legal_tier": "statutory",
            "source": "rowmaps:derbyshire", "source_date": "2026-03-04",
            "motorbike_ok": moto, "fourxfour_ok": fourxfour,
            "access_reason": "because the definitive map says so",
            "access_evidence": evidence, "lengthKm": 1.5,
        },
    }


FIXTURE = [
    _way("boat-1", "boat", 1, 1),
    _way("rb-1", "restricted_byway", 0, 0, lon=-1.68),
    _way("bw-1", "bridleway", 0, 0, lon=-1.66, authority="Staffordshire"),
]


def _built(features=None, **kw):
    """Write a real container and hand back a connection to it."""
    tmp = tempfile.mkdtemp(prefix="tbways-")
    path = os.path.join(tmp, "ways-fixture.tbmap")
    B.write_container(path, features if features is not None else FIXTURE,
                      "area", (11, 12), "2026-03-04T05:06:07Z", **kw)
    return sqlite3.connect(path), tmp, path


def test_the_container_holds_the_schemas_ways_table():
    db, tmp, _ = _built()
    try:
        got = [r[1] for r in db.execute("PRAGMA table_info(ways)")]
        check("the ways table is the schema's table, column for column",
              got == WAYS_COLUMNS, "got %r" % got)
        notnull = {r[1] for r in db.execute("PRAGMA table_info(ways)") if r[3]}
        for col in ("way_class", "authority", "legal_tier", "source",
                    "source_date", "motorbike_ok", "fourxfour_ok",
                    "access_reason", "access_evidence", "length_m",
                    "geometry"):
            check("%s is NOT NULL, as the contract states" % col,
                  col in notnull)
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def test_footpaths_are_not_in_the_container():
    # THE GATE. 435,299 footpaths, 627 MB, no bearing on a motor vehicle.
    db, tmp, _ = _built()
    try:
        n = db.execute(
            "SELECT COUNT(*) FROM ways WHERE way_class = 'footpath'"
        ).fetchone()[0]
        check("a query for class 'footpath' returns 0", n == 0, "got %d" % n)
        check("and the ways that ARE carried are still there",
              db.execute("SELECT COUNT(*) FROM ways").fetchone()[0] == 3)
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def test_the_columns_other_steps_fill_are_left_null():
    db, tmp, _ = _built()
    try:
        for col in JOINED_LATER:
            n = db.execute(
                "SELECT COUNT(*) FROM ways WHERE %s IS NOT NULL" % col
            ).fetchone()[0]
            check("%s is NULL until its own step produces it" % col, n == 0,
                  "%d rows already carry one" % n)
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def test_the_provenance_is_per_way_and_not_per_pack():
    db, tmp, _ = _built()
    try:
        rows = dict(db.execute(
            "SELECT way_uid, legal_tier || ' ' || source || ' ' || source_date "
            "FROM ways"))
        check("every way carries its own tier, source and date",
              all(v == "statutory rowmaps:derbyshire 2026-03-04"
                  for v in rows.values()), "got %r" % rows)
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def test_only_a_statutory_boat_is_open_to_a_vehicle():
    db, tmp, _ = _built()
    try:
        open_to_4x4 = [r[0] for r in db.execute(
            "SELECT way_class FROM ways WHERE fourxfour_ok = 1")]
        check("the one rule the schema exists to enforce",
              open_to_4x4 == ["boat"], "got %r" % open_to_4x4)
        n = db.execute(
            "SELECT COUNT(*) FROM ways WHERE fourxfour_ok = 0 "
            "AND access_evidence = 'none'").fetchone()[0]
        check("nothing is hidden from a 4x4 on no evidence", n == 0)
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def test_a_way_hidden_on_no_evidence_refuses_the_build():
    # The other half, and the half that makes the check above worth having.
    # F1 measured that we hold physical evidence on under 10% of ways, so a
    # builder that started hiding lanes on no evidence would be hiding them on
    # nothing, and the rider would never know a lane existed.
    bad = FIXTURE + [_way("bad", "bridleway", 0, 0, evidence="none",
                          lon=-1.64)]
    try:
        db, tmp, _ = _built(bad)
        db.close(); shutil.rmtree(tmp, ignore_errors=True)
        check("a way hidden with access_evidence 'none' refuses the build",
              False, "it was written out without complaint")
    except SystemExit as exc:
        check("a way hidden with access_evidence 'none' refuses the build",
              "access_evidence" in str(exc), str(exc)[:90])


def test_the_meta_says_what_the_container_holds():
    db, tmp, _ = _built(context_scope="none",
                        context_note="Bridleways and restricted byways are "
                                     "not on this map")
    try:
        meta = dict(db.execute("SELECT key, value FROM meta"))
        check("schema_version starts at 1", meta.get("schema_version") == "1",
              "got %r" % meta.get("schema_version"))
        check("the tier counts are in the meta",
              json.loads(meta["legal_tier_counts"]) == {"statutory": 3})
        check("the class counts are in the meta",
              json.loads(meta["class_counts"]) ==
              {"boat": 1, "bridleway": 1, "restricted_byway": 1})
        check("the authorities are a JSON array",
              json.loads(meta["authorities"]) ==
              ["Derbyshire", "Staffordshire"])
        # STEP 1.2c. Their absence must never read as absence on the ground.
        check("the container says what scope of context it carries",
              meta.get("context_scope") == "none",
              "got %r" % meta.get("context_scope"))
        check("and carries the sentence the app has to show",
              "not on this map" in meta.get("context_note", ""))
        # built_at is the SOURCE date. A run stamp here moved five bytes in a
        # 1.1 MB container and made every rider re-download 343 MB.
        check("built_at is the source date, not the clock",
              meta["built_at"] == "2026-03-04T05:06:07Z")
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def test_a_container_carrying_everything_says_nothing_about_scope():
    db, tmp, _ = _built()
    try:
        meta = dict(db.execute("SELECT key, value FROM meta"))
        check("no context_scope when nothing was filtered out",
              "context_scope" not in meta)
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def test_the_record_the_rtree_and_the_tile_share_one_id():
    db, tmp, _ = _built()
    try:
        ids = [r[0] for r in db.execute("SELECT rowid FROM ways ORDER BY rowid")]
        box = [r[0] for r in db.execute("SELECT id FROM ways_bbox ORDER BY id")]
        check("every record has an rtree row under the same id", ids == box,
              "%r vs %r" % (ids, box))
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def test_the_transitional_lanes_view_still_answers():
    # Six tools and the app's store still read `lanes`. The view keeps every
    # READER working while the pivot lands; phase 2 deletes it.
    db, tmp, _ = _built()
    try:
        rows = dict(db.execute("SELECT lane_uid, vehicle_access FROM lanes"))
        check("a BOAT still reads as open to all five vehicles",
              rows["boat-1"] == 31, "got %r" % rows.get("boat-1"))
        check("a bridleway reads as cycle, horse and foot only",
              rows["bw-1"] == 28, "got %r" % rows.get("bw-1"))
        check("the view carries every way", len(rows) == 3)
        check("and lanes_bbox still answers",
              db.execute("SELECT COUNT(*) FROM lanes_bbox").fetchone()[0] == 3)
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def test_the_tile_carries_what_the_style_has_to_read():
    seen = []

    def on_tile(z, x, y, blob, n):
        seen.append(n)

    props = B.tile_properties(FIXTURE[0]["properties"], with_uid=True)
    check("the tile carries the class", props.get("class") == "boat")
    check("and the legal tier, which the rideable rule turns on",
          props.get("tier") == "statutory")
    check("and the derived verdict for each vehicle",
          props.get("moto") is True and props.get("fourxfour") is True)
    check("the per-vehicle booleans are gone with the partition",
          not any(k.startswith("v_") for k in props), "got %r" % sorted(props))
    # Two ways that differ ONLY in class must not coalesce into one line at low
    # zoom: a restricted byway drawn as a byway is the mutation the falsifier
    # set exists to catch.
    a = B.coalesce_key(B.tile_properties(FIXTURE[0]["properties"], False))
    b = B.coalesce_key(B.tile_properties(FIXTURE[1]["properties"], False))
    check("a BOAT and a restricted byway never share a coalesced line", a != b)
    # And the pair the access columns CANNOT tell apart. A restricted byway and
    # a bridleway are both closed to both vehicles, so a key built from the
    # access verdict alone merges them - and the rider is then told "horse,
    # foot and cycle only" about a way that is nothing of the kind. The class
    # has to be in the key in its own right.
    c = B.coalesce_key(B.tile_properties(
        dict(FIXTURE[1]["properties"], county="Derbyshire"), False))
    d = B.coalesce_key(B.tile_properties(
        dict(FIXTURE[2]["properties"], county="Derbyshire"), False))
    check("nor do a restricted byway and a bridleway, which no access column "
          "tells apart", c != d, "both keyed as %r" % (c,))


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d checks" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

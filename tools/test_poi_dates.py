#!/usr/bin/env python3
"""A POI's date moves when the POI does, and not when we look at it again.

THE COST. `source_date` on a POI was the day Overpass was read, so
`build_pois.py fetch --refresh` re-dated every row in the region. Stable
rowids (stable_ids.number) did not save it: a changeset matches rows on the
rowid and then compares values, and the date is a value. Measured on the six
published regions, a refresh against unchanged OSM made 38.0 MB raw / 17.4 MB
gzipped of changesets for 94 MB of containers - every month, on the paid
freshness tier, for nothing.

THE RULE. A POI whose every other column equals the published row keeps the
published date; a new or changed POI takes the day it was read; and the day
the region was read is stated once, as `meta.pois_checked`.

Each "keeps its date" check below is red with stable_ids.keep_dates removed
from build_pois.write_pois, because the refresh is always read on a later day
than the published build.

Run: python tools/test_poi_dates.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stable_ids as S            # noqa: E402
import build_map_container as B   # noqa: E402
import build_pois as PO           # noqa: E402
import build_changeset as X       # noqa: E402

_passed = 0
_failed = []

PUBLISHED_DAY = "2026-08-24"
REFRESH_DAY = "2026-09-24"


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": %r" % (detail,)) if detail else ""))


def _poi(uid, day, name=None, category="fuel", lat=52.5, lon=-1.7,
         hours=None):
    return {"poi_uid": uid, "category": category, "name": name, "lat": lat,
            "lon": lon, "opening_hours": hours, "source_date": day}


def _way(uid, lon, lat):
    return {"type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [(lon, lat), (lon + 0.004, lat + 0.003)]},
            "properties": {"lane_uid": uid, "class": "boat",
                           "authority": "Derbyshire", "source_date": "2026-03-04",
                           "motorbike_ok": 1, "fourxfour_ok": 1,
                           "access_reason": "x", "access_evidence": "statutory",
                           "lengthKm": 0.4}}


WAYS = [_way("W1", -1.70, 52.50)]


def _container(path, stamp="2026-09-01T00:00:00Z"):
    B.write_container(path, WAYS, "area", (11, 11), stamp)


def _dates(path):
    db = sqlite3.connect(path)
    try:
        return dict(db.execute("SELECT poi_uid, source_date FROM pois"))
    finally:
        db.close()


def _meta(path, key):
    db = sqlite3.connect(path)
    try:
        row = db.execute("SELECT value FROM meta WHERE key = ?",
                         (key,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def _set_meta(path, key, value):
    db = sqlite3.connect(path)
    try:
        db.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))
        db.commit()
    finally:
        db.close()


def _published(tmp):
    path = os.path.join(tmp, "published.tbmap")
    _container(path)
    PO.write_pois(path, [
        _poi("osm:n200", PUBLISHED_DAY, name="Hilltop"),
        _poi("osm:n300", PUBLISHED_DAY, category="toilets", lat=52.51),
        _poi("osm:n400", PUBLISHED_DAY, name="Old name", lat=52.52),
    ])
    return path


# ----------------------------------------------------------------- the rule

def test_an_unchanged_poi_keeps_its_published_date():
    tmp = tempfile.mkdtemp()
    try:
        published = _published(tmp)
        nxt = os.path.join(tmp, "next.tbmap")
        _container(nxt)
        PO.write_pois(nxt, [
            _poi("osm:n100", REFRESH_DAY, name="New pump", lat=52.49),
            _poi("osm:n200", REFRESH_DAY, name="Hilltop"),
            _poi("osm:n300", REFRESH_DAY, category="toilets", lat=52.51),
            _poi("osm:n400", REFRESH_DAY, name="New name", lat=52.52),
        ], previous=published)
        got = _dates(nxt)
        check("PREMISE: the refresh was read a month after the publish",
              _dates(published)["osm:n200"] == PUBLISHED_DAY, got)
        check("an unchanged named POI keeps the published date",
              got["osm:n200"] == PUBLISHED_DAY, got)
        check("an unchanged unnamed POI (NULL name, NULL hours) keeps it too",
              got["osm:n300"] == PUBLISHED_DAY, got)
        check("a POI whose name changed takes the day it was read",
              got["osm:n400"] == REFRESH_DAY, got)
        check("a new POI takes the day it was read",
              got["osm:n100"] == REFRESH_DAY, got)
        check("and the region says when it was read",
              _meta(nxt, "pois_checked") == REFRESH_DAY,
              _meta(nxt, "pois_checked"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_moved_or_recategorised_or_rehoured_poi_is_a_changed_one():
    tmp = tempfile.mkdtemp()
    try:
        published = os.path.join(tmp, "published.tbmap")
        _container(published)
        PO.write_pois(published, [
            _poi("osm:n1", PUBLISHED_DAY),
            _poi("osm:n2", PUBLISHED_DAY, lat=52.51),
            _poi("osm:n3", PUBLISHED_DAY, lat=52.52, hours="24/7")])
        nxt = os.path.join(tmp, "next.tbmap")
        _container(nxt)
        PO.write_pois(nxt, [
            _poi("osm:n1", REFRESH_DAY, lat=52.5000001),
            _poi("osm:n2", REFRESH_DAY, lat=52.51, category="repair"),
            _poi("osm:n3", REFRESH_DAY, lat=52.52, hours="Mo-Fr 08:00-18:00")],
            previous=published)
        got = _dates(nxt)
        check("moved by one stored digit: changed",
              got["osm:n1"] == REFRESH_DAY, got)
        check("another category: changed", got["osm:n2"] == REFRESH_DAY, got)
        check("other opening hours: changed",
              got["osm:n3"] == REFRESH_DAY, got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_without_a_published_file_every_row_is_dated_as_read():
    """A region's first build, or a local run, is what it always was."""
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "first.tbmap")
        _container(path)
        PO.write_pois(path, [_poi("osm:n1", REFRESH_DAY),
                             _poi("osm:n2", REFRESH_DAY, lat=52.51)],
                      previous=os.path.join(tmp, "not-there.tbmap"))
        check("dated as read", set(_dates(path).values()) == {REFRESH_DAY},
              _dates(path))
        check("and checked that day",
              _meta(path, "pois_checked") == REFRESH_DAY)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_checked_is_the_stalest_category_or_what_the_caller_says():
    """A region whose fuel was read today and whose toilets a month ago was
    checked a month ago - poi_staleness.py's rule, for the same reason."""
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "a.tbmap")
        _container(path)
        PO.write_pois(path, [_poi("osm:n1", REFRESH_DAY),
                             _poi("osm:n2", PUBLISHED_DAY, lat=52.51)])
        check("the oldest read date, when nobody says otherwise",
              _meta(path, "pois_checked") == PUBLISHED_DAY,
              _meta(path, "pois_checked"))
        other = os.path.join(tmp, "b.tbmap")
        _container(other)
        PO.write_pois(other, [], checked=REFRESH_DAY)
        check("an empty region still says when it was looked at",
              _meta(other, "pois_checked") == REFRESH_DAY,
              _meta(other, "pois_checked"))
        bare = os.path.join(tmp, "bare.sqlite")
        PO.write_pois(bare, [_poi("osm:n1", REFRESH_DAY)])
        db = sqlite3.connect(bare)
        try:
            has_meta = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
        finally:
            db.close()
        check("a file with no meta table is not given one", has_meta is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_keep_dates_leaves_its_input_alone_and_distrusts_a_missing_column():
    # Row b's `x` is NULL and the published row has no `x` at all - the
    # shape of a column added after the last publish. With a non-NULL value
    # here the check passed with the "missing" sentinel removed (a missing
    # column read as None), because 2 != None said "changed" on its own.
    rows = [{"k": "a", "x": 1, "source_date": REFRESH_DAY},
            {"k": "b", "x": None, "source_date": REFRESH_DAY}]
    before = json.dumps(rows, sort_keys=True)
    got = S.keep_dates(rows, {"a": {"k": "a", "x": 1,
                                    "source_date": PUBLISHED_DAY},
                              "b": {"k": "b", "source_date": PUBLISHED_DAY}},
                       "k")
    check("an equal row takes the published date",
          got[0]["source_date"] == PUBLISHED_DAY, got)
    check("a row the published file cannot vouch for keeps its own",
          got[1]["source_date"] == REFRESH_DAY, got)
    check("the caller's rows are not modified",
          json.dumps(rows, sort_keys=True) == before, rows)
    check("nothing published, nothing kept",
          S.keep_dates(rows, {}, "k") == rows)


# ------------------------------------------------------- through the builder

def _cache(tmp, fetched_at, elements):
    """A build_pois cache for `midlands`, one file per category, all read on
    `fetched_at` - the shape `fetch --refresh` leaves behind."""
    cache = os.path.join(tmp, "cache-%s" % fetched_at)
    os.makedirs(os.path.join(cache, "midlands"))
    for name, _ in PO.CATEGORIES:
        with open(PO.cache_path(cache, "midlands", name), "w",
                  encoding="utf-8") as fh:
            json.dump({"region": "midlands", "category": name,
                       "fetched_at": fetched_at,
                       "elements": [e for e in elements
                                    if PO.categorise(e["tags"]) == name]}, fh)
    return cache


ELEMENTS = [
    {"type": "node", "id": 5, "lat": 52.5010, "lon": -1.6990,
     "tags": {"amenity": "fuel", "name": "Hilltop"}},
    {"type": "node", "id": 7, "lat": 52.5020, "lon": -1.6980,
     "tags": {"amenity": "toilets"}},
]


def test_a_refresh_of_unchanged_osm_changes_no_poi_row():
    """THE COST, END TO END, through the wiring CI runs:
    build_containers._add_pois (which build_all calls with `--previous
    containers`), then build_changeset between the published build and the
    refresh. The refresh read the same OSM a month later; the changeset must
    carry no POI row and no r-tree row - only the one meta key that says when
    the region was checked."""
    import build_containers as C
    tmp = tempfile.mkdtemp()
    try:
        published = os.path.join(tmp, "published.tbmap")
        _container(published)
        C._add_pois(published, WAYS, "midlands",
                    _cache(tmp, PUBLISHED_DAY, ELEMENTS), log=lambda *a: None)

        refreshed = os.path.join(tmp, "refreshed.tbmap")
        _container(refreshed)
        C._add_pois(refreshed, WAYS, "midlands",
                    _cache(tmp, REFRESH_DAY, ELEMENTS), log=lambda *a: None,
                    previous=published)
        # stamp_build.py's rule 3: the ways did not change, something else
        # did, so the build gets a stamp of its own.
        _set_meta(refreshed, "built_at", "2026-09-24T06:00:00Z")

        check("PREMISE: the published build carries both POIs",
              len(_dates(published)) == 2, _dates(published))
        check("the refresh kept both POIs' published dates",
              set(_dates(refreshed).values()) == {PUBLISHED_DAY},
              _dates(refreshed))
        check("and says the region was checked on the refresh day",
              _meta(refreshed, "pois_checked") == REFRESH_DAY,
              _meta(refreshed, "pois_checked"))

        out = os.path.join(tmp, "refresh.tbchange")
        X.build_changeset(published, refreshed, out)
        db = sqlite3.connect(out)
        try:
            poi_rows = db.execute("SELECT count(*) FROM pois").fetchone()[0]
            box_rows = db.execute(
                "SELECT count(*) FROM pois_bbox").fetchone()[0]
            removed = db.execute(
                "SELECT count(*) FROM removed_rows WHERE tbl IN"
                " ('pois', 'pois_bbox')").fetchone()[0]
            checked = db.execute(
                "SELECT value FROM meta WHERE key = 'pois_checked'"
            ).fetchone()
        finally:
            db.close()
        check("the changeset carries no POI row", poi_rows == 0, poi_rows)
        check("no r-tree row", box_rows == 0, box_rows)
        check("and removes nothing", removed == 0, removed)
        check("it restates when the region was checked",
              checked is not None and checked[0] == REFRESH_DAY, checked)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_build_command_states_its_stalest_category():
    """`build_pois.py build` passes `checked` itself, from the cache's read
    dates. With the newest of them instead, a region whose toilets failed to
    refresh last month would say it was checked today - and nothing tested
    the command's choice: every other check here goes through write_pois or
    build_containers, so that mutation passed the whole suite."""
    tmp = tempfile.mkdtemp()
    try:
        cache = _cache(tmp, REFRESH_DAY, ELEMENTS)
        stale = PO.cache_path(cache, "midlands", "toilets")
        with open(stale, encoding="utf-8") as fh:
            blob = json.load(fh)
        blob["fetched_at"] = PUBLISHED_DAY
        with open(stale, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)
        path = os.path.join(tmp, "built.tbmap")
        _container(path)
        # Through the command's own parser, so the test holds the flags the
        # command really takes.
        PO.do_build(PO.parse_args(["build", "--region", "midlands",
                                   "--cache", cache, "--container", path,
                                   "--in-place"]),
                    log=lambda *a: None)
        check("the build command says the stalest category's day",
              _meta(path, "pois_checked") == PUBLISHED_DAY,
              _meta(path, "pois_checked"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_container_build_states_a_stale_category_it_found_nothing_in():
    """CI builds containers through build_containers.build_all, not through
    `build_pois.py build`, and it must say the same thing that command does.

    The shape that decides it: the toilets refresh failed, so their cache is
    a month old, and the one toilet it holds is outside this container's
    bounds. With no `checked` passed, write_pois falls back to the oldest date
    among the ROWS - and no row came from the stale category - so the
    container claimed a refresh of every category on the day only the fuel
    was read. Driven through build_all from sealed packs, the path
    refresh-data.yml takes."""
    import build_containers as C
    import build_packages as P
    key = bytes(range(32))
    tmp = tempfile.mkdtemp(prefix="tbpois-checked-")
    real_dist = P.dist_dir
    P.dist_dir = lambda: tmp
    try:
        cache = _cache(tmp, REFRESH_DAY, [
            # Inside the midlands way's bounds (-1.70..-1.696, 52.50..52.503).
            {"type": "node", "id": 5, "lat": 52.5010, "lon": -1.6990,
             "tags": {"amenity": "fuel", "name": "Hilltop"}},
            # Three degrees east: in the region's cache, outside the container.
            {"type": "node", "id": 7, "lat": 52.5020, "lon": 1.5000,
             "tags": {"amenity": "toilets"}},
        ])
        stale = PO.cache_path(cache, "midlands", "toilets")
        with open(stale, encoding="utf-8") as fh:
            blob = json.load(fh)
        blob["fetched_at"] = PUBLISHED_DAY
        with open(stale, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)

        pack = P.write_package(P.DATASET, "midlands", "Midlands", None, WAYS,
                               key, "2026-09-01T00:00:00Z", note="n")
        manifest = {"schema": 1, "generated": "2026-09-24T09:00:00Z",
                    "dataset": P.DATASET, **P.context_fields(),
                    "packages": [pack]}
        mpath = os.path.join(tmp, "manifest.json")
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        out_dir = os.path.join(tmp, "containers")
        C.build_all(mpath, out_dir, key, None, tmp, poi_cache=cache)
        path = os.path.join(out_dir, "%s-midlands.tbmap" % P.DATASET)

        check("PREMISE: the stale category's cache holds an element",
              len(blob["elements"]) == 1, blob["elements"])
        check("PREMISE: only the fresh category's POI reached the container",
              _dates(path) == {"osm:n5": REFRESH_DAY}, _dates(path))
        check("the container says the stalest category's day",
              _meta(path, "pois_checked") == PUBLISHED_DAY,
              _meta(path, "pois_checked"))
    finally:
        P.dist_dir = real_dist
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("%d passed, %d failed" % (_passed, len(_failed)))
    for failure in _failed:
        print("  FAIL %s" % failure)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

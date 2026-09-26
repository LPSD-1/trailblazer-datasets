#!/usr/bin/env python3
"""The build stamp follows the content: stamp_build.py.

THE STATE THIS EXISTS TO MAKE IMPOSSIBLE is "same build, different bytes".
Every changeset is keyed on `built_at`, so a container whose bytes moved
under an unchanged stamp is one no changeset can reach, and the app fetches
it whole - ~94 MB for the six regions, on the paid tier. It happened every
month (meta.evidence_age) and on every POI, ford or gauge refresh, because
`built_at` followed the ways and nothing else.

The guard, stated as a test: across a run of builds, TWO BUILDS WITH
DIFFERENT CONTENT NEVER SHARE A STAMP, and two with the same content ship
the same bytes.

Run: python tools/test_stamp_build.py
"""
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stamp_build as SB         # noqa: E402
import build_map_container as B  # noqa: E402
import build_pois as PO          # noqa: E402
import evidence_age as EA        # noqa: E402
import publish_changesets as PC  # noqa: E402

_passed = 0
_failed = []

WAYS_STAMP = "2026-09-24T22:40:20Z"


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def _way(uid, lon, lat):
    return {"type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [(lon, lat), (lon + 0.004, lat + 0.003)]},
            "properties": {"lane_uid": uid, "class": "boat",
                           "authority": "Derbyshire", "source_date": "2026-03-04",
                           "motorbike_ok": 1, "fourxfour_ok": 1,
                           "access_reason": "x", "access_evidence": "statutory",
                           "lengthKm": 0.4}}


def _poi(uid, name=None):
    return {"poi_uid": uid, "category": "fuel", "name": name, "lat": 52.501,
            "lon": -1.699, "opening_hours": None, "source_date": "2026-09-01"}


POIS = [_poi("osm:n200"), _poi("osm:n300")]


def build(path, pois=POIS, stamp=WAYS_STAMP, previous=None):
    """A region container as the pipeline writes one, up to the stamp."""
    B.write_container(path, [_way("W1", -1.70, 52.50)], "area", (11, 11),
                      stamp)
    PO.write_pois(path, pois, previous=previous)
    EA.write_meta(path)
    return path


def _raw(path):
    with open(path, "rb") as fh:
        return fh.read()


def _meta(path, key):
    db = sqlite3.connect(path)
    try:
        row = db.execute("SELECT value FROM meta WHERE key=?",
                         (key,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


# ------------------------------------------------------------ the digest

def test_the_digest_ignores_the_stamp_and_the_page_layout():
    tmp = tempfile.mkdtemp()
    try:
        a = build(os.path.join(tmp, "a.tbmap"))
        b = build(os.path.join(tmp, "b.tbmap"), stamp="2027-01-01T00:00:00Z")
        check("PREMISE: the files differ", _raw(a) != _raw(b))
        check("a different built_at alone is the same content",
              SB.content_digest(a) == SB.content_digest(b))
        db = sqlite3.connect(b)
        db.execute("PRAGMA page_size = 1024")
        db.execute("VACUUM")
        db.close()
        check("so is the same rows laid out in other pages",
              SB.content_digest(a) == SB.content_digest(b))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_digest_moves_with_every_kind_of_content():
    tmp = tempfile.mkdtemp()
    try:
        base = SB.content_digest(build(os.path.join(tmp, "a.tbmap")))
        renamed = build(os.path.join(tmp, "b.tbmap"),
                        pois=[_poi("osm:n200", "Renamed"), _poi("osm:n300")])
        check("a POI's name", SB.content_digest(renamed) != base)
        noted = build(os.path.join(tmp, "c.tbmap"))
        db = sqlite3.connect(noted)
        db.execute("UPDATE meta SET value = 'x' WHERE key = 'context_note'")
        if db.total_changes == 0:
            db.execute("INSERT INTO meta VALUES ('context_note', 'x')")
        db.commit()
        db.close()
        check("a meta key", SB.content_digest(noted) != base)
        boxed = build(os.path.join(tmp, "d.tbmap"))
        db = sqlite3.connect(boxed)
        db.execute("UPDATE pois_bbox SET min_lon = min_lon - 0.001")
        db.commit()
        db.close()
        check("an r-tree row", SB.content_digest(boxed) != base)
        tiled = build(os.path.join(tmp, "e.tbmap"))
        db = sqlite3.connect(tiled)
        db.execute("UPDATE tiles SET tile_data = "
                   "CAST(tile_data || X'00' AS BLOB)")
        db.commit()
        db.close()
        check("a tile (WITHOUT ROWID)", SB.content_digest(tiled) != base)
        shaped = build(os.path.join(tmp, "f.tbmap"))
        db = sqlite3.connect(shaped)
        db.execute("CREATE INDEX pois_by_name ON pois(name)")
        db.commit()
        db.close()
        check("the schema", SB.content_digest(shaped) != base)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- the stamp

def test_unchanged_content_ships_the_published_bytes():
    """Next month's run over the same rows: nothing for a rider to fetch."""
    tmp = tempfile.mkdtemp()
    try:
        published = build(os.path.join(tmp, "published.tbmap"))
        built = build(os.path.join(tmp, "built.tbmap"),
                      stamp="2026-10-01T00:00:00Z", previous=published)
        db = sqlite3.connect(built)
        db.execute("PRAGMA page_size = 1024")
        db.execute("VACUUM")
        db.close()
        check("PREMISE: the new build's bytes differ", _raw(built) !=
              _raw(published))
        verdict, stamp = SB.stamp(published, built, "2026-10-01T06:00:00Z")
        check("it is unchanged", verdict == "unchanged", verdict)
        check("the published stamp", stamp == WAYS_STAMP, stamp)
        check("and the published bytes, exactly", _raw(built) ==
              _raw(published))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_poi_refresh_under_the_same_ways_gets_a_new_stamp():
    """THE CASE: the ways (and so the pack stamp) did not move, a POI did."""
    tmp = tempfile.mkdtemp()
    try:
        published = build(os.path.join(tmp, "published.tbmap"))
        built = build(os.path.join(tmp, "built.tbmap"),
                      pois=[_poi("osm:n100")] + POIS, previous=published)
        check("PREMISE: the same ways stamp",
              _meta(built, "built_at") == _meta(published, "built_at"))
        verdict, stamp = SB.stamp(published, built, "2026-10-01T06:00:00Z")
        check("it is changed", verdict == "changed", verdict)
        check("under a stamp the published file does not carry",
              _meta(built, "built_at") != WAYS_STAMP, _meta(built, "built_at"))
        check("which is this run's time", stamp == "2026-10-01T06:00:00Z",
              stamp)
        check("and is what the file says", _meta(built, "built_at") == stamp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_ways_change_keeps_its_own_stamp():
    tmp = tempfile.mkdtemp()
    try:
        published = build(os.path.join(tmp, "published.tbmap"))
        built = build(os.path.join(tmp, "built.tbmap"),
                      stamp="2026-10-02T03:04:05Z", previous=published)
        db = sqlite3.connect(built)
        db.execute("UPDATE ways SET name = 'Amended'")
        db.commit()
        db.close()
        verdict, stamp = SB.stamp(published, built, "2026-10-03T00:00:00Z")
        check("the pack stamp stands", stamp == "2026-10-02T03:04:05Z", stamp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_clock_behind_the_published_stamp_still_moves_it_forward():
    got = SB.next_stamp("2026-10-01T06:00:00Z", "2026-09-24T22:40:20Z",
                        "2026-09-30T00:00:00Z")
    check("one second past the published stamp",
          got == "2026-10-01T06:00:01Z", got)
    got = SB.next_stamp("2026-10-01T06:00:00Z", "2026-10-01T06:00:00Z",
                        "2026-10-01T06:00:00Z")
    check("and never equal to it, even with every input the same",
          got != "2026-10-01T06:00:00Z", got)
    check("always a date the app can parse",
          datetime.datetime.strptime(got, SB.STAMP_FORMAT) is not None)


def test_a_first_build_is_left_alone():
    tmp = tempfile.mkdtemp()
    try:
        built = build(os.path.join(tmp, "built.tbmap"))
        before = _raw(built)
        verdict, stamp = SB.stamp(os.path.join(tmp, "absent.tbmap"), built,
                                  "2026-10-01T00:00:00Z")
        check("first", verdict == "first", verdict)
        check("untouched", _raw(built) == before and stamp == WAYS_STAMP)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_manifest_says_the_stamp_the_file_carries():
    tmp = tempfile.mkdtemp()
    try:
        pub = os.path.join(tmp, "containers")
        new = os.path.join(tmp, "dist", "containers")
        os.makedirs(pub)
        os.makedirs(new)
        build(os.path.join(pub, "ways-a.tbmap"))
        build(os.path.join(pub, "ways-b.tbmap"))
        build(os.path.join(new, "ways-a.tbmap"),
              previous=os.path.join(pub, "ways-a.tbmap"))
        build(os.path.join(new, "ways-b.tbmap"), pois=[_poi("osm:n9")],
              previous=os.path.join(pub, "ways-b.tbmap"))
        manifest = os.path.join(new, "manifest.json")
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump({"generated": WAYS_STAMP, "containers": [
                {"file": "containers/ways-a.tbmap", "generated": WAYS_STAMP},
                {"file": "containers/ways-b.tbmap", "generated": WAYS_STAMP},
            ]}, fh)
        SB.stamp_tree(pub, manifest, "2026-10-01T06:00:00Z",
                      log=lambda *a: None)
        with open(manifest, encoding="utf-8") as fh:
            got = json.load(fh)
        by = dict((e["file"], e["generated"]) for e in got["containers"])
        check("the unchanged one keeps its date",
              by["containers/ways-a.tbmap"] == WAYS_STAMP, by)
        check("the changed one says its new stamp",
              by["containers/ways-b.tbmap"] == _meta(
                  os.path.join(new, "ways-b.tbmap"), "built_at")
              == "2026-10-01T06:00:00Z", by)
        check("and the manifest's own date is the newest",
              got["generated"] == "2026-10-01T06:00:00Z", got["generated"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_rebuild_that_moved_only_built_at_reaches_no_rider():
    """RULE 1, END TO END: stamp_tree then publish_changesets, as the workflow
    runs them.

    The monthly rebuild over unchanged rows: the pack's `generated` moved, so
    the new build's `built_at` is newer, and nothing else is. A changeset
    that moves only `built_at` is refused by the validator, so if this build
    were announced under its own stamp no changeset would reach it and every
    rider would fetch the region whole. Rule 1 is what stops that build
    existing: the published file is shipped, under the published stamp.

    "No changeset" alone is NOT the proof - with rule 1 gone the changeset
    is still refused, and publish reports nothing built either way. The
    proof is the BUILD THE INDEX ANNOUNCES: the one riders already hold.
    """
    tmp = tempfile.mkdtemp()
    try:
        pub = os.path.join(tmp, "containers")
        new = os.path.join(tmp, "dist", "containers")
        out = os.path.join(tmp, "changes")
        os.makedirs(pub)
        os.makedirs(new)
        name = "ways-a.tbmap"
        served = "containers/" + name
        build(os.path.join(pub, name))
        rebuilt_at = "2026-10-01T00:00:00Z"
        build(os.path.join(new, name), stamp=rebuilt_at,
              previous=os.path.join(pub, name))
        for tree, stamp in ((pub, WAYS_STAMP), (new, rebuilt_at)):
            with open(os.path.join(tree, "manifest.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"generated": stamp, "containers": [
                    {"id": "ways-a", "kind": "area", "file": served,
                     "generated": stamp}]}, fh)
        pub_file = os.path.join(pub, name)
        new_file = os.path.join(new, name)
        check("PREMISE: the rebuild carries a newer built_at",
              _meta(new_file, "built_at") == rebuilt_at
              and _meta(pub_file, "built_at") == WAYS_STAMP)
        check("PREMISE: and different bytes", _raw(new_file) !=
              _raw(pub_file))
        check("PREMISE: and the same content",
              SB.content_digest(new_file) == SB.content_digest(pub_file))

        verdicts = SB.stamp_tree(pub, os.path.join(new, "manifest.json"),
                                 "2026-10-01T06:00:00Z", log=lambda *a: None)
        check("stamp_build calls it unchanged",
              verdicts == [(name, "unchanged", WAYS_STAMP)], verdicts)
        check("the published built_at is kept",
              _meta(new_file, "built_at") == WAYS_STAMP,
              _meta(new_file, "built_at"))
        check("the published bytes are shipped",
              _raw(new_file) == _raw(pub_file))
        with open(os.path.join(new, "manifest.json"), encoding="utf-8") as fh:
            got = json.load(fh)
        check("the manifest names the published build",
              got["containers"][0]["generated"] == WAYS_STAMP
              and got["generated"] == WAYS_STAMP, got)

        report = PC.publish(pub, new, out)
        check("PREMISE: publish saw the container",
              served in report["index"]["builds"], report["index"])
        check("no changeset is built", report["built"] == [],
              report["built"])
        check("none is refused", report["refused"] == [], report["refused"])
        check("it is skipped as unchanged",
              report["skipped"] == [("ways-a", "unchanged")],
              report["skipped"])
        check("THE ANNOUNCED BUILD IS THE ONE RIDERS HOLD",
              report["index"]["builds"].get(served) == WAYS_STAMP,
              report["index"]["builds"])
        check("and no .tbchange was written",
              not any(f.endswith(".tbchange")
                      for _, _, fs in os.walk(out) for f in fs))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- the guard

def test_different_content_never_shares_a_stamp_across_a_run_of_builds():
    """The guard itself, over a sequence the pipeline could produce: nothing
    changes, a POI arrives, nothing changes, the ways change, a POI goes,
    the same month's rebuild, and so on - each stamped against the one
    before it as `containers/` would hold it."""
    tmp = tempfile.mkdtemp()
    runs = [
        (POIS, WAYS_STAMP, "2026-09-25T00:00:00Z"),
        (POIS, WAYS_STAMP, "2026-10-01T00:00:00Z"),               # same
        ([_poi("osm:n100")] + POIS, WAYS_STAMP, "2026-10-01T06:00:00Z"),
        ([_poi("osm:n100")] + POIS, WAYS_STAMP, "2026-10-01T12:00:00Z"),
        ([_poi("osm:n100")], "2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z"),
        (POIS, WAYS_STAMP, "2026-09-01T00:00:00Z"),               # clock back
        (POIS, WAYS_STAMP, "2026-11-01T00:00:00Z"),               # same
    ]
    try:
        published = None
        seen = {}
        for i, (pois, ways_stamp, now) in enumerate(runs):
            built = build(os.path.join(tmp, "build-%d.tbmap" % i), pois=pois,
                          stamp=ways_stamp, previous=published)
            SB.stamp(published, built, now)
            digest = SB.content_digest(built)
            stamp = _meta(built, "built_at")
            if published is not None:
                same = digest == SB.content_digest(published)
                check("run %d: same content <=> same bytes" % i,
                      same == (_raw(built) == _raw(published)))
                check("run %d: same content <=> same stamp" % i,
                      same == (stamp == _meta(published, "built_at")),
                      (stamp, _meta(published, "built_at")))
            check("run %d: no earlier build had this stamp with other "
                  "content" % i, seen.get(stamp, digest) == digest,
                  (stamp, seen))
            seen.setdefault(stamp, digest)
            published = built
        check("PREMISE: the sequence produced several builds",
              len(seen) >= 4, seen)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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

#!/usr/bin/env python3
"""The date riders are shown for their lanes does not move with a POI refresh.

    python tools/test_ways_cut.py

THE DEFECT. stamp_build.py's rule 3 gives a container a new `built_at` - the
run's clock - whenever its content changed under unchanged ways: a POI, ford
or gauge refresh. That is right for `built_at`, which is the build id every
changeset is keyed on. But the app showed `built_at` as "cut <date>", so a
new fuel station told every rider in the region that their lanes had been
cut that morning, which is the exact over-claim of freshness the refresh
workflow's own comments forbid.

THE FIX, stated as tests: before rule 3 overwrites the pack stamp,
stamp_build keeps it as `ways_cut`; a changeset carries that unchanged to the
rider's copy; the manifest says it beside `generated`; and a region rule 3
never met carries no such key, its `built_at` still being the pack stamp.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_changeset as C      # noqa: E402
import build_map_container as B  # noqa: E402
import build_pois as PO          # noqa: E402
import evidence_age as EA        # noqa: E402
import stamp_build as SB         # noqa: E402

_passed = 0
_failed = []

WAYS_STAMP = "2026-09-24T22:40:20Z"
POI_RUN = "2026-10-24T06:00:00Z"


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


def _poi(uid):
    return {"poi_uid": uid, "category": "fuel", "name": None, "lat": 52.501,
            "lon": -1.699, "opening_hours": None, "source_date": "2026-09-01"}


POIS = [_poi("osm:n200"), _poi("osm:n300")]
MORE_POIS = [_poi("osm:n100")] + POIS


def build(path, pois=POIS, stamp=WAYS_STAMP, previous=None):
    """A region container as the pipeline writes one, up to the stamp."""
    B.write_container(path, [_way("W1", -1.70, 52.50)], "area", (11, 11),
                      stamp)
    PO.write_pois(path, pois, previous=previous)
    EA.write_meta(path)
    return path


def _meta(path, key):
    db = sqlite3.connect(path)
    try:
        row = db.execute("SELECT value FROM meta WHERE key=?",
                         (key,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def _in(tmp, name):
    return os.path.join(tmp, name)


def _restamped(tmp, name="published.tbmap"):
    """A published region rule 3 has already met: a POI refresh restamped it."""
    first = build(_in(tmp, "first-" + name))
    published = build(_in(tmp, name), pois=MORE_POIS, previous=first)
    SB.stamp(first, published, POI_RUN)
    assert _meta(published, "built_at") == POI_RUN
    return published


def _changeset_to(tmp, old, new):
    """What a rider holding `old` ends up with once `new` is published."""
    rider = _in(tmp, "rider-" + os.path.basename(old))
    shutil.copyfile(old, rider)
    change = _in(tmp, "change-" + os.path.basename(new) + ".tbchg")
    C.build_changeset(old, new, change)
    C.apply_changeset(rider, change)
    return rider


def test_a_container_rule_3_never_met_is_what_the_builder_made():
    """No ways_cut where built_at is still the pack stamp, so every region
    that is not restamped stays byte-for-byte the build golden.py holds."""
    tmp = tempfile.mkdtemp()
    try:
        published = build(_in(tmp, "published.tbmap"))
        again = build(_in(tmp, "again.tbmap"), previous=published)
        SB.stamp(published, again, POI_RUN)
        check("no ways_cut on an unchanged region",
              _meta(again, "ways_cut") is None, _meta(again, "ways_cut"))
        check("whose cut date is its built_at, the pack stamp",
              SB.ways_cut(again) == WAYS_STAMP, SB.ways_cut(again))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_poi_refresh_moves_the_build_and_not_the_cut():
    """THE CASE: the ways did not change, a fuel station did."""
    tmp = tempfile.mkdtemp()
    try:
        published = build(_in(tmp, "published.tbmap"))
        built = build(_in(tmp, "built.tbmap"), pois=MORE_POIS,
                      previous=published)
        verdict, stamp = SB.stamp(published, built, POI_RUN)
        check("PREMISE: it is a rule-3 restamp", verdict == "changed"
              and stamp == POI_RUN, (verdict, stamp))
        check("the build id moved", _meta(built, "built_at") == POI_RUN)
        check("the date riders are shown did not",
              _meta(built, "ways_cut") == WAYS_STAMP,
              _meta(built, "ways_cut"))
        check("ways_cut() says so", SB.ways_cut(built) == WAYS_STAMP)

        rider = _changeset_to(tmp, published, built)
        check("PREMISE: the rider's copy is now the new build",
              _meta(rider, "built_at") == POI_RUN, _meta(rider, "built_at"))
        check("and still says its lanes were cut when they were",
              _meta(rider, "ways_cut") == WAYS_STAMP, _meta(rider, "ways_cut"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_second_refresh_keeps_the_first_cut():
    tmp = tempfile.mkdtemp()
    try:
        published = _restamped(tmp)
        built = build(_in(tmp, "built.tbmap"), pois=[_poi("osm:n9")],
                      previous=published)
        SB.stamp(published, built, "2026-11-24T06:00:00Z")
        check("PREMISE: restamped again",
              _meta(built, "built_at") == "2026-11-24T06:00:00Z")
        check("the lanes were still cut when they were",
              _meta(built, "ways_cut") == WAYS_STAMP, _meta(built, "ways_cut"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_same_content_after_a_restamp_stays_unchanged():
    """A fresh build never carries ways_cut. Hashed, it would make every
    region rule 3 once met read as changed on every run after."""
    tmp = tempfile.mkdtemp()
    try:
        published = _restamped(tmp)
        built = build(_in(tmp, "built.tbmap"), pois=MORE_POIS,
                      previous=published)
        verdict, stamp = SB.stamp(published, built, "2026-11-01T00:00:00Z")
        check("unchanged", verdict == "unchanged", verdict)
        check("the published build", stamp == POI_RUN, stamp)
        check("and the published cut date", _meta(built, "ways_cut") ==
              WAYS_STAMP, _meta(built, "ways_cut"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_ways_change_moves_the_cut():
    """After a restamp, new lanes: the date riders see is the new pack stamp,
    on the publisher and on the rider's patched copy."""
    tmp = tempfile.mkdtemp()
    try:
        published = _restamped(tmp)
        for later, now in (("2026-11-02T03:04:05Z", "2026-11-03T00:00:00Z"),
                           # a pack stamp from before the restamp: rule 3
                           ("2026-10-02T03:04:05Z", "2026-11-03T00:00:00Z")):
            built = build(_in(tmp, "built-%s.tbmap" % later[:10]),
                          pois=MORE_POIS, stamp=later, previous=published)
            db = sqlite3.connect(built)
            db.execute("UPDATE ways SET name = 'Amended'")
            db.commit()
            db.close()
            SB.stamp(published, built, now)
            check("new lanes, new cut date (%s)" % later,
                  SB.ways_cut(built) == later, SB.ways_cut(built))
            rider = _changeset_to(tmp, published, built)
            check("and on the rider's copy (%s)" % later,
                  SB.ways_cut(rider) == later, SB.ways_cut(rider))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_manifest_carries_the_cut_beside_the_build():
    tmp = tempfile.mkdtemp()
    try:
        pub = _in(tmp, "containers")
        new = os.path.join(tmp, "dist", "containers")
        os.makedirs(pub)
        os.makedirs(new)
        build(os.path.join(pub, "ways-a.tbmap"))
        build(os.path.join(new, "ways-a.tbmap"), pois=MORE_POIS,
              previous=os.path.join(pub, "ways-a.tbmap"))
        manifest = os.path.join(new, "manifest.json")
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump({"generated": WAYS_STAMP, "containers": [
                {"file": "containers/ways-a.tbmap", "generated": WAYS_STAMP},
            ]}, fh)
        SB.stamp_tree(pub, manifest, POI_RUN, log=lambda *a: None)
        with open(manifest, encoding="utf-8") as fh:
            entry = json.load(fh)["containers"][0]
        check("PREMISE: generated is the new build",
              entry["generated"] == POI_RUN, entry)
        check("waysCut is when the lanes were cut",
              entry.get("waysCut") == WAYS_STAMP, entry)
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

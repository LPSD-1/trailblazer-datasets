#!/usr/bin/env python3
"""A way two areas carry is drawn by BOTH areas' tiles, through build_all.

    python tools/test_every_area_draws_its_own_ways.py

THE FAULT THIS EXISTS FOR. build_containers.build_all passed
`tile_exclude=claimed` to write_container, so an area's tiles left out every
way an area EARLIER in manifest order had already drawn. The record stayed and
the line went. On the published set of 2 Oct 2026 a rider who downloaded only
Wales was shown 536 of the 1,390 byways Wales carries; North drew 1,042 of
1,792, Midlands 1,771 of 2,138, East Anglia 1,084 of 1,444. Only South West,
first in the manifest, drew everything.

The rule it came from (MAP_ARCHITECTURE.md 19.2 as first written) assumed tiles
had no dedupe. The app has one: PmTilesServer._tileFor merges the tiles of
every mounted container of the same depth and mergeVectorTiles keeps the first
feature with a given id. So the build must draw a shared way in EVERY area that
carries it, with the SAME feature id in each, and leave the once-only drawing
to the app. Both halves are checked here, through the shipping build_all.

Exit 0 clean, 1 on a failure.
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_containers as C           # noqa: E402
import build_map_container as B        # noqa: E402
import build_packages as P             # noqa: E402
import check_containers as CC          # noqa: E402
import test_build_containers as TB     # noqa: E402  (its way and key)
from test_mvt import decode_tile       # noqa: E402

_passed = 0
_failed = []

#: Packed into both areas, as build_packages publishes a way straddling a
#: region boundary. On the Wales / South West line near Chepstow.
SHARED = "MN-1-shared0001"

#: Manifest order matters: the old exclusion took a way away from every area
#: AFTER the first one that carried it, so the area that must still draw it
#: is the SECOND here.
AREAS = (("south-west", "South West", -2.80, 51.60),
         ("wales", "Wales", -3.40, 52.20))


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def _settled(before):
    """Under pytest a test must fail itself; under main() the list reports."""
    if "pytest" in sys.modules and len(_failed) > before:
        raise AssertionError("; ".join(_failed[before:]))


def pipeline_with_a_shared_way():
    """packs -> containers through build_all, two areas sharing one way.

    Returns (container manifest, tmp dir). The caller removes tmp.
    """
    tmp = tempfile.mkdtemp(prefix="tbways-shared-")
    real_dist = P.dist_dir
    P.dist_dir = lambda: tmp
    try:
        packs = []
        for region, label, lon, lat in AREAS:
            feats = [TB._way("%s-own" % region, "boat", 1, 1, lon, lat),
                     TB._way(SHARED, "boat", 1, 1, -2.70, 51.65,
                             authority="Monmouthshire")]
            packs.append(P.write_package(
                P.DATASET, region, label, None, feats, TB.KEY,
                "2026-03-04T05:06:07Z", note="n"))
        manifest = {"schema": 1, "generated": "2026-09-24T09:00:00Z",
                    "dataset": P.DATASET, **P.context_fields(),
                    "packages": packs}
        mpath = os.path.join(tmp, "manifest.json")
        with io.open(mpath, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest))
        stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            out = C.build_all(mpath, os.path.join(tmp, "containers"), TB.KEY,
                              None, tmp)
        finally:
            sys.stdout = stdout
        return out, tmp
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        P.dist_dir = real_dist


def _drawn(path):
    """{zoom: {lane_uid: feature id}} from the container's own tiles."""
    db = sqlite3.connect(path)
    try:
        out = {}
        for zoom, blob in db.execute(
                "SELECT zoom_level, tile_data FROM tiles"):
            got = out.setdefault(zoom, {})
            for layer in decode_tile(blob):
                for feature in layer["features"]:
                    uid = feature["props"].get("lane_uid")
                    if uid:
                        got[uid] = feature["id"]
        return out
    finally:
        db.close()


def _records(path):
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("SELECT way_uid FROM ways")}
    finally:
        db.close()


def _areas(out, tmp):
    return [(e["area"], os.path.join(tmp, e["file"]))
            for e in out["containers"] if e["kind"] == "area"]


def test_a_way_two_areas_carry_is_drawn_by_both():
    before = len(_failed)
    out, tmp = pipeline_with_a_shared_way()
    try:
        areas = _areas(out, tmp)
        # PREMISE: two areas came out, in manifest order, and BOTH carry the
        # shared way's record. Without that there is nothing to exclude.
        check("two area containers were built, south-west first",
              [a for a, _ in areas] == [a for a, _, _, _ in AREAS], areas)
        for area, path in areas:
            check("%s carries the shared way's record" % area,
                  SHARED in _records(path), sorted(_records(path)))

        ids = {}
        for area, path in areas:
            drawn = _drawn(path)
            records = _records(path)
            # PREMISE: tiles exist at every area zoom.
            check("%s has tiles at z%d-z%d" % ((area,) + B.AREA_ZOOMS),
                  sorted(drawn) == list(range(B.AREA_ZOOMS[0],
                                              B.AREA_ZOOMS[1] + 1)),
                  sorted(drawn))
            for zoom, uids in sorted(drawn.items()):
                missing = sorted(records - set(uids))
                check("%s z%d draws every way it carries" % (area, zoom),
                      not missing, missing)
            ids[area] = drawn.get(B.AREA_ZOOMS[1], {}).get(SHARED)

        # THE APP'S HALF OF THE CONTRACT. mergeVectorTiles drops a feature
        # whose id it has already seen, so the shared way is drawn ONCE by a
        # rider holding both areas only if both areas give it the same id.
        check("the shared way has an id in both areas",
              all(v is not None for v in ids.values()) and len(ids) == 2, ids)
        check("and it is the SAME id, so the app draws it once",
              len(set(ids.values())) == 1, ids)
        check("which is stable_id(lane_uid)",
              set(ids.values()) == {B.stable_id(SHARED)}, ids)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _settled(before)


def test_the_guard_passes_a_set_where_both_areas_draw_it():
    """No retired 'one lane, one area' rule left to refuse the fix."""
    before = len(_failed)
    out, tmp = pipeline_with_a_shared_way()
    try:
        problems, checked = [], []
        for e in out["containers"]:
            path = os.path.join(tmp, e["file"])
            meta, found = CC.check_container(path, problems)
            if meta.get("kind") in ("area", "both"):
                checked.append((path, found))
        check("the guard was handed both areas", len(checked) == 2, checked)
        CC.check_agreement(checked, problems)
        check("and refuses nothing", problems == [], problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _settled(before)


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

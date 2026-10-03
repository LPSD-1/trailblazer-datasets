#!/usr/bin/env python3
"""A rider who downloads ONE area must see every byway that area carries.

THE DEFECT. build_containers.build_all passes `tile_exclude=claimed` to
write_container, so an area's tiles leave out every way an area EARLIER in
manifest order already drew (south-west, south-east, east-anglia, midlands,
wales, north). The way's RECORD stays in the later area; only its line is
removed from that area's tiles.

That rule (MAP_ARCHITECTURE.md 19.2) was written when "tiles have no loader
and no dedupe". The app has since grown exactly that dedupe:
PmTilesServer._tileFor merges the tiles of every mounted container of the same
depth and mergeVectorTiles drops a feature whose id it has already seen, ids
being stable_id(lane_uid) and so equal in every area. So the exclusion buys
nothing for a rider who holds both areas, and for a rider who holds only the
later one it is lanes missing from the map at every zoom from z11: the server
asks only mounted containers, the overview stops at z10, and nothing else
draws them.

MEASURED on the published containers, 2 Oct 2026 (z14 tiles against records):

    wales        1390 ways carried,  536 drawn,  854 not drawn by Wales
                 (Shropshire 171, Powys 169, Carmarthenshire 113, ...)
    north        1792 carried, 1042 drawn,  750 not drawn
    midlands     2138 carried, 1771 drawn,  367 not drawn
    east-anglia  1444 carried, 1084 drawn,  360 not drawn
    south-east   3954 carried, 3940 drawn,   14 not drawn
    south-west   1858 carried, 1858 drawn,    0

A rider in Carmarthenshire who downloads "Wales" is shown 536 of its 1390
byways; tapping where a missing one runs finds its record but nothing is drawn.

THE CHECK: for every area container in containers/manifest.json, every way
record must be drawn by that container's own tiles at its top zoom.

Run from the repository root:
    python test/hunt/lane-data/test_every_area_draws_every_way_it_carries.py
Exit 0 clean, 1 on the defect, 2 when the published containers are absent.
"""
import collections
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def check():
    manifest_path = os.path.join(ROOT, "containers", "manifest.json")
    if not os.path.exists(manifest_path):
        print("BLIND: no containers/manifest.json")
        return 2
    from test_mvt import decode_tile  # noqa: E402

    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    areas = [e for e in manifest.get("containers", [])
             if e.get("kind") == "area"]
    # PREMISE: there is something to look at. Zero areas is BLIND, not clean.
    if not areas:
        print("BLIND: the manifest lists no area containers")
        return 2

    records_seen = 0
    tiles_seen = 0
    failures = []
    for entry in areas:
        path = os.path.join(ROOT, entry["file"])
        if not os.path.exists(path):
            print("BLIND: %s is listed and missing" % entry["file"])
            return 2
        db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                             uri=True)
        try:
            records = dict(db.execute("SELECT way_uid, authority FROM ways"))
            meta = dict(db.execute("SELECT key, value FROM meta"))
            top = int(meta.get("max_zoom") or 14)
            drawn = set()
            for (blob,) in db.execute(
                    "SELECT tile_data FROM tiles WHERE zoom_level = ?",
                    (top,)):
                tiles_seen += 1
                for layer in decode_tile(blob):
                    for feature in layer["features"]:
                        uid = feature["props"].get("lane_uid")
                        if uid:
                            drawn.add(uid)
        finally:
            db.close()
        records_seen += len(records)
        missing = [u for u in records if u not in drawn]
        by_authority = collections.Counter(records[u] for u in missing)
        print("  %-12s %5d ways carried, %5d drawn at z%d, %5d NOT drawn  %s"
              % (entry.get("area"), len(records), len(records) - len(missing),
                 top, len(missing),
                 ", ".join("%s %d" % kv for kv in by_authority.most_common(4))))
        if missing:
            failures.append((entry.get("area"), len(records), len(missing),
                             sorted(missing)[:3]))

    # PREMISE: records and tiles were actually read.
    if not records_seen or not tiles_seen:
        print("BLIND: read %d records and %d tiles" % (records_seen,
                                                       tiles_seen))
        return 2

    if failures:
        print("")
        print("FAIL: a rider holding only one of these areas is not shown "
              "every byway it carries:")
        for area, carried, missing, examples in failures:
            print("  %s: %d of %d ways are in its records and in none of its "
                  "tiles (e.g. %s)" % (area, missing, carried,
                                       ", ".join(examples)))
        print("Cause: build_containers.build_all tile_exclude=claimed. The app "
              "already merges and dedupes peer tiles by id "
              "(pmtiles_server.dart mergeVectorTiles).")
        return 1
    print("OK: every area draws every way it carries")
    return 0


def test_every_area_draws_every_way_it_carries():
    code = check()
    if code == 2:
        import pytest
        pytest.skip("published containers absent")
    assert code == 0


if __name__ == "__main__":
    sys.exit(check())

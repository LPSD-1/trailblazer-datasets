#!/usr/bin/env python3
"""One line on the ground is one way: never two records with the same line.

THE DEFECT. build_packages.load_all collapses a council record listed twice
only when its whole lane_uid repeats - same authority, same PATH NUMBER, same
coordinates. duplicate_ways.merge then removes the same way recorded by two
DIFFERENT authorities (Pencelli, 26 Sep). Nothing removes the same line
recorded twice by ONE authority under two numbers or two naming schemes, so
the map draws it twice, a tap finds two lanes, and every lane count carries it
twice.

MEASURED on the published containers, 2 Oct 2026: 41 lines are published as
two ways with BYTE-IDENTICAL geometry (82 records) - the 10-hex geometry hash
in both uids is the same:

    Gwynedd          26  the same record under its Welsh name and its code:
                         "Prow Aberdyfi (byw) Rhif 8#1" = "Y35y8(bwy)#2"
    Cambridgeshire   11  e.g. CB-12 and CB-14 (0bea8a1367), CB-24 and CB-26
    Hertfordshire     3  e.g. HD-003 and HD-056 (c8bbe3074b)
    Devon             1  DN-46 and DN-79 (84fa9e22df)

(A further ~47 same-authority pairs lie within 15 m of each other along their
whole length - BK-18 / BK-9 at 3.7 m, Bedford's "10 K&S" / "8 WYM" - drawn as
two parallel lines exactly as Pencelli was. They are printed, not asserted:
identical geometry is the case nobody can argue is two ways.)

THE CHECK: across every published area container, no two way records with
different uids may carry identical geometry.

Run from the repository root:
    python test/hunt/lane-data/test_one_line_is_not_published_as_two_ways.py
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
    import build_fords as BF  # noqa: E402  the round-tripped geometry reader

    with open(manifest_path, encoding="utf-8") as fh:
        areas = [e for e in json.load(fh).get("containers", [])
                 if e.get("kind") == "area"]
    if not areas:
        print("BLIND: the manifest lists no area containers")
        return 2

    ways = {}
    for entry in areas:
        path = os.path.join(ROOT, entry["file"])
        if not os.path.exists(path):
            print("BLIND: %s is listed and missing" % entry["file"])
            return 2
        db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                             uri=True)
        try:
            for uid, authority, name, blob in db.execute(
                    "SELECT way_uid, authority, name, geometry FROM ways"):
                if uid not in ways:
                    ways[uid] = (authority, name,
                                 tuple(tuple(line)
                                       for line in BF.unpack_geometry(blob)))
        finally:
            db.close()

    # PREMISE: ways with real geometry were read.
    with_lines = [u for u, (_, _, g) in ways.items() if g and len(g[0]) >= 2]
    if not with_lines:
        print("BLIND: read %d ways and none had a line" % len(ways))
        return 2
    print("  %d distinct ways read, %d with a line" % (len(ways),
                                                       len(with_lines)))

    by_line = collections.defaultdict(list)
    for uid in with_lines:
        by_line[ways[uid][2]].append(uid)
    twice = [sorted(uids) for uids in by_line.values() if len(uids) > 1]
    twice.sort()

    if twice:
        per_authority = collections.Counter(ways[g[0]][0] for g in twice)
        print("")
        print("FAIL: %d lines are published as more than one way (%d records "
              "for %d lines) - each is drawn twice and counted twice:"
              % (len(twice), sum(len(g) for g in twice), len(twice)))
        for authority, n in per_authority.most_common():
            print("    %-24s %d" % (authority, n))
        for group in twice[:8]:
            print("  same line: %s" % "  |  ".join(
                "%s (%s)" % (u, ways[u][1]) for u in group))
        return 1
    print("OK: no line is published as two ways")
    return 0


def test_one_line_is_not_published_as_two_ways():
    code = check()
    if code == 2:
        import pytest
        pytest.skip("published containers absent")
    assert code == 0


if __name__ == "__main__":
    sys.exit(check())

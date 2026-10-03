#!/usr/bin/env python3
"""`ways.length_m` is the way's length in metres.

THE DEFECT. build_packages.parse_description reads the third field of
rowmaps' Description ('BO|DY:16247|0.113|...') as kilometres and stores it as
`lengthKm`; build_map_container writes `lengthKm * 1000` into `length_m`. The
field is MILES. Derbyshire's DY 18/3 says 0.113 and its line is about 182 m;
0.113 km would be 113 m.

MEASURED on the 8,493 published ways of 100 m or more, 2 Oct 2026: length_m
divided by the length of the way's own geometry has a median of 0.623
(1/1.604), with the 5th to 95th percentile at 0.617-0.630 - a constant factor
of one mile per
kilometre, not survey noise. tools/gradient.py already saw it ("the stored
length_m disagrees with its own geometry by a median factor of 1.609") and
left it as "somebody else's file to fix". Every published way says it is 38%
shorter than it is; any consumer of the column (the schema documents it as
metres, docs/WAYS-SCHEMA.md) is told so, and the legacy .tbpack carries the
same number as `lengthKm`.

THE CHECK: over the published area containers, the median of length_m over
the geometry's own length lies within 10% of 1.

Run from the repository root:
    python test/hunt/lane-data/test_length_m_is_metres.py
Exit 0 clean, 1 on the defect, 2 when the published containers are absent.
"""
import json
import math
import os
import sqlite3
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def _line_m(line):
    total = 0.0
    for a, b in zip(line, line[1:]):
        k = 111320.0 * math.cos(math.radians((a[1] + b[1]) / 2.0))
        total += math.hypot((b[0] - a[0]) * k, (b[1] - a[1]) * 110574.0)
    return total


def check():
    manifest_path = os.path.join(ROOT, "containers", "manifest.json")
    if not os.path.exists(manifest_path):
        print("BLIND: no containers/manifest.json")
        return 2
    import build_fords as BF  # noqa: E402

    with open(manifest_path, encoding="utf-8") as fh:
        areas = [e for e in json.load(fh).get("containers", [])
                 if e.get("kind") == "area"]
    seen = {}
    for entry in areas:
        path = os.path.join(ROOT, entry["file"])
        if not os.path.exists(path):
            print("BLIND: %s is listed and missing" % entry["file"])
            return 2
        db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                             uri=True)
        try:
            for uid, stored, blob in db.execute(
                    "SELECT way_uid, length_m, geometry FROM ways"):
                if uid in seen:
                    continue
                walked = sum(_line_m(l) for l in BF.unpack_geometry(blob))
                seen[uid] = (stored, walked)
        finally:
            db.close()

    # Ways long enough that rounding of the source's three decimals is noise.
    ratios = sorted(stored / walked for stored, walked in seen.values()
                    if walked >= 100.0 and stored)
    # PREMISE: enough ways to say anything.
    if len(ratios) < 100:
        print("BLIND: only %d ways of 100 m or more with a stored length"
              % len(ratios))
        return 2
    median = statistics.median(ratios)
    p5 = ratios[int(0.05 * len(ratios))]
    p95 = ratios[int(0.95 * len(ratios))]
    print("  %d ways: length_m / geometry length  median %.3f  p5 %.3f  "
          "p95 %.3f" % (len(ratios), median, p5, p95))
    if not 0.9 <= median <= 1.1:
        print("")
        print("FAIL: length_m is %.3f of the way's real length on the median "
              "way (1 / %.3f). rowmaps' Description length is miles, read as "
              "kilometres (build_packages.parse_description)."
              % (median, 1.0 / median))
        return 1
    print("OK: length_m is metres")
    return 0


def test_length_m_is_metres():
    code = check()
    if code == 2:
        import pytest
        pytest.skip("published containers absent")
    assert code == 0


if __name__ == "__main__":
    sys.exit(check())

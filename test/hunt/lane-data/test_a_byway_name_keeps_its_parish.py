#!/usr/bin/env python3
"""A byway's published name must identify it: the parish part is kept.

THE DEFECT. rowmaps' Name is `<authority>|<parish or area>|<number>`, and in
most councils the NUMBER IS ONLY UNIQUE WITHIN ITS PARISH: Wiltshire's
reference for a byway is "LACO24" (Lacock 24), Kent's "AE25", Derbyshire's
"Great Hucklow 18/3". build_packages.normalise keeps only the last field -

    path_no = ref.split("|")[-1]
    name = "%s %s" % (rule["designation"], path_no)

- so its own stated intent ("The council's own path number ... how a rider or
a council officer would refer to this specific way") is not met: the parish
is thrown away and the name no longer says which way it is.

MEASURED from the council cache, 2 Oct 2026: Wiltshire's "BOAT 1" is 21
different byways in 21 parishes, "BOAT 24" nine; 60 Wiltshire numbers, 44
Shropshire, 31 Kent and 21 Derbyshire numbers are each shared by byways in
different parishes. Across the published containers 6,711 of 10,231 ways share
their exact name with another way of the same authority. A rider reading the
lane sheet, searching for a byway by its council reference, or quoting it to
the council or against a traffic order cannot tell which one is meant.

THE CHECK, on the builder itself (no data needed): two council records with
the same number in different parishes of one authority must come out of
normalise() with different names, and each name must carry its parish code.

Run from the repository root:
    python test/hunt/lane-data/test_a_byway_name_keeps_its_parish.py
Exit 0 clean, 1 on the defect, 2 when build_packages cannot be imported.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "tools"))

#: Real council references, verbatim from cache/WT and cache/DY (24 Sep 2026),
#: with short real-shaped lines.
RECORDS = [
    ("WT", "Wiltshire", "WT|LACO|24",
     [[-2.10642, 51.41366], [-2.10480, 51.41144]]),
    ("WT", "Wiltshire", "WT|ORCH|24",
     [[-1.86000, 51.20000], [-1.85800, 51.19900]]),
    ("DY", "Derbyshire", "DY|Great Hucklow-WD41|18/3",
     [[-1.73368, 53.29869], [-1.73221, 53.29955]]),
    ("DY", "Derbyshire", "DY|Stoney Middleton-WD93|18/3",
     [[-1.66000, 53.27500], [-1.65800, 53.27600]]),
]


def _feature(name, coords):
    return {
        "type": "Feature",
        "properties": {"Name": name,
                       "Description": "BO|X:1|0.100|none|0|0|0|0|0,0|0,0"},
        "geometry": {"type": "LineString", "coordinates": coords},
    }


def check():
    try:
        import build_packages as BP  # noqa: E402
    except (ImportError, SystemExit) as e:
        print("BLIND: build_packages will not import (%s)" % e)
        return 2

    out = []
    for code, authority, ref, coords in RECORDS:
        lane = BP.normalise(_feature(ref, coords), code, authority,
                            "byway_open_to_all_traffic")
        if lane is None:
            print("BLIND: normalise refused the fixture %s" % ref)
            return 2
        out.append((ref, lane["properties"]["name"]))
    # PREMISE: the fixture went through the real normalise.
    print("  %d council records normalised" % len(out))
    for ref, name in out:
        print("    %-32s -> %r" % (ref, name))

    problems = []
    names = {}
    for (code, authority, ref, _), (_, name) in zip(RECORDS, out):
        parish = ref.split("|")[1]
        key = (authority, name)
        if key in names:
            problems.append("%s and %s are different byways and both "
                            "publish as %r" % (names[key], ref, name))
        names.setdefault(key, ref)
        # The parish CODE as the council writes it: LACO, or the parish name
        # before rowmaps' '-WD41' sheet suffix.
        parish_word = parish.split("-")[0]
        if parish_word.lower() not in name.lower():
            problems.append("%s publishes as %r: its parish %r is gone"
                            % (ref, name, parish_word))

    if problems:
        print("")
        print("FAIL: published names do not identify the byway:")
        for p in problems:
            print("  " + p)
        print("Cause: build_packages.normalise keeps only "
              "ref.split('|')[-1].")
        return 1
    print("OK: every name keeps its parish")
    return 0


def test_a_byway_name_keeps_its_parish():
    code = check()
    if code == 2:
        import pytest
        pytest.skip("build_packages unavailable")
    assert code == 0


if __name__ == "__main__":
    sys.exit(check())

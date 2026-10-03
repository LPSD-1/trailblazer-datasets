#!/usr/bin/env python3
"""One council byway is one lane, not one lane per piece the source drew.

THE DEFECT. rowmaps delivers many councils' byways as several LineString
pieces under ONE council reference - Kent's `KT|AW|339` is 16 pieces, `KT|AE|101`
15, `KT|CB|199` 5 - usually split where the way crosses a road or a parish
line, end touching end. build_packages.normalise makes every piece its own
way: the lane_uid carries a hash of that piece's coordinates, so the 16
pieces are 16 lanes, each with the same name.

The app was built for the other shape. Lane.lengthM sums `lines` because "a
lane uid with several parts is one right of way the source has drawn in
pieces - a break where it crosses a road, most often"; the container's
geometry blob and build_map_container.lines_of carry several lines per way.
Nothing in the builder ever produces one. So a rider who taps a Kent byway is
told the length of one piece (tens of metres of a 2 km lane), "My lanes" and
starring hold a fragment, "take me to this lane" aims at a fragment, and every
lane count is inflated - Kent's 800 published "byways" are 234 council
records.

MEASURED, 2 Oct 2026:
  * from the council cache: Kent 234 records -> 800 lanes (161 records in more
    than one piece), Wiltshire 92 records in 203 pieces;
  * from the published containers alone: 627 byways whose pieces carry the
    same name in the same authority and meet end to end (within 1 m) are
    published as 2,113 separate lanes - Kent, Surrey, Nottinghamshire,
    Durham, Devon, Isle of Wight, Wokingham, Bedford, ...

THE CHECK, published containers (always present): no two ways of one
authority that carry the same name may meet end to end. And, where
cache/<code>/byway_open_to_all_traffic.json is on disk, no council reference
(the rowmaps Name) may be published as more than one lane.

Run from the repository root:
    python test/hunt/lane-data/test_one_council_byway_is_one_lane.py
Exit 0 clean, 1 on the defect, 2 when the published containers are absent.
"""
import collections
import json
import math
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "tools"))

TOUCH_M = 1.0


def _metres(a, b):
    k = 111320.0 * math.cos(math.radians((a[1] + b[1]) / 2.0))
    return math.hypot((a[0] - b[0]) * k, (a[1] - b[1]) * 110574.0)


def _published():
    manifest_path = os.path.join(ROOT, "containers", "manifest.json")
    if not os.path.exists(manifest_path):
        return None
    import build_fords as BF  # noqa: E402
    with open(manifest_path, encoding="utf-8") as fh:
        areas = [e for e in json.load(fh).get("containers", [])
                 if e.get("kind") == "area"]
    ways = {}
    for entry in areas:
        path = os.path.join(ROOT, entry["file"])
        if not os.path.exists(path):
            return None
        db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                             uri=True)
        try:
            for uid, authority, name, blob in db.execute(
                    "SELECT way_uid, authority, name, geometry FROM ways"):
                if uid not in ways:
                    ways[uid] = (authority, name, BF.unpack_geometry(blob))
        finally:
            db.close()
    return ways


def _split_byways(ways):
    """-> [[uid, ...]] groups of same-authority same-name ways meeting end to
    end, each group one byway published as several lanes."""
    groups = collections.defaultdict(list)
    for uid, (authority, name, lines) in ways.items():
        if lines and len(lines[0]) >= 2:
            groups[(authority, name)].append(uid)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ends = {}
        for u in members:
            lines = ways[u][2]
            ends[u] = [p for line in lines for p in (line[0], line[-1])]
        parent = dict((u, u) for u in members)

        def find(x):
            while parent[x] != x:
                x = parent[x]
            return x

        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if min(_metres(p, q) for p in ends[a]
                       for q in ends[b]) < TOUCH_M:
                    parent[find(a)] = find(b)
        joined = collections.defaultdict(list)
        for u in members:
            joined[find(u)].append(u)
        out.extend(sorted(g) for g in joined.values() if len(g) > 1)
    return sorted(out)


def _from_cache(ways):
    """Council references (rowmaps Name) published as more than one lane, for
    every authority whose BOAT cache is on disk. None when no cache."""
    try:
        import build_packages as BP  # noqa: E402
    except SystemExit:
        return None
    cache = os.path.join(ROOT, "cache")
    authorities_path = os.path.join(cache, "authorities.json")
    if not os.path.exists(authorities_path):
        return None
    with open(authorities_path, encoding="utf-8") as fh:
        authorities = json.load(fh)
    out = {}
    read = 0
    for code in sorted(authorities):
        path = os.path.join(cache, code, "byway_open_to_all_traffic.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            features = json.load(fh).get("features", [])
        lanes = collections.defaultdict(set)
        for f in features:
            lane = BP.normalise(f, code, authorities[code],
                                "byway_open_to_all_traffic")
            if lane is None:
                continue
            read += 1
            uid = lane["properties"]["lane_uid"]
            if uid in ways:
                lanes[f["properties"].get("Name")].add(uid)
        split = dict((ref, uids) for ref, uids in lanes.items()
                     if len(uids) > 1)
        out[code] = (len(lanes), sum(len(u) for u in lanes.values()), split)
    return out if read else None


def check():
    ways = _published()
    if ways is None:
        print("BLIND: published containers absent")
        return 2
    # PREMISE: ways were read.
    if not ways:
        print("BLIND: no ways read from the published containers")
        return 2
    print("  %d distinct published ways read" % len(ways))

    failed = False
    split = _split_byways(ways)
    if split:
        failed = True
        lanes = sum(len(g) for g in split)
        per = collections.Counter(ways[g[0]][0] for g in split)
        print("")
        print("FAIL: %d byways are published as %d separate lanes - same "
              "authority, same name, pieces meeting end to end within %g m:"
              % (len(split), lanes, TOUCH_M))
        for authority, n in per.most_common(8):
            print("    %-24s %d byways" % (authority, n))
        worst = sorted(split, key=len, reverse=True)[:3]
        for g in worst:
            print("  %s (%s) is %d lanes: %s ..."
                  % (ways[g[0]][0], ways[g[0]][1], len(g), ", ".join(g[:3])))

    cached = _from_cache(ways)
    if cached:
        print("")
        print("  against the council cache:")
        for code, (refs, lanes, split_refs) in sorted(cached.items()):
            print("    %s  %4d council records published as %4d lanes; %d "
                  "records in more than one lane%s"
                  % (code, refs, lanes, len(split_refs),
                     (" (e.g. %s -> %d lanes)" % max(
                         ((r, len(u)) for r, u in split_refs.items()),
                         key=lambda kv: kv[1])) if split_refs else ""))
            if split_refs:
                failed = True
    else:
        print("  (no council cache on disk; the published check decides)")

    if failed:
        return 1
    print("OK: every council byway is one lane")
    return 0


def test_one_council_byway_is_one_lane():
    code = check()
    if code == 2:
        import pytest
        pytest.skip("published containers absent")
    assert code == 0


if __name__ == "__main__":
    sys.exit(check())

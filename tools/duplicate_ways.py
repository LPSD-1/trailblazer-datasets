#!/usr/bin/env python3
"""One byway, one line: the same way recorded by two authorities is kept once.

THE DEFECT, FOUND ON A REAL TABLET ON 26 SEP 2026. Where two authorities both
publish a definitive-map record of the same way, rowmaps carries both, and the
build published both - so the map drew the lane twice, as two parallel lines a
metre or two apart. Near Pencelli (51.9161, -3.3210) the Brecon Beacons
National Park's `B1-000/3` and Powys's `PW-34(A)/1` are the same byway. Each
record has its own id (the id carries the authority code and a hash of that
authority's own coordinates, and two authorities never digitise a line to the
same vertices), so nothing that dedupes on the id could see it.

THE TEST FOR "SAME WAY" IS GEOMETRY. Two records are one way when they have:

  * the SAME class. A BOAT and a restricted byway on one line are two
    authorities disagreeing about the law, and that disagreement is data;
  * DIFFERENT authorities. One authority listing a way twice with identical
    coordinates is already collapsed by build_packages.load_all;
  * lengths within LENGTH_RATIO (the shorter at least 85% of the longer), so
    a short byway that shares part of a long one's line is never merged into
    it; and
  * ONE LINE LYING WHOLLY WITHIN MATCH_M OF THE OTHER. The line is sampled
    every SAMPLE_M metres and every sample must lie within 15 m of the other
    line, measured to its segments, not its vertices.

The last rule is what makes "no byway is lost" a fact rather than a hope: a
record is only ever removed in favour of one that it lies wholly within 15 m
of, so every metre of it is still drawn, within 15 m, by the record kept.
Two byways that cross, or run side by side for a stretch, have a part far
from each other and fail it. Two SHORT byways that meet at a junction do not
- a 12 m stub is never more than 12 m from anything it touches - so the line
must also run ALONG the other rather than across it (MIN_TRAVEL).

It is one-directional rather than the symmetric Hausdorff distance because a
boundary lane is often recorded a little further by one of its two councils:
Hampshire's HP-17/1 lies within 4.3 m of Wiltshire's WT-11 along all 619 m,
and WT-11 runs on 72 m past it. Keeping WT-11 loses nothing; the symmetric
test (72 m) would have left both drawn. The keeper must then be the record
that covers the other, whatever the rank below says.

THE SAME WAY SPLIT DIFFERENTLY, which runs FIRST. One council records a lane
as one way and its neighbour as several (Bracknell Forest's BC-20 is Windsor
and Maidenhead's WC-65/1 to /5). No one-to-one pair of those is within 15% in
length, so they are compared side against side; see _grouped(). It runs
before the one-to-one pass so a split group is taken whole: Hampshire's
HP-17/1, /2 and /3 are Wiltshire's WT-11, and pairing /1 first left /2 and /3
drawn twice. What it leaves is then paired one to one.

WHICH ONE IS KEPT, when both cover each other, by authority_rank() and then
by completeness:

  0  a current council - the highway authority for the way, which maintains
     it and makes the traffic orders the app reads against it;
  1  a National Park Authority. Not a highway authority: the council's record
     is kept and the park's is carried in also_recorded_by. (Which of the two
     holds the definitive map inside a Welsh park is not asserted here; the
     council is the highway authority either way.)
  2  an authority that no longer exists. Cumbria County Council (`CU`) was
     abolished on 1 April 2023; Westmorland and Furness (`W1`) is its
     successor, and 78 of the pairs measured are CU+W1. rowmaps still carries
     the old county's dataset, and for ground a successor has published, the
     successor's record is the current one.

Between two of the same rank - two neighbouring councils recording a
boundary lane - the MORE COMPLETE record is kept: the longer line, then the
one with more vertices, then the lower id, so the choice is the same on every
build.

NOTHING IS DROPPED SILENTLY. The kept record carries every removed one in
`also_recorded_by` - the other authority's id, name and code - which
build_map_container writes into the container's meta.

    python tools/duplicate_ways.py --containers containers

measures all of this on a directory of published containers, read-only, and
exits non-zero if any removed record lies further than MATCH_M from what was
kept in its place.

MEASURED on a copy of the containers published on 24 Sep 2026 (10,342
distinct ways): 111 records removed - 10 as split groups, 101 one to one -
leaving 10,231, and 12,702 region rows become 12,576 (-0.99%; the north
-4.5%). 78
are Westmorland and Furness kept over Cumbria, 6 the Lake District over
Cumbria, 5 Powys or Carmarthenshire over the Brecon Beacons, the rest
neighbouring councils. The furthest any removed line lies from what was kept
is 14.4 m. Why 15 m, from the same data: the one-to-one pairs accepted lie
at most 13.5 m from their twin, and the nearest pair of like length that is
refused lies 23.8 m from its neighbour at its worst point - two ~50 m
Cumbrian stubs numbered 059 and 060 that cross rather than coincide. Every
pair accepted travels at least 0.94 of its length along its twin, against
MIN_TRAVEL's 0.5.

WHAT IT LEAVES DRAWN TWICE, deliberately, measured the same way: 7 kept
records (1.2 km) still lie wholly within 15 m of another authority's longer
line. Short records lying on part of a longer one (Essex's EX-13, 453 m, on
Cambridgeshire's CB-3, 785 m; North Northamptonshire's N2-012 on Bedford's
BF-Y8; stubs of 4-80 m at junctions), and one boundary group whose
Shropshire side runs on a kilometre past Powys's - the "shares a line for
part of its length" case, which is not merged.
"""
import argparse
import collections
import math
import os
import sys

#: How far any part of a removed record may lie from the record kept.
MATCH_M = 15.0

#: The shorter record must be at least this fraction of the longer.
LENGTH_RATIO = 0.85

#: How finely each line is sampled. A long straight segment between two
#: vertices is walked at this step, so a line that bows away between vertices
#: is still measured where it bows.
SAMPLE_M = 5.0

#: Grid cell for finding candidates, in metres. Only lines whose boxes,
#: grown by MATCH_M, share a cell are ever compared.
CELL_M = 1000.0

#: Pass 2's link: two records belong to one group when at least this much of
#: one lies within MATCH_M of the other. High, so that a byway CROSSING
#: another, or meeting it at a junction, never links the two.
GROUP_LINK = 0.8

#: How much of a removed line's own length must TRAVEL along the kept one:
#: its samples, projected onto the kept line, must advance along it by at
#: least this fraction of the removed line's length. Two short stubs meeting
#: at a junction lie within 15 m of each other end to end - a 12 m stub is
#: never more than 12 m from anything it touches - but one runs ACROSS the
#: other, and its samples all project onto one spot. A copy of the same way
#: runs along it (1.0 on the published data; a fork at 60 degrees is 0.5).
MIN_TRAVEL = 0.5

#: Rank per authority code; lower is kept. Absent means 0, a current council.
NATIONAL_PARKS = {
    "B1": "Brecon Beacons National Park",
    "L1": "Lake District National Park",
}
SUPERSEDED = {
    # Abolished 1 April 2023; succeeded by Cumberland and by Westmorland and
    # Furness (W1). rowmaps still publishes the county's dataset under CU.
    "CU": "Cumbria County Council (abolished 1 April 2023)",
}


def authority_rank(code):
    if code in SUPERSEDED:
        return 2
    if code in NATIONAL_PARKS:
        return 1
    return 0


# --------------------------------------------------------------------------
# geometry, in local metres
# --------------------------------------------------------------------------

_M_PER_DEG_LAT = 110574.0
_M_PER_DEG_LON_EQ = 111320.0


def _project(coords, lat0):
    k = _M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))
    return [(lon * k, lat * _M_PER_DEG_LAT) for lon, lat in coords]


def _length(points):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in zip(points, points[1:]))


def _samples(points, step=SAMPLE_M):
    out = [points[0]]
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        seg = math.hypot(bx - ax, by - ay)
        n = max(1, int(math.ceil(seg / step)))
        for i in range(1, n + 1):
            t = i / float(n)
            out.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    return out


def _to_segments(p, points):
    px, py = p
    if len(points) == 1:
        return math.hypot(px - points[0][0], py - points[0][1])
    best = float("inf")
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        if ll == 0.0:
            d = math.hypot(px - ax, py - ay)
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < best:
            best = d
    return best


def _directed(a, b):
    """(max, mean) distance from samples of line a to line b, in metres."""
    ds = [_to_segments(p, b) for p in _samples(a)]
    return max(ds), sum(ds) / len(ds)


def _along(p, points, starts):
    """Where the point of [points] nearest [p] lies along it, in metres from
    its start. [starts] is the running length at each vertex."""
    px, py = p
    best, where = float("inf"), 0.0
    for i, ((ax, ay), (bx, by)) in enumerate(zip(points, points[1:])):
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        t = 0.0 if ll == 0.0 else max(0.0, min(1.0, ((px - ax) * dx +
                                                    (py - ay) * dy) / ll))
        d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < best:
            best, where = d, starts[i] + t * math.sqrt(ll)
    return where


def _travel(a, b):
    """How far line a's samples advance along line b, as a fraction of a's
    own length. ~1 for a copy of b or of part of it; ~0 for a stub that
    meets b end-on and runs across it. See MIN_TRAVEL."""
    la = _length(a)
    if la <= 0.0 or len(b) < 2:
        return 1.0
    starts = [0.0]
    for (ax, ay), (bx, by) in zip(b, b[1:]):
        starts.append(starts[-1] + math.hypot(bx - ax, by - ay))
    pos = [_along(p, b, starts) for p in _samples(a)]
    return sum(abs(y - x) for x, y in zip(pos, pos[1:])) / la


def _fraction_within(a, b, match_m):
    samples = _samples(a)
    near = sum(1 for p in samples if _to_segments(p, b) <= match_m)
    return near / float(len(samples))


def compare(a_coords, b_coords):
    """How far apart two lines are. Coordinates are (lon, lat) degrees.

    -> {a_in_b_m: the furthest any part of a lies from b, b_in_a_m: the same
    the other way, hausdorff_m: the larger of the two, mean_m, length_a_m,
    length_b_m, ratio: the shorter length over the longer, a_along_b: how
    far a travels along b as a fraction of a's length, b_along_a: the same
    the other way}.
    """
    lat0 = (a_coords[0][1] + b_coords[0][1]) / 2.0
    a = _project(a_coords, lat0)
    b = _project(b_coords, lat0)
    la, lb = _length(a), _length(b)
    ab_max, ab_mean = _directed(a, b)
    ba_max, ba_mean = _directed(b, a)
    return {
        "a_in_b_m": ab_max,
        "b_in_a_m": ba_max,
        "hausdorff_m": max(ab_max, ba_max),
        "mean_m": (ab_mean + ba_mean) / 2.0,
        "length_a_m": la,
        "length_b_m": lb,
        "ratio": (min(la, lb) / max(la, lb)) if max(la, lb) > 0 else 1.0,
        "a_along_b": _travel(a, b),
        "b_along_a": _travel(b, a),
    }


def lies_on(measure, direction, match_m=MATCH_M):
    """Whether, in a compare() result, line a lies on line b ([direction]
    "a") or b on a ("b"): wholly within [match_m] of it, AND running along
    it rather than across it (MIN_TRAVEL)."""
    within, along = (("a_in_b_m", "a_along_b") if direction == "a"
                     else ("b_in_a_m", "b_along_a"))
    return measure[within] <= match_m and measure[along] >= MIN_TRAVEL


def is_duplicate(measure, match_m=MATCH_M, ratio=LENGTH_RATIO):
    """Whether a compare() result is one way recorded twice: lengths within
    [ratio], and one line lying on the other - wholly within [match_m] of it
    and running along it (lies_on)."""
    return (measure["ratio"] >= ratio and
            (lies_on(measure, "a", match_m) or lies_on(measure, "b", match_m)))


def within_union(record, others):
    """The furthest any part of [record] lies from the nearest of [others],
    in metres - the number that proves a removed record lost nothing."""
    line = _coords(record)
    lat0 = line[0][1]
    mine = _project(line, lat0)
    theirs = [_project(_coords(o), lat0) for o in others]
    return max(min(_to_segments(p, t) for t in theirs)
               for p in _samples(mine))


# --------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------

def _coords(feature):
    g = feature.get("geometry") or {}
    if g.get("type") == "LineString":
        return [tuple(p[:2]) for p in g.get("coordinates") or []]
    return []


def _code(feature):
    props = feature["properties"]
    code = props.get("authorityCode")
    if code:
        return code
    return (props.get("lane_uid") or "").split("-", 1)[0]


def _class(feature):
    props = feature["properties"]
    return props.get("rowType", props.get("class"))


def _line_length(feature):
    line = _coords(feature)
    return _length(_project(line, line[0][1])) if len(line) >= 2 else 0.0


def _bbox_m(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _box_within(inner, outer, slack):
    return (inner[0] >= outer[0] - slack and inner[1] >= outer[1] - slack
            and inner[2] <= outer[2] + slack and inner[3] <= outer[3] + slack)


def _boxes_touch(a, b, slack):
    return not (a[0] > b[2] + slack or b[0] > a[2] + slack or
                a[1] > b[3] + slack or b[1] > a[3] + slack)


def _candidates(features, match_m):
    """Every (i, j), i < j, of two usable lines of the same class from
    different authorities whose boxes come within [match_m], with the
    lines, boxes and lengths the grid was built from."""
    lines = [_coords(f) for f in features]
    lats = [p[1] for line in lines for p in line]
    if not lats:
        return [], lines, [], []
    # One projection for the whole set, at its middle latitude, for the grid
    # and the cheap box test only. Every pair that survives is re-measured at
    # its own latitude.
    lat0 = (min(lats) + max(lats)) / 2.0
    projected = [_project(line, lat0) if len(line) >= 2 else None
                 for line in lines]
    boxes = [(_bbox_m(p) if p else None) for p in projected]
    lengths = [(_length(p) if p else 0.0) for p in projected]
    cells = {}
    for i, box in enumerate(boxes):
        if box is None:
            continue
        for cx in range(int(math.floor((box[0] - match_m) / CELL_M)),
                        int(math.floor((box[2] + match_m) / CELL_M)) + 1):
            for cy in range(int(math.floor((box[1] - match_m) / CELL_M)),
                            int(math.floor((box[3] + match_m) / CELL_M)) + 1):
                cells.setdefault((cx, cy), []).append(i)
    # A little slack over match_m: the shared projection is off by a fraction
    # of a percent away from lat0, and a pair it rejected would never reach
    # the exact measurement.
    slack = match_m * 1.1 + 2.0
    seen = set()
    out = []
    for members in cells.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                i, j = sorted((members[x], members[y]))
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                if _class(features[i]) != _class(features[j]):
                    continue
                if _code(features[i]) == _code(features[j]):
                    continue
                if not _boxes_touch(boxes[i], boxes[j], slack):
                    continue
                out.append((i, j))
    out.sort()
    return out, lines, boxes, lengths


def find_pairs(features, match_m=MATCH_M, ratio=LENGTH_RATIO):
    """Every pair (i, j, measure) of [features] that are one way recorded by
    two authorities, one record each. i < j, indices into [features]."""
    candidates, lines, boxes, lengths = _candidates(features, match_m)
    slack = match_m * 1.1 + 2.0
    out = []
    for i, j in candidates:
        li, lj = lengths[i], lengths[j]
        if max(li, lj) > 0 and min(li, lj) / max(li, lj) < ratio - 0.02:
            continue
        # A line within d of another has its box inside the other's grown
        # by d.
        if not (_box_within(boxes[i], boxes[j], slack) or
                _box_within(boxes[j], boxes[i], slack)):
            continue
        m = compare(lines[i], lines[j])
        if is_duplicate(m, match_m, ratio):
            out.append((i, j, m))
    return out


def _keeper_order(feature, length_m):
    """Sort key: the smallest is kept. See the module docstring."""
    return (authority_rank(_code(feature)), -round(length_m, 3),
            -len(_coords(feature)), feature["properties"].get("lane_uid") or "")


def _components(edges):
    """Connected components of [edges], each a sorted list, in order of their
    smallest member."""
    parent = {}

    def find(i):
        while parent.get(i, i) != i:
            i = parent[i]
        return i

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups = {}
    for a, b in edges:
        for n in (a, b):
            groups.setdefault(find(n), set()).add(n)
    return [sorted(groups[r]) for r in sorted(groups)]


# --------------------------------------------------------------------------
# the two passes
# --------------------------------------------------------------------------

def _pairwise(features, match_m, ratio):
    """Pass 1: one record against one. Run AFTER pass 2 (see merge()), on
    the records pass 2 left.

    -> {removed index: ([keeper index], within_m)}.
    """
    pairs = find_pairs(features, match_m, ratio)
    matched = dict(((i, j), m) for i, j, m in pairs)

    def lies_within(k, keep):
        """How far record k lies from record keep; None past match_m."""
        m = matched.get((min(k, keep), max(k, keep)))
        if m is None:
            return None
        direction = "a" if k < keep else "b"
        if not lies_on(m, direction, match_m):
            return None
        return m["a_in_b_m"] if direction == "a" else m["b_in_a_m"]

    out = {}
    for members in _components([(i, j) for i, j, _ in pairs]):
        # THE KEEPER MUST COVER WHAT IT REPLACES. Among the records that do
        # (the most covered first), the rank and completeness rule decides.
        keep = min(members, key=lambda k: (
            -sum(1 for o in members
                 if o != k and lies_within(o, k) is not None),
            _keeper_order(features[k], _line_length(features[k]))))
        for k in members:
            if k == keep:
                continue
            within = lies_within(k, keep)
            if within is not None:
                out[k] = ([keep], within)
    return out


def _grouped(features, alive, match_m, ratio):
    """Pass 2: the same way SPLIT DIFFERENTLY by two authorities. Run FIRST,
    on every record (see merge()).

    MEASURED on the published containers: records lay wholly within 15 m of
    another authority's because one council records the lane as one way and
    its neighbour as several - Bracknell Forest's BC-20 is Windsor and
    Maidenhead's WC-65/1 to /5. No pair of them is within 15% in length, so
    the one-to-one pass cannot see them, and the map drew every one twice.

    So, for each pair of authorities and each class, records are LINKED where
    GROUP_LINK of one lies within [match_m] of the other, and each linked
    group of three or more is compared SIDE AGAINST SIDE: one authority's
    records together against the other's. The group is one way recorded
    twice when the two sides' total lengths are within [ratio] and one side
    lies wholly within [match_m] of the other - pass 1's two tests, applied to
    the union. The side that covers the other is kept (by rank, then total
    length, when both do) and every record on the other side is removed. A
    group whose sides do not agree - one side running on well past the other
    - is left alone, whole.

    -> {removed index: ([keeper indices], within_m)}.
    """
    sub = [features[i] for i in alive]
    candidates, lines, _, _ = _candidates(sub, match_m)
    links = {}
    for a, b in candidates:
        lat0 = lines[a][0][1]
        pa, pb = _project(lines[a], lat0), _project(lines[b], lat0)
        if max(_fraction_within(pa, pb, match_m),
               _fraction_within(pb, pa, match_m)) >= GROUP_LINK:
            key = (_class(sub[a]),) + tuple(sorted((_code(sub[a]),
                                                    _code(sub[b]))))
            links.setdefault(key, []).append((a, b))

    out = {}
    gone = set()
    for key in sorted(links):
        for members in _components(links[key]):
            if len(members) < 3 or any(n in gone for n in members):
                continue  # a pair is pass 1's; and a record goes only once
            sides = {}
            for n in members:
                sides.setdefault(_code(sub[n]), []).append(n)
            (c1, s1), (c2, s2) = sorted(sides.items())
            l1 = sum(_line_length(sub[n]) for n in s1)
            l2 = sum(_line_length(sub[n]) for n in s2)
            if max(l1, l2) <= 0 or min(l1, l2) / max(l1, l2) < ratio:
                continue
            f1 = [sub[n] for n in s1]
            f2 = [sub[n] for n in s2]
            one_in_two = max(within_union(f, f2) for f in f1)
            two_in_one = max(within_union(f, f1) for f in f2)
            options = []
            if two_in_one <= match_m:
                options.append(((authority_rank(c1), -round(l1, 3), c1),
                                s1, s2))
            if one_in_two <= match_m:
                options.append(((authority_rank(c2), -round(l2, 3), c2),
                                s2, s1))
            if not options:
                continue
            _, keep, drop = min(options)
            keep_f = [sub[k] for k in keep]
            for n in drop:
                # Carried on the kept records it actually runs along, not on
                # every record of the side.
                lat0 = lines[n][0][1]
                mine = _project(lines[n], lat0)
                near = [k for k in keep if _fraction_within(
                    mine, _project(lines[k], lat0), match_m) > 0]
                out[alive[n]] = ([alive[k] for k in (near or keep)],
                                 within_union(sub[n], keep_f))
                gone.add(n)
    return out


def _absorb(keeper, gone):
    """Carry [gone] - and whatever it had itself absorbed - on [keeper]."""
    props = keeper["properties"]
    entries = list(props.get("also_recorded_by") or [])
    have = set(e.get("way_uid") for e in entries)
    p = gone["properties"]
    for e in [{"way_uid": p.get("lane_uid"),
               "authority": p.get("authority"),
               "authority_code": _code(gone),
               "name": p.get("name")}] + list(p.get("also_recorded_by") or []):
        if e.get("way_uid") not in have:
            entries.append(e)
            have.add(e.get("way_uid"))
    entries.sort(key=lambda e: e.get("way_uid") or "")
    props["also_recorded_by"] = entries


def merge(features, match_m=MATCH_M, ratio=LENGTH_RATIO):
    """-> (kept, removed): [features] with each cross-authority duplicate
    removed, in input order, and [(removed, [kept in its place], within_m)],
    where within_m is the furthest any part of the removed line lies from
    the kept ones - at most [match_m], by construction, and re-measured by
    main().

    Each kept feature gains `also_recorded_by`: one entry per record removed
    in its favour, sorted by id. A record is only ever removed in favour of
    records it lies wholly within [match_m] of.
    """
    # THE SPLIT PASS RUNS FIRST. Run second, it met Hampshire's HP-17 - which
    # is Wiltshire's WT-11 split into /1 (622 m), /2 (4 m) and /3 (65 m) - only
    # after pass 1 had taken /1 as WT-11's one-to-one twin (622/712 is within
    # 15%), and the /2 and /3 left behind were no longer within 15% of WT-11:
    # 69 m of the lane stayed drawn twice. Grouped first, the three go
    # together; a group whose sides disagree is left whole, for pass 1.
    everyone = list(range(len(features)))
    grouped = _grouped(features, everyone, match_m, ratio)
    alive = [i for i in everyone if i not in grouped]
    paired = _pairwise([features[i] for i in alive], match_m, ratio)
    paired = dict((alive[k], ([alive[x] for x in keeps], within))
                  for k, (keeps, within) in paired.items())
    if not grouped and not paired:
        return list(features), []
    # Absorbed in the order removed; a record is only ever removed once, and
    # never in favour of a record that is itself removed.
    for removed_at in (grouped, paired):
        for k in sorted(removed_at):
            for keep in removed_at[k][0]:
                _absorb(features[keep], features[k])
    removed_at = dict(grouped)
    removed_at.update(paired)
    removed = [(features[k], [features[x] for x in removed_at[k][0]],
                removed_at[k][1]) for k in sorted(removed_at)]
    kept = [f for i, f in enumerate(features) if i not in removed_at]
    return kept, removed


# --------------------------------------------------------------------------
# measuring the published containers
# --------------------------------------------------------------------------

_CLASS_TO_ROW = {"boat": "byway_open_to_all_traffic",
                 "restricted_byway": "restricted_byway",
                 "bridleway": "bridleway", "osm_track": "osm_track"}


def features_from_containers(directory):
    """Every way in the published area containers, once per uid, in the pack
    feature shape merge() reads. Opened read-only; nothing is written."""
    import glob
    import sqlite3
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_fords as BF  # the geometry reader, round-trip tested

    seen = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.tbmap"))):
        db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                             uri=True)
        try:
            names = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "ways" not in names:
                continue
            for uid, cls, auth, name, geom, length in db.execute(
                    "SELECT way_uid, way_class, authority, name, geometry, "
                    "length_m FROM ways"):
                if uid in seen:
                    continue
                lines = BF.unpack_geometry(geom)
                if len(lines) != 1:
                    continue
                seen[uid] = {
                    "type": "Feature",
                    "properties": {
                        "lane_uid": uid, "class": cls,
                        "rowType": _CLASS_TO_ROW.get(cls, cls),
                        "authority": auth, "name": name,
                        "authorityCode": uid.split("-", 1)[0],
                        "lengthKm": (length or 0) / 1000.0,
                    },
                    "geometry": {"type": "LineString",
                                 "coordinates": [list(p) for p in lines[0]]},
                }
        finally:
            db.close()
    return [seen[k] for k in sorted(seen)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--containers", required=True,
                    help="a directory of published .tbmap files, read-only")
    ap.add_argument("--examples", type=int, default=10)
    args = ap.parse_args(argv)

    features = features_from_containers(args.containers)
    print("distinct ways read: %d" % len(features))
    kept, removed = merge(features)
    print("cross-authority duplicates removed: %d" % len(removed))
    print("ways kept: %d" % len(kept))
    pairs = collections.Counter(
        "%s kept over %s" % ("+".join(sorted(set(_code(k) for k in keeps))),
                             _code(r)) for r, keeps, _ in removed)
    for label, n in pairs.most_common():
        print("  %4d  %s" % (n, label))

    # THE PROOF THAT NO WAY IS LOST, re-measured from the output rather than
    # read back from the decision: every removed record against the records
    # kept in its place, in the direction that matters - how far any part of
    # the removed line lies from them.
    kept_ids = set(id(f) for f in kept)
    worst = 0.0
    for r, keeps, _ in removed:
        if not keeps or any(id(k) not in kept_ids for k in keeps):
            print("  NOT KEPT: %s was removed in favour of a record that is "
                  "not in the output" % r["properties"]["lane_uid"])
            return 1
        within = within_union(r, keeps)
        worst = max(worst, within)
        uid = r["properties"]["lane_uid"]
        if not all(any(e.get("way_uid") == uid
                       for e in k["properties"].get("also_recorded_by") or [])
                   for k in keeps):
            print("  NOT CARRIED: %s is in no kept record's also_recorded_by"
                  % uid)
            return 1
    print("furthest any removed line lies from what was kept in its place: "
          "%.2f m (limit %.0f m)" % (worst, MATCH_M))
    for r, keeps, within in removed[:args.examples]:
        print("  removed %-28s kept %-44s within %5.1f m  %5.0f m"
              % (r["properties"]["lane_uid"],
                 ", ".join(k["properties"]["lane_uid"] for k in keeps),
                 within, _line_length(r)))
    return 1 if worst > MATCH_M else 0


if __name__ == "__main__":
    sys.exit(main())

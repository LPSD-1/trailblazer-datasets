#!/usr/bin/env python3
"""One way recorded by two authorities is published once, and nothing is lost.

    python tools/test_duplicate_ways.py

THE DEFECT, FOUND ON A REAL TABLET ON 26 SEP 2026: near Pencelli the Brecon
Beacons National Park's B1-000/3 and Powys's PW-34(A)/1 are the same byway,
both were published, and the map drew it twice, a metre or two apart.

Every fixture here is two DIFFERENT digitisations - offset, and with vertices
in different places - because two authorities never share coordinates, and a
fixture that did would pass a matcher that only compares vertices. The
records are made by the real normaliser, and the merge is reached through
build_packages.select_ways, the function main() and golden.py both call.
"""
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_map_container as B    # noqa: E402
import build_packages as P         # noqa: E402
import duplicate_ways as D         # noqa: E402

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + repr(detail)) if detail else ""))


# ------------------------------------------------------------- fixtures

LAT0, LON0 = 51.9161, -3.3210           # Pencelli
M_LAT = 1.0 / 110574.0                  # degrees per metre north
M_LON = 1.0 / (111320.0 * math.cos(math.radians(LAT0)))

NAMES = {"B1": "Brecon Beacons National Park", "PW": "Powys",
         "CT": "Carmarthenshire", "CU": "Cumbria",
         "W1": "Westmorland and Furness", "L1": "Lake District National Park",
         "WX": "Wrexham", "SH": "Shropshire"}

BOAT = "byway_open_to_all_traffic"


def line(start_m=0.0, length_m=500.0, offset_m=0.0, step_m=50.0,
         phase_m=0.0):
    """A gently curving lane running east from Pencelli, [offset_m] north of
    the centreline, with a vertex every [step_m] starting [phase_m] in - so
    two copies never share a vertex."""
    pts = []
    xs = [start_m]
    x = start_m + phase_m if phase_m else start_m + step_m
    while x < start_m + length_m:
        xs.append(x)
        x += step_m
    xs.append(start_m + length_m)
    for x in xs:
        y = 30.0 * math.sin(x / 400.0) + offset_m    # a lane is not straight
        pts.append([round(LON0 + x * M_LON, 7), round(LAT0 + y * M_LAT, 7)])
    return pts


def way(code, ref, coords, row_type=BOAT):
    raw = {"type": "Feature",
           "properties": {"Name": "%s|1|%s" % (code, ref),
                          "Description": "BO|%s:1|0.500|none" % code},
           "geometry": {"type": "LineString", "coordinates": coords}}
    f = P.normalise(raw, code, NAMES.get(code, code), row_type)
    assert f is not None
    return f


def uid(f):
    return f["properties"]["lane_uid"]


def run(features, context="none"):
    removed = []
    kept = P.select_ways(features, context, removed=removed)
    return kept, removed


def kept_uids(kept):
    return sorted(uid(f) for f in kept)


# ------------------------------------------------- the defect, and the keep

def test_the_pencelli_pair_is_published_once_and_the_park_is_carried():
    park = way("B1", "000/3", line(length_m=31.0, step_m=7.0, offset_m=1.5))
    powys = way("PW", "34(A)/1", line(length_m=31.0, step_m=10.0,
                                      phase_m=4.0))
    other = way("PW", "35/1", line(start_m=3000.0, length_m=400.0))
    kept, removed = run([park, powys, other])
    check("the pair is published once", kept_uids(kept) ==
          sorted([uid(powys), uid(other)]), kept_uids(kept))
    check("PREMISE: the two had different ids", uid(park) != uid(powys))
    check("the council's record is the one kept, not the park's",
          [uid(r) for r, _, _ in removed] == [uid(park)], removed)
    carried = powys["properties"].get("also_recorded_by")
    check("the park's record rides on the council's", carried == [{
        "way_uid": uid(park), "authority": "Brecon Beacons National Park",
        "authority_code": "B1",
        "name": "Byway open to all traffic (BOAT) 000/3"}], carried)
    check("a way nobody else records carries nothing",
          "also_recorded_by" not in other["properties"])


def test_the_successor_is_kept_over_the_abolished_county():
    old = way("CU", "001", line(length_m=800.0, step_m=40.0, offset_m=2.0))
    new = way("W1", "001", line(length_m=800.0, step_m=55.0, phase_m=20.0))
    kept, removed = run([old, new])
    check("Westmorland and Furness is kept over Cumbria",
          kept_uids(kept) == [uid(new)], kept_uids(kept))


def test_a_park_is_kept_over_the_abolished_county():
    old = way("CU", "050", line(length_m=600.0, offset_m=-2.0))
    park = way("L1", "050", line(length_m=600.0, step_m=35.0, phase_m=9.0))
    kept, _ = run([old, park])
    check("the Lake District's record is kept over Cumbria's",
          kept_uids(kept) == [uid(park)], kept_uids(kept))


def test_between_two_councils_the_more_complete_record_is_kept():
    shorter = way("WX", "34", line(length_m=1000.0, offset_m=3.0))
    longer = way("PW", "34/2", line(length_m=1060.0, step_m=45.0,
                                    phase_m=11.0))
    kept, removed = run([shorter, longer])
    check("the longer boundary record is kept",
          kept_uids(kept) == [uid(longer)], kept_uids(kept))
    # And the other way round, so the rule is the length and not the name.
    shorter2 = way("PW", "34/3", line(start_m=5000.0, length_m=1000.0,
                                      offset_m=3.0))
    longer2 = way("WX", "35", line(start_m=5000.0, length_m=1060.0,
                                   step_m=45.0, phase_m=11.0))
    kept, _ = run([shorter2, longer2])
    check("whichever council holds it", kept_uids(kept) == [uid(longer2)],
          kept_uids(kept))


def test_the_keeper_must_cover_what_it_replaces_whatever_its_rank():
    """Hampshire's HP-17/1 lies within 4.3 m of Wiltshire's WT-11, which
    runs on 72 m past it. Here the successor is the SHORTER record: keeping
    it by rank would drop 80 m of the old county's line from the map."""
    new = way("W1", "060", line(length_m=1000.0, step_m=45.0, phase_m=7.0))
    old = way("CU", "060", line(length_m=1080.0, offset_m=2.0))
    kept, removed = run([new, old])
    check("the record that covers the other is kept, rank notwithstanding",
          kept_uids(kept) == [uid(old)], kept_uids(kept))
    check("and the one it covers is carried on it",
          [e["way_uid"] for e in old["properties"].get(
              "also_recorded_by", [])] == [uid(new)], old["properties"])


def test_a_record_that_runs_on_past_its_twin_is_merged_not_left_twice():
    """The symmetric test fails this: the longer runs 70 m past."""
    hp = way("PW", "17/1", line(length_m=620.0, offset_m=4.0))
    wt = way("WX", "11", line(length_m=700.0, step_m=35.0, phase_m=5.0))
    kept, _ = run([hp, wt])
    check("one of the two is published", len(kept) == 1, kept_uids(kept))
    check("and it is the one that covers the other",
          kept_uids(kept) == [uid(wt)], kept_uids(kept))


# ------------------------------------------------- what must NOT merge

def test_ways_that_meet_end_to_end_are_both_kept():
    a = way("PW", "40/1", line(length_m=500.0))
    b = way("WX", "40", line(start_m=500.0, length_m=500.0, step_m=35.0))
    kept, _ = run([a, b])
    check("two ways meeting at a boundary are two ways", len(kept) == 2,
          kept_uids(kept))


def test_ways_side_by_side_for_a_stretch_are_both_kept():
    # Together for the first 150 m, then B turns away north.
    a = way("PW", "41/1", line(length_m=600.0))
    pts = line(length_m=150.0, offset_m=3.0, step_m=30.0)
    for k in range(7):
        pts.append([round(pts[-1][0] + 50.0 * M_LON, 7),
                    round(pts[-1][1] + 30.0 * M_LAT, 7)])
    b = way("WX", "41", pts)
    check("PREMISE: the two are within 15% in length",
          D.compare(D._coords(a), D._coords(b))["ratio"] >= 0.85,
          D.compare(D._coords(a), D._coords(b)))
    kept, _ = run([a, b])
    check("a shared stretch does not make one way", len(kept) == 2,
          kept_uids(kept))


def test_a_byway_sharing_part_of_a_longer_ones_line_is_kept():
    long_ = way("PW", "42/1", line(length_m=1000.0))
    part = way("WX", "42", line(length_m=300.0, offset_m=2.0, step_m=33.0))
    check("PREMISE: the short one lies wholly on the long one",
          D.compare(D._coords(part), D._coords(long_))["a_in_b_m"] < 5.0)
    kept, _ = run([long_, part])
    check("a different byway on part of the line is not merged into it",
          len(kept) == 2, kept_uids(kept))


def test_the_distance_limit_holds_on_both_sides():
    a = way("PW", "43/1", line(length_m=800.0))
    near = way("WX", "43", line(length_m=800.0, offset_m=13.5, step_m=45.0,
                                phase_m=10.0))
    kept, _ = run([a, near])
    check("13.5 m apart along their length: one way", len(kept) == 1,
          kept_uids(kept))
    b = way("PW", "44/1", line(start_m=4000.0, length_m=800.0))
    apart = way("WX", "44", line(start_m=4000.0, length_m=800.0,
                                 offset_m=17.0, step_m=45.0, phase_m=10.0))
    kept, _ = run([b, apart])
    check("17 m apart along their length: two ways", len(kept) == 2,
          kept_uids(kept))


def test_a_byway_and_a_restricted_byway_on_one_line_are_both_kept():
    """Two authorities disagreeing about the law is data, not a duplicate."""
    boat = way("PW", "45/1", line(length_m=500.0))
    rb = way("WX", "45", line(length_m=500.0, offset_m=2.0, step_m=35.0),
             row_type="restricted_byway")
    kept, _ = run([boat, rb], context="all")
    check("the BOAT and the restricted byway both stay", len(kept) == 2,
          kept_uids(kept))


def test_one_authority_is_never_merged_with_itself():
    a = way("PW", "46/1", line(length_m=500.0))
    b = way("PW", "46/2", line(length_m=500.0, offset_m=2.0, step_m=35.0))
    kept, _ = run([a, b])
    check("one council's two records stay two", len(kept) == 2,
          kept_uids(kept))


def test_a_chain_is_resolved_through_the_record_that_covers_it():
    """A~B and B~C, with A and C 20 m apart, so A is not C's twin. B lies
    within 10 m of both, so B is the one line that loses nothing of either -
    kept though a park's, because covering outranks rank - and A and C are
    carried on it. Kept by rank instead, A would stay, B go, and C be drawn
    20 m from A: the same lane twice."""
    a = way("PW", "47/1", line(length_m=700.0, offset_m=0.0))
    b = way("B1", "47/3", line(length_m=700.0, offset_m=10.0, step_m=35.0))
    c = way("WX", "47", line(length_m=700.0, offset_m=20.0, step_m=40.0,
                             phase_m=6.0))
    check("PREMISE: A and C are not twins",
          not D.is_duplicate(D.compare(D._coords(a), D._coords(c))))
    kept, removed = run([a, b, c])
    check("one line is drawn", kept_uids(kept) == [uid(b)], kept_uids(kept))
    for r, keeps, within in removed:
        check("%s lies within 15 m of what was kept for it" % uid(r),
              D.within_union(r, keeps) <= D.MATCH_M,
              D.within_union(r, keeps))
    check("both are carried on it",
          sorted(e["way_uid"] for e in
                 b["properties"].get("also_recorded_by", [])) ==
          sorted([uid(a), uid(c)]), b["properties"])


# ------------------------------------------------- split differently

def test_one_way_split_differently_by_two_councils_is_published_once():
    """Bracknell Forest's BC-20 is Windsor and Maidenhead's WC-65/1 to /5."""
    whole = way("SH", "UN3", line(length_m=900.0, offset_m=3.0))
    pieces = [way("PW", "224/%d" % (k + 1),
                  line(start_m=300.0 * k, length_m=300.0, step_m=35.0,
                       phase_m=8.0)) for k in range(3)]
    check("PREMISE: no one piece is within 15% of the whole",
          not D.find_pairs([whole] + pieces))
    kept, removed = run([whole] + pieces)
    check("the way is drawn by one authority, not two",
          len(set(D._code(f) for f in kept)) == 1, kept_uids(kept))
    for r, keeps, within in removed:
        check("%s is within 15 m of what was kept" % uid(r),
              D.within_union(r, keeps) <= D.MATCH_M, within)
        check("%s is carried by what was kept" % uid(r),
              all(uid(r) in [e["way_uid"] for e in
                             k["properties"]["also_recorded_by"]]
                  for k in keeps), keeps)


def test_a_split_group_whose_sides_disagree_is_left_whole():
    """One side runs a kilometre on past the other: not the same way."""
    whole = way("SH", "UN4", line(length_m=2000.0, offset_m=3.0))
    pieces = [way("PW", "225/%d" % (k + 1),
                  line(start_m=300.0 * k, length_m=300.0, step_m=35.0,
                       phase_m=8.0)) for k in range(3)]
    kept, _ = run([whole] + pieces)
    check("nothing is merged", len(kept) == 4, kept_uids(kept))


# ------------------------------------------- found by the adversarial review

def straight(points_m):
    """A line through [points_m], (east, north) metres from Pencelli."""
    return [[round(LON0 + x * M_LON, 7), round(LAT0 + y * M_LAT, 7)]
            for x, y in points_m]


def test_a_split_way_one_of_whose_pieces_is_near_the_whole_goes_whole():
    """Hampshire's HP-17/1 (622 m), /2 (4 m) and /3 (65 m) are Wiltshire's
    WT-11 (712 m). /1 alone is within 15% of WT-11, so the one-to-one pass,
    run first, took it - and /2 and /3, no longer a group within 15% of
    WT-11, stayed drawn twice."""
    whole = way("SH", "UN5", line(length_m=900.0, offset_m=3.0))
    pieces = [way("PW", "227/1", line(length_m=780.0, step_m=35.0,
                                      phase_m=8.0)),
              way("PW", "227/2", line(start_m=780.0, length_m=20.0,
                                      step_m=35.0)),
              way("PW", "227/3", line(start_m=800.0, length_m=100.0,
                                      step_m=35.0, phase_m=8.0))]
    check("PREMISE: the long piece alone is within 15% of the whole",
          D.is_duplicate(D.compare(D._coords(pieces[0]), D._coords(whole))))
    kept, removed = run([whole] + pieces)
    check("the lane is drawn by one authority, every piece or none",
          len(set(D._code(f) for f in kept)) == 1, kept_uids(kept))
    for r, keeps, within in removed:
        check("%s lies within 15 m of what was kept" % uid(r),
              D.within_union(r, keeps) <= D.MATCH_M, within)


def test_two_short_stubs_meeting_at_a_junction_are_both_kept():
    """A 12 m stub is never more than 12 m from anything it touches, so the
    distance test alone merged two stubs meeting at a right angle."""
    east = way("PW", "50/1", straight([(0.0, 0.0), (12.0, 0.0)]))
    north = way("WX", "50", straight([(0.0, 0.0), (0.0, 12.0)]))
    m = D.compare(D._coords(east), D._coords(north))
    check("PREMISE: each lies wholly within 15 m of the other, like lengths",
          m["a_in_b_m"] <= D.MATCH_M and m["b_in_a_m"] <= D.MATCH_M and
          m["ratio"] >= D.LENGTH_RATIO, m)
    kept, _ = run([east, north])
    check("two ways that merely touch are two ways", len(kept) == 2,
          kept_uids(kept))


def test_two_routes_between_the_same_two_points_are_both_kept():
    """Same start, same end, a different line between. The straight one has
    only its two end vertices, so a matcher that measured it at its vertices
    - or sampled it coarsely - would find it on the other."""
    direct = way("PW", "51/1", straight([(0.0, 0.0), (600.0, 0.0)]))
    pts = [(x, 0.0) for x in range(0, 300, 50)] + [(300.0, 40.0)] + \
          [(x, 0.0) for x in range(350, 650, 50)]
    detour = way("WX", "51", straight(pts))
    check("PREMISE: within 15% in length",
          D.compare(D._coords(direct), D._coords(detour))["ratio"] >= 0.85)
    kept, _ = run([direct, detour])
    check("a lane sharing both ends with another is not merged into it",
          len(kept) == 2, kept_uids(kept))


def test_a_record_just_short_of_the_length_ratio_is_kept():
    long_ = way("PW", "52/1", line(length_m=1000.0))
    part = way("WX", "52", line(length_m=800.0, offset_m=2.0, step_m=33.0))
    check("PREMISE: 80% of the longer, lying wholly on it",
          D.compare(D._coords(part), D._coords(long_))["a_in_b_m"] < 5.0)
    kept, _ = run([long_, part])
    check("a record under 85% of the other's length is not merged",
          len(kept) == 2, kept_uids(kept))


def test_when_each_covers_the_other_the_longer_is_kept():
    """Covering decides first; only when both cover does length decide. The
    test above it (1000 m against 1060 m) is decided by covering alone."""
    for a_code, b_code, start in (("PW", "WX", 0.0), ("WX", "PW", 6000.0)):
        shorter = way(a_code, "53", line(start_m=start, length_m=1000.0))
        longer = way(b_code, "53", line(start_m=start, length_m=1008.0,
                                        offset_m=3.0, step_m=45.0,
                                        phase_m=11.0))
        m = D.compare(D._coords(shorter), D._coords(longer))
        check("PREMISE: each lies within 15 m of the other",
              m["hausdorff_m"] <= D.MATCH_M, m)
        kept, _ = run([shorter, longer])
        check("the longer of two covering records is kept (%s)" % b_code,
              kept_uids(kept) == [uid(longer)], kept_uids(kept))


def test_a_split_group_is_kept_by_rank_then_length():
    # A park's single record, 8 m longer than the council's three pieces:
    # rank decides, and the council's pieces are kept.
    park = way("B1", "054", line(length_m=908.0, offset_m=3.0))
    pieces = [way("PW", "54/%d" % (k + 1),
                  line(start_m=300.0 * k, length_m=300.0, step_m=35.0,
                       phase_m=8.0)) for k in range(3)]
    kept, _ = run([park] + pieces)
    check("the council's side is kept over the park's",
          kept_uids(kept) == sorted(uid(p) for p in pieces), kept_uids(kept))
    # Two councils: the longer side is kept.
    whole = way("SH", "UN6", line(start_m=6000.0, length_m=908.0,
                                  offset_m=3.0))
    pieces = [way("PW", "55/%d" % (k + 1),
                  line(start_m=6000.0 + 300.0 * k, length_m=300.0,
                       step_m=35.0, phase_m=8.0)) for k in range(3)]
    kept, _ = run([whole] + pieces)
    check("between two councils, the longer side is kept",
          kept_uids(kept) == [uid(whole)], kept_uids(kept))


def test_a_byway_sharing_a_stretch_does_not_stop_a_split_group_merging():
    """GROUP_LINK is high so that a different byway that shares a stretch of
    the lane - here 80 m of its 200 m - is not pulled into the group, where
    its length would put the two sides more than 15% apart and leave the
    whole lane drawn twice."""
    whole = way("SH", "UN7", line(length_m=900.0, offset_m=3.0))
    pieces = [way("PW", "56/%d" % (k + 1),
                  line(start_m=300.0 * k, length_m=300.0, step_m=35.0,
                       phase_m=8.0)) for k in range(3)]
    spur_pts = line(start_m=100.0, length_m=80.0, offset_m=-2.0, step_m=20.0)
    for _ in range(4):
        spur_pts.append([spur_pts[-1][0],
                         round(spur_pts[-1][1] + 30.0 * M_LAT, 7)])
    spur = way("PW", "57", spur_pts)
    kept, _ = run([whole] + pieces + [spur])
    check("the other byway is kept", uid(spur) in kept_uids(kept),
          kept_uids(kept))
    lane = [f for f in kept if f is not spur]
    check("and the lane is drawn by one authority",
          len(set(D._code(f) for f in lane)) == 1, kept_uids(kept))


# ------------------------------------------- the container, and its ids

def test_the_container_carries_the_other_record_and_keeps_every_id():
    tmp = tempfile.mkdtemp()
    try:
        def build(features, name, kind="area"):
            path = os.path.join(tmp, name)
            kept = P.select_ways(features)
            B.write_container(path, kept, kind, (11, 11) if kind == "area"
                              else (8, 8), "2026-09-26")
            db = sqlite3.connect(path)
            try:
                rows = dict(db.execute("SELECT way_uid, rowid FROM ways")) \
                    if kind == "area" else {}
                meta = dict(db.execute("SELECT key, value FROM meta"))
            finally:
                db.close()
            return rows, meta

        def fixtures():
            return (way("PW", "34(A)/1", line(length_m=300.0, step_m=10.0,
                                                phase_m=4.0)),
                    way("B1", "000/3", line(length_m=300.0, step_m=7.0,
                                            offset_m=1.5)),
                    way("PW", "35/1", line(start_m=3000.0, length_m=400.0)))

        powys, park, other = fixtures()
        before, meta0 = build([powys, other], "before.tbmap")
        powys, park, other = fixtures()
        after, meta1 = build([powys, park, other], "after.tbmap")
        check("the duplicate adds no row", sorted(after) == sorted(before),
              sorted(after))
        check("every kept way keeps its rowid", after == before,
              (before, after))
        check("the rowid is the hash of the kept uid, as it always was",
              after[uid(powys)] == B.stable_id(uid(powys)))
        check("the removed record's rowid is a gap, not reused",
              B.stable_id(uid(park)) not in after.values())
        check("no duplicate, no key", "also_recorded_by" not in meta0,
              sorted(meta0))
        carried = json.loads(meta1.get("also_recorded_by", "{}"))
        check("the container says who else records the kept way",
              carried == {uid(powys): [{
                  "way_uid": uid(park),
                  "authority": "Brecon Beacons National Park",
                  "authority_code": "B1",
                  "name": "Byway open to all traffic (BOAT) 000/3"}]}, carried)
        powys, park, other = fixtures()
        _rows, meta2 = build([powys, park, other], "overview.tbmap",
                             kind="overview")
        check("an overview, with no records, carries no such key",
              "also_recorded_by" not in meta2, sorted(meta2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_boundary_twin_is_resolved_the_same_in_both_regions():
    """The merge runs on the national pool, before the region split, so a
    lane published in two regions is the same record in both."""
    lon = -2.60        # the Wales / Midlands seam
    lat = 52.40

    def at(offset_m, step_m, phase_m=0.0):
        pts = line(length_m=900.0, offset_m=offset_m, step_m=step_m,
                   phase_m=phase_m)
        return [[round(p[0] - LON0 + lon - 450.0 * M_LON, 7),
                 round(p[1] - LAT0 + lat, 7)] for p in pts]

    sh = way("SH", "UN9", at(0.0, 50.0))
    pw = way("PW", "226/1", at(2.5, 37.0, 9.0))
    pool = P.select_ways([sh, pw])
    regions = {}
    for rid, _, box in P.REGIONS:
        here = [uid(f) for f in pool if P.in_region(f, box)]
        if here:
            regions[rid] = here
    check("PREMISE: the lane straddles two regions", len(regions) == 2,
          regions)
    check("and both publish the same one record",
          len(set(tuple(v) for v in regions.values())) == 1 and
          all(len(v) == 1 for v in regions.values()), regions)


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_") \
                and fn.__module__ == __name__:
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

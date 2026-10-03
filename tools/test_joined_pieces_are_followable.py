#!/usr/bin/env python3
"""A byway joined from pieces names every piece it was published as.

    python tools/test_joined_pieces_are_followable.py

THE DEFECT (measured 3 Oct 2026, on the first build with join_pieces). Until
2 Oct each piece of a council record the source drew in pieces was a lane of
its own, and riders hold notes, stars, photographs and plans against those
piece ids. The joined lane's id hashes all its parts, so all 2,588 piece ids
left the data with NOTHING mapping them to the lane they became. The app's
path-number rule could follow 1,226 of them, only on a phone holding every
area; 1,057 shared a number with another byway and were refused; 300 "#n"
pieces matched no number at all; and 5 were moved onto a byway of the same
number in another parish, up to 58 km away.

THE FIX, checked here end to end:
  * every piece id is an `also_recorded_by` entry on the joined lane, with
    authority, code and name (the app's parser drops an entry without an
    authority), which shipped apps already follow for any set of areas;
  * `joined_from` lists them, and the container carries it as meta, so a
    newer app can tell the lane's own pieces from another council's record;
  * a pure port of the app's fold rule (laneFoldsOf + the fold branch of
    _laneCandidates) moves every piece onto its one lane;
  * the manifest says how much line each package holds (`lengthKm`, each
    distinct line once), which check_build.py now gates on;
  * joined lanes whose pieces lie more than 1 km apart are listed, in the
    output and in dist/reports/joined-far-apart.json, and nothing is refused.

Exit 0 when all hold, 1 on any failure.
"""
import base64
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_map_container as BMC  # noqa: E402
import build_packages as BP  # noqa: E402

_failed = []
_passed = 0


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": %r" % (detail,)) if detail != ""
                                 else ""))


def rec(ref, coords, code="KT", authority="Kent", miles=0.1):
    return BP.normalise(
        {"type": "Feature",
         "geometry": {"type": "LineString", "coordinates": coords},
         "properties": {"Name": ref,
                        "Description": "BO|%s:1|%.3f|none" % (code, miles)}},
        code, authority, "byway_open_to_all_traffic")


# Kent's AW 339 in three pieces end to end, the "#n" shape too, a byway of
# the same number in another parish, and a one-piece record.
P1 = rec("KT|AW|339", [[0.500, 51.20], [0.503, 51.20]])
P2 = rec("KT|AW|339#1", [[0.503, 51.20], [0.506, 51.20]])
P3 = rec("KT|AW|339#2", [[0.506, 51.20], [0.509, 51.20]])
AE339 = rec("KT|AE|339", [[0.600, 51.30], [0.603, 51.30]])
ONE = rec("KT|AW|340", [[0.700, 51.25], [0.703, 51.25]])
# Another authority's record carried on piece 2 by duplicate_ways.
P2["properties"]["also_recorded_by"] = [
    {"way_uid": "SU-9-aaaaaaaaaa", "authority": "Surrey",
     "authority_code": "SU", "name": "Byway open to all traffic (BOAT) 9"}]
PIECES = sorted(f["properties"]["lane_uid"] for f in (P1, P2, P3))


def laneFoldsOf(also_by_container):
    """The app's backup_service.dart laneFoldsOf, ported: dropped -> kept."""
    folds = {}
    for recorded in also_by_container:
        for kept, entries in recorded.items():
            for e in entries:
                uid = e.get("way_uid")
                # AlsoRecordedBy._fromJson drops an entry with no authority.
                if not (e.get("authority") or "").strip():
                    continue
                if uid is None or uid == kept:
                    continue
                folds.setdefault(uid, set()).add(kept)
    return folds


def follows_by_fold(uid, folds, known):
    """The fold branch of _laneCandidates: one kept record present -> it."""
    kept = folds.get(uid)
    if not kept or len(kept) != 1:
        return None
    only = next(iter(kept))
    return only if only in known else None


def test_join_names_every_piece():
    joined = BP.join_pieces([P1, ONE, P2, AE339, P3])
    lane = [f for f in joined if len(BP.lines_of(f)) > 1]
    check("PREMISE: the three pieces are one lane", len(lane) == 1,
          [f["properties"]["name"] for f in joined])
    if not lane:
        return
    props = lane[0]["properties"]
    uid = props["lane_uid"]
    check("PREMISE: the joined id is no piece's id", uid not in PIECES, uid)
    check("joined_from lists every piece, sorted, never the lane itself",
          props.get("joined_from") == PIECES, props.get("joined_from"))
    entries = dict((e["way_uid"], e) for e in props.get("also_recorded_by")
                   or [])
    for piece in PIECES:
        e = entries.get(piece)
        check("%s is an also_recorded_by entry on the lane" % piece,
              e is not None, sorted(entries))
        if e:
            check("%s's entry has the authority, code and name the app's "
                  "parser keeps" % piece,
                  e.get("authority") == "Kent"
                  and e.get("authority_code") == "KT"
                  and (e.get("name") or "").startswith("Byway"), e)
    check("another authority's record carried on a piece is still there",
          "SU-9-aaaaaaaaaa" in entries, sorted(entries))
    check("and is not called one of the lane's own pieces",
          "SU-9-aaaaaaaaaa" not in props.get("joined_from", []))
    single = [f for f in joined
              if f["properties"]["lane_uid"] == ONE["properties"]["lane_uid"]]
    check("a one-piece record keeps its id and gains no joined_from",
          single and "joined_from" not in single[0]["properties"]
          and "also_recorded_by" not in single[0]["properties"])


def test_the_container_carries_it_and_the_app_rule_follows_it():
    joined = BP.join_pieces([P1, ONE, P2, AE339, P3])
    lane = [f for f in joined if len(BP.lines_of(f)) > 1][0]
    uid = lane["properties"]["lane_uid"]
    tmp = tempfile.mkdtemp(prefix="tb-joined-")
    try:
        path = os.path.join(tmp, "c.tbmap")
        BMC.write_container(path, joined, "area", (11, 11), "2026-10-03")
        db = sqlite3.connect(path)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        known = set(r[0] for r in db.execute("SELECT way_uid FROM ways"))
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    joined_from = json.loads(meta.get("joined_from") or "{}")
    check("meta.joined_from maps the lane to its pieces",
          joined_from == {uid: PIECES}, joined_from)
    also = json.loads(meta.get("also_recorded_by") or "{}")
    folds = laneFoldsOf([also])
    for piece in PIECES:
        check("the app's fold rule moves %s onto the joined lane" % piece,
              follows_by_fold(piece, folds, known) == uid,
              folds.get(piece))
    check("PREMISE: no piece id is still a lane",
          not (set(PIECES) & known), sorted(set(PIECES) & known))


def test_distinct_line_km_counts_a_line_once():
    line = [[-1.70, 52.50], [-1.70, 52.509]]   # ~1 km due north
    a = {"properties": {}, "geometry": {"type": "LineString",
                                        "coordinates": line}}
    b = {"properties": {}, "geometry": {"type": "MultiLineString",
                                        "coordinates": [line]}}
    one = BP.distinct_line_km([a])
    check("PREMISE: the line is about a kilometre", 0.99 < one < 1.01, one)
    check("one line carried by two records counts once",
          abs(BP.distinct_line_km([a, b]) - one) < 1e-9,
          BP.distinct_line_km([a, b]))


def test_far_apart_pieces_are_listed_not_refused():
    near = BP.join_pieces([P1, P2, P3])[0]
    check("pieces end to end have no gap", BP.joined_gap_km(near) == 0.0,
          BP.joined_gap_km(near))
    far1 = rec("CB|Balsham|4", [[0.300, 52.10], [0.303, 52.10]], code="CB",
               authority="Cambridgeshire")
    far2 = rec("CB|Balsham|4", [[0.340, 52.10], [0.343, 52.10]], code="CB",
               authority="Cambridgeshire")
    far = BP.join_pieces([far1, far2])[0]
    gap = BP.joined_gap_km(far)
    check("PREMISE: the two pieces are one lane",
          len(BP.lines_of(far)) == 2)
    check("the gap is measured (~2.5 km)", 2.3 < gap < 2.6, gap)
    listed = BP.joined_far_apart([near, far])
    check("only the far-apart lane is listed",
          [r["way_uid"] for r in listed] == [far["properties"]["lane_uid"]],
          listed)


def test_main_writes_length_and_the_report():
    root = tempfile.mkdtemp(prefix="tb-joined-main-")
    try:
        cache, dist = os.path.join(root, "cache"), os.path.join(root, "dist")
        os.makedirs(os.path.join(cache, "DE"))
        os.makedirs(dist)
        with open(os.path.join(cache, "authorities.json"), "w") as fh:
            json.dump({"DE": "Derbyshire"}, fh)
        rows = [("100|1/1", [(-1.700, 52.500), (-1.697, 52.500)]),
                ("100|1/1", [(-1.697, 52.500), (-1.694, 52.500)]),
                ("100|7", [(-1.600, 52.450), (-1.599, 52.450)]),
                ("100|7", [(-1.560, 52.450), (-1.559, 52.450)])]
        with open(os.path.join(cache, "DE",
                               "byway_open_to_all_traffic.json"), "w") as fh:
            json.dump({"type": "FeatureCollection", "features": [
                {"type": "Feature",
                 "properties": {"Name": "DE|%s" % r[0],
                                "Description": "BO|DE:1|0.100|none"},
                 "geometry": {"type": "LineString", "coordinates": r[1]}}
                for r in rows]}, fh)
        keyfile = os.path.join(root, "key.b64")
        with open(keyfile, "w") as fh:
            fh.write(base64.b64encode(b"k" * 32).decode("ascii"))
        real = (BP.cache_dir, BP.dist_dir, sys.argv)
        BP.cache_dir = lambda: cache
        BP.dist_dir = lambda: dist
        sys.argv = ["build_packages.py", "--key", keyfile, "--previous", ""]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                BP.main()
        finally:
            BP.cache_dir, BP.dist_dir, sys.argv = real
        with open(os.path.join(dist, "manifest.json"), encoding="utf8") as fh:
            manifest = json.load(fh)
        entries = manifest["packages"]
        check("PREMISE: main() wrote a package", len(entries) >= 1, entries)
        want = sum(BP._km_between(r[1][0], r[1][1]) for r in rows)
        got = sum(e.get("lengthKm", 0) for e in entries)
        check("every manifest entry carries lengthKm",
              all(isinstance(e.get("lengthKm"), float) for e in entries),
              entries)
        check("lengthKm is the km of line it seals (%.3f vs %.3f)"
              % (got, want), abs(got - want) < 0.002)
        report_path = os.path.join(dist, BP.JOINED_REPORT)
        check("the far-apart report is written", os.path.isfile(report_path))
        if os.path.isfile(report_path):
            with open(report_path, encoding="utf8") as fh:
                report = json.load(fh)
            far = [r["name"] for r in report["far_apart"]]
            check("DE 100 7, pieces 2.7 km apart, is listed and 1/1 is not",
                  far == ["Byway open to all traffic (BOAT) 100 7"], report)
        check("and the build output lists it too",
              "100 7" in out.getvalue()
              and "more than 1 km apart" in out.getvalue(), out.getvalue())
    finally:
        shutil.rmtree(root, ignore_errors=True)


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

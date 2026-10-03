#!/usr/bin/env python3
"""check_build.py judges a lane build by the length of line it carries.

    python tools/test_check_build_gates_on_length.py

THE DEFECT (refresh run 37086495190, 3 Oct 2026). The first build to publish
one council byway drawn in pieces as one lane, and one line recorded twice as
one way, went 12,576 -> 10,375 rows (-17.5%; the South East 3,954 -> 2,899,
-27%) and check_build.py refused it as "data loss". Measured against the
published containers it had lost nothing: every one of the 2,691 ids that
went is covered, along its whole length within 15 m, by a lane of the new
build, and the km of line went 5,430 -> 5,364 (-1.2%, the doubles folded).
A row count cannot tell sixteen pieces joined into one lane from fifteen
byways lost. Length can, and only falls when a way does.

THE CHECKS:
  * those numbers, in manifests that carry lengthKm, publish;
  * the same numbers WITHOUT lengthKm are still refused - which is what
    makes the length the thing deciding, not a loosened row limit;
  * real loss is refused: a region losing a third of its km, the country
    losing 5%, a type losing 5%;
  * rows are still printed and refused past MAX_ROW_COLLAPSE (half lost);
  * a published manifest written before lengthKm is MEASURED from its
    packs (fill_lengths), and when it cannot be measured the gate says so
    and refuses - it is never skipped;
  * joined lanes whose pieces lie far apart are printed and put in the job
    summary, and never refuse a build.

Exit 0 when all hold, 1 on any failure.
"""
import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_packages as BP  # noqa: E402
import check_build as CB  # noqa: E402

_failed = []
_passed = 0


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": %r" % (detail,)) if detail != ""
                                 else ""))


REGIONS = ("south-west", "south-east", "east-anglia", "midlands", "wales",
           "north")
# The published build of 1 Oct 2026, and the refused one of 3 Oct: rows from
# the run log, km measured from the containers (compare.py).
PUBLISHED_ROWS = dict(zip(REGIONS, (1858, 3954, 1444, 2138, 1390, 1792)))
FOLDED_ROWS = dict(zip(REGIONS, (1712, 2899, 1280, 1771, 1266, 1447)))
PUBLISHED_KM = dict(zip(REGIONS, (913.8, 1826.7, 1127.3, 1099.9, 647.0,
                                  1129.1)))
FOLDED_KM = dict(zip(REGIONS, (912.5, 1813.1, 1077.5, 1077.4, 634.0,
                               1136.5)))


def manifest(rows, km=None):
    return {"packages": [
        dict({"package": "ways", "region": r, "area": r,
              "file": "packages/ways-%s.tbpack" % r, "laneCount": rows[r]},
             **({"lengthKm": km[r]} if km is not None else {}))
        for r in REGIONS]}


def gate(previous, new, **kw):
    problems = []
    with contextlib.redirect_stdout(io.StringIO()) as out:
        CB.check_totals(previous, new, problems, **kw)
    return problems, out.getvalue()


def test_the_folding_build_publishes():
    problems, out = gate(manifest(PUBLISHED_ROWS, PUBLISHED_KM),
                         manifest(FOLDED_ROWS, FOLDED_KM))
    check("PREMISE: the rows really fell 17.5%, the South East 27%",
          sum(FOLDED_ROWS.values()) == 10375
          and sum(PUBLISHED_ROWS.values()) == 12576)
    check("12,576 -> 10,375 rows with the line all there is not refused",
          problems == [], problems)
    check("the row count is still printed", "12576 ->  10375" in out, out)
    check("and the length is", "length of line" in out, out)


def test_without_length_it_is_still_refused():
    problems, _ = gate(manifest(PUBLISHED_ROWS), manifest(FOLDED_ROWS))
    check("the same rows with no length to go on are refused - the length "
          "is what lets it through, not a looser row limit",
          any("south-east fell 27%" in p for p in problems), problems)


def test_real_loss_is_refused():
    lost = dict(FOLDED_KM, **{"south-east": FOLDED_KM["south-east"] * 0.66})
    problems, _ = gate(manifest(PUBLISHED_ROWS, PUBLISHED_KM),
                       manifest(PUBLISHED_ROWS, lost))
    check("a region losing a third of its line is refused, rows unchanged",
          any("ways/south-east line fell" in p for p in problems), problems)
    check("and the country losing 6% of its line is refused",
          any("national length of line fell" in p for p in problems),
          problems)

    thin = dict((r, v * 0.95) for r, v in PUBLISHED_KM.items())
    problems, _ = gate(manifest(PUBLISHED_ROWS, PUBLISHED_KM),
                       manifest(PUBLISHED_ROWS, thin))
    check("5% of the line gone evenly - under every per-region limit - is "
          "refused nationally and for the type",
          any("national length" in p for p in problems)
          and any(p.startswith("ways line fell") for p in problems),
          problems)


def test_rows_still_have_a_floor():
    halved = dict((r, v // 3) for r, v in PUBLISHED_ROWS.items())
    problems, _ = gate(manifest(PUBLISHED_ROWS, PUBLISHED_KM),
                       manifest(halved, PUBLISHED_KM))
    check("two thirds of the rows gone is refused whatever the length says",
          any("row count fell" in p for p in problems)
          and any("south-east fell" in p for p in problems), problems)


def test_a_region_at_zero_is_still_refused():
    rows = dict(FOLDED_ROWS, wales=0)
    km = dict(FOLDED_KM, wales=0.0)
    problems, _ = gate(manifest(PUBLISHED_ROWS, PUBLISHED_KM),
                       manifest(rows, km))
    check("a region at zero rows and km is refused",
          any("wales lost every one" in p for p in problems), problems)


def _seal(path, features, key):
    body = json.dumps({"type": "FeatureCollection",
                       "features": features}).encode("utf8")
    with open(path, "wb") as fh:
        fh.write(BP.pack(body, key))


def _feature(uid, line):
    return {"type": "Feature", "properties": {"lane_uid": uid},
            "geometry": {"type": "LineString", "coordinates": line}}


def test_an_old_manifest_is_measured_from_its_packs():
    tmp = tempfile.mkdtemp(prefix="tb-len-")
    try:
        key = b"q" * 32
        keyfile = os.path.join(tmp, "k.b64")
        with open(keyfile, "w") as fh:
            fh.write(base64.b64encode(key).decode("ascii"))
        packs = os.path.join(tmp, "packages")
        os.makedirs(packs)
        line = [[-1.70, 52.50], [-1.70, 52.509]]
        for r in REGIONS:
            # Two records of one line: measured once.
            _seal(os.path.join(packs, "ways-%s.tbpack" % r),
                  [_feature("A-1-%s" % r, line), _feature("A-2-%s" % r, line)],
                  key)
        old = manifest(PUBLISHED_ROWS)
        why = CB.fill_lengths(old, packs, keyfile)
        check("an old manifest is measured, not skipped", why is None, why)
        want = round(BP.distinct_line_km([_feature("x", line)]), 3)
        check("each entry gets the km of its pack, each line once",
              all(p.get("lengthKm") == want for p in old["packages"]),
              [p.get("lengthKm") for p in old["packages"]])

        missing = manifest(PUBLISHED_ROWS)
        why = CB.fill_lengths(missing, os.path.join(tmp, "nowhere"), keyfile)
        check("a pack that is not there to measure is a stated reason",
              why and "not there to measure" in why, why)
        why = CB.fill_lengths(manifest(PUBLISHED_ROWS), packs, "")
        check("no key is a stated reason", why and "no --key" in why, why)
        problems, _ = gate(manifest(PUBLISHED_ROWS),
                           manifest(FOLDED_ROWS, FOLDED_KM),
                           lengths_blind=why)
        check("and a gate that could not measure refuses, saying why",
              any("length gate could not run" in p for p in problems),
              problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_command_line_measures_and_lists():
    """main(): the published side measured from packages/ beside it, the new
    side from its own lengthKm, and the joined-lane report printed and put
    in the job summary, never as a problem."""
    tmp = tempfile.mkdtemp(prefix="tb-len-cli-")
    try:
        key = b"z" * 32
        keyfile = os.path.join(tmp, "k.b64")
        with open(keyfile, "w") as fh:
            fh.write(base64.b64encode(key).decode("ascii"))
        published = os.path.join(tmp, "published")
        os.makedirs(os.path.join(published, "packages"))
        dist = os.path.join(tmp, "dist")
        os.makedirs(os.path.join(dist, "packages"))
        os.makedirs(os.path.join(dist, "reports"))
        old_rows, new_rows, km = {}, {}, {}
        for i, r in enumerate(REGIONS):
            lat = 50.5 + i * 0.5
            pieces = [[[-1.70, lat + k * 0.003], [-1.70, lat + (k + 1) * 0.003]]
                      for k in range(4)]
            # Published: four pieces, four lanes. New: one joined lane.
            _seal(os.path.join(published, "packages", "ways-%s.tbpack" % r),
                  [_feature("P-%d-%s" % (k, r), p)
                   for k, p in enumerate(pieces)], key)
            # New: two of the pieces joined into one lane - three rows, the
            # same four lines.
            joined = {"type": "Feature", "properties": {"lane_uid": "J-" + r},
                      "geometry": {"type": "MultiLineString",
                                   "coordinates": pieces[:2]}}
            built = [joined] + [_feature("P-%d-%s" % (k, r), pieces[k])
                                for k in (2, 3)]
            _seal(os.path.join(dist, "packages", "ways-%s.tbpack" % r),
                  built, key)
            old_rows[r], new_rows[r] = 4, 3
            km[r] = round(BP.distinct_line_km(built), 3)
        with open(os.path.join(published, "manifest.json"), "w") as fh:
            json.dump(manifest(old_rows), fh)
        with open(os.path.join(dist, "manifest.json"), "w") as fh:
            json.dump(manifest(new_rows, km), fh)
        with open(os.path.join(dist, "reports",
                               "joined-far-apart.json"), "w") as fh:
            json.dump({"limit_km": 1.0, "joined": 6, "far_apart": [
                {"way_uid": "CB-4-646d204c41", "authority": "Cambridgeshire",
                 "name": "Byway open to all traffic (BOAT) Balsham 4",
                 "pieces": 2, "gap_km": 1.807, "length_km": 4.64}]}, fh)
        summary = os.path.join(tmp, "summary.md")
        env = dict(os.environ, GITHUB_STEP_SUMMARY=summary)
        run = subprocess.run(
            [sys.executable, os.path.join(HERE, "check_build.py"),
             "--previous", os.path.join(published, "manifest.json"),
             "--new", os.path.join(dist, "manifest.json"),
             "--dist", os.path.join(dist, "packages"),
             "--cache", os.path.join(tmp, "cache"),
             "--key", keyfile,
             "--baseline", os.path.join(tmp, "none-baseline.json"),
             "--closures-previous", os.path.join(tmp, "none.json"),
             "--closures-new", os.path.join(tmp, "none.json")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, env=env)
        out = run.stdout
        check("the published side was measured from its packs",
              "length of line:" in out
              and "length gate could not run" not in out, out)
        check("two pieces become one lane: 25% fewer rows, no line lost, "
              "no totals problem",
              "fell" not in out.split("problem(s) with this build")[-1]
              if "problem(s)" in out else True, out)
        check("the far-apart lane is listed in the output",
              "Balsham 4" in out and "not refused" in out, out)
        check("and in the job summary",
              os.path.isfile(summary)
              and "Balsham 4" in open(summary, encoding="utf8").read())
        # The only problem left is the authority floor (no cache here).
        tail = out.split("problem(s) with this build:")[-1]
        listed = [l for l in tail.splitlines() if l.startswith("  - ")]
        check("the only problem is the empty cache's authority floor",
              len(listed) == 1 and "authorities.json" in listed[0], listed)
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

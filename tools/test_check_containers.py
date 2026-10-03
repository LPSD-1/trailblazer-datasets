#!/usr/bin/env python3
"""check_containers.py, run over containers the real pipeline builds.

    python tools/test_check_containers.py

THE FAULT THIS EXISTS FOR. The guard knew two record tables, `lanes` and
`orders`, and refused every post-cutover area container - whose records are in
`ways` - with "no record table". It went unseen until the cutover dry run,
because nothing had ever handed the guard a container the new builder wrote:
golden did not call it and no suite did. A publishing guard tested only on the
shape being retired is tested on nothing that will ship.
"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_map_container as B        # noqa: E402
import check_containers as CC          # noqa: E402
import test_build_containers as TB     # noqa: E402  (the real pipeline)

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def _built():
    """packs -> containers, exactly as refresh-data.yml builds them."""
    out, tmp = TB._pipeline()
    folder = os.path.join(tmp, "containers")
    paths = sorted(os.path.join(folder, n) for n in os.listdir(folder)
                   if n.endswith(".tbmap"))
    return tmp, paths


def test_a_ways_container_passes_the_guard():
    tmp, paths = _built()
    try:
        areas = [p for p in paths if "overview" not in os.path.basename(p)]
        # THE PREMISE: the pipeline built area containers, in the ways shape.
        check("the pipeline built area containers", len(areas) >= 2, paths)
        for p in areas:
            db = sqlite3.connect(p)
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            db.close()
            check("%s is the ways shape" % os.path.basename(p),
                  "ways" in tables and "lanes" not in tables, sorted(tables))

        problems, checked = [], []
        for p in paths:
            meta, found = CC.check_container(p, problems)
            if meta.get("kind") in ("area", "both"):
                checked.append((p, found))
                check("%s: its records are read" % os.path.basename(p),
                      bool(found["records"]), found["records"])
                check("%s: and its tiles are decoded" % os.path.basename(p),
                      bool(found["tiles"]), found["tiles"])
        CC.check_agreement(checked, problems)
        check("a correctly built ways dataset is not refused", problems == [],
              problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_guard_still_refuses_a_ways_container_that_disagrees():
    """The fix must not be 'accept anything with a ways table'."""
    tmp, paths = _built()
    try:
        area = [p for p in paths if "overview" not in os.path.basename(p)][0]
        db = sqlite3.connect(area)
        db.execute("DELETE FROM ways WHERE rowid = (SELECT MIN(rowid) "
                   "FROM ways)")
        db.commit()
        db.close()
        problems, checked = [], []
        for p in paths:
            meta, found = CC.check_container(p, problems)
            if meta.get("kind") in ("area", "both"):
                checked.append((p, found))
        CC.check_agreement(checked, problems)
        check("a drawn way with no record is refused",
              any("identify nothing" in x for x in problems), problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_guard_refuses_an_area_that_leaves_out_a_way_it_carries():
    """Even when ANOTHER area draws it: a rider may hold this one alone.

    The published set of 2 Oct 2026 passed this guard with Wales drawing 536
    of its 1,390 byways, because agreement was pooled across every area and
    each missing way was drawn by an area earlier in the manifest. Built here
    exactly as that set was: two areas carrying one way, the second written
    with `tile_exclude` naming it.
    """
    tmp = tempfile.mkdtemp(prefix="tbways-guard-")
    try:
        shared = TB._way("MN-1-shared0001", "boat", 1, 1, -2.70, 51.65)
        paths = []
        for i, (area, lon, lat) in enumerate((("south-west", -2.80, 51.60),
                                              ("wales", -3.40, 52.20))):
            path = os.path.join(tmp, "ways-%s.tbmap" % area)
            feats = [TB._way("%s-own" % area, "boat", 1, 1, lon, lat), shared]
            B.write_container(
                path, feats, "area", (13, 14), "2026-03-04",
                tile_exclude={"MN-1-shared0001"} if i else None)
            paths.append(path)

        problems, checked = [], []
        for p in paths:
            meta, found = CC.check_container(p, problems)
            checked.append((p, found))
        # PREMISE: the set is the published shape - the way is drawn by the
        # first area, recorded by both, and left out of the second's tiles.
        check("the first area draws the shared way",
              "MN-1-shared0001" in checked[0][1]["tiles"], checked[0][1])
        check("the second area records it",
              "MN-1-shared0001" in checked[1][1]["records"], checked[1][1])
        check("and does not draw it",
              "MN-1-shared0001" not in checked[1][1]["tiles"], checked[1][1])
        check("so the pooled set draws everything it records",
              set().union(*(f["records"] for _, f in checked))
              <= set().union(*(f["tiles"] for _, f in checked)))

        CC.check_agreement(checked, problems)
        named = [x for x in problems if x.startswith("ways-wales.tbmap")
                 and "none of its tiles" in x]
        check("the guard refuses the area that leaves it out", len(named) == 1,
              problems)
        check("and only that one", len(problems) == 1, problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_the_guard(paths):
    """check_containers.main() over `paths`, as refresh-data.yml runs it.

    Returns (exit code, what it printed)."""
    argv, stdout = sys.argv, sys.stdout
    sys.argv = ["check_containers.py"] + list(paths)
    sys.stdout = io.StringIO()
    try:
        code = CC.main()
        return code, sys.stdout.getvalue()
    finally:
        sys.argv, sys.stdout = argv, stdout


def test_the_guard_as_ci_runs_it_passes_two_areas_that_both_draw_a_way():
    """The COMMAND, not its functions, over the shape the fix publishes.

    The functions above are what the other tests call, and main() is what CI
    calls. The old one-owner rule lived in main() as well as in its own
    function; put back inline there, under any name, it refuses every correct
    build - it refused the fixed rebuild of 2 Oct 2026 with "is also in" - and
    nothing calling only check_agreement would notice. So the command itself
    is run here, over two areas that BOTH draw a way they share, and it must
    say OK; and over the old shape, where the second leaves it out, it must
    refuse - so a main() that stopped calling the agreement check is red too.
    """
    for exclude, want in ((False, 0), (True, 1)):
        tmp = tempfile.mkdtemp(prefix="tbways-guard-cli-")
        try:
            shared = TB._way("MN-1-shared0001", "boat", 1, 1, -2.70, 51.65)
            paths = []
            for i, (area, lon, lat) in enumerate(
                    (("south-west", -2.80, 51.60), ("wales", -3.40, 52.20))):
                path = os.path.join(tmp, "ways-%s.tbmap" % area)
                feats = [TB._way("%s-own" % area, "boat", 1, 1, lon, lat),
                         shared]
                B.write_container(
                    path, feats, "area", (13, 14), "2026-03-04",
                    tile_exclude={"MN-1-shared0001"} if (i and exclude)
                    else None)
                paths.append(path)
            # PREMISE: the way is in both areas' tiles, or in only the first.
            drawn = []
            for p in paths:
                _meta, found = CC.check_container(p, [])
                drawn.append("MN-1-shared0001" in found["tiles"])
            check("premise: the shared way is drawn by %s"
                  % ("the first area only" if exclude else "both areas"),
                  drawn == [True, not exclude], drawn)

            code, said = _run_the_guard(paths)
            if exclude:
                check("the guard command refuses the area that leaves it out",
                      code == 1 and "ways-wales.tbmap" in said
                      and "none of its tiles" in said, (code, said))
            else:
                check("the guard command passes two areas that both draw it",
                      code == 0 and "Containers OK." in said, (code, said))
                check("and names no way as being in two areas",
                      "also in" not in said, said)
            check("exit %d as expected (%s)" % (want, "old shape" if exclude
                                                else "fixed shape"),
                  code == want, code)
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

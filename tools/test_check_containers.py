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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

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
        if len(checked) > 1:
            CC.check_no_lane_in_two_areas(checked, problems)
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

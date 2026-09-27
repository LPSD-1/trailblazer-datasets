#!/usr/bin/env python3
"""The app's live-shape changeset fixture: are its dates ones a build could
carry?

    python tools/test_changeset_fixture_dates_are_a_builds.py

ROUND-12 RESIDUE. make_changeset_fixture.py moves the POI and ford dates of
`after` on to 2026-10-01 (the "refetch"), but `after`'s `built_at` is
2026-09-26T06:01:20Z. No build can say its POIs were checked five days after
it was built: build_pois.py stamps `pois_checked` with the day it fetched, and
the pack is stamped after that. The app's tests that read this fixture are
therefore told of a check from the future, and a date comparison the app
makes (anything "newer than the build") is exercised on a shape no real
container has.

Reads the fixture committed in the app repository checked out beside this
one; BLIND (exit 2) when it is not there.
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_FIXTURE = os.path.join(os.path.dirname(ROOT), "greenroadmap-app", "test",
                           "fixtures", "changesets_live_shape")

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
        print("  ok   %s" % name)
    else:
        _failed.append(name)
        print("  FAIL %s %s" % (name, detail))


def _meta(path):
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        return dict(db.execute("SELECT key, value FROM meta"))
    finally:
        db.close()


def main():
    builds = [os.path.join(APP_FIXTURE, n) for n in ("before.tbmap",
                                                     "after.tbmap")]
    if not all(os.path.exists(b) for b in builds):
        print("BLIND: the app's live-shape fixture is not beside this repo")
        return 2
    for path in builds:
        name = os.path.basename(path)
        meta = _meta(path)
        built = meta.get("built_at")
        check("%s: PREMISE it has a built_at" % name, bool(built))
        if not built:
            continue
        day = built[:10]
        for key in ("pois_checked", "fords_checked"):
            value = meta.get(key)
            if value is None:
                continue
            check("%s: %s (%s) is not after the build (%s)"
                  % (name, key, value, built),
                  value[:10] <= day,
                  "- a build cannot have checked anything after it was built")
    print()
    if _failed:
        print("%d checks, %d failed:" % (_passed + len(_failed), len(_failed)))
        for f in _failed:
            print("  " + f)
        return 1
    print("%d checks, all passed" % _passed)
    return 0


def test_changeset_fixture_dates_are_a_builds():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())

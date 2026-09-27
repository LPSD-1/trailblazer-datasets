#!/usr/bin/env python3
"""Does make_changeset_fixture.py WRITE a build whose dates a build could
carry - not only is the committed fixture right today?

    python tools/test_changeset_fixture_generator_dates_are_a_builds.py

ROUND-12 RESIDUE. The generator dated `after`'s edits "the next month"
(2026-10-01) under a `built_at` of 2026-09-26T06:01:20Z, so `after` said its
POIs were checked five days after it was built. test_changeset_fixture_dates_
are_a_builds.py reads the fixture committed in the app repository; this reads
what the generator produces, so a regeneration cannot bring the shape back.

Runs the generator into a temporary directory from the published container
(BLIND, exit 2, when it is not there) and checks:
  * no date in either build is later than the day it was built;
  * the refetch still MOVED the dates (else the fixture proves nothing about
    evidence_dates or pois_checked changing): after's pois_checked is its own
    build day, later than before's;
  * the check itself can fail: a planted future date is found, and a build
    dated on or before the newest date `before` carries is refused.
"""
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import make_changeset_fixture as M   # noqa: E402

PUBLISHED = os.path.join(ROOT, M.DEFAULT_SOURCE)

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
    if not os.path.isfile(PUBLISHED):
        print("BLIND: no published container at %s" % PUBLISHED)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "fixture")
        M.make(PUBLISHED, out)
        before = os.path.join(out, "before.tbmap")
        after = os.path.join(out, "after.tbmap")
        old, new = _meta(before), _meta(after)
        check("PREMISE: both builds carry built_at and pois_checked",
              all(m.get(k) for m in (old, new)
                  for k in ("built_at", "pois_checked")), (old, new))
        for name, path in (("before", before), ("after", after)):
            late = M.dated_after_build(path)
            check("%s.tbmap carries nothing dated after its build" % name,
                  not late, late)
        check("after's pois_checked is the day after was built",
              new.get("pois_checked") == new.get("built_at", "")[:10],
              (new.get("pois_checked"), new.get("built_at")))
        check("the refetch moved pois_checked on from before's",
              old.get("pois_checked", "") < new.get("pois_checked", ""),
              (old.get("pois_checked"), new.get("pois_checked")))
        check("the refetch moved evidence_dates",
              old.get("evidence_dates") != new.get("evidence_dates"))

        # THE CHECK CAN FAIL: a date from the future is seen.
        planted = os.path.join(tmp, "planted.tbmap")
        shutil.copyfile(after, planted)
        db = sqlite3.connect(planted)
        try:
            db.execute("UPDATE meta SET value = '2026-10-01' "
                       "WHERE key = 'pois_checked'")
            db.execute("UPDATE fords SET source_date = '2099-01-01'")
            db.commit()
        finally:
            db.close()
        seen = M.dated_after_build(planted)
        check("PREMISE: a planted future pois_checked and ford date are seen",
              "pois_checked" in seen and "fords.source_date" in seen, seen)

    built = datetime.datetime(2026, 9, 26, 6, 1, 20)
    check("the refetch is the build's day",
          M.refetch_day(built, datetime.date(2026, 9, 24))
          == datetime.date(2026, 9, 26))
    try:
        M.refetch_day(built, datetime.date(2026, 9, 26))
        refused = False
    except SystemExit:
        refused = True
    check("a build no later than before's newest date is refused", refused)

    print()
    if _failed:
        print("%d checks, %d failed:" % (_passed + len(_failed), len(_failed)))
        for f in _failed:
            print("  " + f)
        return 1
    print("%d checks, all passed" % _passed)
    return 0


def test_changeset_fixture_generator_dates_are_a_builds():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())

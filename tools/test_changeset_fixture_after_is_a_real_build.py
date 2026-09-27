#!/usr/bin/env python3
"""The app's live-shape changeset fixture: is its `after` build one this
repository could publish?

    python tools/test_changeset_fixture_after_is_a_real_build.py

ROUND-11 RESIDUE. make_changeset_fixture.py cuts `before` and `after` from a
published container and changes the ways between them (27 rows to 26). The
published container carries `ways_cut`, because rule 3 of stamp_build.py
restamped it; the generator copies that key into BOTH builds, and
test_make_changeset_fixture.py demands it there (`_meta_keys(after) ==
_meta_keys(PUBLISHED)`).

But `ways_cut` is written by rule 3 alone, and rule 3 applies only when the
ways did NOT change. A build whose ways changed is a fresh builder output that
rule 2 leaves as it is: `built_at` is its pack stamp and it has no
`ways_cut`, so its changeset lists `ways_cut` in `removed_meta` and the app
shows the new `built_at` as the date the lanes were cut. The fixture instead
tells the app's tests that lanes changed on 26 Sep were "cut" on 24 Sep.

Reads the fixture committed in the app repository checked out beside this
one; BLIND (exit 2) when it is not there.
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import build_changeset as C          # noqa: E402
import stamp_build as S              # noqa: E402

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


def _ways(path):
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        return sorted(db.execute("SELECT * FROM ways"))
    finally:
        db.close()


def main():
    before = os.path.join(APP_FIXTURE, "before.tbmap")
    after = os.path.join(APP_FIXTURE, "after.tbmap")
    change = os.path.join(APP_FIXTURE, "before_to_after.tbchange")
    if not all(os.path.isfile(p) for p in (before, after, change)):
        print("BLIND: the app fixture is not checked out at %s" % APP_FIXTURE)
        return 2

    ways_changed = _ways(before) != _ways(after)
    check("PREMISE: the fixture's ways change between the builds",
          ways_changed)
    new_meta = C.snapshot(after)["meta"]
    check("PREMISE: after.tbmap has a built_at", "built_at" in new_meta)

    if ways_changed:
        check("after.tbmap, whose ways changed, carries no ways_cut (rule 3, "
              "the only writer of ways_cut, never applies to it)",
              S.WAYS_CUT not in new_meta,
              "ways_cut %s under built_at %s"
              % (new_meta.get(S.WAYS_CUT), new_meta.get("built_at")))
        check("the date the app shows for after.tbmap's lanes is its own "
              "built_at", S.ways_cut(after) == new_meta.get("built_at"),
              "%s, not %s" % (S.ways_cut(after), new_meta.get("built_at")))

        db = sqlite3.connect("file:%s?mode=ro" % change.replace("\\", "/"),
                             uri=True)
        try:
            meta = dict(db.execute("SELECT key, value FROM meta"))
        finally:
            db.close()
        import json
        removed = json.loads(meta.get("removed_meta") or "[]")
        check("the changeset removes ways_cut, as one between two real "
              "builds would", S.WAYS_CUT in removed, removed)

    print("%d checks, %s" % (_passed + len(_failed),
                             "all passed" if not _failed
                             else "%d FAILED" % len(_failed)))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

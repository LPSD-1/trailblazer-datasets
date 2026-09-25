#!/usr/bin/env python3
"""Is the app's changeset fixture the shape this repository publishes?

    python tools/test_make_changeset_fixture.py

THE DEFECT THIS EXISTS FOR. The app's changeset tests said their fixtures
were "the live ways shape" and carried four tables, while every published
region container carries eleven and four more meta keys. Both repositories'
tests were green and no region update could ever be applied. A fixture is a
CLAIM about the published data, so it is checked against the published data:
`containers/ways-east-anglia.tbmap`, read on every run, not a schema copied
into this file.

It checks the generator's output in a temporary directory, and - when the app
repository is checked out beside this one - the fixture committed there too.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import build_changeset as C          # noqa: E402
import make_changeset_fixture as M   # noqa: E402
import test_build_changeset as T     # noqa: E402

PUBLISHED = os.path.join(ROOT, M.DEFAULT_SOURCE)
APP_FIXTURE = os.path.join(os.path.dirname(ROOT), "greenroadmap-app", "test",
                           "fixtures", "changesets_live_shape")

#: The meta keys the app reads, which a changeset must restate. Named here
#: so a container that stopped carrying one is a red test, not a quiet one.
APP_META = ("built_at", "kind", "bounds", "context_note", "context_scope",
            "evidence_age", "schema_version", "min_zoom", "max_zoom")

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


def _meta_keys(path):
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        return {k for (k,) in db.execute("SELECT key FROM meta")}
    finally:
        db.close()


def _carried(path):
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        return C.carried_tables(db)
    finally:
        db.close()


def check_fixture(where, label):
    """Every property the app's tests rely on, of one fixture directory."""
    before = os.path.join(where, "before.tbmap")
    after = os.path.join(where, "after.tbmap")
    change = os.path.join(where, M.CHANGESET_NAME)
    for path in (before, after, change, os.path.join(where, "README.md")):
        check("%s: %s is there" % (label, os.path.basename(path)),
              os.path.isfile(path))
    if not all(os.path.isfile(p) for p in (before, after, change)):
        return

    published = M.schema_of(PUBLISHED)
    for path in (before, after):
        name = os.path.basename(path)
        check("%s: %s has the published sqlite_master, exactly" % (label, name),
              M.schema_of(path) == published,
              sorted(set(M.schema_of(path)) ^ set(published))[:4])
        check("%s: %s has the published meta keys" % (label, name),
              _meta_keys(path) == _meta_keys(PUBLISHED),
              sorted(_meta_keys(path) ^ _meta_keys(PUBLISHED)))
        counts = M.counts_of(path)
        empty = [t for t, n in counts.items() if n == 0 and t != "ford_gauges"]
        check("%s: %s has rows in every table" % (label, name), not empty,
              empty)
        check("%s: %s is under 1 MB" % (label, name),
              os.path.getsize(path) <= M.MAX_BYTES, os.path.getsize(path))
    check("%s: the changeset is under 1 MB" % label,
          os.path.getsize(change) <= M.MAX_BYTES)

    db = sqlite3.connect("file:%s?mode=ro" % change.replace("\\", "/"),
                         uri=True)
    try:
        import json
        meta = dict(db.execute("SELECT key, value FROM meta"))
        carries = json.loads(meta.get("carries") or "{}")
        written = {t: db.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                   for t in carries}
        removed = dict(db.execute(
            "SELECT tbl, COUNT(*) FROM removed_rows GROUP BY tbl"))
        tiles = db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    finally:
        db.close()
    check("%s: the changeset carries every table the container holds"
          % label, carries == _carried(before) == _carried(PUBLISHED),
          carries)
    for key in sorted(_meta_keys(PUBLISHED) - {"built_at", "kind"}):
        check("%s: the changeset restates %s" % (label, key), key in meta)
    new_meta = C.snapshot(after)["meta"]
    old_meta = C.snapshot(before)["meta"]
    check("%s: evidence_age moved between the builds" % label,
          old_meta["evidence_age"] != new_meta["evidence_age"])
    for table in ("ways", "pois", "fords", "way_wetness"):
        check("%s: a %s row written" % (label, table), written.get(table, 0))
        check("%s: a %s row removed" % (label, table), removed.get(table, 0))
    check("%s: tiles re-cut" % label, tiles > 0)

    # AND IT LANDS, applied by the test's own applier rather than the
    # generator's, so the two implementations check each other.
    with tempfile.TemporaryDirectory() as tmp:
        working = os.path.join(tmp, "w.tbmap")
        shutil.copyfile(before, working)
        T.apply_v2(working, change)
        check("%s: before + changeset is after, in every table" % label,
              T.every_table(working) == T.every_table(after))


def test_the_published_container_is_the_live_shape():
    print("the published region container")
    check("it is there", os.path.isfile(PUBLISHED), PUBLISHED)
    if not os.path.isfile(PUBLISHED):
        return
    try:
        carried = _carried(PUBLISHED)
        check("the changeset builder can carry every table it holds", True)
    except SystemExit as e:
        check("the changeset builder can carry every table it holds", False,
              str(e))
        return
    # SAID, NOT GATED. This file runs in the scheduled refresh before
    # anything is built, against the containers published LAST time. A gate
    # here would deadlock the first run after a schema change: the commit
    # that adds a table updates LIVE_TABLES, the published set does not have
    # the table until that run publishes it, and the run would refuse to.
    # The shape itself is gated above and below, against the file, whatever
    # it holds; this only says when test_build_changeset.py's hand-built
    # live pair has fallen behind it.
    if carried != T.LIVE_TABLES:
        print("  NOTE the published tables %s differ from the live pair "
              "test_build_changeset.py models %s; update LIVE_TABLES and "
              "write_live_shape" % (sorted(carried), sorted(T.LIVE_TABLES)))
    missing = [k for k in APP_META if k not in _meta_keys(PUBLISHED)]
    if missing:
        print("  NOTE the published meta has none of %s, which the app "
              "reads" % missing)


def test_the_generator_makes_the_live_shape():
    print("the generator's output")
    with tempfile.TemporaryDirectory() as tmp:
        report = M.make(PUBLISHED, tmp)
        check("it reports three files", len(report["sizes"]) == 3,
              report["sizes"])
        check_fixture(tmp, "generated")


def test_the_generator_refuses_a_fixture_that_is_not_the_source():
    print("the generator refuses a fixture with a different schema")
    real = M.make_before

    def broken(source, path, box):
        as_of = real(source, path, box)
        db = sqlite3.connect(path)
        db.execute("DROP TABLE wet_gauges")
        db.commit()
        db.close()
        return as_of
    M.make_before = broken
    try:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                M.make(PUBLISHED, tmp)
                check("a dropped table is refused", False, "it was written")
            except SystemExit as e:
                check("a dropped table is refused", True, str(e))
    finally:
        M.make_before = real


def test_the_app_fixture_is_the_live_shape():
    print("the fixture committed in the app repository")
    if not os.path.isdir(APP_FIXTURE):
        # Not a pass: CI checks out this repository alone, so there is no
        # subject here to judge. Said out loud rather than counted green.
        print("  BLIND %s is not checked out beside this repository"
              % APP_FIXTURE)
        return
    check_fixture(APP_FIXTURE, "app")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failed:
        print("%d FAILED: %s" % (len(_failed), ", ".join(_failed)))
        return 1
    print("%d checks, all passed" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

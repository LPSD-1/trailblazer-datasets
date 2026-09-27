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
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import build_changeset as C          # noqa: E402
import evidence_age as EA            # noqa: E402
import make_changeset_fixture as M   # noqa: E402
import stamp_build as S              # noqa: E402
import test_build_changeset as T     # noqa: E402

PUBLISHED = os.path.join(ROOT, M.DEFAULT_SOURCE)
APP_FIXTURE = os.path.join(os.path.dirname(ROOT), "greenroadmap-app", "test",
                           "fixtures", "changesets_live_shape")

#: The meta keys the app reads, which a changeset must restate. Named here
#: so a container that stopped carrying one is a red test, not a quiet one.
APP_META = ("built_at", "kind", "bounds", "context_note", "context_scope",
            "evidence_dates", "schema_version", "min_zoom", "max_zoom")

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
    """The meta keys, with the legacy `evidence_age` read as the
    `evidence_dates` that replaces it.

    ONE TRANSITION. A container published before evidence_dates carries
    evidence_age, and the fixture is the shape the NEXT build publishes; the
    two name the same thing ("how old is this answer") and the first build
    after the switch removes the old key (its changeset's `removed_meta`).
    Once the published set carries evidence_dates this mapping is a no-op.
    """
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        return {EA.META_KEY if k == EA.LEGACY_KEY else k
                for (k,) in db.execute("SELECT key FROM meta")}
    finally:
        db.close()


#: The meta key a build writes only where a record here carries one
#: (build_map_container.write_container), so a cut of a region carries it
#: only when one of its ways does. Checked by `_also_recorded_by_for`.
ALSO = "also_recorded_by"

#: Keys a container carries or not by what it holds, never by schema:
#: `ways_cut` (the stamps, checked below) and `also_recorded_by`.
CONDITIONAL_META = {S.WAYS_CUT, ALSO}


def _meta_value(path, key):
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        got = db.execute("SELECT value FROM meta WHERE key = ?",
                         (key,)).fetchone()
        return got[0] if got else None
    finally:
        db.close()


def _also_recorded_by_for(path):
    """The published region's also_recorded_by, cut to `path`'s ways."""
    published = json.loads(_meta_value(PUBLISHED, ALSO) or "{}")
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        kept = {r[0] for r in db.execute("SELECT way_uid FROM ways")}
    finally:
        db.close()
    return {k: v for k, v in published.items() if k in kept}


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
    # `ways_cut` ASIDE: it is a stamp, not a key every build has. Rule 3 of
    # stamp_build.py writes it, and only for a build whose ways did not
    # change - so `before`, a restamped build, carries it whether or not the
    # published file does today, and `after`, whose ways change, never does.
    for path in (before, after):
        name = os.path.basename(path)
        check("%s: %s has the published sqlite_master, exactly" % (label, name),
              M.schema_of(path) == published,
              sorted(set(M.schema_of(path)) ^ set(published))[:4])
        check("%s: %s has the published meta keys (ways_cut aside)"
              % (label, name),
              _meta_keys(path) - CONDITIONAL_META
              == _meta_keys(PUBLISHED) - CONDITIONAL_META,
              sorted((_meta_keys(path) ^ _meta_keys(PUBLISHED))
                     - CONDITIONAL_META))
        want = _also_recorded_by_for(path)
        got = _meta_value(path, ALSO)
        check("%s: %s carries also_recorded_by for its own ways only, and "
              "no key where none of them has one" % (label, name),
              (json.loads(got) if got is not None else None)
              == (want or None), (got, want))
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
    for key in sorted((_meta_keys(PUBLISHED) - {"built_at", "kind"}
                       - CONDITIONAL_META)
                      | (_meta_keys(after) & {ALSO})):
        check("%s: the changeset restates %s" % (label, key), key in meta)
    new_meta = C.snapshot(after)["meta"]
    old_meta = C.snapshot(before)["meta"]

    # THE STAMPS, as two real builds carry them: `before` restamped by rule
    # 3 over unchanged ways, `after` a fresh build of changed ways.
    removed_meta = json.loads(meta.get("removed_meta") or "[]")
    check("%s: before.tbmap is a restamped build: ways_cut, and a later "
          "built_at" % label,
          S.WAYS_CUT in old_meta
          and old_meta[S.WAYS_CUT] < old_meta.get("built_at", ""),
          (old_meta.get(S.WAYS_CUT), old_meta.get("built_at")))
    check("%s: after.tbmap, whose ways changed, carries no ways_cut"
          % label, S.WAYS_CUT not in new_meta, new_meta.get(S.WAYS_CUT))
    check("%s: the lanes of after.tbmap are dated by its own built_at"
          % label, S.ways_cut(after) == new_meta.get("built_at"),
          S.ways_cut(after))
    check("%s: the changeset removes ways_cut and restates none" % label,
          S.WAYS_CUT in removed_meta and S.WAYS_CUT not in meta,
          removed_meta)
    pois = sqlite3.connect("file:%s?mode=ro" % after.replace("\\", "/"),
                           uri=True)
    try:
        newest_poi = pois.execute(
            "SELECT MAX(source_date) FROM pois").fetchone()[0]
    finally:
        pois.close()
    check("%s: pois_checked moved with the refetch, and no POI was first "
          "seen after it" % label,
          old_meta.get("pois_checked") != new_meta.get("pois_checked")
          and meta.get("pois_checked") == new_meta.get("pois_checked")
          and newest_poi <= new_meta.get("pois_checked", ""),
          (old_meta.get("pois_checked"), new_meta.get("pois_checked"),
           newest_poi))
    check("%s: evidence_dates moved between the builds" % label,
          old_meta.get(EA.META_KEY) != new_meta.get(EA.META_KEY)
          and EA.META_KEY in new_meta)
    check("%s: no fixture carries the legacy evidence_age" % label,
          EA.LEGACY_KEY not in old_meta and EA.LEGACY_KEY not in new_meta)
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


def test_the_generator_refuses_an_after_that_keeps_ways_cut():
    print("the generator refuses an after.tbmap that kept before's ways_cut")
    real = M.make_after

    def copied(before, path, as_of, source):
        edits = real(before, path, as_of, source)
        db = sqlite3.connect(path)
        db.execute("INSERT INTO meta VALUES (?, ?)",
                   (S.WAYS_CUT, S.ways_cut(before)))
        db.commit()
        db.close()
        return edits
    M.make_after = copied
    try:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                M.make(PUBLISHED, tmp)
                check("an after with ways_cut is refused", False,
                      "it was written")
            except SystemExit as e:
                check("an after with ways_cut is refused",
                      "ways_cut" in str(e), str(e))
    finally:
        M.make_after = real


def test_keep_before_leaves_before_alone():
    print("--keep-before rewrites after, the changeset and the README only")
    with tempfile.TemporaryDirectory() as tmp:
        M.make(PUBLISHED, tmp)
        path = os.path.join(tmp, "before.tbmap")
        # MARKED, so a regenerated `before` cannot pass for the kept one:
        # make_before is deterministic, and without this a --keep-before
        # that cut `before` afresh wrote the same bytes and passed.
        db = sqlite3.connect(path)
        try:
            db.execute("PRAGMA user_version = 11")
        finally:
            db.close()
        with open(path, "rb") as fh:
            was = fh.read()
        os.remove(os.path.join(tmp, "after.tbmap"))
        report = M.make(PUBLISHED, tmp, keep_before=True)
        with open(path, "rb") as fh:
            check("before.tbmap is byte for byte the one that was there",
                  fh.read() == was)
        check("after.tbmap is written again",
              os.path.isfile(os.path.join(tmp, "after.tbmap")))
        with open(os.path.join(tmp, "README.md"), encoding="utf-8") as fh:
            check("the README gives the command that wrote the set",
                  "--keep-before" in fh.read())
        check("and it is the live shape, stamps and all",
              report["removed_meta"] == [S.WAYS_CUT], report["removed_meta"])
        check_fixture(tmp, "kept")


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

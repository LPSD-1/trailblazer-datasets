"""A changeset that moves only meta is a changeset, and is published.

WHAT WAS WRONG. `validate_changeset._content_problems` counted tiles, records
and table removals, and nothing in `meta`. The one-off switch from
`meta.evidence_age` (ages in days, which moved every container's bytes each
month) to `meta.evidence_dates` (dates, which do not) changes NO row of any
container - it removes one key and adds another. Measured on Wales and South
West, that changeset is 15,872 bytes; the validator refused it as "carries no
tiles, no records and no removals", publish_changesets.py skipped it, and
every rider holding a region re-downloaded all six - ~94 MB, on the paid
freshness tier - to receive two meta keys. The app has applied a meta-only
changeset all along (the app repo's
test/wiring/changesets/an_update_that_moves_only_meta_applies_in_place_test.dart);
it was never offered one.

THE PREMISE IS A REAL CONTAINER. The switch is made with `evidence_age.
write_meta`, the tool the pipeline runs, on a copy of a container this
repository publishes. Today that container still carries `evidence_age`;
once this fix ships the switch, the next refresh commits it carrying
`evidence_dates`, and a premise that demanded the published file be
pre-switch failed from then on - inside the `tools/test_*.py` loop that
refresh-data.yml runs with `|| exit 1`, so the fix would have blocked every
refresh after its own first one. The old build is therefore PUT in the
pre-switch shape (`_pre_switch`) when the published one has moved on, and
the premise still checks that `write_meta` makes the switch.

AND THE REFUSAL STAYS. A changeset that changes nothing at all is still
refused: by the publisher, which holds the build the changeset is cut from
and so can see that every restated key is one the rider already has; and by
the window gate in refresh-data.yml, which holds only the files, when there
is nothing in one that could move a rider's container.

Run: python tools/test_validate_changeset.py
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_changeset  # noqa: E402
import evidence_age  # noqa: E402
import publish_changesets  # noqa: E402
import validate_changeset  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLISHED = os.path.join(REPO, "containers")
#: The region the cost verifier measured the refusal on.
SUBJECT = "ways-wales.tbmap"

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


def _meta(path):
    db = sqlite3.connect(path)
    try:
        return dict(db.execute("SELECT key, value FROM meta"))
    finally:
        db.close()


def _sql(path, *statements):
    db = sqlite3.connect(path)
    try:
        for statement, args in statements:
            db.execute(statement, args)
        db.commit()
    finally:
        db.close()


def _restamp(path, built_at):
    _sql(path, ("UPDATE meta SET value=? WHERE key='built_at'", (built_at,)))


def _later(stamp):
    """One second past a build stamp, the way stamp_build.py's rule 3 moves a
    build whose ways did not change."""
    import datetime
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.datetime.strptime(stamp, fmt)
            + datetime.timedelta(seconds=1)).strftime(fmt)


def _tree(directory, src):
    """A one-container tree with the manifest publish() reads."""
    os.makedirs(directory, exist_ok=True)
    shutil.copyfile(src, os.path.join(directory, SUBJECT))
    with open(os.path.join(directory, "manifest.json"), "w",
              encoding="utf8") as fh:
        json.dump({"containers": [{"id": "gb-wales", "kind": "area",
                                   "file": "containers/%s" % SUBJECT}]}, fh)
    return directory


def _publish(work, old_src, new_src):
    """Run the publisher over one old and one new build, as CI does."""
    old = _tree(os.path.join(work, "old"), old_src)
    new = _tree(os.path.join(work, "new"), new_src)
    out = os.path.join(work, "changes")
    report = publish_changesets.publish(old, new, out)
    files = [os.path.join(out, "gb-wales", name)
             for name in (os.listdir(os.path.join(out, "gb-wales"))
                          if os.path.isdir(os.path.join(out, "gb-wales"))
                          else [])]
    return report, files


def _changeset(work, old_path, new_path, name):
    out = os.path.join(work, name)
    build_changeset.build_changeset(old_path, new_path, out)
    return out


def _pre_switch(src, path):
    """Copy a published container to `path` in the shape it had before the
    evidence_age -> evidence_dates switch.

    A no-op on a container that still carries `evidence_age`. On one the
    switch has already reached, the legacy key is written back the way the
    old tool wrote it (an age report, `read_container`) and `evidence_dates`
    taken out - otherwise this test goes red the day its own fix ships.
    """
    shutil.copyfile(src, path)
    held = _meta(path)
    if evidence_age.LEGACY_KEY in held:
        return
    import datetime
    legacy = evidence_age.read_container(path, datetime.date(2026, 1, 1))
    _sql(path,
         ("DELETE FROM meta WHERE key = ?", (evidence_age.META_KEY,)),
         ("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
          (evidence_age.LEGACY_KEY, json.dumps(legacy, sort_keys=True))))


# ---------------------------------------------------------------------------


def test_the_evidence_key_switch_is_published(work, src):
    print("the evidence_age -> evidence_dates switch, and nothing else, "
          "reaches a rider")
    old = os.path.join(work, "old.tbmap")
    new = os.path.join(work, "new.tbmap")
    _pre_switch(src, old)
    shutil.copyfile(old, new)
    evidence_age.write_meta(new)
    _restamp(new, _later(_meta(old)["built_at"]))

    before, after = _meta(old), _meta(new)
    premise = ("evidence_age" in before and "evidence_age" not in after
               and "evidence_dates" in after)
    check("premise: the old build carries evidence_age and write_meta "
          "leaves evidence_dates instead", premise,
          "before %s, after %s" % (sorted(before), sorted(after)))
    if not premise:
        return

    cs = _changeset(work, old, new, "switch.tbchange")
    db = sqlite3.connect(cs)
    try:
        rows = sum(db.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                   for (t,) in db.execute(
                       "SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name != 'meta' AND name NOT LIKE 'sqlite_%'"))
    finally:
        db.close()
    check("premise: the changeset carries no row of any table", rows == 0,
          "%d rows" % rows)

    found = validate_changeset.problems_with(cs, against=old)
    check("the validator accepts it against the build it applies to",
          not found, "; ".join(found))
    found = validate_changeset.problems_with(cs)
    check("and on its own, as the window gate in refresh-data.yml runs it",
          not found, "; ".join(found))

    report, files = _publish(os.path.join(work, "p1"), old, new)
    check("publish_changesets builds it", len(report["built"]) == 1,
          "built %s, skipped %s" % (report["built"], report["skipped"]))
    announced = [c for c in report["index"]["changesets"]
                 if c["container"] == "containers/%s" % SUBJECT]
    check("and announces it in changes/index.json", len(announced) == 1,
          "%s" % report["index"]["changesets"])
    check("and the file it wrote is the one it announced, and passes the "
          "window gate", len(files) == 1
          and not validate_changeset.problems_with(files[0]),
          "%s" % files)
    if files:
        print("       %d bytes instead of %d"
              % (os.path.getsize(files[0]), os.path.getsize(new)))


def test_a_meta_value_change_alone_is_published(work, src):
    print("a restated key whose value moved, with no row moving, is "
          "something to apply")
    old = os.path.join(work, "old.tbmap")
    new = os.path.join(work, "new.tbmap")
    shutil.copyfile(src, old)
    shutil.copyfile(src, new)
    _sql(new, ("UPDATE meta SET value = value || ' Amended.' "
               "WHERE key = 'context_note'", ()))
    _restamp(new, _later(_meta(old)["built_at"]))
    check("premise: context_note differs",
          _meta(old).get("context_note") != _meta(new).get("context_note"))

    cs = _changeset(work, old, new, "note.tbchange")
    found = validate_changeset.problems_with(cs, against=old)
    check("accepted against the build it applies to", not found,
          "; ".join(found))
    found = validate_changeset.problems_with(cs)
    check("and by the window gate", not found, "; ".join(found))
    report, _ = _publish(os.path.join(work, "p2"), old, new)
    check("and published", len(report["built"]) == 1,
          "skipped %s" % report["skipped"])


def test_a_removed_key_alone_is_published(work, src):
    # THE REMOVAL HALF ON ITS OWN. The key switch also restates a new key,
    # so it passes on that alone: a validator that ignored removed_meta
    # entirely went green on every other case here, and would refuse the
    # build that retires a key and adds none - a whole download again.
    print("a key the new build drops, with nothing else moving, is "
          "something to apply")
    old = os.path.join(work, "old.tbmap")
    new = os.path.join(work, "new.tbmap")
    shutil.copyfile(src, old)
    shutil.copyfile(src, new)
    _sql(new, ("DELETE FROM meta WHERE key = 'context_note'", ()))
    _restamp(new, _later(_meta(old)["built_at"]))

    cs = _changeset(work, old, new, "drop.tbchange")
    carried = _meta(cs)
    check("premise: it removes context_note and restates nothing new",
          json.loads(carried["removed_meta"]) == ["context_note"]
          and all(_meta(old).get(k) == v for k, v in carried.items()
                  if k not in validate_changeset.REQUIRED_META),
          "%s" % carried.get("removed_meta"))
    found = validate_changeset.problems_with(cs, against=old)
    check("accepted against the build it applies to", not found,
          "; ".join(found))
    report, _ = _publish(os.path.join(work, "p4"), old, new)
    check("and published", len(report["built"]) == 1,
          "skipped %s" % report["skipped"])


def test_a_changeset_that_changes_nothing_is_still_refused(work, src):
    print("a changeset that changes nothing is still refused")
    old = os.path.join(work, "old.tbmap")
    new = os.path.join(work, "new.tbmap")
    shutil.copyfile(src, old)
    shutil.copyfile(src, new)
    _restamp(new, _later(_meta(old)["built_at"]))

    cs = _changeset(work, old, new, "nothing.tbchange")
    found = validate_changeset.problems_with(cs, against=old)
    check("refused against the build it applies to: every restated key is "
          "one the rider already holds",
          any("nothing" in p or "no tiles" in p for p in found),
          "%s" % found)
    report, files = _publish(os.path.join(work, "p3"), old, new)
    check("and the publisher does not announce it",
          not report["built"] and not files
          and not report["index"]["changesets"],
          "built %s" % report["built"])

    # THE WINDOW GATE HOLDS NO OLD BUILD, so it refuses what it can prove:
    # a file with nothing in it that could move a container at all.
    bare = os.path.join(work, "bare.tbchange")
    shutil.copyfile(cs, bare)
    _sql(bare, ("DELETE FROM meta WHERE key NOT IN (%s)"
                % ",".join("?" * len(validate_changeset.REQUIRED_META)),
                validate_changeset.REQUIRED_META))
    found = validate_changeset.problems_with(bare)
    check("a file that restates no key and removes none is refused on its "
          "own", any("no tiles" in p for p in found), "%s" % found)

    # A removal of a key the rider does not have removes nothing.
    ghost = os.path.join(work, "ghost.tbchange")
    shutil.copyfile(cs, ghost)
    _sql(ghost, ("UPDATE meta SET value=? WHERE key='removed_meta'",
                 (json.dumps(["no_such_key"]),)))
    found = validate_changeset.problems_with(ghost, against=old)
    check("removing only a key the build does not hold is still nothing",
          any("nothing" in p or "no tiles" in p for p in found),
          "%s" % found)


def test_checked_against_the_wrong_build(work, src):
    print("a changeset checked against a build it was not cut from")
    old = os.path.join(work, "old.tbmap")
    new = os.path.join(work, "new.tbmap")
    _pre_switch(src, old)
    shutil.copyfile(old, new)
    evidence_age.write_meta(new)
    _restamp(new, _later(_meta(old)["built_at"]))
    cs = _changeset(work, old, new, "switch.tbchange")
    found = validate_changeset.problems_with(cs, against=new)
    check("is refused, not judged against the wrong meta",
          any("from_build" in p for p in found), "%s" % found)
    # Nor against one that is not there: falling back to the file-alone
    # check would accept every changeset build_changeset writes, since it
    # restates every key.
    found = validate_changeset.problems_with(
        cs, against=os.path.join(work, "absent.tbmap"))
    check("and refused, not waved through, against a build that will not "
          "open", any("will not open" in p for p in found), "%s" % found)


def main():
    src = os.path.join(PUBLISHED, SUBJECT)
    print("the subject is a container this repository publishes")
    check("%s is present" % SUBJECT, os.path.isfile(src), src)
    if not os.path.isfile(src):
        print("\nBLIND: no subject")
        return 1
    for test in (test_the_evidence_key_switch_is_published,
                 test_a_meta_value_change_alone_is_published,
                 test_a_removed_key_alone_is_published,
                 test_a_changeset_that_changes_nothing_is_still_refused,
                 test_checked_against_the_wrong_build):
        work = tempfile.mkdtemp(prefix="tb-validate-cs-")
        try:
            test(work, src)
        except Exception as e:      # a crash is a failure, not a skip
            check(test.__name__, False, "raised %r" % e)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if FAILURES:
        print("\nFAILED: %d" % len(FAILURES))
        for f in FAILURES:
            print("  " + f)
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

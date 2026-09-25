"""Tests for the publisher half of the changeset join.

WHAT WAS WRONG. `build_changeset.py` could write a `.tbchange` and the app
could apply one, and nothing joined them: refresh-data.yml's only changeset
reference was `python tools/test_build_changeset.py` - it ran the TESTS and
never BUILT one - `find . -name "*.tbchange"` returned nothing, and
catalogue.json contained zero occurrences of the word. This suite covers the
piece that closes that: publish_changesets.py writes the files and
build_catalogue.py announces them.

THE PREMISE TEST IS `test_the_published_containers_are_here`. Every measurement
below is taken against the containers this repository actually publishes, and
zero subjects is BLIND, not PASS.

Run: python tools/test_publish_changesets.py
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_catalogue  # noqa: E402
import build_changeset  # noqa: E402
import publish_changesets  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLISHED = os.path.join(REPO, "containers")

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


def _restamp(path, built_at):
    db = sqlite3.connect(path)
    try:
        db.execute("UPDATE meta SET value=? WHERE key='built_at'", (built_at,))
        db.commit()
    finally:
        db.close()


def _amend(path, n=12):
    """Change `n` lane names, the shape of a council revision."""
    db = sqlite3.connect(path)
    try:
        table = "ways" if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ways'"
        ).fetchone() else "lanes"
        rows = [r[0] for r in db.execute(
            "SELECT rowid FROM %s LIMIT %d" % (table, n))]
        for rowid in rows:
            db.execute("UPDATE %s SET name=? WHERE rowid=?"
                       % table, ("Amended lane %d" % rowid, rowid))
        db.commit()
        return len(rows)
    finally:
        db.close()


def _tree(directory, containers):
    """A container tree with a manifest, the shape publish() reads."""
    os.makedirs(directory, exist_ok=True)
    entries = []
    for pack_id, src in containers:
        name = "%s.tbmap" % pack_id
        shutil.copyfile(src, os.path.join(directory, name))
        # THE CATALOGUE'S PACK ID IS NOT THIS ID. `build_catalogue.lane_areas`
        # appends the dataset name, so `gb-south-west` here becomes
        # `gb-south-west-ways` there. The tests below deliberately use the
        # divergent pair, because matching on the id is the mistake that made
        # every lane pack's changeset invisible in catalogue.json.
        entries.append({"id": pack_id, "kind": "area",
                        "file": "containers/%s" % name})
    with open(os.path.join(directory, "manifest.json"), "w",
              encoding="utf8") as fh:
        json.dump({"containers": entries}, fh)
    return directory


def published_containers():
    manifest = os.path.join(PUBLISHED, "manifest.json")
    if not os.path.isfile(manifest):
        return []
    with open(manifest, encoding="utf8") as fh:
        out = []
        for entry in json.load(fh).get("containers", []):
            path = os.path.join(PUBLISHED, os.path.basename(entry["file"]))
            if os.path.isfile(path):
                out.append((entry["id"], path))
        return sorted(out)


# ---------------------------------------------------------------------------


def test_the_published_containers_are_here():
    """ZERO SUBJECTS IS BLIND, NOT PASS.

    Everything below measures a real published container against an amended
    copy of itself. With none present the suite would go green having proved
    nothing at all, which is the failure mode this whole repository's test
    suites exist to avoid.
    """
    print("the containers this repository publishes are present")
    found = published_containers()
    check("at least one published container", len(found) > 0,
          "found %d under %s" % (len(found), PUBLISHED))
    if found:
        built = [b for b in (publish_changesets._built_at(p)
                             for _, p in found) if b]
        check("and every one carries a built_at", len(built) == len(found),
              "%d of %d" % (len(built), len(found)))
    return found


def test_a_real_pair_of_builds(found):
    """MEASURED, on a container this repository actually publishes.

    The rule is DO NOT PUBLISH A CHANGESET BIGGER THAN THE THING IT REPLACES,
    and a rule nobody measures is a rule that quietly stops holding. This takes
    a published container, amends twelve lanes in a copy of it - the shape of a
    council revision - and prints the ratio.
    """
    print("a real pair of builds, measured")
    if not found:
        check("measured against a real build", False, "no containers")
        return
    pack_id, src = found[0]
    tmp = tempfile.mkdtemp(prefix="tb-changes-")
    try:
        old_dir = _tree(os.path.join(tmp, "old"), [(pack_id, src)])
        new_dir = _tree(os.path.join(tmp, "new"), [(pack_id, src)])
        new_path = os.path.join(new_dir, "%s.tbmap" % pack_id)
        amended = _amend(new_path)
        _restamp(new_path, "2099-01-01T00:00:00Z")
        check("twelve lanes amended in the copy", amended > 0, amended)

        out = os.path.join(tmp, "changes")
        report = publish_changesets.publish(old_dir, new_dir, out,
                                            base_url="https://example.test/")
        check("a changeset was published", len(report["built"]) == 1, report)
        if not report["measured"]:
            check("and it was measured", False, report)
            return
        _, delta, whole, ratio = report["measured"][0]
        print("    %s: %d bytes against %d - %.2f%% of the whole build"
              % (pack_id, delta, whole, 100.0 * ratio))
        check("and it is smaller than the container",
              build_changeset.is_worth_publishing(delta, whole),
              "%d of %d (%.3f)" % (delta, whole, ratio))

        # The file is really there, under the name the index gives.
        entry = report["index"]["changesets"][0]
        check("the index joins on the container's published file",
              entry["container"] == "containers/%s.tbmap" % pack_id, entry)
        rel = entry["file"].rsplit("/", 2)[-2:]
        on_disk = os.path.join(out, *rel)
        check("the .tbchange exists on disk", os.path.isfile(on_disk),
              on_disk)
        check("and the index says what it weighs",
              entry["bytes"] == os.path.getsize(on_disk), entry)
        check("and the build it lands on", entry["to"] ==
              "2099-01-01T00:00:00Z", entry)
        check("and the build it applies to",
              entry["from"] == publish_changesets._built_at(src), entry)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_changeset_bigger_than_the_build_is_not_published():
    """The refusal, exercised rather than asserted about.

    A ratio of 0 means "nothing may be published", which is the cheapest way to
    drive the same branch a re-cut tile set would.
    """
    print("a changeset that is not smaller is refused")
    found = published_containers()
    if not found:
        check("refused", False, "no containers")
        return
    pack_id, src = found[0]
    tmp = tempfile.mkdtemp(prefix="tb-changes-big-")
    try:
        old_dir = _tree(os.path.join(tmp, "old"), [(pack_id, src)])
        new_dir = _tree(os.path.join(tmp, "new"), [(pack_id, src)])
        new_path = os.path.join(new_dir, "%s.tbmap" % pack_id)
        _amend(new_path)
        _restamp(new_path, "2099-01-01T00:00:00Z")
        out = os.path.join(tmp, "changes")
        report = publish_changesets.publish(old_dir, new_dir, out,
                                            max_ratio=0.0)
        check("nothing was published", not report["built"], report["built"])
        check("and the reason says so",
              any("not smaller" in why for _, why in report["skipped"]),
              report["skipped"])
        check("and the file was not left behind",
              not any(name.endswith(".tbchange")
                      for _root, _d, files in os.walk(out) for name in files),
              out)
        # And the rider is still told which build the container is at, so the
        # app can tell "already current" from "no changeset".
        check("but the build stamp is still announced",
              report["index"]["builds"].get(
                  "containers/%s.tbmap" % pack_id) == "2099-01-01T00:00:00Z",
              report["index"]["builds"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_window_holds_a_rider_two_updates_behind():
    """Two consecutive runs leave a chain a rider two behind can walk.

    Each run has the bytes of exactly ONE older build, so it writes exactly one
    changeset. The window is what turns that into a catch-up for somebody who
    missed a run - which, at four refreshes a day, is most riders.
    """
    print("a rider two updates behind")
    found = published_containers()
    if not found:
        check("two updates behind", False, "no containers")
        return
    pack_id, src = found[0]
    tmp = tempfile.mkdtemp(prefix="tb-changes-chain-")
    try:
        out = os.path.join(tmp, "changes")
        b0 = publish_changesets._built_at(src)
        old_dir = _tree(os.path.join(tmp, "b0"), [(pack_id, src)])

        mid_dir = _tree(os.path.join(tmp, "b1"), [(pack_id, src)])
        mid_path = os.path.join(mid_dir, "%s.tbmap" % pack_id)
        _amend(mid_path, 6)
        _restamp(mid_path, "2099-01-01T00:00:00Z")
        publish_changesets.publish(old_dir, mid_dir, out,
                                   base_url="https://example.test/")

        new_dir = _tree(os.path.join(tmp, "b2"), [(pack_id, mid_path)])
        new_path = os.path.join(new_dir, "%s.tbmap" % pack_id)
        _amend(new_path, 9)
        _restamp(new_path, "2099-01-02T00:00:00Z")
        report = publish_changesets.publish(mid_dir, new_dir, out,
                                            base_url="https://example.test/")

        index = report["index"]
        check("two changesets are published", len(index["changesets"]) == 2,
              index["changesets"])
        froms = {c["from"] for c in index["changesets"]}
        check("one of them applies to the oldest build", b0 in froms, froms)
        served = "containers/%s.tbmap" % pack_id
        check("and the newest build is what they lead to",
              index["builds"][served] == "2099-01-02T00:00:00Z",
              index["builds"])

        # And the catalogue announces BOTH links, so the app can walk them.
        # The catalogue's own id, which is NOT the container's.
        catalogue = {"schema": 2, "continents": [{"countries": [{
            "code": "GB", "areas": [{"id": "gb-x", "packs": [
                {"id": "%s-ways" % pack_id, "kind": "lanes",
                 "file": served}]}]}]}]}
        builds, by_pack = build_catalogue.changesets_index(
            os.path.join(out, "index.json"))
        build_catalogue.attach_changesets(catalogue, builds, by_pack)
        pack = catalogue["continents"][0]["countries"][0]["areas"][0]["packs"][0]
        check("the catalogue carries the build",
              pack.get("build") == "2099-01-02T00:00:00Z", pack)
        check("and both links of the chain",
              len(pack.get("changesets") or []) == 2, pack.get("changesets"))
        check("and each one names a file and a hash",
              all(c.get("file") and c.get("sha256")
                  for c in pack.get("changesets") or []), pack)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_dangling_changeset_is_not_announced():
    """A link that does not reach this build is dropped from the catalogue.

    Every byte of catalogue.json is fetched by every rider on every check, and
    a changeset the app can apply and still not be current is a byte that buys
    nothing. `Pack.changesetChainFrom` would refuse it anyway.
    """
    print("a changeset that does not reach this build")
    index = {
        "builds": {"containers/p.tbmap": "B3"},
        "changesets": [
            {"pack": "p", "container": "containers/p.tbmap",
             "from": "B1", "to": "B2", "file": "a.tbchange",
             "sha256": "aa", "bytes": 10},
            {"pack": "p", "container": "containers/p.tbmap",
             "from": "B9", "to": "BX", "file": "b.tbchange",
             "sha256": "bb", "bytes": 10},
        ],
    }
    tmp = tempfile.mkdtemp(prefix="tb-changes-dangle-")
    try:
        path = os.path.join(tmp, "index.json")
        with open(path, "w", encoding="utf8") as fh:
            json.dump(index, fh)
        builds, by_pack = build_catalogue.changesets_index(path)
        catalogue = {"schema": 2, "continents": [{"countries": [{
            "code": "GB", "areas": [{"id": "a", "packs": [
                {"id": "p-ways", "kind": "lanes",
                 "file": "containers/p.tbmap"}]}]}]}]}
        build_catalogue.attach_changesets(catalogue, builds, by_pack)
        pack = catalogue["continents"][0]["countries"][0]["areas"][0]["packs"][0]
        check("nothing is announced", not pack.get("changesets"),
              pack.get("changesets"))
        check("but the build still is", pack.get("build") == "B3", pack)

        index["changesets"].append(
            {"pack": "p", "container": "containers/p.tbmap",
             "from": "B2", "to": "B3", "file": "c.tbchange",
             "sha256": "cc", "bytes": 10})
        with open(path, "w", encoding="utf8") as fh:
            json.dump(index, fh)
        builds, by_pack = build_catalogue.changesets_index(path)
        catalogue = {"schema": 2, "continents": [{"countries": [{
            "code": "GB", "areas": [{"id": "a", "packs": [
                {"id": "p-ways", "kind": "lanes",
                 "file": "containers/p.tbmap"}]}]}]}]}
        build_catalogue.attach_changesets(catalogue, builds, by_pack)
        pack = catalogue["continents"][0]["countries"][0]["areas"][0]["packs"][0]
        got = {(c["from"], c["to"]) for c in pack.get("changesets") or []}
        check("and once the chain joins up, both links are",
              got == {("B1", "B2"), ("B2", "B3")}, got)
        check("and the unreachable one still is not",
              ("B9", "BX") not in got, got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_index_does_not_move_when_nothing_changed():
    """121 runs a month must cost riders nothing.

    A run stamp in this file would make the Publish step's "did anything
    change" diff answer yes on every run over data that never moved - which is
    the fault containers/manifest.json already has, and the reason this
    repository's lane refresh is called a bandwidth bill four times a day.
    """
    print("an unchanged build writes nothing")
    found = published_containers()
    if not found:
        check("nothing written", False, "no containers")
        return
    pack_id, src = found[0]
    tmp = tempfile.mkdtemp(prefix="tb-changes-idem-")
    try:
        old_dir = _tree(os.path.join(tmp, "old"), [(pack_id, src)])
        new_dir = _tree(os.path.join(tmp, "new"), [(pack_id, src)])
        out = os.path.join(tmp, "changes")
        first = publish_changesets.publish(old_dir, new_dir, out)
        check("the first run writes the build stamp", first["index_written"],
              first)
        second = publish_changesets.publish(old_dir, new_dir, out)
        check("and a second run writes nothing at all",
              not second["index_written"], second)
        check("and no changeset was built either", not second["built"],
              second["built"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_same_build_different_bytes_is_refused_not_skipped():
    """THE STATE NO CHANGESET CAN COVER, refused at the gate.

    Before stamp_build.py, a POI or ford refresh - or simply the 1st of the
    month, through meta.evidence_age - rewrote a region's bytes under the
    built_at it already had. This step called that "unchanged" and moved on,
    the index announced the old build under a new hash, and every rider
    fetched the region whole. Now it fails the job.
    """
    print("same built_at, different bytes, is refused")
    found = published_containers()
    if not found:
        check("refused", False, "no containers")
        return
    pack_id, src = found[0]
    tmp = tempfile.mkdtemp(prefix="tb-changes-samebuild-")
    try:
        old_dir = _tree(os.path.join(tmp, "old"), [(pack_id, src)])
        new_dir = _tree(os.path.join(tmp, "new"), [(pack_id, src)])
        new_path = os.path.join(new_dir, "%s.tbmap" % pack_id)
        was = publish_changesets._built_at(new_path)
        check("PREMISE: an amended copy", _amend(new_path, 1) == 1)
        check("PREMISE: under the same built_at",
              publish_changesets._built_at(new_path) == was)
        out = os.path.join(tmp, "changes")
        report = publish_changesets.publish(old_dir, new_dir, out)
        check("it is refused", len(report["refused"]) == 1, report["refused"])
        check("not waved through as unchanged",
              not any(why == "unchanged" for _, why in report["skipped"]),
              report["skipped"])
        argv = sys.argv
        sys.argv = ["publish_changesets.py", "--old", old_dir, "--new",
                    new_dir, "--out", out]
        try:
            code = publish_changesets.main()
        finally:
            sys.argv = argv
        check("and the step exits non-zero", code == 1, code)

        # And the honest "unchanged" - same stamp, same bytes - still is.
        same_dir = _tree(os.path.join(tmp, "same"), [(pack_id, src)])
        report = publish_changesets.publish(old_dir, same_dir, out)
        check("identical bytes are not refused", not report["refused"],
              report["refused"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    found = test_the_published_containers_are_here()
    test_same_build_different_bytes_is_refused_not_skipped()
    test_a_real_pair_of_builds(found)
    test_a_changeset_bigger_than_the_build_is_not_published()
    test_the_window_holds_a_rider_two_updates_behind()
    test_a_dangling_changeset_is_not_announced()
    test_the_index_does_not_move_when_nothing_changed()
    if FAILURES:
        print("\n%d failure(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("\nall changeset publishing tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

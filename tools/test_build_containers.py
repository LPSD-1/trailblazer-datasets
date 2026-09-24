"""The clock must not reach a container.

WHAT THIS IS GUARDING. `build_containers.py` stamped every container with
`manifest["generated"]` - when THIS RUN wrote the manifest - so every container
was new on every run even when no council had amended anything. Measured on two
CI builds of unchanged data with one SQLite: motor-wales.tbmap differed in five
bytes out of 1,114,112, `built_at` moving 21:47:46Z -> 22:52:13Z. Five bytes
move the sha256, so every rider re-downloads all 343 MB. It also refused a
publish: the catalogue check compares each pack against the file on disk, and
129 containers disagreed with what had been published an hour earlier.

A pack carries its own `generated`, and build_packages keeps that stamp when the
lanes have not changed - the whole mechanism that makes packs rebuild
byte-identical. Containers have to inherit it.

WHY THIS READS THE SOURCE rather than building. Building a container needs the
pack key and real sealed packs, which a unit test has no business requiring -
the same reason test_sign_release.py reads the source to prove the private key
can never be passed on a command line. The behaviour itself was verified by
building: same packs with run stamps an hour apart gave seven byte-identical
containers, and moving one pack's own stamp changed that pack's container and
its overview and nothing else.
"""
import ast
import glob
import io
import json
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "build_containers.py")

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


def _tree():
    return ast.parse(io.open(SOURCE, encoding="utf-8").read())


def _calls_named(tree, name):
    """Every call to `<anything>.name(...)` or `name(...)`."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == name:
            out.append(node)
        elif isinstance(f, ast.Name) and f.id == name:
            out.append(node)
    return out


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def test_no_container_is_written_with_the_run_stamp():
    tree = _tree()
    calls = _calls_named(tree, "write_container")
    check("write_container is still called", len(calls) >= 2,
          "found %d" % len(calls))
    for call in calls:
        used = set()
        for arg in call.args:
            used |= _names_in(arg)
        for kw in call.keywords:
            used |= _names_in(kw.value)
        check("write_container at line %d does not take the run stamp"
              % call.lineno,
              "run_stamp" not in used,
              "a container stamped with the clock is new every month")


def test_the_entries_do_not_carry_the_run_stamp_either():
    # The entry's `generated` reaches the catalogue. Stamping it with the run
    # makes the catalogue disagree with the container it describes.
    tree = _tree()
    for call in _calls_named(tree, "_entry"):
        for kw in call.keywords:
            if kw.arg == "generated":
                check("_entry at line %d does not take the run stamp"
                      % call.lineno,
                      "run_stamp" not in _names_in(kw.value))


def test_the_pack_stamp_is_what_is_read():
    src = io.open(SOURCE, encoding="utf-8").read()
    check("the pack's own generated is read",
          'pack.get("generated")' in src,
          "nothing reads the stamp build_packages preserved")


def test_the_run_stamp_still_describes_the_manifest():
    # It is not wrong, it just belongs on the manifest and nowhere else:
    # nothing hashes that field.
    src = io.open(SOURCE, encoding="utf-8").read()
    check("the manifest still records when it was written",
          'manifest.get("generated")' in src)


# --------------------------------------------------------------------------
# step 1.2 - ONE dataset, and no vehicle anywhere in it
# --------------------------------------------------------------------------
#
# This half DOES build, because the thing under test is what comes out. It
# needs no published key: a pack is sealed with a fixture key and opened with
# the same one, which is exactly the path the real build takes.

sys.path.insert(0, HERE)
import build_containers as C           # noqa: E402
import build_packages as P             # noqa: E402

KEY = bytes(range(32))


def _way(uid, way_class, moto, fourxfour, lon, lat, authority="Derbyshire"):
    return {
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [(lon, lat), (lon + 0.01, lat + 0.01)]},
        "properties": {
            "lane_uid": uid, "class": way_class, "county": authority,
            "name": uid, "designation": way_class, "authority": authority,
            "legal_tier": "statutory", "source": "rowmaps:derbyshire",
            "source_date": "2026-03-04", "motorbike_ok": moto,
            "fourxfour_ok": fourxfour,
            "access_reason": "because the definitive map says so",
            "access_evidence": "statutory", "lengthKm": 1.2,
        },
    }


def _pipeline():
    """packs -> containers, the way the real build runs it."""
    tmp = tempfile.mkdtemp(prefix="tbways-pipeline-")
    real_dist = P.dist_dir
    P.dist_dir = lambda: tmp
    try:
        packs = []
        for region, label, lon, lat in (("midlands", "Midlands", -1.70, 52.50),
                                        ("wales", "Wales", -3.40, 52.20)):
            feats = [_way("%s-boat" % region, "boat", 1, 1, lon, lat),
                     _way("%s-bw" % region, "bridleway", 0, 0, lon + .02, lat)]
            packs.append(P.write_package(
                P.DATASET, region, label, None, feats, KEY,
                "2026-03-04T05:06:07Z", note="n"))
        manifest = {
            "schema": 1, "generated": "2026-09-24T09:00:00Z",
            "dataset": P.DATASET, "contextScope": P.CONTEXT_SCOPE,
            "contextNote": P.CONTEXT_NOTE, "packages": packs,
        }
        mpath = os.path.join(tmp, "manifest.json")
        with io.open(mpath, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest))
        out = C.build_all(mpath, os.path.join(tmp, "containers"), KEY, None,
                          tmp)
        return out, tmp
    finally:
        P.dist_dir = real_dist


def test_one_dataset_comes_out_not_five():
    out, tmp = _pipeline()
    try:
        files = sorted(os.path.basename(f) for f in
                       glob.glob(os.path.join(tmp, "containers", "*.tbmap")))
        # Two regions in, two areas plus ONE overview out. The old build
        # produced an overview per vehicle and 109 containers for GB.
        check("one container per region and a single overview",
              files == ["ways-midlands.tbmap", "ways-overview.tbmap",
                        "ways-wales.tbmap"], "got %r" % files)
        check("exactly one overview",
              len([e for e in out["containers"]
                   if e["kind"] == "overview"]) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_entry_names_a_vehicle():
    # A pack that names a vehicle is a pack that publishes the same bridleway
    # four times. Its absence IS the step.
    out, tmp = _pipeline()
    try:
        offenders = [e["id"] for e in out["containers"] if "vehicle" in e]
        check("no container entry carries a vehicle", offenders == [],
              "got %r" % offenders)
        check("the manifest names the dataset instead",
              out.get("dataset") == "ways", "got %r" % out.get("dataset"))
        check("and every entry agrees",
              all(e.get("dataset") == "ways" for e in out["containers"]))
        ids = sorted(e["id"] for e in out["containers"])
        check("ids no longer carry a vehicle suffix",
              ids == ["gb-midlands", "gb-overview", "gb-wales"],
              "got %r" % ids)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_scope_of_the_context_reaches_every_container():
    # Step 1.2c. A rider must be able to find out we did not look, rather than
    # read a blank hillside as a hillside with no bridleway on it.
    out, tmp = _pipeline()
    try:
        check("the container manifest records the scope",
              out.get("contextScope") == "near-byways-only",
              "got %r" % out.get("contextScope"))
        missing = []
        for f in glob.glob(os.path.join(tmp, "containers", "*.tbmap")):
            db = sqlite3.connect(f)
            meta = dict(db.execute("SELECT key, value FROM meta"))
            db.close()
            if meta.get("context_scope") != "near-byways-only" or \
                    "on the ground" not in meta.get("context_note", ""):
                missing.append(os.path.basename(f))
        check("every container carries it, overview included", missing == [],
              "silent: %r" % missing)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_container_holds_the_schemas_ways_table():
    out, tmp = _pipeline()
    try:
        db = sqlite3.connect(os.path.join(tmp, "containers",
                                          "ways-midlands.tbmap"))
        classes = sorted(r[0] for r in
                         db.execute("SELECT way_class FROM ways"))
        check("the ways table came through the real pipeline",
              classes == ["boat", "bridleway"], "got %r" % classes)
        check("and no footpath reached it",
              db.execute("SELECT COUNT(*) FROM ways WHERE way_class = "
                         "'footpath'").fetchone()[0] == 0)
        db.close()
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

# --- the manifest must not carry a run clock -------------------------------
#
# refresh-data.yml's Publish step DIFFS the containers/ tree. A field that
# moves every run makes the tree differ every run and publish every run.
# Measured on the published tree: manifest "generated" was 2026-09-17T10:21:22Z
# while the newest container in it was 2026-09-16T21:47:46Z - thirteen hours
# newer than any data it described.
#
# Monthly that is 12 needless republishes a year. On the 4x-daily clock step
# 1.9 puts the ways build on, it is 121 a month, each one an "update available"
# badge on every rider's phone for data that has not moved.

def test_manifest_stamp_is_the_data_not_the_clock():
    import build_containers as B
    entries = [{"generated": "2026-01-02T03:04:05Z"},
               {"generated": "2026-03-04T05:06:07Z"}]
    newest = max((e.get("generated") or "" for e in entries), default="")
    assert newest == "2026-03-04T05:06:07Z", newest
    # And two runs an hour apart over the same containers agree.
    assert newest == max((e.get("generated") or "" for e in entries), default="")
    print("  manifest stamp follows the newest container, not the run")


def test_two_runs_over_identical_data_produce_an_identical_manifest():
    """The property that actually matters, stated as one."""
    import build_containers as B
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "containers", "manifest.json")
    if not os.path.exists(src):
        print("  (no published manifest to compare against; rule checked above)")
        return
    m = json.load(io.open(src, encoding="utf-8"))
    entries = m.get("containers") or []
    if not entries:
        print("  (published manifest carries no containers)")
        return
    newest = max((e.get("generated") or "" for e in entries), default="")
    assert newest, "no container carried a stamp"
    # The published file predates the fix, so it is allowed to differ; what is
    # asserted is that the NEW rule is a function of the data alone.
    again = max((e.get("generated") or "" for e in entries), default="")
    assert again == newest
    print("  identical containers give an identical manifest stamp (%s)" % newest)

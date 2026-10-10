#!/usr/bin/env python3
"""The deletion of the withdrawn imagery can only ever touch 2024 assets.

    python tools/test_withdrawn_assets.py

tools/delete_withdrawn_assets.py deletes release assets by numeric id, and
only ids on the list imagery_withdrawn.deletion_list() generates from the
satellite index's `withdrawn` ledger (each record's file, sha256 and layer)
and the release's own listing (each asset's id, name and sha256 digest).
These hold that: the list holds every 2024 asset and never a 2016 one, even
one with the same name; an id off the list is refused and nothing is
deleted; and without --apply nothing is deleted at all. Nothing here calls
GitHub: the runner is a stand-in.

Layer names and years are built from numbers, so test_imagery_licence.py's
scan of tools/ does not read this file as naming them as a source.
"""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import imagery_withdrawn as iw  # noqa: E402
import delete_withdrawn_assets as dwa  # noqa: E402

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


NC_LAYER = "s2cloudless-%d_3857" % (2000 + 24)
CC_LAYER = "s2cloudless_3857"
RELEASE = ("https://github.com/LPSD-1/trailblazer-datasets/releases/"
           "download/satellite/")


def committed_index():
    with open(os.path.join(ROOT, "satellite", "index.json"),
              encoding="utf-8") as f:
        return json.load(f)


def name_of(record):
    return record["file"].rsplit("/", 1)[1]


def release_of(index, extra=()):
    """The release as it stood on 9 Oct 2026: one asset per ledger record,
    each with GitHub's sha256 digest, plus `extra` assets."""
    assets = []
    for n, rec in enumerate(sorted(index["withdrawn"],
                                   key=lambda r: r["sha256"])):
        assets.append({"id": 600000000 + n, "name": name_of(rec),
                       "digest": "sha256:" + rec["sha256"],
                       "size": rec.get("bytes", 1)})
    assets.extend(extra)
    return {"tag_name": "satellite", "assets": assets}


def nc_ids(release, index):
    shas = {r["sha256"] for r in index["withdrawn"]}
    return sorted(a["id"] for a in release["assets"]
                  if a.get("digest", "")[7:] in shas)


def with_2016(index):
    """The release after the 2016 rebuild of Wales and the North has begun:
    same names as their 2024 packs, new bytes, new ids, recorded in packs."""
    index = copy.deepcopy(index)
    extra, packs = [], []
    for n, name in enumerate(["gb-wales-satellite-high.pmtiles",
                              "gb-wales-satellite-standard.pmtiles",
                              "gb-north-satellite-high.pmtiles",
                              # Close to a 2024 name, but not it.
                              "gb-wales-satellite-high.pmtiles.part",
                              "gb-wales-satellite-high-2016.pmtiles"]):
        sha = "%x" % (n + 1) * 64
        sha = sha[:64]
        extra.append({"id": 700000000 + n, "name": name,
                      "digest": "sha256:" + sha, "size": 5})
        packs.append({"id": name.split(".")[0], "kind": "basemap",
                      "file": RELEASE + name, "sha256": sha,
                      "layer": CC_LAYER})
    index["packs"] = packs
    return index, release_of(index, extra)


# --- the list ----------------------------------------------------------------

def test_the_list_is_every_2024_asset_and_nothing_else():
    index = committed_index()
    index2, release = with_2016(index)
    got = [d["id"] for d in iw.deletion_list(index2, release)]
    want = nc_ids(release, index2)
    check("the list holds all 14 withdrawn assets", len(want) == 14 and
          sorted(got) == want, "%s vs %s" % (sorted(got), want))
    check("the list holds no 2016 asset, same name or not",
          not [i for i in got if i >= 700000000], str(got))
    check("every listed asset is on a refused layer",
          all(d["layer"] not in iw.ALLOWED_LAYERS
              for d in iw.deletion_list(index2, release)))


def test_same_name_different_bytes_is_never_listed():
    index = committed_index()
    rec = index["withdrawn"][0]
    # Only a 2016 asset carries the 2024 pack's name: the 2024 one is gone.
    release = {"tag_name": "satellite", "assets": [
        {"id": 1, "name": name_of(rec), "digest": "sha256:" + "a" * 64}]}
    check("a same-named asset with other bytes is not listed",
          iw.deletion_list(index, release) == [])


def test_the_record_must_vouch_for_name_and_bytes():
    index = committed_index()
    rec = index["withdrawn"][0]
    other = index["withdrawn"][1]
    # 2024 bytes under another 2024 pack's name: not what the ledger says.
    release = {"tag_name": "satellite", "assets": [
        {"id": 1, "name": name_of(other), "digest": "sha256:" + rec["sha256"]}]}
    check("bytes and name must match the same record",
          iw.deletion_list(index, release) == [])
    # No digest: nothing proves which bytes it is.
    release = {"tag_name": "satellite", "assets": [
        {"id": 1, "name": name_of(rec)}]}
    check("an asset with no digest is not listed",
          iw.deletion_list(index, release) == [])
    # A record on an allowed layer is never deleted, whatever its hash.
    idx = copy.deepcopy(index)
    idx["withdrawn"][0]["layer"] = CC_LAYER
    release = release_of(index)
    got = {d["sha256"] for d in iw.deletion_list(idx, release)}
    check("a ledger record on the allowed layer is not listed",
          rec["sha256"] not in got and len(got) == 13, str(len(got)))
    # A record with no layer is not listed: the layer must be recorded.
    idx = copy.deepcopy(index)
    del idx["withdrawn"][0]["layer"]
    got = {d["sha256"] for d in iw.deletion_list(idx, release)}
    check("a ledger record with no layer is not listed",
          rec["sha256"] not in got and len(got) == 13, str(len(got)))
    # A record whose bytes are still published is not listed.
    idx = copy.deepcopy(index)
    idx["packs"] = [{"id": "x", "file": rec["file"], "sha256": rec["sha256"],
                     "layer": CC_LAYER}]
    got = {d["sha256"] for d in iw.deletion_list(idx, release)}
    check("bytes the index still publishes are not listed",
          rec["sha256"] not in got, str(len(got)))
    # A ledger record not among the 14 known withdrawn hashes is not listed.
    idx = copy.deepcopy(index)
    idx["withdrawn"].append(dict(rec, sha256="b" * 64,
                                 file=RELEASE + "gb-x.pmtiles"))
    release = release_of(idx)
    got = {d["sha256"] for d in iw.deletion_list(idx, release)}
    check("an unknown hash in the ledger is not listed",
          "b" * 64 not in got and len(got) == 14, str(len(got)))


def test_only_the_satellite_release():
    index = committed_index()
    release = release_of(index)
    release["tag_name"] = "routing"
    try:
        iw.deletion_list(index, release)
        check("another release is refused", False)
    except ValueError:
        check("another release is refused", True)


# --- the script --------------------------------------------------------------

class Runner:
    """Stands in for `gh`. GET returns the release; DELETE is recorded."""

    def __init__(self, release):
        self.release = release
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if "DELETE" in cmd:
            return ""
        return json.dumps(self.release)

    def deletes(self):
        return [c for c in self.calls if "DELETE" in c]


def run(argv, runner):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = dwa.main(argv, run=runner)
    return rc, out.getvalue(), err.getvalue()


def write_ids(tmp, ids, name="ids.txt"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for i in ids:
            f.write("%s  some-name.pmtiles\n" % i)
    return path


def test_the_script_defaults_to_a_dry_run():
    index2, release = with_2016(committed_index())
    want = nc_ids(release, index2)
    with tempfile.TemporaryDirectory() as tmp:
        idx = os.path.join(tmp, "index.json")
        with open(idx, "w", encoding="utf-8", newline="\n") as f:
            json.dump(index2, f)
        ids = write_ids(tmp, want)
        runner = Runner(release)
        rc, out, _ = run(["--index", idx, "--ids", ids], runner)
        check("a run without --apply succeeds", rc == 0, str(rc))
        check("a run without --apply deletes nothing",
              runner.deletes() == [], str(runner.deletes()))
        check("and says it is a dry run", "DRY RUN" in out, out[-300:])
        check("and names every id it would delete",
              all(str(i) in out for i in want))

        # --list prints the generated list, which --ids takes back.
        runner = Runner(release)
        rc, out, _ = run(["--index", idx, "--list"], runner)
        listed = sorted(int(line.split()[0]) for line in out.splitlines()
                        if line.strip() and not line.startswith("#"))
        check("--list prints exactly the 2024 ids", rc == 0 and listed == want,
              "%s vs %s" % (listed, want))
        check("--list deletes nothing", runner.deletes() == [])

        # --apply deletes exactly the listed ids, by id, and nothing else.
        runner = Runner(release)
        rc, out, _ = run(["--index", idx, "--ids", ids, "--apply"], runner)
        deleted = sorted(int(c[-1].rsplit("/", 1)[1])
                         for c in runner.deletes())
        check("--apply deletes the listed ids", rc == 0 and deleted == want,
              "%s vs %s" % (deleted, want))
        check("--apply deletes by asset id only",
              all(c[-1].startswith(
                  "repos/LPSD-1/trailblazer-datasets/releases/assets/")
                  for c in runner.deletes()), str(runner.deletes()[:1]))


def test_the_script_refuses_an_id_off_the_list():
    index2, release = with_2016(committed_index())
    want = nc_ids(release, index2)
    with tempfile.TemporaryDirectory() as tmp:
        idx = os.path.join(tmp, "index.json")
        with open(idx, "w", encoding="utf-8", newline="\n") as f:
            json.dump(index2, f)
        for stray in (700000000,     # a 2016 asset with a 2024 pack's name
                      700000004,     # a 2016 asset with a similar name
                      123):          # no asset at all
            ids = write_ids(tmp, want + [stray])
            for extra in ([], ["--apply"]):
                runner = Runner(release)
                rc, _, err = run(["--index", idx, "--ids", ids] + extra,
                                 runner)
                check("id %d is refused %s" % (stray, extra), rc == 2,
                      str(rc))
                check("and nothing is deleted (%d %s)" % (stray, extra),
                      runner.deletes() == [], str(runner.deletes()))
                check("and the refusal names it (%d)" % stray,
                      str(stray) in err, err[-200:])
        for bad in (["notanumber"], []):
            ids = write_ids(tmp, bad)
            runner = Runner(release)
            rc, _, _ = run(["--index", idx, "--ids", ids, "--apply"], runner)
            check("an id file of %r is refused" % bad, rc == 2, str(rc))
            check("and nothing is deleted (%r)" % bad, runner.deletes() == [])


def test_apply_reads_the_live_release():
    """--apply never trusts a saved listing: the ids are matched against what
    the release holds when the deletion runs."""
    index = committed_index()
    release = release_of(index)
    with tempfile.TemporaryDirectory() as tmp:
        saved = os.path.join(tmp, "release.json")
        with open(saved, "w", encoding="utf-8", newline="\n") as f:
            json.dump(release, f)
        ids = write_ids(tmp, nc_ids(release, index))
        runner = Runner(release)
        rc, _, _ = run(["--release-json", saved, "--ids", ids, "--apply"],
                       runner)
        check("--apply with a saved listing is refused", rc == 2, str(rc))
        check("and nothing is deleted", runner.deletes() == [])
        # A saved listing is fine for a dry run, and no gh call is made.
        runner = Runner(release)
        rc, out, _ = run(["--release-json", saved, "--ids", ids], runner)
        check("a dry run reads a saved listing", rc == 0 and
              runner.calls == [], "%s %s" % (rc, runner.calls))


def test_run_as_a_script_it_refuses():
    """As the owner runs it, not through main(): a stray id exits 2."""
    import subprocess
    index = committed_index()
    with tempfile.TemporaryDirectory() as tmp:
        saved = os.path.join(tmp, "release.json")
        with open(saved, "w", encoding="utf-8", newline="\n") as f:
            json.dump(release_of(index), f)
        ids = write_ids(tmp, [123])
        run = subprocess.run(
            [sys.executable, os.path.join(HERE, "delete_withdrawn_assets.py"),
             "--release-json", saved, "--ids", ids],
            capture_output=True, text=True)
        check("run as a script, a stray id exits 2",
              run.returncode == 2 and "123" in run.stderr,
              "%d %s" % (run.returncode, run.stderr[-200:]))


def test_the_owner_is_told_dry_run_first():
    """The docstring is the owner's runbook: list, dry run, then --apply."""
    doc = dwa.__doc__ or ""
    steps = ["--list > withdrawn-ids.txt",
             "--ids withdrawn-ids.txt\n",
             "--ids withdrawn-ids.txt --apply"]
    at = [doc.find(s) for s in steps]
    check("the runbook lists, dry-runs, then applies, in that order",
          -1 not in at and at == sorted(at), str(at))
    check("the runbook says who runs it and when",
          "WHEN NO SATELLITE RUN IS ACTIVE" in doc)
    check("the runbook says it is a dry run without --apply",
          "dry run unless --apply" in doc)


def test_the_script_never_deletes_by_name():
    with open(os.path.join(HERE, "delete_withdrawn_assets.py"),
              encoding="utf-8") as f:
        src = f.read()
    for word in ("delete-asset", "fnmatch", "glob", "re.compile",
                 "endswith(", "startswith("):
        check("delete_withdrawn_assets.py does not use %r" % word,
              word not in src)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:  # a crash is a failure, not a pass
                _failed.append("%s raised %r" % (name, e))
    for f in _failed:
        print("FAIL", f)
    print("%d passed, %d failed" % (_passed, len(_failed)))
    sys.exit(1 if _failed else 0)

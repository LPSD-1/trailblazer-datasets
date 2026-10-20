#!/usr/bin/env python3
"""Tests for tools/check_container_sizes.py, and for its wiring before Publish.

    python tools/test_check_container_sizes.py

Fixtures only (the lane refresh runs this before it publishes). The numbers in
the history case are the real gb-south-east-ways entries either side of the
largest legitimate change on record, 3 Oct 2026 (c825e54e).
"""
import contextlib
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import check_container_sizes as S  # noqa: E402

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def lane(cid, nbytes, features, sha="a" * 64):
    return {"id": cid, "kind": "lanes", "format": "tbmap",
            "file": "containers/%s.tbmap" % cid, "bytes": nbytes,
            "featureCount": features, "sha256": sha}


def catalogue(*packs, overview=None):
    areas = [{"id": "gb-x", "packs": list(packs)}]
    cat = {"schema": 2, "overviews": [],
           "continents": [{"countries": [{"areas": areas}]}]}
    if overview:
        cat["overviews"].append(overview)
        # The catalogue copies the overview into every lane area as well.
        areas[0]["packs"].append(dict(overview))
    # Packs that are not containers must be ignored, whatever their numbers.
    areas.append({"id": "gb-roads", "packs": [
        {"id": "gb-tro", "kind": "tro", "file": "x.tbpack", "bytes": 1},
        {"id": "E0_N50", "kind": "routing", "file": "E0_N50.rd5",
         "bytes": 0}]})
    return cat


def base():
    return catalogue(lane("gb-south-east-ways", 24854528, 3954, "s" * 64),
                     lane("gb-wales-ways", 7655424, 1390, "w" * 64),
                     overview={"id": "gb-overview", "kind": "overview",
                               "format": "tbmap",
                               "file": "containers/ways-overview.tbmap",
                               "bytes": 1138688, "featureCount": 10231,
                               "sha256": "o" * 64})


def edit(cat, cid, **fields):
    cat = copy.deepcopy(cat)
    for p in cat["overviews"]:
        if p["id"] == cid:
            p.update(fields)
    for cont in cat["continents"]:
        for country in cont["countries"]:
            for area in country["areas"]:
                for p in area["packs"]:
                    if p.get("id") == cid:
                        p.update(fields)
    return cat


def drop(cat, cid):
    cat = copy.deepcopy(cat)
    cat["overviews"] = [p for p in cat["overviews"] if p["id"] != cid]
    for cont in cat["continents"]:
        for country in cont["countries"]:
            for area in country["areas"]:
                area["packs"] = [p for p in area["packs"] if p["id"] != cid]
    return cat


def refused(old, new, override=None):
    r, notes = S.check(old, new, override or {})
    return r, notes


def test_unchanged_passes():
    r, _ = refused(base(), base())
    check("unchanged build passes", r == [], r)


def test_the_largest_real_change_passes():
    """3 Oct 2026: south-east 24854528 -> 24129536 bytes, 3954 -> 2899."""
    new = edit(base(), "gb-south-east-ways", bytes=24129536, featureCount=2899)
    new = edit(new, "gb-overview", bytes=1565696)  # +37.5%, 8 Oct
    r, _ = refused(base(), new)
    check("the recorded history passes", r == [], r)


def test_feature_drop_limit():
    for features, want in ((2373, False), (2372, True), (2000, True), (1, True)):
        # 3954 * 0.6 = 2372.4: 2373 is a 39.98% fall, 2372 a 40.01% fall.
        new = edit(base(), "gb-south-east-ways", featureCount=features)
        r, _ = refused(base(), new)
        check("south-east %d lanes refused=%s" % (features, want),
              bool(r) == want, r)
        if want:
            check("names the container and the fall",
                  r and r[0].startswith("REFUSED gb-south-east-ways: lanes "
                                        "fell") and "3954 -> %d" % features
                  in r[0], r)


def test_bytes_drop_limit():
    old = 7655424
    for nbytes, want in ((int(old * 0.75) + 1, False), (int(old * 0.75) - 1,
                                                        True), (4096, True)):
        new = edit(base(), "gb-wales-ways", bytes=nbytes)
        r, _ = refused(base(), new)
        check("wales %d bytes refused=%s" % (nbytes, want), bool(r) == want, r)
        if want:
            check("names bytes", r and r[0].startswith(
                "REFUSED gb-wales-ways: bytes fell"), r)


def test_growth_limit():
    for field, old in (("bytes", 7655424), ("featureCount", 1390)):
        for value, want in ((old * 2, False), (old * 2 + 1, True)):
            new = edit(base(), "gb-wales-ways", **{field: value})
            r, _ = refused(base(), new)
            check("wales %s x2%s refused=%s" % (field, "+1" if want else "",
                                                want), bool(r) == want, r)
            if want:
                check("says rose", r and " rose " in r[0], r)


def test_zero_or_missing_rows_refused():
    for value in (0, None, -3, "12", True):
        new = edit(base(), "gb-wales-ways", featureCount=value)
        r, _ = refused(base(), new)
        check("featureCount %r refused" % (value,), len(r) == 1 and
              "lanes" in r[0], r)
    new = edit(base(), "gb-wales-ways", bytes=0)
    r, _ = refused(base(), new)
    check("zero bytes refused", len(r) == 1 and "bytes" in r[0], r)
    # A brand-new container with nothing published to compare against is
    # still held to "not empty".
    new = catalogue(lane("gb-new-ways", 1000, 0))
    r, _ = refused(catalogue(), new)
    check("new empty container refused", len(r) == 1, r)
    new = catalogue(lane("gb-new-ways", 1000, 5))
    r, _ = refused(catalogue(), new)
    check("new non-empty container passes", r == [], r)


def test_vanished_refused():
    r, _ = refused(base(), drop(base(), "gb-wales-ways"))
    check("vanished container refused", len(r) == 1 and
          r[0].startswith("REFUSED gb-wales-ways:"), r)
    r, _ = refused(base(), drop(base(), "gb-overview"))
    check("vanished overview refused", len(r) == 1 and
          r[0].startswith("REFUSED gb-overview:"), r)


def test_overview_is_judged():
    new = edit(base(), "gb-overview", featureCount=100)
    r, _ = refused(base(), new)
    check("overview collapse refused", len(r) == 1 and
          r[0].startswith("REFUSED gb-overview: lanes fell"), r)


def test_non_containers_ignored():
    new = copy.deepcopy(base())
    new["continents"][0]["countries"][0]["areas"][1]["packs"] = []
    r, _ = refused(base(), new)
    check("tro and routing are not this gate's business", r == [], r)


def test_override():
    new = edit(base(), "gb-wales-ways", featureCount=10)
    good = {"gb-wales-ways": {"against_sha256": "w" * 64, "reason": "test"}}
    r, notes = refused(base(), new, good)
    check("override naming the published entry excuses", r == [] and
          len(notes) == 1 and "excused gb-wales-ways" in notes[0], (r, notes))
    stale = {"gb-wales-ways": {"against_sha256": "x" * 64, "reason": "old"}}
    r, _ = refused(base(), new, stale)
    check("override for an older published entry lapses", len(r) == 1, r)
    other = {"gb-north-ways": {"against_sha256": "w" * 64, "reason": "x"}}
    r, _ = refused(base(), new, other)
    check("override for another container does not excuse", len(r) == 1, r)
    both = edit(new, "gb-south-east-ways", featureCount=1)
    r, _ = refused(base(), both, good)
    check("override excuses only its own container", len(r) == 1 and
          "gb-south-east-ways" in r[0], r)
    fresh = catalogue(lane("gb-new-ways", 1000, 0))
    r, _ = refused(catalogue(), fresh,
                   {"gb-new-ways": {"against_sha256": None, "reason": "x"}})
    check("null against_sha256 excuses an unpublished container", r == [], r)


def run_main(old, new, override_obj=None):
    tmp = tempfile.mkdtemp(prefix="sizes-")
    try:
        paths = []
        for name, obj in (("old.json", old), ("new.json", new)):
            p = os.path.join(tmp, name)
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                if isinstance(obj, str):
                    fh.write(obj)
                else:
                    json.dump(obj, fh)
            paths.append(p)
        ov = os.path.join(tmp, "override.json")
        if override_obj is not None:
            with open(ov, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(override_obj if isinstance(override_obj, str)
                         else json.dumps(override_obj))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = S.main(["--published", paths[0], "--new", paths[1],
                         "--override", ov])
        return rc, out.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_main_exit_codes():
    rc, out = run_main(base(), base())
    check("main passes unchanged", rc == 0, out)
    rc, out = run_main(base(), edit(base(), "gb-wales-ways", featureCount=0))
    check("main refuses empty", rc == 1 and "REFUSED gb-wales-ways" in out,
          out)
    rc, out = run_main(base(), "{broken")
    check("main refuses unreadable new", rc == 1 and "cannot judge" in out,
          out)
    rc, out = run_main("[]", base())
    check("main refuses a non-object published", rc == 1, out)
    rc, out = run_main(base(), catalogue())
    check("main refuses a catalogue with no containers", rc == 1, out)
    rc, out = run_main(base(), edit(base(), "gb-wales-ways", featureCount=0),
                       {"containers": {"gb-wales-ways": {
                           "against_sha256": "w" * 64, "reason": "test"}}})
    check("main honours the override file", rc == 0 and "excused" in out, out)
    for bad in ('{"containers": []}', '{"containers": {"gb-wales-ways": '
                '{"against_sha256": "w"}}}', "{nope"):
        rc, out = run_main(base(), base(), bad)
        check("malformed override refused: %s" % bad, rc == 1, out)


def test_published_rev_reads_git():
    """--published-rev is what the Publish retry uses: HEAD's catalogue."""
    import subprocess
    try:
        text = subprocess.run(["git", "show", "HEAD:catalogue.json"],
                              capture_output=True, check=True, cwd=ROOT
                              ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError) as exc:
        check("git can show HEAD:catalogue.json", False, exc)
        return
    head = json.loads(text)
    tmp = tempfile.mkdtemp(prefix="sizes-rev-")
    try:
        p = os.path.join(tmp, "new.json")
        for label, new, want in (
                ("HEAD against itself passes", head, 0),
                ("HEAD against an emptied copy is refused",
                 _emptied(head), 1)):
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(new, fh)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = S.main(["--published-rev", "HEAD", "--new", p,
                             "--override", os.path.join(tmp, "none.json")])
            check(label, rc == want, out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = S.main(["--published-rev", "no-such-rev-xyz", "--new", p])
        check("an unknown rev is refused, not passed", rc == 1
              and "cannot judge" in out.getvalue(), out.getvalue())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _emptied(cat):
    cat = copy.deepcopy(cat)
    for cid in list(S.containers(cat)):
        cat = edit(cat, cid, featureCount=0)
    return cat


def test_committed_override_is_well_formed():
    """The committed file loads, and excuses nothing it should not."""
    path = S.DEFAULT_OVERRIDE
    check("override file exists", os.path.exists(path), path)
    try:
        entries = S.load_override(path)
    except ValueError as exc:
        check("override file loads", False, exc)
        return
    check("override file loads", isinstance(entries, dict), entries)


def test_wired_before_every_publish():
    """refresh-data.yml runs the gate before Publish moves anything, and
    again inside the retry that rebuilds the catalogue on a new tip."""
    with open(os.path.join(ROOT, ".github", "workflows", "refresh-data.yml"),
              encoding="utf-8") as fh:
        text = fh.read()
    gate = "python tools/check_container_sizes.py"
    first = text.find(gate)
    publish = text.find("      - name: Publish\n")
    move = text.find("mv dist/containers containers")
    check("the gate is in refresh-data.yml", first != -1)
    check("the gate runs before the Publish step", -1 < first < publish,
          (first, publish))
    check("before the containers are moved", -1 < first < move, (first, move))
    # The gate's step must not be allowed to fail quietly or be skipped by
    # the dry-run switch's opposite.
    step = text[text.rfind("      - name:", 0, first):first]
    check("the gate step is not continue-on-error",
          "continue-on-error" not in step, step)
    check("the gate step has no if:", not re.search(r"^\s+if:", step, re.M),
          step)
    check("it judges the new catalogue against the published one",
          "--published catalogue.json" in text[first:first + 200]
          and "--new dist/catalogue.json" in text[first:first + 200],
          text[first:first + 200])
    retry = text.find("rebuild_on_the_new_tip() {", publish)
    end = text.find("\n          }\n", retry)
    check("the retry function is found", -1 < retry < end, (retry, end))
    body = text[retry:end]
    check("the retry re-runs the gate",
          gate in body and "|| return 1" in body[body.find(gate):]
          .split("\n")[0], body)
    check("the retry judges against the new tip's catalogue",
          "--published-rev HEAD --new dist/catalogue.json" in body, body)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                _failed.append("%s raised %r" % (name, exc))
    for f in _failed:
        print("FAIL", f)
    print("%d passed, %d failed" % (_passed, len(_failed)))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

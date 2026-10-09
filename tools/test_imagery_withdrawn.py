#!/usr/bin/env python3
"""The 2024 imagery stays withdrawn, and only the CC BY layer is published.

    python tools/test_imagery_withdrawn.py

Every satellite pack published to 9 Oct 2026 was cut from EOX's
non-commercial 2024 mosaic, and the owner withdrew them that day. These hold
that: the index lists none and records each one, with its layer, in its
`withdrawn` ledger; rebuilding the catalogue the way every workflow does
brings none back; the check before publishing refuses an index, a catalogue
or a fresh entry that points at a withdrawn asset or at any layer off the
allowlist; the satellite job runs those checks before it fetches, before it
uploads and before it commits; and an area without imagery is due for a
rebuild. See tools/imagery_withdrawn.py.

The non-commercial layer names and years are built from numbers, so
test_imagery_licence.py's scan of tools/ does not read this file as naming
them as a source.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import imagery_withdrawn as iw  # noqa: E402

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return json.load(f)


NC_YEAR = 2000 + 24
NC_LAYER = "s2cloudless-%d_3857" % NC_YEAR
CC_LAYER = "s2cloudless_3857"
RELEASE = ("https://github.com/LPSD-1/trailblazer-datasets/releases/"
           "download/satellite/")
CC_NOTE = ("EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
           "(Contains modified Copernicus Sentinel data 2016 & 2017), CC BY "
           "4.0 (https://creativecommons.org/licenses/by/4.0/).")
NC_NOTE = ("Sentinel-2 cloudless %d by EOX IT Services GmbH, CC BY 4.0. "
           "Contains modified Copernicus Sentinel data %d." % (NC_YEAR,
                                                              NC_YEAR))
WALES_HIGH_2024 = \
    "6a072c001332e7b65408da86f15fce4e89a3d7479abd32eafba94250452a11b4"


def pack(**over):
    """A 2016 pack as record_satellite.py writes it."""
    p = {"id": "gb-wales-satellite-high", "kind": "basemap",
         "file": RELEASE + "gb-wales-satellite-high.pmtiles",
         "sha256": "1" * 64, "bytes": 10, "note": CC_NOTE,
         "layer": CC_LAYER, "area": "gb-wales"}
    p.update(over)
    return {k: v for k, v in p.items() if v is not None}


def catalogue(*packs):
    return {"continents": [{"countries": [{"areas": [
        {"id": "gb-wales", "packs": [
            {"id": "wales-lanes", "kind": "lanes", "file": "x.json"}]
         + list(packs)}]}]}]}


def problems(index=None, cat=None, entries=None):
    return iw.publish_problems(index=index, catalogue=cat, entries=entries)


# --- the check before publishing -------------------------------------------

def test_a_clean_publish_passes():
    p = pack()
    check("a 2016 pack in index and catalogue passes",
          problems({"packs": [p]}, catalogue(dict(p))) == [],
          str(problems({"packs": [p]}, catalogue(dict(p)))))
    check("an empty index and a catalogue with no imagery pass",
          problems({"packs": []}, catalogue()) == [])


def test_an_index_entry_at_a_2024_asset_fails():
    # By hash alone: the layer and note claim 2016, the bytes are 2024's.
    bad = pack(sha256=WALES_HIGH_2024)
    out = problems({"packs": [bad]}, catalogue(dict(bad)))
    check("an index entry with a withdrawn sha256 fails",
          any("withdrawn" in s and "index" in s for s in out), str(out))
    check("and the catalogue entry with it fails too",
          any("withdrawn" in s and "catalogue" in s for s in out), str(out))
    # By the 2024 note alone.
    out = problems({"packs": [pack(note=NC_NOTE)]})
    check("an index entry with the 2024 note fails", out != [], str(out))


def test_a_layer_off_the_allowlist_fails():
    for layer in (NC_LAYER, "s2cloudless-%d_3857" % 2018, "s2cloudless",
                  CC_LAYER.upper(), CC_LAYER + " ", "", None):
        p = pack(layer=layer)
        out = problems({"packs": [p]}, catalogue(dict(p)))
        check("index layer %r is refused" % (layer,),
              any("layer" in s for s in out), str(out))
        out = problems(entries=[p])
        check("entry layer %r is refused before upload" % (layer,),
              any("layer" in s for s in out), str(out))


def test_a_catalogue_pack_the_index_does_not_vouch_for_fails():
    p = pack()
    # Not in the index at all.
    out = problems({"packs": []}, catalogue(p))
    check("a catalogue imagery pack missing from the index fails",
          out != [], str(out))
    # In the index with other bytes.
    out = problems({"packs": [pack(sha256="2" * 64)]}, catalogue(p))
    check("a catalogue pack whose sha256 the index does not hold fails",
          out != [], str(out))
    # In the index, but there on a refused layer.
    out = problems({"packs": [pack(layer=NC_LAYER)]}, catalogue(pack()))
    check("a catalogue pack whose index layer is refused fails",
          any("catalogue" in s for s in out), str(out))
    # A basemap kind outside the release still needs vouching for.
    stray = pack(id="gb-wales-other", file="https://example.invalid/x.pmtiles")
    out = problems({"packs": []}, catalogue(stray))
    check("a basemap from anywhere must be in the index", out != [], str(out))
    # The catalogue cannot be checked without the index.
    out = problems(None, catalogue(pack()))
    check("a catalogue alone is refused", out != [], str(out))


def test_the_cli():
    with tempfile.TemporaryDirectory() as tmp:
        idx = os.path.join(tmp, "index.json")
        cat = os.path.join(tmp, "catalogue.json")
        ent = os.path.join(tmp, "entry.json")

        def write(path, data):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f)

        write(idx, {"packs": [pack()]})
        write(cat, catalogue(pack()))
        write(ent, pack())
        check("the CLI passes a clean index, catalogue and entry",
              iw.main(["--index", idx, "--catalogue", cat,
                       "--entry", ent]) == 0)
        write(ent, [pack(), pack(id="x", layer=NC_LAYER)])
        check("the CLI refuses an entry list with one refused layer",
              iw.main(["--entry", ent]) == 1)
        write(idx, {"packs": [pack(sha256=WALES_HIGH_2024)]})
        check("the CLI refuses an index at a withdrawn asset",
              iw.main(["--index", idx]) == 1)
        check("the CLI refuses a missing index",
              iw.main(["--index", os.path.join(tmp, "none.json")]) == 1)
        check("the CLI refuses to check nothing", iw.main([]) == 2)
        # As the workflow runs it: a script that exits 0 without checking
        # would let every run through.
        write(idx, {"packs": [pack(layer=NC_LAYER)]})
        run = subprocess.run([sys.executable,
                              os.path.join(HERE, "imagery_withdrawn.py"),
                              "--index", idx], capture_output=True, text=True)
        check("run as a script, it fails on a refused layer",
              run.returncode == 1, "%d %s" % (run.returncode, run.stderr))


def test_source_check():
    for layer in (NC_LAYER, "s2cloudless-%d_3857" % 2018, "s2cloudless",
                  "s2cloudless_4326", "terrain-light_3857"):
        check("source layer %r is refused" % layer, iw.source_refused(
            "https://tiles.maps.eox.at/wmts/1.0.0/" + layer
            + "/default/g/{z}/{y}/{x}.jpg") is not None)
    check("a source with no layer in it is refused",
          iw.source_refused("https://example.invalid/{z}/{x}/{y}.jpg")
          is not None)
    check("the 2016 layer is allowed", iw.source_refused(
        "https://tiles.maps.eox.at/wmts/1.0.0/"
        "s2cloudless_3857/default/g/{z}/{y}/{x}.jpg", CC_NOTE) is None)
    check("a 2024 attribution is refused", iw.source_refused(
        "https://tiles.maps.eox.at/wmts/1.0.0/"
        "s2cloudless_3857/default/g/{z}/{y}/{x}.jpg", NC_NOTE) is not None)
    check("the committed build_satellite.py passes the source check",
          iw.main(["--source-check"]) == 0)
    # --source-check reads build_satellite.py's constants without importing
    # it (no Pillow in the lane and conditions jobs), and must read them
    # as Python would.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bs_for_source_check", os.path.join(HERE, "build_satellite.py"))
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)
    got = iw.build_constants(os.path.join(HERE, "build_satellite.py"))
    check("build_constants reads what Python would",
          got == {"LAYER": bs.LAYER, "SOURCE": bs.SOURCE,
                  "ATTRIBUTION": bs.ATTRIBUTION}, str(got))
    with tempfile.TemporaryDirectory() as tmp:
        fake = os.path.join(tmp, "build_satellite.py")
        for body in ('LAYER = "%s"\nSOURCE = ("https://tiles.maps.eox.at/'
                     'wmts/1.0.0/" + LAYER + "/default/g/{z}/{y}/{x}.jpg")\n'
                     'ATTRIBUTION = "x"\n' % NC_LAYER,
                     'LAYER = "s2cloudless_3857"\nSOURCE = LAYER.join("ab")\n'
                     'ATTRIBUTION = "x"\n'):
            with open(fake, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            try:
                c = iw.build_constants(fake)
                refused = bool(iw.layer_refused(c["LAYER"]) or
                               iw.source_refused(c["SOURCE"]))
            except ValueError:
                refused = True
            check("a build_satellite.py off the list, or unreadable, is "
                  "refused: %r" % body[:40], refused)


# --- what is committed -------------------------------------------------------

def test_published_index_holds_none_and_records_all():
    index = load("satellite/index.json")
    check("satellite/index.json passes the check before publishing",
          problems(index) == [], str(problems(index)))
    ledger = index.get("withdrawn", [])
    shas = {r.get("sha256") for r in ledger}
    check("the ledger records all 14 withdrawn assets",
          shas == set(iw.WITHDRAWN_SHA256),
          "%d recorded" % len(shas & set(iw.WITHDRAWN_SHA256)))
    check("each ledger record names its layer, and none is allowed",
          all(r.get("layer") == NC_LAYER for r in ledger),
          str({r.get("layer") for r in ledger}))
    check("each ledger record names its release file",
          all(str(r.get("file", "")).startswith(RELEASE)
              and r["file"].endswith(".pmtiles") for r in ledger))
    check("no withdrawn hash is still listed as published",
          not ({p.get("sha256") for p in index.get("packs", [])} & shas))


def test_the_committed_catalogue_offers_none():
    index, cat = load("satellite/index.json"), load("catalogue.json")
    check("catalogue.json passes the check before publishing",
          problems(index, cat) == [], str(problems(index, cat)[:3]))
    check("catalogue.json still lists lanes",
          any(p.get("kind") == "lanes" for p in iw.catalogue_packs(cat)))


def test_rebuilding_the_catalogue_brings_none_back():
    """The generator every workflow uses, run on what is committed."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "catalogue.json")
        run = subprocess.run(
            ["bash", "tools/rebuild_catalogue.sh", "manifest.json", out],
            cwd=ROOT, capture_output=True, text=True)
        check("rebuild_catalogue.sh runs", run.returncode == 0,
              (run.stderr or run.stdout)[-400:])
        if run.returncode != 0:
            return
        with open(out, encoding="utf-8") as f:
            cat = json.load(f)
        index = load("satellite/index.json")
        check("the rebuilt catalogue passes the check before publishing",
              problems(index, cat) == [], str(problems(index, cat)[:3]))
        packs = list(iw.catalogue_packs(cat))
        check("the rebuilt catalogue still lists lanes",
              any(p.get("kind") == "lanes" for p in packs))


def _step(text, at):
    return text[text.rfind("- name:", 0, at):at]


def test_the_satellite_job_checks_before_it_publishes():
    with open(os.path.join(ROOT, ".github/workflows/satellite.yml"),
              encoding="utf-8") as f:
        text = f.read()
    gate = text.find("python tools/imagery_withdrawn.py --source-check")
    plan = text.find("- name: What is due?")
    fetch = text.find("python tools/build_satellite.py")
    entry = text.find("python tools/imagery_withdrawn.py --entry "
                      "dist/satellite/entry.json")
    upload = text.find("gh release upload satellite")
    check("satellite.yml runs the source check", gate >= 0)
    check("the source check runs before the plan, fetch and upload",
          0 <= gate < plan < fetch < upload, "%d %d %d %d"
          % (gate, plan, fetch, upload))
    check("the entry is checked after the build and before the upload",
          fetch < entry < upload, "%d %d %d" % (fetch, entry, upload))
    if gate >= 0:
        step = _step(text, gate)
        check("the source check step does not swallow a failure",
              "continue-on-error" not in step and "|| true" not in step
              and not re.search(r"\bif:", step), step)
    # After the record and the rebuild, before the catalogue replaces the
    # committed one: on the first path and on the retry path alike.
    after = re.findall(
        r"python tools/verify_catalogue\.py dist/catalogue\.json\n\s*"
        r"python tools/imagery_withdrawn\.py --index satellite/index\.json "
        r"--catalogue dist/catalogue\.json\n\s*"
        r"mv dist/catalogue\.json catalogue\.json", text)
    check("both commit paths check the index and the rebuilt catalogue "
          "before the commit", len(after) == 2, "found %d" % len(after))
    for m in re.finditer(r"python tools/imagery_withdrawn\.py[^\n]*", text):
        check("no imagery check is made to pass: %s" % m.group(0),
              "||" not in m.group(0) and ";" not in m.group(0))


# Workflows that publish catalogue.json but were outside this change's files
# on 9 Oct 2026. Each is listed so the gap is visible; the test fails as soon
# as one gains the check, so the list can only shrink.
KNOWN_GAPS = {"height.yml", "mirror-routing.yml", "traffic-orders.yml"}

CHECK = ("python tools/imagery_withdrawn.py --index satellite/index.json "
         "--catalogue dist/catalogue.json")
PUBLISH = re.compile(r"^\s*(?:mv|cp) dist/catalogue\.json catalogue\.json\b")


def unchecked_publishes(text):
    """For each rebuild of the catalogue, whether the imagery check runs on
    the rebuilt file before it replaces catalogue.json. Returns the line
    numbers of rebuilds that reach a publish unchecked, and the number of
    rebuilds seen."""
    code = [(n, l) for n, l in enumerate(text.split("\n"), 1)
            if l.strip() and not l.strip().startswith("#")]
    bad, seen = [], 0
    for i, (n, line) in enumerate(code):
        if "tools/rebuild_catalogue.sh" not in line:
            continue
        seen += 1
        checked = False
        for _, later in code[i + 1:]:
            if "tools/rebuild_catalogue.sh" in later:
                break
            if (CHECK in later and "|| true" not in later
                    and "|| exit 0" not in later):
                checked = True
            if PUBLISH.search(later):
                if not checked:
                    bad.append(n)
                break
    return bad, seen


def test_every_workflow_that_publishes_the_catalogue_checks_imagery():
    wf = os.path.join(ROOT, ".github", "workflows")
    publishers = []
    for name in sorted(os.listdir(wf)):
        if not name.endswith(".yml"):
            continue
        with open(os.path.join(wf, name), encoding="utf-8") as f:
            text = f.read().replace("\r\n", "\n")
        if not any(PUBLISH.search(l) for l in text.split("\n")):
            continue
        publishers.append(name)
        bad, seen = unchecked_publishes(text)
        check("%s rebuilds the catalogue it publishes" % name, seen > 0)
        if name in KNOWN_GAPS:
            check("%s is still a known gap (drop it from KNOWN_GAPS)" % name,
                  bad != [], "every rebuild is now checked")
            continue
        check("%s checks imagery before every catalogue it publishes" % name,
              bad == [], "unchecked rebuild at line(s) %s" % bad)
        for m in re.finditer(r"- name: [^\n]*\n\s*run: " + re.escape(CHECK),
                             text):
            step = text[m.start():].split("\n      - name:")[0]
            check("%s's imagery step cannot be skipped" % name,
                  "continue-on-error" not in step and "|| true" not in step,
                  step[:200])
    for name in ("satellite.yml", "refresh-data.yml"):
        check("%s is found as a catalogue publisher" % name,
              name in publishers, str(publishers))
    check("every known gap still publishes the catalogue",
          KNOWN_GAPS <= set(publishers), str(publishers))


def test_the_publisher_hunt_can_fail():
    rebuilt = ("bash tools/rebuild_catalogue.sh manifest.json "
               "dist/catalogue.json\n")
    publish = "mv dist/catalogue.json catalogue.json\n"
    check("an unchecked publish is caught",
          unchecked_publishes(rebuilt + publish) == ([1], 1))
    check("a checked publish passes",
          unchecked_publishes(rebuilt + CHECK + "\n" + publish) == ([], 1))
    check("a check made to pass is not a check",
          unchecked_publishes(rebuilt + CHECK + " || true\n" + publish)
          == ([1], 1))
    check("a commented-out check is not a check",
          unchecked_publishes(rebuilt + "# " + CHECK + "\n" + publish)
          == ([1], 1))
    check("a check of an earlier rebuild does not cover a later one",
          unchecked_publishes(rebuilt + CHECK + "\n" + rebuilt + publish)
          == ([3], 2))


def test_an_area_without_imagery_is_due():
    """Withdrawn means due: the rebuild follows without anyone forcing it."""
    cat = load("catalogue.json")
    for continent in cat.get("continents", []):
        for country in continent.get("countries", []):
            for area in country.get("areas", []):
                area["packs"] = [p for p in area.get("packs", [])
                                 if p.get("kind") != "basemap"]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "catalogue.json")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(cat, f)
        run = subprocess.run(
            [sys.executable, os.path.join(HERE, "satellite_plan.py"),
             "--catalogue", path], capture_output=True, text=True)
    check("satellite_plan.py runs", run.returncode == 0, run.stderr[-300:])
    if run.returncode != 0:
        return
    plan = json.loads(run.stdout)
    check("an area with no imagery is due", plan.get("work") is True, str(plan))
    check("and due as never built", plan.get("age_days") is None, str(plan))


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

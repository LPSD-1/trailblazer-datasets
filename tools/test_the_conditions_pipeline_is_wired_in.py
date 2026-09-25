#!/usr/bin/env python3
"""Does anything actually RUN the conditions pipeline?

    python tools/test_the_conditions_pipeline_is_wired_in.py

THE DEFECT THIS REPOSITORY KEEPS PRODUCING is a complete, tested feature that
nothing ever calls. Ten of them now - the 4x4 filter, POIs, `context_note`,
build_pois.py, the HelpScreen, the blank-map download offer, the closure voice,
screen-off audio, the record card, and a lane-count control a rider could press
that changed nothing. Every one had green tests, because every one of those
tests constructed the thing itself.

`ea_flood.py`, `build_wet.py`, `build_fords.py` and `evidence_age.py` were the
eleventh. All four were complete, all four had passing suites, all four had
been proved end to end against live Environment Agency data, and no CI job ran
any of them - so not one byte reached a rider.

SO THIS FILE NEVER CALLS THE PIPELINE. It reads the shipped
`.github/workflows/refresh-data.yml`, the shipped `build_catalogue.py` and the
shipped contract in `docs/WAYS-SCHEMA.md`, and asks whether the invocation is
there. A test that imported `build_wet` and called `assign` would pass on the
day the workflow was deleted, which is precisely the test every one of those
ten features already had.

AND IT EXECUTES THE WORKFLOW'S OWN GUARDS. The two "prove it did something"
steps are extracted from the YAML and run against fixtures, green on real data
and red on an empty subject. A guard nobody has watched fail is a guard.

ZERO SUBJECTS IS BLIND, NOT A PASS. Every scanner below is run a second time
over an empty subject and must report failures. A checker that finds nothing
wrong in an empty file is not a checker, and that is how a "wired in" test
passes on a workflow with the steps taken out.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import build_catalogue as K            # noqa: E402
import build_pois as PO                # noqa: E402

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "refresh-data.yml")
SCHEMA_DOC = os.path.join(ROOT, "docs", "WAYS-SCHEMA.md")

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def workflow_text():
    with open(WORKFLOW, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------- 1. the call sites exist
#
# Each entry is (what it is, the tokens that must all appear on one invocation).
# Matched as a run of tokens rather than a single string because the YAML wraps
# a long command over several lines with backslashes, and a test that demanded
# one literal line would fail on reformatting rather than on removal.

STATIC_CALLS = [
    ("the rainfall station list",
     ["ea_flood.py stations", "--parameter rainfall", "--out cache/ea"]),
    ("the river-level station list",
     ["ea_flood.py stations", "--parameter level", "--out cache/ea"]),
    ("wetness written into the container",
     ["build_wet.py assign", "--container", "--stations",
      "cache/ea/stations-rainfall.json", "--in-place"]),
    ("the ford fetch",
     ["build_fords.py fetch", "--region", "--cache cache/fords"]),
    ("fords written into the container",
     ["build_fords.py build", "--region", "--cache cache/fords",
      "--container", "cache/ea/stations-level.json", "--in-place"]),
    ("the evidence-age summary",
     ["evidence_age.py --write", "--as-of"]),
    ("the manifest put back in step with the edited containers",
     ["restamp_containers.py", "--manifest dist/containers/manifest.json",
      "--require-moved"]),
]

LIVE_CALLS = [
    ("the rainfall readings",
     ["ea_flood.py readings", "--parameter rainfall",
      "--stations cache/ea/stations-rainfall.json", "--days 2",
      "--out cache/ea"]),
    ("the river-level readings",
     ["ea_flood.py readings", "--parameter level",
      "--stations cache/ea/stations-level.json", "--out cache/ea"]),
    ("the published rain feed",
     ["build_wet.py feed", "--region", "cache/ea/readings-rainfall.json",
      "--out published/wet"]),
    ("the published river feed",
     ["build_fords.py feed", "--region", "cache/ea/readings-level.json",
      "--out published/rivers"]),
]


def missing_calls(text, wanted):
    """Which of `wanted` has no invocation in `text`. The premise subject."""
    out = []
    for name, tokens in wanted:
        # An invocation is a window of the file containing every token with no
        # intervening `- name:`, which is where one step ends and the next
        # begins. Cheap, and it cannot be satisfied by tokens scattered across
        # unrelated steps.
        found = False
        for block in re.split(r"\n      - name:", text):
            if all(token in block for token in tokens):
                found = True
                break
        if not found:
            out.append(name)
    return out


def test_every_static_invocation_is_in_the_workflow():
    text = workflow_text()
    absent = missing_calls(text, STATIC_CALLS)
    check("every STATIC-half invocation is in refresh-data.yml", not absent,
          "missing: %s" % ", ".join(absent))


def test_every_live_invocation_is_in_the_workflow():
    text = workflow_text()
    absent = missing_calls(text, LIVE_CALLS)
    check("every LIVE-half invocation is in refresh-data.yml", not absent,
          "missing: %s" % ", ".join(absent))


def test_the_scanner_cannot_pass_over_an_empty_workflow():
    """THE PREMISE. Zero subjects is blind, not a pass.

    If `missing_calls` returned [] for an empty file, the two tests above would
    go green on a workflow with every step deleted - which is the exact state
    this file was written to end.
    """
    check("an empty workflow fails every static call",
          len(missing_calls("", STATIC_CALLS)) == len(STATIC_CALLS),
          repr(missing_calls("", STATIC_CALLS)))
    check("an empty workflow fails every live call",
          len(missing_calls("", LIVE_CALLS)) == len(LIVE_CALLS))
    check("there is something to check in the first place",
          len(STATIC_CALLS) >= 7 and len(LIVE_CALLS) >= 4,
          "%d static, %d live" % (len(STATIC_CALLS), len(LIVE_CALLS)))
    # And it must not pass on tokens merely PRESENT somewhere in the file:
    # scattered across steps is not an invocation.
    scattered = "\n      - name: a\n        run: ea_flood.py stations" \
                "\n      - name: b\n        run: --parameter rainfall" \
                "\n      - name: c\n        run: --out cache/ea"
    check("tokens scattered across three steps are not one invocation",
          "the rainfall station list" in missing_calls(scattered,
                                                       STATIC_CALLS))


# ------------------------------- 2. the two halves are genuinely separated
#
# NOT "two clocks". That was the wrong word for it and a verifier caught it:
# there is exactly ONE schedule (cron 23 3,9,15,21) and the refresh job carries
# no `if:` slowing it down, so BOTH halves fire four times a day. What keeps
# the container bytes still is caching - the 30-day gauge-network cache, the
# self-skipping fords cache and the pinned --as-of - not a second timer.
#
# The property that actually matters is below and is unchanged: the live half
# is a separate job that NEVER writes into a container. Rain written into one
# would republish a fifth of a gigabyte to say it drizzled.

def test_the_halves_are_in_different_jobs():
    text = workflow_text()
    jobs = re.split(r"\n  (\w[\w-]*):\n", text)
    # ["preamble", "refresh", "<body>", "conditions", "<body>"]
    named = dict(zip(jobs[1::2], jobs[2::2]))
    check("there is a job for the live half", "conditions" in named,
          repr(sorted(named)))
    if "conditions" not in named:
        return
    live = named["conditions"]
    refresh = named.get("refresh", "")

    check("the live half runs even when the lane build failed",
          "if: always()" in live)
    check("and never races its push", "needs: refresh" in live)
    check("it checks out the tip, not the triggering commit",
          "ref: main" in live)

    # THE ONE THAT MATTERS. "TOUCHING NO CONTAINER" is the design: rain written
    # into a container would republish a fifth of a gigabyte to say it drizzled.
    check("the live half never writes into a container",
          "--in-place" not in live,
          "the live half must not carry --in-place")
    for writer in ("build_wet.py assign", "build_fords.py build",
                   "evidence_age.py --write", "restamp_containers.py"):
        check("the live half does not run %s" % writer, writer not in live)
    for reader in ("build_wet.py feed", "build_fords.py feed"):
        check("the static half does not publish a feed (%s)" % reader,
              reader not in refresh)

    # And no secret, which is its own failure mode: a step that can only work
    # on the scheduled run is a step nobody can test.
    check("the live half needs no secret",
          "secrets." not in live, "the live half must need no secret")


def test_evidence_age_is_pinned_and_not_run_against_today():
    """Ages are in days against a date. Run with today's, the key changes every
    morning, every container's bytes move, and every rider re-downloads the
    country to learn the median got a day older."""
    text = workflow_text()
    call = [b for b in re.split(r"\n      - name:", text)
            if "evidence_age.py --write" in b]
    check("evidence_age is invoked exactly once", len(call) == 1, len(call))
    if len(call) != 1:
        return
    check("and against a pinned date, not today", "--as-of" in call[0])
    check("pinned to the first of the month", "%Y-%m-01" in call[0],
          call[0].strip()[:200])


def test_the_restamp_runs_after_the_writes_and_before_the_catalogue():
    """build_containers.py takes each container's sha256, sizes and signature
    when it finishes that file. Everything above writes new tables into it.
    Restamping in the wrong place publishes a download that fails its own
    checksum - measured: 12f0fff6 -> 95a7fe24 on motor-south-west."""
    text = workflow_text()
    def at(needle):
        return text.find(needle)
    writes = [at("build_wet.py assign"), at("build_fords.py build"),
              at("evidence_age.py --write")]
    restamp = at("restamp_containers.py")
    catalogue = at("rebuild_catalogue.sh dist/manifest.json")
    check("every in-place write is found", all(i > 0 for i in writes), writes)
    check("the restamp is found", restamp > 0)
    check("the restamp runs after every in-place write",
          restamp > max(writes), "%d vs %d" % (restamp, max(writes)))
    check("and before the catalogue that carries the hashes",
          catalogue > restamp, "%d vs %d" % (catalogue, restamp))


#: Every step that writes a container in dist/containers, in the order the
#: workflow must run them - the container build, the POIs inside it, the
#: conditions tables, evidence_age, and the restamp that re-signs the result.
#: As invocations, and the LAST of each is what counts: a comment naming a
#: tool early in the file must not stand in for a call moved below the step.
CONTAINER_WRITES = ["python tools/build_containers.py",
                    "python tools/build_wet.py assign",
                    "python tools/build_fords.py build",
                    "python tools/evidence_age.py --write",
                    "python tools/restamp_containers.py"]


def changeset_order_problems(text):
    """Why the changesets would NOT describe the bytes riders download.

    A changeset is the difference between the published container and the
    one this run publishes. Built before any of CONTAINER_WRITES, it describes
    a container nobody has: applied, the rider's file is stamped as the new
    build while missing the tables and meta written after it was cut - and the
    signature it is then vouched for by is the restamped file's, not theirs.
    Built after the Publish step's move, `containers/` and `dist/containers`
    are the same tree and there is nothing to diff.
    """
    step = [b for b in re.split(r"\n      - name:", text)
            if "publish_changesets.py" in b and "--new dist/containers" in b]
    if not step:
        return ["no step runs publish_changesets.py --new dist/containers"]
    built = text.find(step[0])
    out = []
    for write in CONTAINER_WRITES:
        at = text.rfind(write)
        if at < 0:
            out.append("%s is not in the workflow" % write)
        elif at > built:
            out.append("%s writes the containers AFTER the changesets are "
                       "built from them" % write)
    later = text[built + len(step[0]):]
    for needle in ("--in-place", "restamp_containers.py", "write_meta"):
        if needle in later:
            out.append("%s runs after the changesets" % needle)
    move = text.find("mv dist/containers containers")
    if move < 0 or move < built:
        out.append("the changesets are not built before the Publish move")
    catalogue = text.find("rebuild_catalogue.sh dist/manifest.json")
    if catalogue < 0 or catalogue < built:
        out.append("the catalogue is built before the changesets it announces")
    return out


def test_the_changesets_are_cut_from_the_bytes_that_ship():
    problems = changeset_order_problems(workflow_text())
    check("the changesets are built after every container write and "
          "before the publish", not problems, problems)

    # AND IT CAN SAY NO. The changeset step moved above the restamp - the
    # order that would have cut every changeset from an unsigned, half-written
    # container - and an empty workflow.
    text = workflow_text()
    blocks = re.split(r"(?=\n      - name:)", text)
    cs = [i for i, b in enumerate(blocks) if "publish_changesets.py" in b]
    rs = [i for i, b in enumerate(blocks) if "restamp_containers.py \\" in b
          or "restamp_containers.py\n" in b]
    check("the reorder fixture finds both steps", cs and rs, (cs, rs))
    if cs and rs:
        moved = list(blocks)
        step = moved.pop(cs[0])
        moved.insert(rs[0], step)
        check("a changeset step above the restamp is refused",
              changeset_order_problems("".join(moved)))
    check("an empty workflow is refused", changeset_order_problems(""))


# ----------------------------------- 3. the workflow's own guards can fail

def heredoc_after(marker, text=None):
    """The `python - <<'PY' ... PY` body of the step naming `marker`."""
    text = workflow_text() if text is None else text
    block = [b for b in re.split(r"\n      - name:", text) if marker in b]
    if not block:
        return None
    match = re.search(r"python - <<'PY'\n(.*?)\n\s*PY\b", block[0], re.S)
    if not match:
        return None
    return textwrap.dedent(match.group(1))


def _tools_copy(where):
    """The real tools/, so an extracted guard imports the shipped modules."""
    dest = os.path.join(where, "tools")
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(HERE):
        if name.endswith(".py"):
            shutil.copyfile(os.path.join(HERE, name),
                            os.path.join(dest, name))
    return dest


def _run_guard(source, cwd, env=None):
    full = dict(os.environ)
    full.update(env or {})
    proc = subprocess.run([sys.executable, "-"], input=source, cwd=cwd,
                          env=full, text=True, capture_output=True)
    return proc.returncode, (proc.stdout + proc.stderr)


def _container(path, wetness=1, gauges=1, fords=1, meta=True):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    if meta:
        db.execute("INSERT INTO meta VALUES ('evidence_age','{}')")
    db.execute("CREATE TABLE way_wetness (id INTEGER PRIMARY KEY,"
               " susceptibility TEXT, basis TEXT, basis_value TEXT,"
               " gauge INTEGER, gauge_m REAL)")
    db.execute("CREATE TABLE wet_gauges (id INTEGER PRIMARY KEY,"
               " station_id TEXT, label TEXT, lat REAL, lon REAL)")
    db.execute("CREATE TABLE fords (ford_uid TEXT PRIMARY KEY, way_id INTEGER,"
               " ford_tag TEXT, name TEXT, lat REAL, lon REAL, way_m REAL,"
               " gauge INTEGER, gauge_m REAL, source_date TEXT)")
    db.execute("CREATE TABLE ford_gauges (id INTEGER PRIMARY KEY,"
               " station_id TEXT, label TEXT, river TEXT, lat REAL, lon REAL,"
               " typical_low_m REAL, typical_high_m REAL)")
    for i in range(wetness):
        db.execute("INSERT INTO way_wetness VALUES (?,'soft','none',NULL,1,9.0)",
                   (i + 1,))
    for i in range(gauges):
        db.execute("INSERT INTO wet_gauges VALUES (?,'E7050','Foo',53.0,-1.6)",
                   (i + 1,))
    for i in range(fords):
        db.execute("INSERT INTO fords VALUES (?,1,'yes',NULL,53.0,-1.6,2.0,"
                   "1,900.0,'2026-09-24')", ("osm:n%d" % i,))
    db.commit()
    db.close()


def test_the_container_guard_refuses_an_empty_subject():
    source = heredoc_after("Prove the containers actually carry conditions")
    check("the container guard is in the workflow", source is not None)
    if source is None:
        return
    tmp = tempfile.mkdtemp()
    try:
        _tools_copy(tmp)
        listing = os.path.join(tmp, "listing.txt")

        good = os.path.join(tmp, "ways-north.tbmap")
        _container(good)
        with open(listing, "w", encoding="utf-8") as fh:
            fh.write("north %s\n" % good)
        code, out = _run_guard(source, tmp, {"REGION_CONTAINERS": listing})
        check("a container carrying conditions passes the guard", code == 0,
              out[-400:])

        # 1. ZERO SUBJECTS. The whole point.
        with open(listing, "w", encoding="utf-8") as fh:
            fh.write("")
        code, out = _run_guard(source, tmp, {"REGION_CONTAINERS": listing})
        check("NO containers at all is refused, not passed", code != 0,
              out[-300:])
        check("and it says so in those words", "BLIND" in out, out[-300:])

        # 2. Present but empty: the table is there and nothing is in it.
        #
        # FORDS STAY POPULATED HERE, and that is the point. Built with
        # `fords=0` as well, this went red through the national fords check
        # instead - so weakening the way_wetness check on its own left the test
        # green, and a mutation survived that should not have. One failure per
        # fixture, or the fixture is testing something else.
        empty = os.path.join(tmp, "ways-empty.tbmap")
        _container(empty, wetness=0, fords=1)
        with open(listing, "w", encoding="utf-8") as fh:
            fh.write("north %s\n" % empty)
        code, out = _run_guard(source, tmp, {"REGION_CONTAINERS": listing})
        check("an empty way_wetness is refused", code != 0, out[-300:])

        # 3. A country with no fords anywhere.
        nofords = os.path.join(tmp, "ways-nofords.tbmap")
        _container(nofords, fords=0)
        with open(listing, "w", encoding="utf-8") as fh:
            fh.write("north %s\n" % nofords)
        code, out = _run_guard(source, tmp, {"REGION_CONTAINERS": listing})
        check("not one ford in the country is refused", code != 0,
              out[-300:])

        # 4. A region whose container never got the tables at all.
        bare = os.path.join(tmp, "ways-bare.tbmap")
        db = sqlite3.connect(bare)
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.commit()
        db.close()
        with open(listing, "w", encoding="utf-8") as fh:
            fh.write("north %s\n" % bare)
        code, out = _run_guard(source, tmp, {"REGION_CONTAINERS": listing})
        check("a container with none of the five tables is refused", code != 0,
              out[-300:])

        # 5. And the meta key the app reads for "surveys as of ...".
        nometa = os.path.join(tmp, "ways-nometa.tbmap")
        _container(nometa, meta=False)
        with open(listing, "w", encoding="utf-8") as fh:
            fh.write("north %s\n" % nometa)
        code, out = _run_guard(source, tmp, {"REGION_CONTAINERS": listing})
        check("a missing meta.evidence_age is refused", code != 0, out[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _feed(path, region, stations, as_of="2026-09-24T12:00:00Z"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "region": region, "as_of": as_of,
                   "stations": stations}, fh, indent=1, sort_keys=True)


def test_the_feed_guard_refuses_an_empty_subject():
    source = heredoc_after("Prove the feeds are not empty")
    check("the feed guard is in the workflow", source is not None)
    if source is None:
        return
    tmp = tempfile.mkdtemp()
    try:
        _tools_copy(tmp)

        def lay(stations, regions=None, as_of="2026-09-24T12:00:00Z"):
            shutil.rmtree(os.path.join(tmp, "published"), ignore_errors=True)
            for region in (regions if regions is not None
                           else sorted(PO.REGIONS)):
                _feed(os.path.join(tmp, "published", "wet", "%s.json" % region),
                      region, stations, as_of)
                _feed(os.path.join(tmp, "published", "rivers",
                                   "%s.json" % region), region, stations,
                      as_of)

        lay({"E7050": {"mm_24h": 1.0}})
        code, out = _run_guard(source, tmp)
        check("six regions of populated feeds pass", code == 0, out[-500:])

        # 1. ZERO SUBJECTS, in the shape that actually happens: the right
        # files, the right schema, and not one gauge in them.
        lay({})
        code, out = _run_guard(source, tmp)
        check("feeds with no gauges at all are refused", code != 0, out[-400:])
        check("and it says zero subjects is blind", "BLIND" in out, out[-400:])

        # 2. A REGION SKIPPED. Five feeds where six were asked for.
        lay({"E7050": {"mm_24h": 1.0}}, regions=sorted(PO.REGIONS)[:-1])
        code, out = _run_guard(source, tmp)
        check("a region with no feed is refused", code != 0, out[-400:])

        # 3. A feed with no timestamp: the app cannot tell fresh from stale,
        # and a stale reassurance is the error this feature must not make.
        lay({"E7050": {"mm_24h": 1.0}}, as_of="")
        code, out = _run_guard(source, tmp)
        check("a feed with no as_of is refused", code != 0, out[-400:])

        # 4. Nothing published at all.
        shutil.rmtree(os.path.join(tmp, "published"), ignore_errors=True)
        code, out = _run_guard(source, tmp)
        check("no feeds at all is refused", code != 0, out[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------ 4. the catalogue can carry the feeds

def test_the_catalogue_block_is_reachable_without_a_new_flag():
    """`rebuild_catalogue.sh` is THE only way any workflow may rebuild the
    catalogue, and it is not in this change's file set. The feeds have to
    arrive through a DEFAULT, or every job that does not pass a new flag
    publishes a catalogue with no conditions - which is how imagery, the trips
    pack and the routing mirror were each deleted by a build that succeeded."""
    source = open(os.path.join(HERE, "build_catalogue.py"),
                  encoding="utf-8").read()
    match = re.search(r'add_argument\("--conditions",\s*default="([^"]*)"',
                      source)
    check("build_catalogue.py takes --conditions", match is not None)
    if match:
        check("and it defaults to where the live half writes",
              match.group(1) == "published", match.group(1))
    script = open(os.path.join(HERE, "rebuild_catalogue.sh"),
                  encoding="utf-8").read()
    check("so the shared rebuild script needs no new line",
          "--conditions" not in script,
          "rebuild_catalogue.sh now names --conditions; if that was "
          "deliberate the default above is no longer load-bearing")


def test_the_block_names_every_published_feed():
    tmp = tempfile.mkdtemp()
    try:
        for region in sorted(PO.REGIONS):
            _feed(os.path.join(tmp, "published", "wet", "%s.json" % region),
                  region, {"E7050": {"mm_24h": 1.0}})
            _feed(os.path.join(tmp, "published", "rivers", "%s.json" % region),
                  region, {"45120": {"m": 0.4}})
        block = K.conditions_block(os.path.join(tmp, "published"),
                                   "https://example.invalid/tb/")
        feeds = block["feeds"]
        check("one feed per region per kind",
              len(feeds) == 2 * len(PO.REGIONS), len(feeds))
        kinds = {f["kind"] for f in feeds}
        check("both kinds are named", kinds == {"wet", "rivers"}, kinds)
        for feed in feeds:
            rel = "published/%s/%s.json" % (feed["kind"], feed["region"])
            check("%s is served where it is written" % feed["id"],
                  feed["file"] == "https://example.invalid/tb/" + rel,
                  feed["file"])
            # AGAINST THE FILE, not against the shape. This asked only that
            # the size was positive and the hash 64 characters long, and a
            # verifier proved the whole suite stayed green - 108 checks, 0
            # failed - with conditions_block() emitting a constant
            # "0"*64 for every feed. The catalogue would then have listed
            # feeds no app could verify, and the only thing that would have
            # noticed was golden.py byte-comparing the finished file, which
            # says "something changed" rather than "this hash is wrong".
            #
            # The hash is the one field a rider depends on and cannot see.
            on_disk = os.path.join(
                tmp, "published", feed["kind"], "%s.json" % feed["region"])
            check("%s size matches the file" % feed["id"],
                  feed["bytes"] == os.path.getsize(on_disk),
                  (feed["bytes"], os.path.getsize(on_disk)))
            with open(on_disk, "rb") as fh:
                want = hashlib.sha256(fh.read()).hexdigest()
            check("%s hash matches the file" % feed["id"],
                  feed["sha256"] == want, (feed["sha256"], want))
            check("%s carries its as_of" % feed["id"],
                  feed["asOf"] == "2026-09-24T12:00:00Z", feed["asOf"])

        # AND NO TWO FEEDS SHARE A HASH. The per-feed check above would pass a
        # function that hashed the same fixed bytes every time; this is what
        # catches it. The fixtures differ by region, so identical hashes mean
        # the hash is not being taken of the file.
        digests = [f["sha256"] for f in feeds]
        check("every feed has its own hash",
              len(set(digests)) == len(digests),
              "%d distinct of %d" % (len(set(digests)), len(digests)))

        # A temp dir is ABSOLUTE, and deriving the URL from the full path would
        # put C:/Users/.../published into the catalogue - machine-dependent,
        # which is the one claim golden.py exists to make, and a 404 besides.
        check("an absolute feed directory still serves a relative URL",
              not any("tmp" in f["file"].lower().replace("/tb/", "")
                      for f in feeds),
              feeds[0]["file"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_empty_conditions_directory_is_not_a_missing_one():
    """THE PREMISE, on the catalogue side. `conditions: {feeds: []}` says "this
    build published none"; a MISSING key says "built by something that had
    never heard of them". The app can act on the first and only guess at the
    second, and telling those apart is the whole reason this pipeline was
    invisible for a fortnight."""
    tmp = tempfile.mkdtemp()
    try:
        block = K.conditions_block(os.path.join(tmp, "published"), "u/")
        check("a missing directory still yields a block", block is not None)
        check("with no feeds in it", block["feeds"] == [], block)
        check("and it still says which schema it is",
              block.get("schema") == 1, block)

        os.makedirs(os.path.join(tmp, "published", "wet"))
        block = K.conditions_block(os.path.join(tmp, "published"), "u/")
        check("an empty wet directory yields no feeds, not a crash",
              block["feeds"] == [], block)

        # AND THE KEY REACHES THE CATALOGUE, which is the half this test used
        # to miss: `conditions_block` returning an empty block says nothing
        # about whether `build` emits it. Made conditional on there being feeds
        # - the obvious tidy-up - the whole distinction is lost and a reader
        # cannot tell "published none" from "never heard of them" again.
        manifest = os.path.join(tmp, "manifest.json")
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "generated": "2026-01-01T00:00:00Z",
                       "packages": [], "regions": []}, fh)
        routing_index, K.routing_index = K.routing_index, lambda: {}
        try:
            catalogue = K.build(manifest, "https://e.invalid/",
                                "2026-01-01T00:00:00Z", conditions_dir=None)
        finally:
            K.routing_index = routing_index
        check("a catalogue with no feeds still carries the conditions key",
              "conditions" in catalogue, sorted(catalogue))
        check("and it is an empty block, not a null",
              (catalogue.get("conditions") or {}).get("feeds") == [],
              catalogue.get("conditions"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_conditions_are_not_packs_and_not_in_the_download_budget():
    """`Pack.fromJson` used to refuse the WHOLE index over one unknown kind -
    every download on every install, until `real_catalogue_test.dart` caught
    it. And the default-UK budget is a number about how much of the country
    fits on a phone, not about a 24 kB feed that goes stale in six hours."""
    tmp = tempfile.mkdtemp()
    try:
        _feed(os.path.join(tmp, "published", "wet", "north.json"), "north",
              {"E7050": {"mm_24h": 1.0}})
        block = K.conditions_block(os.path.join(tmp, "published"), "u/")
        catalogue = {"schema": 2, "baseUrl": "u/", "conditions": block,
                     "continents": [{"id": "europe", "countries": [
                         {"code": "GB", "label": "GB", "areas": [
                             {"id": "gb-north", "packs": []}]}]}]}
        check("the block is not reachable as a pack",
              list(K.packs_in(catalogue)) == [], list(K.packs_in(catalogue)))
        group = K.default_uk_download(catalogue)
        check("and adds nothing to the default UK download",
              sum(p.get("bytes", 0) for p in group) == 0, group)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------- 5. the contract records it all

DOC_TABLES = ("way_wetness", "wet_gauges", "fords", "ford_gauges",
              "fords_bbox")


def doc_gaps(text):
    """What the contract fails to record. The premise subject.

    THE DECLARATION, NOT A MENTION. Asked as "does the word appear anywhere",
    renaming `CREATE TABLE ford_gauges` to `ford_stations` left this green,
    because `ford_gauges.id` and `ford_gauges.station_id` are named in the
    comments and the feed table further down. A contract that names a column of
    a table it no longer declares is exactly the drift this doc exists to stop,
    so the subject is the CREATE statement.
    """
    gaps = []
    declared = set(re.findall(
        r"CREATE (?:VIRTUAL )?TABLE (?:IF NOT EXISTS )?(\w+)", text))
    for table in DOC_TABLES:
        if table not in declared:
            gaps.append("table %s" % table)
    if "evidence_age" not in text:
        gaps.append("meta.evidence_age")
    for feed in ("published/wet/", "published/rivers/"):
        if feed not in text:
            gaps.append("the %s feed" % feed)
    # The additive claim is the one a reader acts on: it is what says an
    # already-published container is still valid.
    if not re.search(r"additive", text, re.I):
        gaps.append("the statement that all of this is additive")
    return gaps


def test_the_contract_records_the_new_tables():
    with open(SCHEMA_DOC, encoding="utf-8") as fh:
        text = fh.read()
    gaps = doc_gaps(text)
    check("docs/WAYS-SCHEMA.md records every new table and feed", not gaps,
          "missing: %s" % ", ".join(gaps))
    check("it still calls itself the contract", "the contract" in text)


def test_the_doc_scanner_cannot_pass_over_an_empty_doc():
    """THE PREMISE. A doc checker that finds nothing wrong in an empty file
    would go green the day somebody deleted the section."""
    gaps = doc_gaps("")
    check("an empty contract fails every table", len(gaps) >= len(DOC_TABLES),
          repr(gaps))
    check("and there is more than one thing being checked", len(gaps) >= 8,
          len(gaps))


def test_the_tables_the_doc_names_are_the_tables_the_tools_write():
    """Both ends against the contract, which is what 0.8 asks of it. Read from
    the tools' own SCHEMA text, so a table renamed in one and not the other is
    caught here rather than on a phone."""
    import build_fords as F
    import build_wet as W
    written = set(re.findall(r"CREATE (?:VIRTUAL )?TABLE IF NOT EXISTS (\w+)",
                             W.SCHEMA + F.SCHEMA))
    check("the tools write exactly the five tables the doc names",
          written == set(DOC_TABLES),
          "tools-doc=%s doc-tools=%s" % (sorted(written - set(DOC_TABLES)),
                                         sorted(set(DOC_TABLES) - written)))
    check("and there are five of them, not zero", len(written) == 5, written)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for failure in _failed:
        print("  FAIL %s" % failure)
    print("%d checks, %d failed" % (_passed + len(_failed), len(_failed)))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Checks on the imagery tiers, run by hand and by the satellite workflow.

    python tools/test_build_satellite.py

WHAT THIS IS REALLY GUARDING
----------------------------
One number in this repository decides how big a download every rider gets by
default, and it is never shown to anybody.

The app offers imagery at more than one detail level and remembers which the
rider picked. Until they pick, `effectiveImageryDetailProvider` takes the
COARSEST - smallest, and the one every area has - and it works out which that
is by sorting the published tiers on `groundMetresPerPixel` and taking the
last. So the ordering of two numbers in `build_satellite.py`, numbers that
appear on no screen, is what stands between "everything for this county" being
about 100 MB and being about 460 MB.

The app has its own test that the coarsest wins. It builds its own fixtures, so
it proves the mechanism and cannot see these values at all. This is the other
end of that coupling - the same shape as the gazetteer's folding, where one
side indexed and the other queried and only a test across both would have
noticed.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "build_satellite", os.path.join(HERE, "build_satellite.py"))
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def main():
    tiers = bs.TIERS
    check(len(tiers) >= 2, "there is only one tier; there is no choice to make")

    # Coarsest first in this file, and the app sorts finest first. Neither
    # order is wrong; what matters is that they disagree consistently.
    zooms = [t["zoom"] for t in tiers]
    check(zooms == sorted(zooms),
          "TIERS is not ordered by zoom, so reading it is guesswork")

    # THE ONE THAT MATTERS. More zoom is more rendered pixels, so fewer metres
    # of ground per pixel. If this were ever written the other way round, the
    # app would sort the tiers backwards, decide the SHARPEST was the coarsest,
    # and hand every rider who has expressed no preference the largest file
    # published - silently, because the number is never displayed.
    for finer, coarser in zip(tiers[1:], tiers[:-1]):
        check(
            finer["groundMetresPerPixel"] < coarser["groundMetresPerPixel"],
            "tier %r is the higher zoom but does not claim finer pixels "
            "(%s vs %s) - the app would default every rider to the big one"
            % (finer["id"], finer["groundMetresPerPixel"],
               coarser["groundMetresPerPixel"]))

    # And the smallest pack really is the one the app will pick. Tile counts
    # are what decides the bytes, so this asserts against the tile maths rather
    # than against the claim.
    box = (-3.25, 51.9, 0.15, 53.6)   # the Midlands
    counts = {t["id"]: len(list(bs.tiles_in(box, 0, t["zoom"]))) for t in tiers}
    default_id = max(tiers, key=lambda t: t["groundMetresPerPixel"])["id"]
    smallest_id = min(counts, key=lambda k: counts[k])
    check(default_id == smallest_id,
          "the app defaults to %r but %r is the smaller download (%s)"
          % (default_id, smallest_id, counts))

    # Every tier needs an id and a label or the app cannot remember or offer
    # it - `ImageryDetail.fromJson` drops a tier missing either, and a dropped
    # tier is one the rider can never choose.
    for t in tiers:
        check(bool(t.get("id")), "a tier has no id")
        check(bool(t.get("label")), "tier %r has no label" % t.get("id"))
        check(bool(t.get("description")),
              "tier %r has no description; the picker would show a bare label"
              % t["id"])

    ids = [t["id"] for t in tiers]
    check(len(set(ids)) == len(ids), "two tiers share an id: %s" % ids)

    # THE PLANNER MUST RECOGNISE ITS OWN OUTPUT. One area now publishes one
    # pack per tier, with the tier in the id, and the planner asks about the
    # area. Keyed on the pack id it found neither, concluded the area had never
    # been built, and would have re-fetched it every run for ever against a
    # free service.
    plan = importlib.util.spec_from_file_location(
        "satellite_plan", os.path.join(HERE, "satellite_plan.py"))
    sp = importlib.util.module_from_spec(plan)
    plan.loader.exec_module(sp)

    built = {
        "continents": [{"countries": [{"areas": [{
            "packs": [
                {"kind": "basemap", "id": "gb-south-east-satellite-standard",
                 "generated": "2026-09-12T21:21:00Z"},
                {"kind": "basemap", "id": "gb-south-east-satellite-high",
                 "generated": "2026-09-12T21:21:00Z"},
            ]}]}]}],
    }
    seen = sp.existing_satellite(built)
    check("gb-south-east-satellite" in seen,
          "the planner cannot see its own tiered packs: %s" % sorted(seen))

    # And the shape published before tiers existed still counts.
    old = {"continents": [{"countries": [{"areas": [{"packs": [
        {"kind": "basemap", "id": "gb-midlands-satellite",
         "generated": "2026-09-12T19:06:35Z"}]}]}]}]}
    check("gb-midlands-satellite" in sp.existing_satellite(old),
          "an untiered pack published earlier stopped counting")

    # An untiered pack left beside its area's tiers does not age the area,
    # and an area with nothing at z14 is due before any merely old one.
    mixed = {"continents": [{"countries": [{"areas": [{"packs": [
        {"kind": "basemap", "id": "gb-east-anglia-satellite", "maxZoom": 13,
         "generated": "2026-09-11T22:40:02Z"},
        {"kind": "basemap", "id": "gb-east-anglia-satellite-high",
         "maxZoom": 14, "generated": "2026-10-07T09:59:31Z"},
        {"kind": "basemap", "id": "gb-east-anglia-satellite-standard",
         "maxZoom": 13, "generated": "2026-10-07T09:59:31Z"},
        {"kind": "basemap", "id": "gb-midlands-satellite", "maxZoom": 13,
         "generated": "2026-09-30T19:06:35Z"}]}]}]}]}
    seen = sp.existing_satellite(mixed)
    check(seen.get("gb-east-anglia-satellite") == "2026-10-07T09:59:31Z",
          "a superseded untiered pack still ages its area: %s" % seen)
    check(sp.below_zoom(mixed, 14) == {"gb-midlands-satellite"},
          "the area with nothing at z14 is not due: %s"
          % sp.below_zoom(mixed, 14))

    check_root_limit()
    check_blocks(sp)
    check_blocks_from_main(sp)
    check_workflow_wiring()
    check_layer_allowlist()
    check_state_out()
    check_incomplete_run_is_green()

    if failures:
        for f in failures:
            print("FAIL: %s" % f, file=sys.stderr)
        return 1

    print("build_satellite: %d tiers, %s" % (
        len(tiers), ", ".join("%s z0-%d (%s tiles)"
                              % (t["id"], t["zoom"], f"{counts[t['id']]:,}")
                              for t in tiers)))
    print("  default tier is %r, which is the smaller download" % default_id)
    print("build_satellite: all checks passed")
    return 0


# --- the root directory limit ------------------------------------------------
#
# SIX OF TEN PUBLISHED IMAGERY PACKS COULD NOT BE OPENED, and nothing said so.
# They downloaded, they matched their sha256, they took up 1.1 GB on the phone,
# and the app refused every one of them with "CorruptArchive: Root directory is
# out of bounds" - the reader enforcing the PMTiles v3 rule that the header and
# the whole root directory must fit in the first 16,384 bytes.
#
# The builder put every entry in the root and wrote leaf_length as 0, so the
# root grew with the tile count. The bias is the cruel part: the bigger the
# pack, the more certain it was to fail, so every high-detail pack and the
# whole of the North were dead while the four smallest worked.

def _entries(n):
    """`n` entries that cannot be run-length collapsed into fewer."""
    # Non-consecutive ids and varying lengths, so the directory is genuinely
    # large rather than compressing down to nothing.
    return [(i * 7, i * 1000, 500 + (i % 97), 1) for i in range(n)]


def _check_true(message, condition):
    check(condition, message)


def check_root_limit():
    """The 16 KB root rule. THIS USED TO BE DEAD CODE: it sat after
    `sys.exit(main())`, so running this file never reached it, and it
    called `check_true` and `build_satellite`, neither of which exists
    here. The guard written for the six unopenable packs never ran."""
    _check_true(
        "a small archive keeps everything in the root",
        bs.build_directories(_entries(50))[1] == b"",
    )

    _root, _leaves, _count = bs.build_directories(_entries(200000))
    _check_true(
        "a big archive spills into leaves",
        _leaves != b"" and _count > 1,
    )
    _check_true(
        "and the root then fits the spec's 16,384 bytes",
        bs.HEADER_LENGTH + len(_root) <= bs.ROOT_LIMIT,
    )

    # The check that would have caught it: EVERY pack this repo publishes, measured
    # against the limit a reader will actually apply.
    for _n in (1000, 50000, 143637, 400000):
        _r, _l, _c = bs.build_directories(_entries(_n))
        _check_true(
            "%d entries produce a conformant root" % _n,
            bs.HEADER_LENGTH + len(_r) <= bs.ROOT_LIMIT,
        )


# --- a refusal is remembered ---------------------------------------------------
#
# A STOP USED TO BE FORGOTTEN BY THE NEXT RUN. EOX refused us, the area stopped
# with no pack, and twelve hours later the planner picked the same area again
# and asked the same service for the same tiles. A stop now writes a record in
# satellite/blocks.json, and the planner skips that area for at least 7 days,
# or until EOX's Retry-After if that is longer.

def _plan(sp, catalogue, blocks, extra=()):
    """satellite_plan's own main(), as the workflow runs it."""
    with tempfile.TemporaryDirectory() as tmp:
        cat = os.path.join(tmp, "catalogue.json")
        with open(cat, "w", encoding="utf-8", newline=LF) as f:
            json.dump(catalogue, f)
        path = os.path.join(tmp, "blocks.json")
        if blocks is not None:
            with open(path, "w", encoding="utf-8", newline=LF) as f:
                f.write(blocks if isinstance(blocks, str)
                        else json.dumps(blocks))
        argv, out = sys.argv, io.StringIO()
        sys.argv = ["satellite_plan.py", "--catalogue", cat,
                    "--blocks", path] + list(extra)
        try:
            with contextlib.redirect_stdout(out),                     contextlib.redirect_stderr(io.StringIO()):
                rc = sp.main()
        except (Exception, SystemExit) as e:  # noqa: BLE001
            return {"error": repr(e)}, None
        finally:
            sys.argv = argv
        try:
            return json.loads(out.getvalue()), rc
        except ValueError:
            return {"output": out.getvalue()}, rc


def check_blocks(sp):
    """A refusal is EOX's, not one area's: EOX is a single host, so asking
    for the next area twelve hours later would be working around its no.
    Records are keyed by host, and a live one stops every area."""
    now = dt.datetime.now(dt.timezone.utc)
    host = "tiles.maps.eox.at"

    def stamp(days_ago):
        return (now - dt.timedelta(days=days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    def area(aid):
        return {"id": aid, "label": aid, "bounds": {
            "west": -1, "south": 51, "east": 0, "north": 52},
            "packs": [{"kind": "lanes", "id": aid + "-ways"}]}
    # Neither area has imagery, so both are due; gb-a is first in line.
    catalogue = {"continents": [{"countries": [{"label": "GB", "areas": [
        area("gb-a"), area("gb-b")]}]}]}

    def rec(days_ago, status=403, retry_after=None, where="gb-a-satellite"):
        return {"hosts": {host: {"at": stamp(days_ago), "status": status,
                                 "retry_after": retry_after,
                                 "area": where}}}

    def picks(blocks):
        plan, _rc = _plan(sp, catalogue, blocks)
        return plan.get("id") if plan.get("work") else plan

    check(getattr(sp, "block_host", lambda: None)() == host,
          "the planner blocks %r, not EOX's tile host" % getattr(sp, "block_host", lambda: None)())
    got = picks(None)
    check(got == "gb-a-satellite",
          "with no blocks file gb-a is not planned: %r" % (got,))
    # Refused in gb-a twelve hours ago: gb-b must NOT be planned now.
    got = picks(rec(0.5))
    check(isinstance(got, dict) and got.get("work") is False
          and host in got.get("why", "")
          and stamp(0.5 - 7)[:10] in got.get("why", ""),
          "12 hours after EOX refused gb-a, the plan is %r; nothing may be "
          "asked of %s until the block ends" % (got, host))
    got = picks(rec(6.9, status=429, where="gb-b-satellite"))
    check(isinstance(got, dict) and got.get("work") is False,
          "6.9 days after a refusal an area was planned: %r" % (got,))
    got = picks(rec(7.1))
    check(got == "gb-a-satellite",
          "7.1 days after a refusal with no Retry-After nothing is "
          "planned: %r" % (got,))
    got = picks(rec(8, retry_after=30 * 86400))
    check(isinstance(got, dict) and got.get("work") is False,
          "a Retry-After of 30 days was cut to 7: %r" % (got,))
    got = picks(rec(31, retry_after=30 * 86400))
    check(got == "gb-a-satellite",
          "nothing is planned after a 30-day Retry-After ran out: %r"
          % (got,))
    # Another host's block does not stop EOX's areas.
    other = rec(1)
    other["hosts"] = {"elsewhere.example": other["hosts"][host]}
    got = picks(other)
    check(got == "gb-a-satellite",
          "another host's block stopped the EOX areas: %r" % (got,))
    # A record whose time cannot be read is a block, not a pass.
    got = picks({"hosts": {host: {"at": "yesterday", "status": 403}}})
    check(isinstance(got, dict) and got.get("work") is False,
          "a block record with an unreadable time was ignored: %r" % (got,))
    # A blocks file that cannot be read stops the plan rather than being
    # taken as "no blocks".
    plan, rc = _plan(sp, catalogue, "{not json")
    check(plan.get("work") is not True and rc not in (0, None),
          "an unreadable blocks file was read as no blocks: %r, rc %r"
          % (plan, rc))


class _Git:
    """Stands in for git: `main` is main's satellite/blocks.json text (None:
    not on main); `fail` names the subcommand that fails."""

    def __init__(self, main=None, fail=None):
        self.main, self.fail, self.calls = main, fail, []

    def __call__(self, argv, **kw):
        import subprocess
        self.calls.append(argv)
        sub, out, rc = argv[1], "", 0
        if sub == self.fail:
            rc = 128
        elif sub == "rev-parse":
            out = "true" + LF
        elif sub == "ls-tree":
            out = ("satellite/blocks.json" + LF if self.main is not None
                   else "")
        elif sub == "show":
            out = self.main or ""
        return subprocess.CompletedProcess(argv, rc, out, "fatal: no")


def check_blocks_from_main(sp):
    """The block that decides is main's latest, fetched at plan time: a
    re-run, or a run from a branch, has a checkout from before the stop was
    committed. Main's copy unreadable plans nothing; where the two copies
    differ, the later block wins."""
    now = dt.datetime.now(dt.timezone.utc)
    host = "tiles.maps.eox.at"

    def rec(days_ago, retry_after=None):
        return {"hosts": {host: {
            "at": (now - dt.timedelta(days=days_ago)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "status": 403, "retry_after": retry_after,
            "area": "gb-a-satellite"}}}

    def area(aid):
        return {"id": aid, "label": aid, "bounds": {
            "west": -1, "south": 51, "east": 0, "north": 52},
            "packs": [{"kind": "lanes", "id": aid + "-ways"}]}
    catalogue = {"continents": [{"countries": [{"label": "GB", "areas": [
        area("gb-a")]}]}]}
    real = getattr(sp, "GIT_RUN", None)

    def plan(local, git):
        sp.GIT_RUN = git
        try:
            return _plan(sp, catalogue, local,
                         ["--blocks-ref", "origin/main"])
        finally:
            sp.GIT_RUN = real

    blocked = lambda p: p.get("work") is False and host in p.get("why", "")
    git = _Git(main=json.dumps(rec(0.5)))
    got, rc = plan(None, git)
    check(blocked(got),
          "a block on main, absent from this checkout, did not stop the "
          "plan: %r" % (got,))
    check(any(c[1] == "fetch" and "origin" in c for c in git.calls)
          and ["git", "show", "origin/main:satellite/blocks.json"]
          in git.calls
          and any(c[1] == "fetch" and "--depth=1" in c for c in git.calls),
          "the planner did not fetch and read main's copy: %r" % git.calls)
    for fail in ("rev-parse", "fetch", "ls-tree", "show"):
        got, rc = plan(None, _Git(main=json.dumps(rec(0.5)), fail=fail))
        if fail == "rev-parse":
            # Not knowing whether the clone is shallow only costs depth.
            check(blocked(got), "rev-parse failing lost the block: %r"
                  % (got,))
            continue
        check(got.get("work") is not True and rc not in (0, None),
              "git %s failing was read as no block: %r, rc %r"
              % (fail, got, rc))
    got, rc = plan(None, _Git(main="{not json"))
    check(got.get("work") is not True and rc not in (0, None),
          "main's unreadable copy was read as no block: %r, rc %r"
          % (got, rc))
    got, rc = plan(None, _Git(main=json.dumps({"hosts": []})))
    check(got.get("work") is not True and rc not in (0, None),
          "main's copy of the wrong shape was read as no block: %r" % (got,))
    got, rc = plan(None, _Git())
    check(got.get("id") == "gb-a-satellite",
          "with no blocks anywhere nothing was planned: %r" % (got,))
    # The later block wins, whichever copy holds it.
    got, rc = plan(rec(8), _Git(main=json.dumps(rec(0.5))))
    check(blocked(got), "an expired local block hid main's live one: %r"
          % (got,))
    got, rc = plan(rec(8, retry_after=30 * 86400),
                   _Git(main=json.dumps(rec(7.5))))
    check(blocked(got), "main's expired block hid this checkout's live "
                        "one: %r" % (got,))
    got, rc = plan(rec(8), _Git(main=json.dumps(rec(7.5))))
    check(got.get("id") == "gb-a-satellite",
          "two expired blocks still stopped the plan: %r" % (got,))
    # Without --blocks-ref, git is never run (tests and local use).
    git = _Git()
    sp.GIT_RUN = git
    try:
        _plan(sp, catalogue, None)
    finally:
        sp.GIT_RUN = real
    check(git.calls == [], "the planner ran git without --blocks-ref: %r"
          % git.calls)


# One shell command: the line naming the tool, and every line it continues
# onto with a trailing backslash.
CMD = r"python tools/%s\.py(?:[^\n]*\\\n)*[^\n]*"
LF = "\n"


def check_workflow_wiring():
    """satellite.yml hands the planner the blocks file on both of its paths,
    and the build writes there."""
    here = os.path.dirname(HERE)
    with open(os.path.join(here, ".github", "workflows", "satellite.yml"),
              encoding="utf-8") as f:
        text = f.read()
    commands = re.findall(CMD % "satellite_plan", text)
    check(len(commands) == 2 and all(
        "--blocks satellite/blocks.json" in c
        and "--blocks-ref origin/main" in c for c in commands),
        "satellite.yml runs the planner without the blocks file, or without "
        "main's copy of it: %r" % commands)
    # Each flag on a line of its own: a two-character backslash-n where a
    # line continuation belongs hands the planner a stray argument.
    check(not any("\\n" in c for c in commands),
          "a planner command has a literal backslash-n: %r" % commands)
    builds = re.findall(CMD % "build_satellite", text)
    check(len(builds) == 1 and
          "--block-out satellite/blocks.json" in builds[0],
          "satellite.yml's build does not record a stop: %r" % builds)


# --- only the CC BY layer may be built ----------------------------------------
#
# The 2024 mosaic (CC BY-NC-SA) was withdrawn on 9 Oct 2026. The allowlist is
# in code (imagery_withdrawn.ALLOWED_LAYERS), so pointing LAYER or SOURCE at
# any other layer stops the build before a single tile is asked for. The
# other-year names are built from numbers so the licence hunt's own scan of
# tools/ does not read this file as naming them as a source.

def _eox(layer):
    return ("https://tiles.maps.eox.at/wmts/1.0.0/" + layer
            + "/default/g/{z}/{y}/{x}.jpg")


NC_LAYERS = ["s2cloudless-%d_3857" % year for year in (2018, 2020, 2024)] + [
    "s2cloudless", "s2cloudless_3857 ", "S2CLOUDLESS_3857", "s2cloudless_4326"]

# Smallest thing stage() accepts as a tile.
JPEG = b"\xff\xd8\xff\xe0" + b"tile"

BOX = ["--bbox", "-1.0", "52.0", "-0.99", "52.01", "--id", "t", "--label", "T",
       "--min-zoom", "13", "--max-zoom", "13"]


class _Spy:
    made = []

    def __init__(self, *a, **k):
        _Spy.made.append(k)
        raise AssertionError("a Fetcher was made for a refused layer")


def _build_refused(layer, source):
    """Run main() with LAYER/SOURCE set; True if it refused before fetching."""
    with tempfile.TemporaryDirectory() as tmp:
        staging = os.path.join(tmp, "s")
        bs.LAYER, bs.SOURCE, bs.Fetcher = layer, source, _Spy
        sys.argv = ["build_satellite.py"] + BOX + ["--staging", staging]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            try:
                rc = bs.main()
            except AssertionError as e:
                rc = "fetched: %s" % e
        check(rc == bs.REFUSED_LAYER_EXIT,
              "build_satellite built from %r (rc %r)" % (source, rc))
        check(not os.path.exists(staging),
              "build_satellite staged tiles from %r" % source)
        check("refus" in err.getvalue().lower(),
              "build_satellite gave no reason for refusing %r: %r"
              % (source, err.getvalue()))


def check_layer_allowlist():
    real = (bs.LAYER, bs.SOURCE, bs.Fetcher, sys.argv)
    _Spy.made = []
    try:
        for layer in NC_LAYERS:
            # The build: refused before the Fetcher exists, so nothing is
            # fetched and nothing is staged.
            _build_refused(layer, _eox(layer))
            bs.LAYER, bs.SOURCE, bs.Fetcher = real[:3]
            # The Fetcher itself, for anything that makes one directly
            # (sample_imagery.py does).
            try:
                bs.Fetcher(sharpen=False, source=_eox(layer))
                check(False, "Fetcher accepted %r" % layer)
            except ValueError:
                pass
        # LAYER allowed but SOURCE on another layer, and the other way round:
        # staging and the recorded layer are keyed by LAYER, the fetch by
        # SOURCE, so both must name the allowed layer.
        _build_refused(real[0], _eox(NC_LAYERS[-1]))
        _build_refused(NC_LAYERS[0], real[1])
    finally:
        bs.LAYER, bs.SOURCE, bs.Fetcher, sys.argv = real
    check(not _Spy.made, "a Fetcher was made for a refused layer: %r"
          % _Spy.made)

    # The allowed layer still builds, and every entry records its layer, which
    # is what the check before publishing and the deletion list read.
    try:
        bs.Fetcher(sharpen=False, source=bs.SOURCE)
    except ValueError as e:
        check(False, "Fetcher refused the CC BY layer: %s" % e)
    with tempfile.TemporaryDirectory() as tmp:
        staging = os.path.join(tmp, "s")
        bbox = (-1.0, 52.0, -0.99, 52.01)
        for z, x, y in bs.tiles_in(bbox, 13, 13):
            bs.stage(staging, z, x, y, JPEG)
        entry_out = os.path.join(tmp, "entry.json")
        sys.argv = (["build_satellite.py"] + BOX
                    + ["--staging", staging, "--package",
                       "--out", os.path.join(tmp, "t.pmtiles"),
                       "--entry-out", entry_out])
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = bs.main()
        finally:
            sys.argv = real[3]
        check(rc == 0, "the CC BY layer did not package (rc %r)" % rc)
        if rc == 0:
            with open(entry_out, encoding="utf-8") as f:
                entry = json.load(f)
            entries = entry if isinstance(entry, list) else [entry]
            check(entries and all(e.get("layer") == "s2cloudless_3857"
                                  for e in entries),
                  "the entry does not record its layer: %r"
                  % [e.get("layer") for e in entries])



# --- a run that fetched part of an area is not a failure -------------------
#
# gb-north is 78,026 tiles at z14 and a run fetches 55,000, so it takes two
# runs and the first writes no pack. On 9 Oct 2026 "Would the app be able to
# read it?" read that as "no packs were written" and failed both scheduled
# runs. The build now says which it was (--state-out): "incomplete" skips the
# pack check, the clear and the publish; anything else, a missing state
# included, still runs the check, which fails when no pack was written.

def check_state_out():
    real = (bs.Fetcher, sys.argv)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            staging = os.path.join(tmp, "s")
            state = os.path.join(tmp, "state")
            out = os.path.join(tmp, "t.pmtiles")
            tiles = list(bs.tiles_in((-1.0, 52.0, -0.95, 52.05), 13, 13))
            bs.stage(staging, *tiles[0], JPEG)
            asked = []

            class Idle:
                lock = threading.Lock()
                done, failed, stopped = 0, [], None

                def __init__(self, *a, **k):
                    pass

                def get(self, *t):
                    asked.append(t)
                    return None
            bs.Fetcher = Idle
            box = ["--bbox", "-1.0", "52.0", "-0.95", "52.05", "--id", "t",
                   "--label", "T", "--min-zoom", "13", "--max-zoom", "13"]
            argv = (["build_satellite.py"] + box
                    + ["--staging", staging, "--package", "--out", out,
                       "--state-out", state])
            sys.argv = argv + ["--budget", "0"]
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = bs.main()
            got = open(state, encoding="utf-8").read().strip() \
                if os.path.exists(state) else None
            check(len(tiles) > 1 and rc == 0 and got == "incomplete"
                  and not os.path.exists(out) and not asked,
                  "a part-fetched area: rc %r, state %r, pack %s"
                  % (rc, got, os.path.exists(out)))
            for t in tiles:
                bs.stage(staging, *t, JPEG)
            sys.argv = argv
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = bs.main()
            got = open(state, encoding="utf-8").read().strip()
            check(rc == 0 and got == "packaged" and os.path.exists(out),
                  "a complete area: rc %r, state %r, pack %s"
                  % (rc, got, os.path.exists(out)))
    except (Exception, SystemExit) as e:  # noqa: BLE001
        check(False, "the state could not be driven: %r" % e)
    finally:
        bs.Fetcher, sys.argv = real


def _steps(text):
    """{step name: (if condition or '', block text)} of the workflow."""
    out = {}
    for m in re.finditer(r"\n      - name: ([^\n]+)\n(.*?)(?=\n      - |\Z)",
                         text, re.S):
        cond = re.search(r"\n        if: ([^\n]+)", "\n" + m.group(2))
        out[m.group(1).strip()] = (cond.group(1).strip() if cond else "",
                                   m.group(2))
    return out


def _runs(cond, outputs, failed=False):
    """Whether an `if:` runs, given step outputs {"fetch.state": ...}."""
    if not cond:
        return not failed
    expr = re.sub(r"steps\.(\w+)\.outputs\.(\w+)",
                  lambda m: repr(outputs.get("%s.%s" % m.groups(), "")),
                  cond)
    expr = re.sub(r"steps\.(\w+)\.outcome",
                  lambda m: repr(outputs.get("%s.outcome" % m.group(1),
                                             "success")), expr)
    expr = (expr.replace("&&", " and ").replace("||", " or ")
            .replace("always()", "True").replace("success()", str(not failed))
            .replace("failure()", str(failed)).replace("cancelled()", "False"))
    ok = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 - test input
    if "always()" not in cond and "failure()" not in cond \
            and "success()" not in cond and "cancelled()" not in cond:
        ok = ok and not failed
    return bool(ok)


def check_incomplete_run_is_green():
    with open(os.path.join(os.path.dirname(HERE), ".github", "workflows",
                           "satellite.yml"), encoding="utf-8") as f:
        text = f.read()
    steps = _steps(text)
    names = ("Fetch and package", "Would the app be able to read it?",
             "Clear the staged tiles", "Publish", "Not finished this run",
             "Save the staged tiles")
    missing = [n for n in names if n not in steps]
    check(not missing, "satellite.yml has no step %r" % missing)
    if missing:
        return
    fetch = steps["Fetch and package"][1]
    check("--state-out dist/satellite/state" in fetch
          and re.search(r'echo "state=\$\(cat dist/satellite/state[^\n]*'
                        r'>> "\$GITHUB_OUTPUT"', fetch),
          "the fetch step does not hand the build's state on")
    check('sys.exit("no packs were written")'
          in steps["Would the app be able to read it?"][1],
          "the pack check no longer fails when no pack was written")
    check("nothing to build" in steps["Not finished this run"][1],
          "the incomplete run does not say so plainly")
    for state, want in (("incomplete", {"Would the app be able to read it?":
                                        False,
                                        "Clear the staged tiles": False,
                                        "Publish": False,
                                        "Not finished this run": True,
                                        "Save the staged tiles": True}),
                        ("packaged", {"Would the app be able to read it?":
                                      True, "Clear the staged tiles": True,
                                      "Publish": True,
                                      "Not finished this run": False}),
                        ("", {"Would the app be able to read it?": True,
                              "Not finished this run": False})):
        outs = {"plan.work": "true", "fetch.state": state,
                "clear.outcome": "success" if state != "incomplete"
                else "skipped"}
        for name, runs in want.items():
            got = _runs(steps[name][0], outs)
            check(got == runs,
                  "state %r: step %r runs=%r, want %r (if: %s)"
                  % (state, name, got, runs, steps[name][0]))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The imagery we ship must be imagery we may ship in a paid app.

    python tools/test_imagery_licence.py

WHY THIS EXISTS
---------------
EOX publish a Sentinel-2 cloudless mosaic for most years, and the years are
NOT licensed alike. From EOX's licence page
(https://cloudless.eox.at/license-non-commercial) and the layer abstracts in
its WMTS capabilities (https://tiles.maps.eox.at/wmts/1.0.0/
WMTSCapabilities.xml), both read 9 Oct 2026 and quoted, dated, in
docs/licences/eox-s2cloudless-2026-10-09.md:

  s2cloudless_3857        2016   CC BY 4.0 on the licence page AND abstract
  s2cloudless-2017_3857   2017   CC BY 4.0 in the abstract only
  s2cloudless-2018_3857   2017 data, CC BY-NC-SA 4.0   (sic: 2018 layer)
  s2cloudless-2019 .. -2025      CC BY-NC-SA 4.0

Trail Blazer is sold, so NonCommercial rules every year from 2018 out, and
only 2016, the year both sources agree on, is relied on. This repository
shipped the 2024 layer with an attribution saying "CC BY 4.0", which was
simply wrong. The checks below hold the source to the 2016 layer, and hold
every place that names the imagery to the one ATTRIBUTION constant,
so the year cannot drift in one file and not the others.

CC BY 4.0 s.3(a) needs the creator, the licence with a link to it, and a
statement that the material was modified. We sharpen and recompress every
tile, so it was.
"""
import contextlib
import datetime as dt
import email.message
import email.utils
import hashlib
import html
import http.server
import importlib.util
import io
import json
import os
import re
import ssl
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The ONLY layer relied on for a paid app: (mosaic year, the attribution EOX's
# licence page requires for it, verbatim). s2cloudless_3857 is the one year
# both the licence page and the WMTS <Abstract> call CC BY 4.0; 2017 is CC BY
# in its abstract alone. Note s2cloudless-2018_3857 holds 2017 data but is
# NC, so a year alone is not enough: it is the LAYER that is allowed.
CC_BY_LAYERS = {"s2cloudless_3857": (
    2016, "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
          "(Contains modified Copernicus Sentinel data 2016 & 2017)")}


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def layer_of(source):
    m = re.search(r"/wmts/1\.0\.0/([^/]+)/", source)
    return m.group(1) if m else None


# A JPEG's first bytes. A 200 whose body is not an image is EOX's busy page
# served as a success, and counts as a failed tile, so a stand-in tile has to
# look like what EOX serves.
JPG = b"\xff\xd8\xff\xe0" + b"tile"


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """Answers each request from a script: bytes for a 200, or an HTTP
    status code to raise. Records every request it is asked to make."""

    def __init__(self, script, default=JPG):
        self.script, self.default, self.calls = list(script), default, []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, req.get_header("User-agent")))
        item = self.script.pop(0) if self.script else self.default
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            code, headers = item
            msg = email.message.Message()
            for k, v in headers.items():
                msg[k] = v
            raise urllib.error.HTTPError(req.full_url, code, "no", msg, None)
        return _Response(item)


class _Clock:
    """Time that moves only when the code under test sleeps."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def _guarded(name, fn):
    """Each check on its own, so one that cannot run hides no other."""
    try:
        fn()
    except (Exception, SystemExit) as e:  # noqa: BLE001
        check(False, "%s could not be driven: %r" % (name, e))


class FakeGit:
    """Answers the planner's git commands: `main` is the text of main's
    satellite/blocks.json (None: not on main), `fail` the subcommand that
    fails. Records every command."""

    def __init__(self, main=None, fail=None):
        self.main, self.fail, self.calls = main, fail, []

    def __call__(self, argv, **kw):
        import subprocess
        self.calls.append(argv)
        sub = argv[1]
        out, rc = "", 0
        if sub == self.fail:
            rc = 128
        elif sub == "rev-parse":
            out = "false\n"
        elif sub == "ls-tree":
            out = "satellite/blocks.json\n" if self.main is not None else ""
        elif sub == "show":
            out = self.main or ""
        return subprocess.CompletedProcess(argv, rc, out, "fatal: no")


def _live_block(days_ago=0.5):
    at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago))
    return json.dumps({"hosts": {"tiles.maps.eox.at": {
        "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": 403,
        "retry_after": None, "area": "gb-a-satellite"}}})


def sample_honours_blocks(si):
    """sample_imagery asks EOX for nothing while main records a live block,
    nor when main's copy cannot be read; with no block it goes ahead."""
    real = (si.FETCHER, si.GIT_RUN, si.BLOCKS, sys.argv)
    asked = []

    class Spy:
        stopped, failed = None, []

        def get(self, z, x, y):
            asked.append((z, x, y))
            return None   # the sample then stops; enough to see it asked

    try:
        with tempfile.TemporaryDirectory() as tmp:
            si.FETCHER = Spy()
            si.BLOCKS = os.path.join(tmp, "absent.json")
            sys.argv = ["sample_imagery.py", "--lat", "52", "--lon", "-1",
                        "--out", tmp]
            for label, git, want_ask in (
                    ("a live block on main", FakeGit(main=_live_block()),
                     False),
                    ("main unreadable", FakeGit(fail="fetch"), False),
                    ("main's copy not JSON", FakeGit(main="{bad"), False),
                    ("no block", FakeGit(), True),
                    ("an expired block", FakeGit(main=_live_block(8)),
                     True)):
                si.GIT_RUN = git
                del asked[:]
                try:
                    with contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()):
                        rc = si.main()
                except SystemExit as e:
                    rc = e.code
                check(bool(asked) == want_ask
                      and (want_ask or rc not in (0, None)),
                      "sample_imagery with %s: asked %d tiles, rc %r"
                      % (label, len(asked), rc))
                check(any(c[1:2] == ["show"] or c[1:2] == ["fetch"]
                          for c in git.calls),
                      "sample_imagery did not read main's blocks (%s): %r"
                      % (label, git.calls))
                # FakeGit says the clone is full: a --depth would make the
                # owner's clone shallow.
                check(not any(a.startswith("--depth") for c in git.calls
                              if c[1:2] == ["fetch"] for a in c),
                      "sample_imagery fetched with --depth on a full clone "
                      "(%s): %r" % (label, git.calls))
    finally:
        si.FETCHER, si.GIT_RUN, si.BLOCKS, sys.argv = real
    # And the refusal reaches the shell as a non-zero exit.
    with open(si.__file__, encoding="utf-8") as f:
        check("sys.exit(main())" in f.read(),
              "sample_imagery.py drops main()'s exit code")


def service_checks(bs):
    # (c) Who we are, with no email address.
    check(bs.USER_AGENT ==
          "TrailBlazer-data/1.0 (+https://lpsd-1.github.io/trailblazer-help/)",
          "USER_AGENT is %r" % bs.USER_AGENT)

    def fetcher(script, **kw):
        clock = _Clock()
        opener = _Opener(script)
        f = bs.Fetcher(sharpen=False, opener=opener, sleep=clock.sleep,
                       clock=clock, **kw)
        return f, opener, clock

    def long_waits(clock):
        return [s for s in clock.slept if s >= 1]

    def waited_out():
        # A 429 that says when to come back is waited out, then the tile
        # arrives; the request carried the User-Agent.
        f, op, clock = fetcher([(429, {"Retry-After": "7"}), JPG])
        got = f.get(13, 1, 2)
        check(got == JPG and 7 in clock.slept and f.stopped is None
              and f.block is None,
              "a 429 with Retry-After 7 was not waited out: got %r, slept %s"
              % (got, clock.slept))
        check(all(ua == bs.USER_AGENT for _u, ua in op.calls),
              "a request went without the User-Agent: %r" % op.calls)

    def default_tries():
        # FOUR requests for one tile, then the area stops: the number is
        # pinned here through the default, with no retries= override.
        # A 429 stops sooner: the third within the hour stops the area.
        for code, tries in ((429, 3), (503, 4)):
            f, op, clock = fetcher([(code, {})] * 10)
            f.get(13, 1, 2)
            check(len(op.calls) == tries and f.stopped,
                  "HTTP %d with the default tries: %d requests, stopped=%r; "
                  "expected %d and a stop" % (code, len(op.calls), f.stopped,
                                              tries))

    def refusals():
        # (a) Refused every time: a bounded number of tries, then the whole
        # run stops - no other tile is asked for. Every redirect, and every
        # 5xx, not just the common ones.
        for code in (301, 302, 303, 307, 308, 429, 500, 502, 503, 504):
            f, op, clock = fetcher([(code, {})] * 10, retries=3)
            first = f.get(13, 1, 2)
            asked = len(op.calls)
            second = f.get(13, 1, 3)
            check(first is None and asked == 3 and f.stopped,
                  "HTTP %d: %d requests, stopped=%r; expected 3 and a stop"
                  % (code, asked, f.stopped))
            check(second is None and len(op.calls) == asked,
                  "HTTP %d: the run went on asking after it was refused"
                  % code)
            check((getattr(f, "block", None) or {}).get("status") == code,
                  "HTTP %d: the stop does not record its status: %r"
                  % (code, getattr(f, "block", None)))

    def stop_at_once():
        # 401 and 403 say "not you", and 410 (gone) and 451 (unavailable for
        # legal reasons) are refusals as plain: the area stops on the first
        # one, with no retry and no wait.
        check(set(bs.STOP_AT_ONCE) == {401, 403, 410, 451},
              "STOP_AT_ONCE is %r" % (bs.STOP_AT_ONCE,))
        for code in (401, 403, 410, 451):
            f, op, clock = fetcher([(code, {})] * 10)
            got = f.get(13, 1, 2)
            f.get(13, 1, 3)
            check(got is None and len(op.calls) == 1 and f.stopped
                  and not long_waits(clock)
                  and (getattr(f, "block", None) or {}).get("status") == code,
                  "HTTP %d was retried or did not stop the area: %d "
                  "requests, slept %s, stopped=%r, block %r"
                  % (code, len(op.calls), clock.slept, f.stopped,
                     getattr(f, "block", None)))

    def retry_after_limit():
        # 600 seconds is the longest wait, pinned as a number. Up to it,
        # the wait is honoured in full.
        f, op, clock = fetcher([(503, {"Retry-After": "600"}), JPG])
        got = f.get(13, 1, 2)
        check(got == JPG and 600 in clock.slept and f.stopped is None,
              "a Retry-After of 600 s was not waited out: got %r, slept %s"
              % (got, clock.slept))
        # Past it, the area stops at once and records what EOX asked for.
        # Never shortened: no shorter wait and another try.
        for ra in ("601", "999999"):
            f, op, clock = fetcher([(503, {"Retry-After": ra}), JPG])
            got = f.get(13, 1, 2)
            block = getattr(f, "block", None) or {}
            check(got is None and len(op.calls) == 1 and f.stopped
                  and not long_waits(clock)
                  and block.get("retry_after") == float(ra),
                  "Retry-After %s was shortened rather than stopping the "
                  "area: got %r, %d requests, slept %s, block %r"
                  % (ra, got, len(op.calls), clock.slept, block))
        # The HTTP-date form says the same, and is read, not guessed at.
        base = 1800000000.0
        later = email.utils.formatdate(base + 2 * 86400, usegmt=True)
        f, op, clock = fetcher([(429, {"Retry-After": later}), JPG],
                               wall=lambda: base)
        got = f.get(13, 1, 2)
        block = getattr(f, "block", None) or {}
        check(got is None and f.stopped and len(op.calls) == 1
              and abs((block.get("retry_after") or 0) - 2 * 86400) < 2,
              "a Retry-After date two days off did not stop the area: "
              "got %r, block %r, slept %s" % (got, block, clock.slept))
        soon = email.utils.formatdate(base + 30, usegmt=True)
        f, op, clock = fetcher([(429, {"Retry-After": soon}), JPG],
                               wall=lambda: base)
        got = f.get(13, 1, 2)
        check(got == JPG and any(29 <= s <= 31 for s in clock.slept),
              "a Retry-After date 30 s off was not waited out: got %r, "
              "slept %s" % (got, clock.slept))

    def backoff_grows():
        f, op, clock = fetcher([(503, {})] * 3 + [JPG])
        f.get(13, 1, 2)
        waits = long_waits(clock)
        check(len(waits) == 3 and waits == sorted(waits) and
              waits[0] < waits[-1],
              "backoff without Retry-After does not grow: %s" % clock.slept)

    def network_retry():
        # A network fault is not a refusal: retried after a short, paced
        # wait (through the injected clock, so it is the fetcher's own).
        f, op, clock = fetcher([urllib.error.URLError("reset"), JPG])
        check(f.get(13, 1, 2) == JPG and 1.5 in clock.slept
              and f.stopped is None,
              "a network fault was not retried after 1.5 s: slept %s"
              % clock.slept)

    def not_found():
        # A 404 is a tile that is not there: recorded, not retried, and it
        # does not stop the run.
        f, op, clock = fetcher([(404, {})])
        check(f.get(13, 1, 2) is None and len(op.calls) == 1
              and f.stopped is None and len(f.failed) == 1,
              "a 404 was retried or stopped the run: %d calls, stopped=%r"
              % (len(op.calls), f.stopped))

    def missing_tiles_count():
        # A 404 (or any other 4xx that is not a refusal) is a failed tile:
        # never retried, but it counts toward the streak and the share, so a
        # run of them stops the area rather than reading as success.
        for code in (404, 400):
            f, op, clock = fetcher([(code, {})] * 30)
            for y in range(19):
                f.get(13, 1, y)
            check(f.stopped is None and len(op.calls) == 19,
                  "19 HTTP %d tiles stopped the area or were retried: "
                  "stopped=%r, %d requests" % (code, f.stopped,
                                               len(op.calls)))
            f.get(13, 1, 19)
            f.get(13, 1, 20)
            block = getattr(f, "block", None) or {}
            check(f.stopped and len(op.calls) == 20
                  and block.get("status") == code,
                  "20 HTTP %d tiles running did not stop the area: "
                  "stopped=%r, %d requests, block %r"
                  % (code, f.stopped, len(op.calls), block))
        # And the share: 11 of the last 200 stop it.
        f, op, clock = fetcher([(404, {}) if i % 15 == 0 else JPG
                                for i in range(200)])
        for i in range(200):
            f.get(13, 1, i)
        check(f.stopped, "11 missing tiles in the last 200 did not stop "
                         "the area")

    def too_many_requests():
        # Every 429 is EOX saying "too many", even one that a wait clears:
        # TOO_MANY_429 of them within TOO_MANY_WINDOW seconds stop the area
        # and record the block, however the tiles they were about ended.
        check(bs.TOO_MANY_429 == 3 and bs.TOO_MANY_WINDOW == 3600,
              "the 429 limit is %r in %r s, not 3 in an hour"
              % (getattr(bs, "TOO_MANY_429", None),
                 getattr(bs, "TOO_MANY_WINDOW", None)))
        ra = (429, {"Retry-After": "1"})
        f, op, clock = fetcher([ra, JPG, ra, JPG, ra, JPG, JPG])
        got = [f.get(13, 1, y) for y in range(3)]
        block = getattr(f, "block", None) or {}
        check(got[:2] == [JPG, JPG] and got[2] is None and f.stopped
              and block.get("status") == 429 and len(op.calls) == 5,
              "three 429s, each cleared by a wait, did not stop the area: "
              "got %r, stopped=%r, block %r, %d requests"
              % (got, f.stopped, block, len(op.calls)))
        f, op, clock = fetcher([ra, JPG, ra, JPG, JPG])
        got = [f.get(13, 1, y) for y in range(3)]
        check(got == [JPG] * 3 and f.stopped is None,
              "two 429s stopped the area: %r" % f.stopped)
        # Outside the window they are not counted together.
        f, op, clock = fetcher([ra, JPG, ra, JPG, ra, JPG])
        for y in range(3):
            f.get(13, 1, y)
            clock.now += 1800
        check(f.stopped is None,
              "429s an hour and more apart stopped the area: %r" % f.stopped)

    def absurd_retry_after():
        # inf, 1e400 (inf as a float), nan and -inf are not waits a clock
        # can hold. Each is a refusal asking for a long time: the area stops
        # on the first, the block is RETRY_AFTER_ABSURD (a year), no crash.
        check(bs.RETRY_AFTER_ABSURD == 365 * 86400,
              "RETRY_AFTER_ABSURD is %r, not a year"
              % getattr(bs, "RETRY_AFTER_ABSURD", None))
        for ra in ("inf", "1e400", "nan", "-inf", "Infinity",
                   str(10 ** 12)):
            f, op, clock = fetcher([(429, {"Retry-After": ra}), JPG])
            got = f.get(13, 1, 2)
            block = getattr(f, "block", None) or {}
            check(got is None and f.stopped and len(op.calls) == 1
                  and block.get("retry_after") == bs.RETRY_AFTER_ABSURD,
                  "Retry-After %r: got %r, %d requests, block %r"
                  % (ra, got, len(op.calls), block))

    def negative_retry_after():
        # A finite negative Retry-After, or an HTTP date already past, is
        # "now": 0, never a negative wait and never a negative record.
        base = 1800000000.0
        past = email.utils.formatdate(base - 3600, usegmt=True)
        for ra in ("-30", "-0.5", past):
            f, op, clock = fetcher([(403, {"Retry-After": ra}), JPG],
                                   wall=lambda: base)
            f.get(13, 1, 2)
            block = getattr(f, "block", None) or {}
            check(f.stopped and block.get("retry_after") == 0,
                  "Retry-After %r on a 403 recorded %r, not 0"
                  % (ra, block.get("retry_after")))
            f, op, clock = fetcher([(429, {"Retry-After": ra}), JPG],
                                   wall=lambda: base)
            got = f.get(13, 1, 2)
            check(got == JPG and all(s >= 0 for s in clock.slept),
                  "Retry-After %r on a 429: got %r, slept %s"
                  % (ra, got, clock.slept))
        # And the record never holds a negative wait, whatever it is handed.
        with tempfile.TemporaryDirectory() as tmp:
            rec = bs.record_block(os.path.join(tmp, "b.json"), "a",
                                  {"status": 403, "retry_after": -5.0,
                                   "reason": "r"})
        check(rec.get("retry_after") == 0,
              "record_block wrote a negative retry_after: %r" % rec)

    def network_pause_is_host_wide():
        # The backoff after a network fault holds every connection, as a
        # Retry-After does: another worker asking during it leaves no
        # sooner than the faulted one.
        clock = _Clock()
        starts = []
        nested = []
        box = {}

        def sleep(s):
            if s >= 1 and not nested:
                nested.append(True)
                box["f"].get(13, 5, 6)   # another worker, mid-backoff
            clock.sleep(s)

        op = _Opener([urllib.error.URLError("reset"), JPG, JPG])
        real_open = op.open

        def timed(req, timeout=None):
            starts.append((req.full_url, clock.now))
            return real_open(req, timeout)
        op.open = timed
        box["f"] = bs.Fetcher(sharpen=False, opener=op, sleep=sleep,
                              clock=clock)
        box["f"].get(13, 1, 2)
        other = [t for u, t in starts if u.endswith("/13/6/5.jpg")]
        check(nested and other and other[0] >= 1000.0 + 1.5,
              "another worker asked during a network backoff: faulted at "
              "1000, it asked at %s" % other)

    def scheduled_request_waits():
        # A request already given its slot, asleep until it, when another
        # connection is told to wait: it waits too, rather than leaving at
        # the slot it was given before the pause.
        clock = _Clock()
        box = {}
        held = []

        def sleep(s):
            if not held:
                held.append(True)
                box["f"]._hold(7)   # what a refused connection does
            clock.sleep(s)

        op = _Opener([JPG, JPG])
        starts = []
        real_open = op.open

        def timed(req, timeout=None):
            starts.append(clock.now)
            return real_open(req, timeout)
        op.open = timed
        box["f"] = bs.Fetcher(sharpen=False, opener=op, sleep=sleep,
                              clock=clock)
        box["f"].get(13, 1, 2)       # at 1000, no sleep
        box["f"].get(13, 1, 3)       # slot 1000.25; the pause lands mid-sleep
        check(held and len(starts) == 2 and starts[1] >= 1000.0 + 7,
              "a scheduled request left during a pause: starts %s" % starts)

    def paced():
        # (b) A polite rate, stated and pinned: four a second.
        check(bs.MAX_REQUESTS_PER_SECOND == 4.0,
              "MAX_REQUESTS_PER_SECOND is %r, not 4"
              % bs.MAX_REQUESTS_PER_SECOND)
        f, op, clock = fetcher([])
        starts = []
        real_open = op.open

        def timed(req, timeout=None):
            starts.append(clock.now)
            return real_open(req, timeout)
        op.open = timed
        for y in range(6):
            f.get(13, 1, y)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        check(len(gaps) == 5 and all(abs(g - 0.25) < 1e-9 for g in gaps),
              "requests were not paced to 4 a second: gaps %s" % gaps)

    def paced_across_threads():
        # Three connections, as main() runs them, share ONE slot: a slot per
        # thread would triple the rate. Time stands still here and each
        # request's start is when its sleep would have ended, so the spacing
        # is exact and owes nothing to the scheduler.
        tl = threading.local()
        lock = threading.Lock()
        starts = []

        class Op(_Opener):
            def open(self, req, timeout=None):
                with lock:
                    starts.append(1000.0 + getattr(tl, "slept", 0.0))
                tl.slept = 0.0
                return _Response(JPG)

        def sleep(s):
            tl.slept = getattr(tl, "slept", 0.0) + s

        f = bs.Fetcher(sharpen=False, opener=Op([]), sleep=sleep,
                       clock=lambda: 1000.0)

        def worker(n):
            for y in range(3):
                f.get(13, n, y)
        threads = [threading.Thread(target=worker, args=(n,))
                   for n in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        check(len(starts) == 9 and all(abs(g - 0.25) < 1e-9 for g in gaps),
              "three connections were not paced together at 4 a second: "
              "starts %s" % starts)

    def pause_is_host_wide():
        # A Retry-After pauses every connection, not just the refused one:
        # another worker asking while this one waits leaves no sooner.
        clock = _Clock()
        starts = []
        nested = []
        box = {}

        def sleep(s):
            if s >= 5 and not nested:
                nested.append(True)
                box["f"].get(13, 5, 6)   # another worker, mid-wait
            clock.sleep(s)

        op = _Opener([(429, {"Retry-After": "7"}), JPG, JPG])
        real_open = op.open

        def timed(req, timeout=None):
            starts.append((req.full_url, clock.now))
            return real_open(req, timeout)
        op.open = timed
        box["f"] = bs.Fetcher(sharpen=False, opener=op, sleep=sleep,
                              clock=clock)
        box["f"].get(13, 1, 2)
        other = [t for u, t in starts if u.endswith("/13/6/5.jpg")]
        check(nested and other and other[0] >= 1000.0 + 7,
              "another worker asked during a Retry-After pause: refused at "
              "1000, it asked at %s" % other)

    def nothing_after_a_stop():
        # A worker held in the pacing sleep when another is refused must not
        # send its request once it wakes.
        clock = _Clock()
        box = {}

        def sleep(s):
            box["f"].stopped = box["f"].stopped or "refused elsewhere"
            clock.sleep(s)

        op = _Opener([JPG, JPG])
        box["f"] = bs.Fetcher(sharpen=False, opener=op, sleep=sleep,
                              clock=clock)
        box["f"].get(13, 1, 2)
        got = box["f"].get(13, 1, 3)
        check(got is None and len(op.calls) == 1,
              "a request left after the area was stopped: %d requests"
              % len(op.calls))

    def connection_failures():
        # Resets, timeouts, TLS faults and a 200 that is not an image are
        # refusals too, counted per tile: 20 tiles running, or more than 5%
        # of the last 200, stop the area as an HTTP refusal does.
        faults = [ConnectionResetError("reset"), TimeoutError("timed out"),
                  ssl.SSLError("bad record mac"),
                  urllib.error.URLError("refused"),
                  b"<html>heavyload</html>"]
        f, op, clock = fetcher([faults[i % 5] for i in range(20)]
                               + [JPG] * 5, retries=1)
        for y in range(19):
            f.get(13, 1, y)
        check(f.stopped is None,
              "19 failed tiles running stopped the area: %r" % f.stopped)
        f.get(13, 1, 19)
        asked = len(op.calls)
        f.get(13, 1, 20)
        block = getattr(f, "block", None) or {}
        check(f.stopped and asked == 20 and len(op.calls) == 20
              and block.get("status") == "network",
              "20 failed tiles running did not stop the area: stopped=%r, "
              "%d requests, block %r" % (f.stopped, len(op.calls), block))

        f, op, clock = fetcher([faults[i % 5] for i in range(19)] + [JPG]
                               + [faults[i % 5] for i in range(19)],
                               retries=1)
        for y in range(39):
            f.get(13, 1, y)
        check(f.stopped is None,
              "a good tile did not break the run of failures: %r"
              % f.stopped)

        def run(fail_at, n):
            g, op2, _c = fetcher([faults[0] if i in fail_at else JPG
                                  for i in range(n)], retries=1)
            for i in range(n):
                g.get(13, 1, i)
            return g
        # The share is of the last 200 tiles, so it is judged once 200 have
        # been asked for; before that, 20 running is the rule. 10 of 200 is
        # 5%, which is not MORE than 5%.
        g = run(set(range(0, 136, 15)), 200)
        check(g.stopped is None,
              "10 failed tiles of 200 (5%%) stopped the area: %r" % g.stopped)
        g = run(set(range(0, 151, 15)), 199)
        check(g.stopped is None,
              "the share was judged before 200 tiles: %r" % g.stopped)
        g = run(set(range(0, 151, 15)), 200)
        check(g.stopped,
              "11 failed tiles in the last 200 did not stop the area")
        g = run(set(range(0, 136, 15)) | set(range(350, 486, 15)), 486)
        check(g.stopped is None,
              "failures more than 200 tiles back still counted: %r"
              % g.stopped)

        # And the busy page is never kept as a tile.
        f, op, clock = fetcher([b"<html>busy</html>"], retries=1)
        check(f.get(13, 1, 2) is None and len(f.failed) == 1,
              "a 200 that is not an image was kept as a tile")

    for name, fn in (("the Retry-After wait", waited_out),
                     ("the default tries", default_tries),
                     ("the refusal sweep", refusals),
                     ("the stop-at-once codes", stop_at_once),
                     ("the Retry-After limit", retry_after_limit),
                     ("the backoff", backoff_grows),
                     ("the network retry", network_retry),
                     ("the 404 case", not_found),
                     ("missing tiles counted", missing_tiles_count),
                     ("the 429 count", too_many_requests),
                     ("an absurd Retry-After", absurd_retry_after),
                     ("a negative Retry-After", negative_retry_after),
                     ("the network pause", network_pause_is_host_wide),
                     ("the scheduled request", scheduled_request_waits),
                     ("the pacing", paced),
                     ("the pacing across threads", paced_across_threads),
                     ("the host-wide pause", pause_is_host_wide),
                     ("the stop after pacing", nothing_after_a_stop),
                     ("the connection failures", connection_failures)):
        _guarded(name, fn)

    # (a) A redirect is never followed, through the opener the build uses,
    # and through sample_imagery's own FETCHER, the real object.
    si = load("sample_imagery")
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path.startswith("/heavyload"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"busy")
                return
            self.send_response(302)
            self.send_header("Location", "/heavyload")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        clock = _Clock()
        f = bs.Fetcher(sharpen=False, retries=2, sleep=clock.sleep,
                       clock=clock, source=base + "/{z}/{x}/{y}")
        got = f.get(13, 1, 2)
        check(got is None and f.stopped and
              not any(h.startswith("/heavyload") for h in hits),
              "a redirect was followed or not treated as a refusal: "
              "got %r, stopped=%r, requests %s" % (got, f.stopped, hits))

        real = si.FETCHER
        del hits[:]
        try:
            real.opener.open(urllib.request.Request(base + "/13/1/2"),
                             timeout=10).read()
            followed = True
        except urllib.error.HTTPError as e:
            followed = e.code != 302
        check(not followed and hits == ["/13/1/2"],
              "sample_imagery's FETCHER follows a redirect: %s" % hits)
        check(abs(real.interval - 0.25) < 1e-9 and real.retries == 4,
              "sample_imagery's FETCHER is not the build's polite one: "
              "interval %r, retries %r" % (real.interval, real.retries))
    except Exception as e:  # noqa: BLE001
        check(False, "the redirect check could not run: %r" % e)
    finally:
        server.shutdown()
        server.server_close()

    _guarded("sample_imagery and the blocks file",
             lambda: sample_honours_blocks(si))

    # (a, d) The build end to end over a tiny area: staged tiles are never
    # fetched again, and a refusal stops the area with no pack written, so
    # the published one stays, and records the stop where the planner
    # reads it.
    box = ["--bbox", "-1.0", "51.0", "-0.9", "51.1", "--min-zoom", "0",
           "--max-zoom", "2", "--id", "t", "--label", "T"]
    wanted = list(bs.tiles_in((-1.0, 51.0, -0.9, 51.1), 0, 2))
    real_fetcher, argv = bs.Fetcher, sys.argv
    try:
        with tempfile.TemporaryDirectory() as tmp:
            staging = os.path.join(tmp, "staging")
            bs.stage(staging, *wanted[0], b"old")
            opened = []

            def make(script):
                def factory(sharpen):
                    op = _Opener(script)
                    opened.append(op)
                    clock = _Clock()
                    return real_fetcher(sharpen=sharpen, opener=op,
                                        sleep=clock.sleep, clock=clock)
                return factory

            bs.Fetcher = make([])
            sys.argv = ["build_satellite.py"] + box + ["--staging", staging]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = bs.main()
            asked = [u for op in opened for u, _ua in op.calls]
            first = "/%d/%d/%d.jpg" % (wanted[0][0], wanted[0][2],
                                       wanted[0][1])
            check(rc == 0 and len(asked) == len(wanted) - 1 and
                  not any(u.endswith(first) for u in asked),
                  "a staged tile was fetched again, or a tile was missed: "
                  "%d asked of %d wanted, rc %r" % (len(asked), len(wanted),
                                                      rc))
            opened.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                rc = bs.main()
            again = sum(len(op.calls) for op in opened)
            check(rc == 0 and again == 0,
                  "a complete staging set was fetched again: %d requests"
                  % again)

            # Refused: exit 3, no pack, and a block record beside the one
            # already there for another area.
            blocks = os.path.join(tmp, "satellite", "blocks.json")
            os.makedirs(os.path.dirname(blocks))
            other = {"at": "2026-10-01T00:00:00Z", "status": 429,
                     "retry_after": None, "until": "2026-10-08T00:00:00Z",
                     "reason": "earlier"}
            with open(blocks, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"hosts": {"elsewhere.example": other}}, fh)
            staging2 = os.path.join(tmp, "staging2")
            out = os.path.join(tmp, "t.pmtiles")
            bs.Fetcher = make([(429, {})] * 100)
            sys.argv = (["build_satellite.py"] + box +
                        ["--staging", staging2, "--package", "--out", out,
                         "--block-out", blocks])
            log = io.StringIO()
            before = dt.datetime.now(dt.timezone.utc)
            with contextlib.redirect_stdout(log), \
                    contextlib.redirect_stderr(log):
                rc = bs.main()
            check(rc == 3 and not os.path.exists(out) and
                  "STOPPED" in log.getvalue() and
                  len(opened[-1].calls) <= 4,
                  "a refused area was not stopped cleanly: rc %r, pack %s, "
                  "%d requests, log %r" % (rc, os.path.exists(out),
                                           len(opened[-1].calls),
                                           log.getvalue()[-300:]))
            with open(blocks, encoding="utf-8") as fh:
                areas = json.load(fh).get("hosts", {})
            rec = areas.get("tiles.maps.eox.at") or {}

            def when(s):
                return dt.datetime.fromisoformat(
                    str(s).replace("Z", "+00:00"))
            try:
                at, until = when(rec.get("at")), when(rec.get("until"))
            except (TypeError, ValueError):
                at = until = None
            check(areas.get("elsewhere.example") == other,
                  "the stop overwrote another host's block: %r" % areas)
            check(rec.get("status") == 429 and rec.get("retry_after") is None
                  and rec.get("area") == "t"
                  and at is not None
                  and abs((at - before).total_seconds()) < 120
                  and until - at == dt.timedelta(days=7),
                  "the stop was not recorded with its time, status and a "
                  "7-day block: %r" % rec)

            # A 403 asking for a month: recorded as a month.
            bs.Fetcher = make([(403, {"Retry-After": "2592000"})] * 100)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = bs.main()
            with open(blocks, encoding="utf-8") as fh:
                rec = (json.load(fh).get("hosts", {})
                       .get("tiles.maps.eox.at") or {})
            try:
                at, until = when(rec.get("at")), when(rec.get("until"))
            except (TypeError, ValueError):
                at = until = None
            check(rc == 3 and len(opened[-1].calls) == 1
                  and rec.get("status") == 403
                  and rec.get("retry_after") == 2592000
                  and at is not None and until - at == dt.timedelta(days=30),
                  "a 403 asking for 30 days was not recorded as 30 days: "
                  "rc %r, %d requests, %r" % (rc, len(opened[-1].calls), rec))

            # A Retry-After that is not a number of seconds a clock can
            # hold: still a refusal, recorded, exit 3 - not a crash with
            # nothing recorded and the next run asking again.
            for ra in ("inf", "1e400", "nan", "-inf"):
                bs.Fetcher = make([(503, {"Retry-After": ra})] * 100)
                try:
                    with contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()):
                        rc = bs.main()
                except Exception as e:  # noqa: BLE001
                    rc = repr(e)
                with open(blocks, encoding="utf-8") as fh:
                    rec = (json.load(fh).get("hosts", {})
                           .get("tiles.maps.eox.at") or {})
                try:
                    at, until = when(rec.get("at")), when(rec.get("until"))
                except (TypeError, ValueError):
                    at = until = None
                check(rc == 3 and len(opened[-1].calls) == 1
                      and rec.get("retry_after") == bs.RETRY_AFTER_ABSURD
                      and at is not None
                      and until - at == dt.timedelta(
                          seconds=bs.RETRY_AFTER_ABSURD),
                      "Retry-After %r was not recorded as a long block: rc "
                      "%r, %d requests, %r" % (ra, rc, len(opened[-1].calls),
                                               rec))
    except (Exception, SystemExit) as e:  # noqa: BLE001 - argparse exits
        check(False, "the build could not be driven: %r" % e)
    finally:
        bs.Fetcher, sys.argv = real_fetcher, argv


def main():
    bs = load("build_satellite")
    layer = layer_of(bs.SOURCE)
    year, required = CC_BY_LAYERS.get(layer, (None, None))

    # 1. The source is a CC BY layer.
    check(year is not None,
          "SOURCE uses %r, which is not a CC BY layer (allowed: %s)"
          % (layer, sorted(CC_BY_LAYERS)))

    # 2. The attribution says what CC BY 4.0 s.3(a) requires.
    a = bs.ATTRIBUTION
    for needed in ("CC BY 4.0",
                   "https://creativecommons.org/licenses/by/4.0/",
                   "EOX IT Services GmbH",
                   "https://cloudless.eox.at",
                   "modified"):
        check(needed in a, "ATTRIBUTION lacks %r: %r" % (needed, a))
    check(required is not None and required in a,
          "ATTRIBUTION does not carry EOX's required words for the layer "
          "(%r): %r" % (required, a))
    # No other year may appear in it: "2016 ... data 2024" is the mistake
    # this replaces, half corrected.
    years = set(int(y) for y in re.findall(r"\b(20\d\d)\b", a))
    check(years == {2016, 2017},
          "ATTRIBUTION names years %s, expected the 2016 mosaic's data "
          "years 2016 and 2017" % sorted(years))

    # 3. Staged tiles are kept per layer, so a staging cache filled from an
    #    older layer can never be resumed into a pack labelled with this one.
    staged = bs.staged_path("staging", 13, 1, 2)
    check(layer is not None and layer in staged.replace("\\", "/").split("/"),
          "staged tiles are not kept per layer: %r" % staged)

    # 4. The sample plates come from the same layer.
    si = load("sample_imagery")
    check(si.SOURCE == bs.SOURCE,
          "sample_imagery.SOURCE %r differs from build_satellite.SOURCE"
          % si.SOURCE)

    # 4a. Every plate is regenerated from LAYER: the six the tool makes, no
    #     more, each recorded with its bytes in satellite/samples/index.json.
    #     The plates published before 9 Oct 2026 were cut from 2024 (NC).
    names = list(getattr(si, "SAMPLE_NAMES", ()))
    check(sorted(names) == sorted([
        "standard", "standard-sharpened", "detailed",
        "zoom-standard", "zoom-sharpened", "zoom-detailed"]),
        "sample_imagery does not make all six plates: %r" % names)
    samples = os.path.join(ROOT, "satellite", "samples")
    try:
        with open(os.path.join(samples, "index.json"), encoding="utf-8") as f:
            index = json.load(f)
    except (OSError, ValueError) as e:
        index = {}
        check(False, "satellite/samples/index.json unreadable: %s" % e)
    check(index.get("layer") == layer,
          "the plates record layer %r, not %r" % (index.get("layer"), layer))
    check(index.get("attribution") == a,
          "the plates' index does not carry ATTRIBUTION")
    files = index.get("files", {})
    on_disk = sorted(n for n in os.listdir(samples) if n.endswith(".jpg"))
    check(sorted(files) == on_disk == sorted(n + ".jpg" for n in names),
          "plates on disk %s, indexed %s, made %s"
          % (on_disk, sorted(files), names))
    for n in on_disk:
        with open(os.path.join(samples, n), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        check(files.get(n) == digest,
              "%s is not the plate the index records" % n)

    # 4b. The plates themselves, from synthetic tiles. The z13 tile is blue
    #     with a red centre quarter, the z14 mosaic green with a yellow one,
    #     so each plate's corner says which tiles it came from and whether it
    #     is the whole ground or the centre quarter blown up.
    from PIL import Image

    def target(px, ground, centre):
        img = Image.new("RGB", (px, px), ground)
        img.paste(Image.new("RGB", (px // 2, px // 2), centre),
                  (px // 4, px // 4))
        return img

    blue, red = (0, 0, 255), (255, 0, 0)
    green, yellow = (0, 255, 0), (255, 255, 0)
    want = {"standard": blue, "standard-sharpened": blue, "detailed": green,
            "zoom-standard": red, "zoom-sharpened": red,
            "zoom-detailed": yellow}
    try:
        plates = si.plates(target(256, blue, red),
                           target(512, green, yellow), 128)
    except Exception as e:  # noqa: BLE001
        plates = {}
        check(False, "sample_imagery.plates failed: %r" % e)
    check(sorted(plates) == sorted(want),
          "plates() made %s, not %s" % (sorted(plates), sorted(want)))
    for n, img in plates.items():
        check(img.size == (128, 128), "%s is %s, not 128px" % (n, img.size))
        got = img.convert("RGB").getpixel((5, 5))
        check(all(abs(g - w) < 40 for g, w in zip(got, want.get(n, got))),
              "%s corner is %s, expected %s" % (n, got, want.get(n)))
    if "standard" in plates and "standard-sharpened" in plates:
        check(plates["standard"].tobytes()
              != plates["standard-sharpened"].tobytes(),
              "standard-sharpened is not sharpened")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            si.write_plates(tmp, plates)
            with open(os.path.join(tmp, "index.json"), encoding="utf-8") as f:
                made = json.load(f)
        except Exception as e:  # noqa: BLE001
            made = {}
            check(False, "sample_imagery.write_plates failed: %r" % e)
        check(made.get("layer") == layer and made.get("attribution") == a,
              "write_plates does not record LAYER and ATTRIBUTION: %r"
              % {k: made.get(k) for k in ("layer", "attribution")})
        for n in names:
            path = os.path.join(tmp, n + ".jpg")
            ok = os.path.exists(path)
            check(ok, "write_plates did not write %s.jpg" % n)
            if ok:
                with open(path, "rb") as f:
                    check(made.get("files", {}).get(n + ".jpg")
                          == hashlib.sha256(f.read()).hexdigest(),
                          "write_plates recorded the wrong hash for %s" % n)

    # 4b'. main() end to end, with the network replaced by flat tiles: it
    #      must write all six plates and the index, with where they are of.
    #      Git is a stand-in too: the real one would fetch origin into
    #      whatever clone the suite runs in (on 10 Oct 2026 a mutant of the
    #      planner's --depth made the owner's clone shallow that way).
    with tempfile.TemporaryDirectory() as tmp:
        fetch, argv, git_run = si.fetch, sys.argv, si.GIT_RUN
        si.fetch = lambda z, x, y: Image.new("RGB", (256, 256), blue)
        si.GIT_RUN = no_block = FakeGit()
        sys.argv = ["sample_imagery.py", "--lat", "52.12", "--lon", "1.40",
                    "--out", tmp, "--size", "64"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                si.main()
            with open(os.path.join(tmp, "index.json"), encoding="utf-8") as f:
                ran = json.load(f)
        except Exception as e:  # noqa: BLE001
            ran = {"error": repr(e)}
        finally:
            si.fetch, sys.argv, si.GIT_RUN = fetch, argv, git_run
        check(any(c[1:2] == ["fetch"] for c in no_block.calls),
              "sample_imagery's end-to-end run used the real git, not the "
              "stand-in: %r" % no_block.calls)
        check(ran.get("layer") == layer and ran.get("lat") == 52.12
              and ran.get("z13_tile") == [4127, 2701]
              and sorted(ran.get("files", {})) == sorted(
                  n + ".jpg" for n in names),
              "sample_imagery main() did not write the plates and index: %r"
              % ran)

    # 4b''. The plates are fetched the way the packs are: the build's
    #       fetcher, its User-Agent, and a refusal ends the run.
    jpeg = io.BytesIO()
    Image.new("RGB", (256, 256), blue).save(jpeg, format="JPEG")
    real = si.FETCHER
    try:
        clock = _Clock()
        op = _Opener([jpeg.getvalue()])
        si.FETCHER = bs.Fetcher(sharpen=False, opener=op, sleep=clock.sleep,
                                clock=clock)
        img = si.fetch(13, 1, 2)
        check(img.size == (256, 256) and op.calls and
              op.calls[0][1] == bs.USER_AGENT,
              "sample_imagery.fetch does not use the build's fetcher: %r"
              % op.calls)
        si.FETCHER = bs.Fetcher(sharpen=False, retries=2,
                                opener=_Opener([(429, {})] * 5),
                                sleep=clock.sleep, clock=clock)
        try:
            si.fetch(13, 1, 2)
            check(False, "sample_imagery.fetch carried on after a refusal")
        except SystemExit as e:
            check("429" in str(e), "the refusal is not reported: %s" % e)
    except Exception as e:  # noqa: BLE001
        check(False, "sample_imagery.fetch could not be driven: %r" % e)
    finally:
        si.FETCHER = real

    # 4c. A pack published under any other attribution is due NOW, however
    #     young: otherwise the NC 2024 packs stay served for up to 25 days.
    sp = load("satellite_plan")
    fresh = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def area(aid, note):
        return {"id": aid, "label": aid, "bounds": {
            "west": -1, "south": 51, "east": 0, "north": 52},
            "packs": [{"kind": "lanes", "id": aid + "-ways"}] + [
                {"kind": "basemap", "id": "%s-satellite-%s" % (aid, t),
                 "maxZoom": z, "generated": fresh, "note": note}
                for t, z in (("standard", 13), ("high", 14))]}

    old_note = ("Sentinel-2 cloudless 2024 by EOX IT Services GmbH, CC BY "
                "4.0. Contains modified Copernicus Sentinel data 2024.")

    def plan(*areas):
        """satellite_plan's own main(), as the workflow runs it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalogue.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"continents": [{"countries": [
                    {"label": "GB", "areas": list(areas)}]}]}, f)
            argv, out = sys.argv, io.StringIO()
            sys.argv = ["satellite_plan.py", "--catalogue", path]
            try:
                with contextlib.redirect_stdout(out):
                    sp.main()
            except Exception as e:  # noqa: BLE001
                return {"error": repr(e)}
            finally:
                sys.argv = argv
            return json.loads(out.getvalue())

    due = plan(area("gb-a", a), area("gb-b", old_note))
    check(due.get("work") is True and due.get("id") == "gb-b-satellite",
          "a day-old pack under the old attribution is not due: %r" % due)
    idle = plan(area("gb-a", a), area("gb-b", a))
    check(idle.get("work") is False,
          "packs under ATTRIBUTION and a day old were rebuilt: %r" % idle)
    # A pack with no note at all is not known to be the CC BY mosaic.
    bare = area("gb-b", a)
    del bare["packs"][1]["note"]
    due = plan(area("gb-a", a), bare)
    check(due.get("work") is True and due.get("id") == "gb-b-satellite",
          "a day-old pack with no note is not due: %r" % due)
    # The wrong words on only the high-detail pack are enough.
    half = area("gb-b", a)
    half["packs"][2]["note"] = old_note
    due = plan(area("gb-a", a), half)
    check(due.get("work") is True and due.get("id") == "gb-b-satellite",
          "a wrong note on only the high-detail pack is not due: %r" % due)

    # THE ORDER, pinned: imagery served under another licence is replaced
    # first, because it is a live breach; then an area with no imagery at
    # all; then one built below z14; then the oldest. Each area is listed
    # ahead of the one that must beat it.
    never = {"id": "gb-n", "label": "gb-n", "bounds": {
        "west": -1, "south": 51, "east": 0, "north": 52},
        "packs": [{"kind": "lanes", "id": "gb-n-ways"}]}
    low = area("gb-z", a)
    low["packs"] = low["packs"][:2]          # standard only: below z14
    old = area("gb-o", a)
    for p in old["packs"][1:]:
        p["generated"] = "2026-08-01T00:00:00Z"
    for areas, want in (((never, low, old, area("gb-w", old_note)),
                         "gb-w-satellite"),
                        ((low, old, never), "gb-n-satellite"),
                        ((old, low), "gb-z-satellite"),
                        ((area("gb-a", a), old), "gb-o-satellite")):
        due = plan(*areas)
        check(due.get("id") == want,
              "with %s due, the plan took %r, not %s"
              % ([x["id"] for x in areas], due.get("id"), want))

    # 5. The comparison page credits the plates with the same words.
    dp = load("make_detail_page")
    text = re.sub(r"\s+", " ",
                  html.unescape(re.sub(r"<[^>]+>", "", dp.PAGE))
                  .replace(" ", " "))
    check(a in text, "make_detail_page's footer is not ATTRIBUTION")

    # 6. The release notes and commit bodies the workflow writes.
    wf = read(".github/workflows/satellite.yml")
    notes = re.findall(r'^\s*NOTES="([^"]*)"\s*$', wf, re.M)
    check(len(notes) == 1 and notes[0].startswith(a),
          "satellite.yml's release notes are not ATTRIBUTION: %r" % notes)
    # And set on the release that already exists, not only on creating it:
    # the release was made under the 2024 mosaic and kept its notes.
    check(re.search(r'^\s*gh release edit satellite --notes "\$NOTES"\s*$',
                    wf, re.M) is not None,
          "satellite.yml never updates an existing release's notes")
    for n, line in enumerate(wf.splitlines(), 1):
        if re.search(r"cloudless", line, re.I) and year is not None:
            check("%d" % year in line,
                  "satellite.yml:%d names the mosaic without its year %d: %s"
                  % (n, year, line.strip()))

    # 7. The public status page says which mosaic.
    st = load("build_status")
    rows = [r for r in st.DATASETS if r[0] == "imagery"]
    check(len(rows) == 1 and year is not None and "%d" % year in rows[0][3],
          "build_status's imagery row does not name year %s: %r"
          % (year, rows))

    # 8. The builder's own account of the licence cites where it was read.
    for cite in ("WMTSCapabilities.xml", "license-non-commercial",
                 "CC BY-NC-SA 4.0"):
        check(cite in (bs.__doc__ or ""),
              "build_satellite's docstring does not cite %r" % cite)

    # 8b. The dated evidence for the licence relied on: each section of
    #     docs/licences/eox-s2cloudless-2026-10-09.md, by what it must quote.
    try:
        ev = read("docs/licences/eox-s2cloudless-2026-10-09.md")
    except OSError as e:
        ev = ""
        check(False, "the licence evidence is missing: %s" % e)
    blocks = [b for b in re.split(r"\n\s*\n", ev) if b.strip()]
    wants = [
        ("the introduction", ["s2cloudless_3857", "robots.txt"]),
        ("the WMTS abstract", [
            "Source: https://tiles.maps.eox.at/wmts/1.0.0/"
            "WMTSCapabilities.xml", "Fetched: 9 October 2026",
            "> EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
            "(Contains modified Copernicus Sentinel data 2016) released "
            "under &lt;a rel=\"license\" href=\"https://creativecommons.org/"
            "licenses/by/4.0/\"&gt;Creative Commons Attribution 4.0 "
            "International License&lt;/a&gt;."]),
        ("the 2016 licence sentence", [
            "Source: https://cloudless.eox.at/license-non-commercial",
            "Fetched: 9 October 2026",
            "> For the year 2016, EOxCloudless is licensed under the "
            "Creative Commons Attribution 4.0 International License."]),
        ("the required attribution", ["> \"%s\"" % required]
         if required else ["(no layer)"]),
        ("the NonCommercial years", [
            "> For the years 2018 to 2025, EOxCloudless WM(T)S layers is "
            "licensed under the Creative Commons Attribution-NonCommercial-"
            "ShareAlike 4.0 International License."]),
        ("how it is used", ["> " + a]),
    ]
    check(len(blocks) == len(wants),
          "the licence evidence has %d sections, expected %d"
          % (len(blocks), len(wants)))
    for block, (what, needles) in zip(blocks, wants):
        flat = re.sub(r"\s*\n\s*", " ", block)
        for needle in needles:
            check(needle in flat, "the licence evidence's %s lacks %r"
                  % (what, needle))

    # 9. Nothing in the tools, workflows or docs still names a NonCommercial
    #    layer or year as the source, or says that the CC BY licence covers
    #    "the data" as if every year were licensed alike.
    stale = [
        r"(?i)CC(&nbsp;| )BY licence covers the data",
        r"s2cloudless-20(1[7-9]|2\d)_3857",
        r"(?i)cloudless 20(1[7-9]|2\d)\b",
        r"\b20(1[7-9]|2\d) Sentinel-2 cloudless",
        r"Copernicus Sentinel data 20(1[89]|2\d)",
        r"Copernicus Sentinel data 2017[;.)]",
        r"mosaic is 20(1[7-9]|2\d)",
        r"\b20(1[7-9]|2\d) mosaic's CC",
    ]
    me = os.path.basename(__file__)
    paths = []
    for d, exts in (("tools", (".py",)), (".github/workflows", (".yml",)),
                    ("docs", (".md",)), ("", (".md",))):
        full = os.path.join(ROOT, d)
        for name in sorted(os.listdir(full)):
            if name.endswith(exts) and name != me:
                paths.append(os.path.join(d, name) if d else name)
    for rel in paths:
        for n, line in enumerate(read(rel).splitlines(), 1):
            for pat in stale:
                if re.search(pat, line):
                    check(False, "%s:%d still names a NonCommercial mosaic: %s"
                          % (rel, n, line.strip()))

    # 10. EOX's tile service: no bulk terms, but it rate-limits with HTTP
    #     errors and redirects to a "heavyload" page. We never work around
    #     a block.
    service_checks(bs)

    if failures:
        for f in failures:
            print("FAIL:", f)
        print("%d failure(s)" % len(failures))
        return 1
    print("imagery licence: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

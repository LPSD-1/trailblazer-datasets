#!/usr/bin/env python3
"""The ride forecast feed, end to end, offline.

    python tools/test_build_forecast.py

Every request goes to a fake opener that records it, so each of MET Norway's
terms is checked on the request actually built rather than on a constant:
the User-Agent, the four-decimal coordinates, the conditional request, the
throttle. And the two refusals that keep a rider from being told something
untrue are checked on the files actually written: an hour MET did not give is
null and not zero, and a region with no cell at all writes nothing.
"""
import datetime
import email.utils
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_forecast as F  # noqa: E402
import build_pois as PO     # noqa: E402

_passed = 0
_failed = []

NOW = datetime.datetime(2026, 9, 30, 9, 23, tzinfo=datetime.timezone.utc)
START = NOW.replace(minute=0)


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


# ------------------------------------------------------------- fixtures

def compact(updated=NOW - datetime.timedelta(minutes=30), hours=60,
            gap_at=None, rain=0.4):
    """A compact response: hourly entries from START, one with no
    `next_1_hours` at `gap_at`, then a six-hourly tail like MET's."""
    series = []
    for i in range(hours):
        t = START + datetime.timedelta(hours=i)
        data = {"instant": {"details": {"air_temperature": 10.0 + i * 0.1,
                                        "wind_speed": 4.0}}}
        if i != gap_at:
            data["next_1_hours"] = {"details": {"precipitation_amount": rain}}
        series.append({"time": F._iso(t), "data": data})
    for k in range(4):
        t = START + datetime.timedelta(hours=hours + 6 * k)
        series.append({"time": F._iso(t), "data": {
            "instant": {"details": {"air_temperature": 8.0,
                                    "wind_speed": 3.0}},
            "next_6_hours": {"details": {"precipitation_amount": 9.0}}}})
    return {"properties": {"meta": {"updated_at": F._iso(updated)},
                           "timeseries": series}}


class _Response:
    def __init__(self, body, status=200, headers=None):
        self.status = status
        self._raw = json.dumps(body).encode("utf-8")
        self.headers = headers or {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """Answers every point with `respond(lat, lon, request)`; records all."""

    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        return self.respond(float(q["lat"][0]), float(q["lon"][0]), request)


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _http_date(dt):
    return email.utils.format_datetime(dt, usegmt=True)


def build(tmp, opener, region="midlands", clock=None):
    clock = clock or Clock()

    class A:
        pass
    args = A()
    args.region = region
    args.out = os.path.join(tmp, "published", "forecast")
    args.cache = os.path.join(tmp, "cache", "forecast")
    logs = []
    rc = F.do_build(args, opener=opener, sleep=clock.sleep, clock=clock,
                    now=NOW, log=logs.append)
    path = os.path.join(args.out, "%s.json" % region)
    body = json.load(open(path, encoding="utf-8")) \
        if os.path.exists(path) else None
    return rc, body, logs, args


def _tmp():
    d = tempfile.mkdtemp(prefix="tb-forecast-")
    return d


# ------------------------------------------------------------------ tests

def test_selftest_passes():
    out = []
    check("build_forecast --selftest passes", F.selftest(log=out.append) == 0,
          out)


def test_every_request_identifies_us_and_sends_four_decimals():
    tmp = _tmp()
    try:
        opener = Opener(lambda lat, lon, req: _Response(compact()))
        rc, body, _, _ = build(tmp, opener)
        check("the build succeeded", rc == 0)
        # PREMISE: requests were made, one per lattice point in the box.
        expected = len(F.grid_points(PO.REGIONS["midlands"]))
        check("one request per grid point in the region",
              len(opener.requests) == expected,
              "%d != %d" % (len(opener.requests), expected))
        uas = {r.get_header("User-agent") for r in opener.requests}
        check("every request carries the identifying User-Agent",
              uas == {F.USER_AGENT}, repr(uas))
        check("which names the repository, so MET can reach us",
              "github.com/LPSD-1/trailblazer-datasets" in F.USER_AGENT)
        for r in opener.requests:
            parsed = urllib.parse.urlparse(r.full_url)
            check("only api.met.no's compact endpoint is asked",
                  r.full_url.startswith(F.API + "?"), r.full_url)
            q = urllib.parse.parse_qs(parsed.query)
            check("lat and lon and nothing else", set(q) == {"lat", "lon"},
                  r.full_url)
            for v in (q["lat"][0], q["lon"][0]):
                decimals = len(v.split(".")[1]) if "." in v else 0
                check("coordinates carry at most four decimals",
                      decimals <= 4, v)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_requests_are_throttled_to_five_a_second():
    tmp = _tmp()
    try:
        clock = Clock()

        def respond(lat, lon, req):
            stamps.append(clock.t)
            return _Response(compact())
        stamps = []
        build(tmp, Opener(respond), clock=clock)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        check("PREMISE: more than one request", len(stamps) > 1)
        check("no two requests closer than THROTTLE_S",
              all(g >= F.THROTTLE_S - 1e-9 for g in gaps), min(gaps))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_hours_are_written_null_never_zero():
    tmp = _tmp()
    try:
        opener = Opener(lambda lat, lon, req: _Response(
            compact(gap_at=3, hours=40)))
        rc, body, _, _ = build(tmp, opener)
        check("PREMISE: a file was written", body is not None)
        cell = body["cells"][0]
        check("48 slots per series", len(cell["rain_mm"]) == F.HOURS,
              len(cell["rain_mm"]))
        check("a known hour is its figure", cell["rain_mm"][0] == 0.4,
              cell["rain_mm"][:4])
        check("an hour with no next_1_hours is null",
              cell["rain_mm"][3] is None, cell["rain_mm"][:5])
        check("but its wind and temperature are still there",
              cell["wind_ms"][3] == 4.0 and cell["temp_c"][3] is not None)
        check("hours past the hourly part are null, not zero",
              all(v is None for v in cell["rain_mm"][40:]),
              cell["rain_mm"][38:])
        check("and no six-hour total is smeared into a single hour",
              9.0 not in cell["rain_mm"])
        check("no zero stands in for a null anywhere",
              cell["rain_mm"].count(0.0) == 0)
        check("the file says what it is", body["source"] == F.SOURCE and
              "CC BY 4.0" in body["licence"] and body["stale_after_h"] == 12
              and body["step_h"] == 1 and body["hours"] == F.HOURS)
        check("start is the run's hour", body["start"] == F._iso(START))
        check("as_of is MET's own updated_at, not the run clock",
              body["as_of"] == F._iso(NOW - datetime.timedelta(minutes=30)),
              body["as_of"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_region_with_zero_cells_writes_nothing_and_fails():
    tmp = _tmp()
    try:
        out = os.path.join(tmp, "published", "forecast")
        os.makedirs(out)
        before = '{"region":"midlands","cells":[{"lat":1}]}'
        with open(os.path.join(out, "midlands.json"), "w") as fh:
            fh.write(before)

        def fail(lat, lon, req):
            raise urllib.error.URLError("down")
        opener = Opener(fail)
        rc, body, logs, _ = build(tmp, opener)
        check("PREMISE: it tried", len(opener.requests) > 0)
        check("zero cells is a failed run", rc != 0, rc)
        check("and the published file is left as it was",
              open(os.path.join(out, "midlands.json")).read() == before)
        check("and it says why", any("REFUSED" in line for line in logs),
              logs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_unexpired_answer_is_not_asked_for_again():
    tmp = _tmp()
    try:
        later = _http_date(NOW + datetime.timedelta(hours=1))
        first = Opener(lambda lat, lon, req: _Response(
            compact(), headers={"Expires": later,
                                "Last-Modified": _http_date(NOW)}))
        build(tmp, first)
        check("PREMISE: the first run asked", len(first.requests) > 0)
        second = Opener(lambda lat, lon, req: _Response(compact()))
        rc, body, _, _ = build(tmp, second)
        check("an answer MET said holds until later is not re-asked",
              len(second.requests) == 0, len(second.requests))
        check("and the feed is still built from it", rc == 0 and body)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_expired_answer_is_asked_conditionally_and_304_reuses_it():
    tmp = _tmp()
    try:
        modified = _http_date(NOW - datetime.timedelta(hours=1))
        gone = _http_date(NOW - datetime.timedelta(minutes=1))
        first = Opener(lambda lat, lon, req: _Response(
            compact(), headers={"Expires": gone, "Last-Modified": modified}))
        build(tmp, first)

        def not_modified(lat, lon, req):
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified",
                                         {}, io.BytesIO(b""))
        second = Opener(not_modified)
        rc, body, _, _ = build(tmp, second)
        check("PREMISE: the second run asked again", len(second.requests) > 0)
        ims = {r.get_header("If-modified-since") for r in second.requests}
        check("with If-Modified-Since from the cached Last-Modified",
              ims == {modified}, repr(ims))
        check("and a 304 reuses the cached answer", rc == 0 and body and
              len(body["cells"]) == len(second.requests))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_403_stops_the_run_asking():
    tmp = _tmp()
    try:
        def blocked(lat, lon, req):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                         io.BytesIO(b""))
        opener = Opener(blocked)
        rc, _, _, _ = build(tmp, opener)
        check("one 403 and no more requests this run",
              len(opener.requests) == 1, len(opener.requests))
        check("and with nothing cached, the run fails", rc != 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _TimingOpener(Opener):
    """An Opener that also records the timeout each request was given."""

    def __init__(self, respond):
        super().__init__(respond)
        self.timeouts = []

    def __call__(self, request, timeout=None):
        self.timeouts.append(timeout)
        return super().__call__(request, timeout)


def test_an_unreachable_met_stops_the_run_asking_soon():
    # A MET that hangs rather than refuses: every request runs to its timeout.
    # 394 points at a 60-second timeout each is hours; the conditions job's
    # 30-minute limit kills it, and the rain and river feeds - published by
    # later steps of the same job - never go out. So a run of failures in a
    # row stops the asking, and no single request may wait long.
    tmp = _tmp()
    try:
        def hangs(lat, lon, req):
            raise urllib.error.URLError("timed out")
        opener = _TimingOpener(hangs)
        rc, body, logs, _ = build(tmp, opener)
        n = len(opener.requests)
        check("PREMISE: it tried", n > 0)
        check("a MET that does not answer is asked a handful of times, not "
              "for every point", n <= F.MAX_FAILURES_IN_A_ROW, n)
        check("no request may wait more than 30 seconds",
              all(t is not None and t <= 30 for t in opener.timeouts),
              opener.timeouts[:3])
        check("and with nothing cached, the run fails",
              rc != 0 and body is None)
        check("and it says why",
              any("not answering" in str(line) for line in logs), logs[-3:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # A FLAKY POINT IS NOT AN OUTAGE: every third point failing must not stop
    # the run, or one bad minute would cost a region its forecast.
    tmp = _tmp()
    try:
        count = [0]

        def flaky(lat, lon, req):
            count[0] += 1
            if count[0] % 3 == 0:
                raise urllib.error.URLError("timed out")
            return _Response(compact())
        opener = Opener(flaky)
        rc, body, _, _ = build(tmp, opener)
        points = len(F.grid_points(PO.REGIONS["midlands"]))
        check("a scattered failure does not stop the asking",
              len(opener.requests) == points, (len(opener.requests), points))
        check("and the region is built from the rest",
              rc == 0 and body is not None and len(body["cells"]) > 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_forecast_older_than_the_stale_limit_is_not_published():
    tmp = _tmp()
    try:
        old = NOW - datetime.timedelta(hours=F.STALE_AFTER_H + 1)
        opener = Opener(lambda lat, lon, req: _Response(compact(updated=old)))
        rc, body, _, _ = build(tmp, opener)
        check("a cell made 13 hours ago is dropped, so the region has none",
              rc != 0 and body is None, (rc, body and len(body["cells"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_overlapping_regions_share_their_points():
    a = set(F.grid_points(PO.REGIONS["wales"]))
    b = set(F.grid_points(PO.REGIONS["midlands"]))
    check("PREMISE: the boxes overlap", a & b)
    tmp = _tmp()
    try:
        opener = Opener(lambda lat, lon, req: _Response(compact()))

        class A:
            pass
        args = A()
        args.region = None
        args.out = os.path.join(tmp, "out")
        args.cache = os.path.join(tmp, "cache")
        clock = Clock()
        rc = F.do_build(args, opener=opener, sleep=clock.sleep, clock=clock,
                        now=NOW, log=lambda *_: None)
        unique = {p for r in PO.REGIONS.values() for p in F.grid_points(r)}
        check("every region built", rc == 0 and all(os.path.exists(
            os.path.join(args.out, "%s.json" % r)) for r in PO.REGIONS))
        check("each shared point is fetched once a run",
              len(opener.requests) == len(unique),
              "%d requests for %d points" % (len(opener.requests),
                                              len(unique)))
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

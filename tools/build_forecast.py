#!/usr/bin/env python3
"""The weather forecast for a ride, as a static file per region.

    python tools/build_forecast.py build --out published/forecast \
        --cache cache/forecast
    python tools/build_forecast.py build --region midlands --out ...
    python tools/build_forecast.py --selftest

Writes `published/forecast/<region>.json`:

    {"region": "midlands", "as_of": "<UTC ISO>",
     "source": "MET Norway locationforecast 2.0 compact",
     "licence": "Data from MET Norway, CC BY 4.0",
     "stale_after_h": 12, "start": "<UTC ISO hour>", "step_h": 1,
     "hours": 48,
     "cells": [{"lat": 52.5, "lon": -1.6,
                "rain_mm": [...], "wind_ms": [...], "temp_c": [...]}]}

WHY A FILE, AND WHY THIS IS NOT A SERVER.
The app runs no server and never will: riders own their data. It also
promises (DATA-SAFETY.md) that location is "transmitted never", so a phone
asking a weather API for the forecast at its stops is out. MET Norway's own
terms point the same way - "Browsers and mobile apps should not contact the
API directly, but instead use a local proxy ... where you can cache data".
So this does what build_wet.py does for rain gauges: the scheduled job asks
once, for a coarse grid, and publishes a static file on the same host as the
rain and river feeds. Phones download a REGION's file and pick their own cells
out of it. Nothing is received from a phone; nothing about a rider is stored.

MET NORWAY'S TERMS, AND HOW EACH IS MET HERE.
  * Identify yourself: every request carries USER_AGENT, which names this
    repository so MET can reach whoever runs it.
  * Coordinates to at most 4 decimals: `_coord` rounds and the URL is built
    from it; the selftest and the suite both check the URL.
  * Honour Expires and If-Modified-Since: each point's answer is cached with
    both headers; an unexpired answer is not asked for again, and an expired
    one is asked for conditionally, so an unchanged forecast costs a 304.
  * Under 20 requests a second: THROTTLE_S keeps it to 5. A run is ~400
    points, four runs a day.
  * Credit: CC BY 4.0, so `licence` says so in every file and the app shows
    "MET Norway" on every forecast line.

NULL IS UNKNOWN, NEVER ZERO. Any hour MET did not give - past the hourly part
of the forecast, a missing `next_1_hours`, an absent field - is written null.
A zero here would reach a rider as "dry".

ZERO CELLS IS BLIND, NOT A PASS. A region whose every point failed writes
nothing and the run exits non-zero; the file already published stays, and the
app's stale rule takes over from there. A file of `"cells": []` would tell
every rider "no forecast for this place" for six hours, greenly.
"""
import argparse
import datetime
import email.utils
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import build_pois as PO  # noqa: E402  (REGIONS: one table of boxes, not two)

API = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

#: MET Norway requires an identifying User-Agent with a way to reach us.
USER_AGENT = "TrailBlazerDatasets/1.0 github.com/LPSD-1/trailblazer-datasets"

SOURCE = "MET Norway locationforecast 2.0 compact"
LICENCE = "Data from MET Norway, CC BY 4.0"

#: The grid. 0.25 degrees of latitude is ~28 km; 0.4 of longitude is ~27 km
#: at 52N, so the cells are roughly square on the ground. Coarse on purpose: a
#: grid point speaks for a ride's area, not its gateway, and the app says
#: "advisory" and takes the worst cell across a ride's stops.
LAT_STEP = 0.25
LON_STEP = 0.4

#: Hours published per cell, from the hour the run starts.
HOURS = 48

#: The app refuses figures older than this. The job runs every six hours, so
#: one missed run is still usable and two are not.
STALE_AFTER_H = 12

#: Seconds between requests: 5 a second, a quarter of MET's 20/s ceiling.
THROTTLE_S = 0.2

#: MET's coordinate precision rule: no more than four decimals.
COORD_DP = 4

#: Seconds one request may wait. MET answers in well under a second; one that
#: takes longer than this is not going to answer usefully.
REQUEST_TIMEOUT_S = 20

#: Network failures in a row (no HTTP answer at all) after which MET is taken
#: to be down for this run and asked nothing more. WHY: this runs inside the
#: conditions job, whose 30-minute limit also covers publishing the rain and
#: river feeds. A MET that hangs rather than refuses would otherwise cost every
#: one of ~400 points its full timeout, the job would be killed, and the gauges
#: would not publish either. A scattered failure resets the count, so one bad
#: point never costs a region.
MAX_FAILURES_IN_A_ROW = 8


def _coord(x):
    """A coordinate as MET wants it: at most four decimals, no trailing
    zeros, no float noise (0.30000000000000004)."""
    text = ("%.*f" % (COORD_DP, round(float(x), COORD_DP))).rstrip("0")
    text = text.rstrip(".")
    return "0" if text in ("-0", "") else text


def grid_points(bbox):
    """Lattice points inside a region box (W, S, E, N), inclusive.

    SNAPPED TO ONE LATTICE FOR THE WHOLE COUNTRY, not stepped from each box's
    corner, so two regions that overlap (the boxes do, at every border) share
    their points and each is fetched once a run.
    """
    west, south, east, north = bbox
    out = []
    i0 = int(math.ceil(south / LAT_STEP - 1e-9))
    i1 = int(math.floor(north / LAT_STEP + 1e-9))
    j0 = int(math.ceil(west / LON_STEP - 1e-9))
    j1 = int(math.floor(east / LON_STEP + 1e-9))
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            out.append((round(i * LAT_STEP, COORD_DP),
                        round(j * LON_STEP, COORD_DP)))
    return out


def point_url(lat, lon):
    return "%s?lat=%s&lon=%s" % (API, _coord(lat), _coord(lon))


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text):
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _parse_http_date(text):
    if not text:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return round(float(v), 1)
    return None


def hourly(body, start, hours=HOURS):
    """Three series of `hours` slots from `start`, from a compact response.

    Each slot is the entry whose `time` IS that hour, or null. MET goes from
    hourly to six-hourly steps a few days out, and its six-hourly entries
    carry `next_6_hours` and no `next_1_hours`; those hours are null rather
    than a six-hour total smeared across one slot.
    """
    by_time = {}
    series = (((body or {}).get("properties") or {}).get("timeseries")) or []
    for entry in series:
        if not isinstance(entry, dict):
            continue
        at = _parse_iso(entry.get("time"))
        if at is not None:
            by_time[at] = entry.get("data") or {}
    rain, wind, temp = [], [], []
    for i in range(hours):
        data = by_time.get(start + datetime.timedelta(hours=i))
        if not isinstance(data, dict):
            rain.append(None)
            wind.append(None)
            temp.append(None)
            continue
        instant = ((data.get("instant") or {}).get("details")) or {}
        next1 = ((data.get("next_1_hours") or {}).get("details")) or {}
        rain.append(_number(next1.get("precipitation_amount")))
        wind.append(_number(instant.get("wind_speed")))
        temp.append(_number(instant.get("air_temperature")))
    return rain, wind, temp


def updated_at(body):
    meta = (((body or {}).get("properties") or {}).get("meta")) or {}
    return _parse_iso(meta.get("updated_at"))


class Fetcher:
    """Asks MET for one point at a time: throttled, cached, conditional.

    `opener`, `sleep` and `clock` are injectable so the suite can prove the
    headers, the throttle and the cache without the network.
    """

    def __init__(self, cache_dir, opener=None, sleep=time.sleep,
                 clock=time.monotonic, now=None, log=print):
        self.cache_dir = cache_dir
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.clock = clock
        self.now = now or (lambda: datetime.datetime.now(
            datetime.timezone.utc))
        self.log = log
        self._last = None
        self.requests = 0
        self.not_modified = 0
        self.from_cache = 0
        self.failed = 0
        self.blocked = False
        self._in_a_row = 0
        self._memo = {}

    def _cache_path(self, lat, lon):
        return os.path.join(self.cache_dir,
                            "%s_%s.json" % (_coord(lat), _coord(lon)))

    def _read_cache(self, lat, lon):
        try:
            with open(self._cache_path(lat, lon), encoding="utf-8") as fh:
                blob = json.load(fh)
            return blob if isinstance(blob, dict) and "body" in blob else None
        except (OSError, ValueError):
            return None

    def _write_cache(self, lat, lon, blob):
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = self._cache_path(lat, lon) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)
            os.replace(tmp, self._cache_path(lat, lon))
        except OSError:
            pass  # a cache we could not write is a slower run, not a wrong one

    def _throttle(self):
        if self._last is not None:
            wait = THROTTLE_S - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def get(self, lat, lon):
        """The parsed compact body for a point, or None."""
        key = (lat, lon)
        if key in self._memo:
            return self._memo[key]
        cached = self._read_cache(lat, lon)
        expires = _parse_http_date((cached or {}).get("expires"))
        if cached is not None and expires is not None and expires > self.now():
            # MET said this answer holds until `expires`: do not ask again.
            self.from_cache += 1
            self._memo[key] = cached["body"]
            return cached["body"]
        if self.blocked:
            # Refused once this run: stop asking, use what we hold.
            self._memo[key] = (cached or {}).get("body")
            return self._memo[key]
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if cached and cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
        request = urllib.request.Request(point_url(lat, lon), headers=headers)
        self._throttle()
        self.requests += 1
        body = None
        try:
            with self.opener(request, timeout=REQUEST_TIMEOUT_S) as response:
                status = getattr(response, "status", 200)
                raw = response.read()
                info = response.headers
                if status == 304:
                    raise _NotModified(info)
                body = json.loads(raw.decode("utf-8"))
                self._in_a_row = 0
                self._write_cache(lat, lon, {
                    "last_modified": info.get("Last-Modified"),
                    "expires": info.get("Expires"),
                    "body": body,
                })
        except _NotModified as nm:
            body = self._not_modified(lat, lon, cached, nm.headers)
        except urllib.error.HTTPError as error:
            if error.code == 304:
                body = self._not_modified(lat, lon, cached, error.headers)
            else:
                self.failed += 1
                if error.code in (403, 429):
                    # 403 is MET blocking us; 429 is too fast. Either way,
                    # asking the next 400 points is the wrong answer.
                    self.blocked = True
                    self.log("MET Norway answered %d; stopping requests for "
                             "this run" % error.code)
                body = (cached or {}).get("body")
        except Exception as error:  # noqa: BLE001 - one point, not the run
            self.failed += 1
            self.log("  %s,%s: %s" % (_coord(lat), _coord(lon), error))
            self._in_a_row += 1
            if self._in_a_row >= MAX_FAILURES_IN_A_ROW and not self.blocked:
                # Down, not flaky: see MAX_FAILURES_IN_A_ROW.
                self.blocked = True
                self.log("MET Norway is not answering (%d failures in a row); "
                         "stopping requests for this run" % self._in_a_row)
            body = (cached or {}).get("body")
        self._memo[key] = body
        return body

    def _not_modified(self, lat, lon, cached, headers):
        self.not_modified += 1
        self._in_a_row = 0
        if cached is None:
            return None
        if headers is not None and headers.get("Expires"):
            cached["expires"] = headers.get("Expires")
            self._write_cache(lat, lon, cached)
        return cached["body"]


class _NotModified(Exception):
    def __init__(self, headers):
        super().__init__("304")
        self.headers = headers


def feed_body(region, cells, as_of, start):
    return {
        "region": region,
        "as_of": _iso(as_of),
        "source": SOURCE,
        "licence": LICENCE,
        "stale_after_h": STALE_AFTER_H,
        "start": _iso(start),
        "step_h": 1,
        "hours": HOURS,
        "cells": cells,
    }


def build_region(region, fetcher, start, now):
    """The feed body for one region, or None when no cell could be built.

    A cell whose forecast was made more than STALE_AFTER_H before `now` - a
    cached answer from a run long ago, served because MET refused us - is
    DROPPED rather than published: its age would become the file's `as_of`
    and make the whole region stale in the app.
    """
    bbox = PO.REGIONS[region]
    cells, oldest = [], None
    for lat, lon in grid_points(bbox):
        body = fetcher.get(lat, lon)
        if body is None:
            continue
        made = updated_at(body)
        if made is None or (now - made).total_seconds() > STALE_AFTER_H * 3600:
            continue
        rain, wind, temp = hourly(body, start)
        if all(v is None for v in rain + wind + temp):
            continue
        cells.append({"lat": lat, "lon": lon, "rain_mm": rain,
                      "wind_ms": wind, "temp_c": temp})
        if oldest is None or made < oldest:
            oldest = made
    if not cells:
        return None
    # THE OLDEST FORECAST IN THE FILE is the age the app may quote for it.
    return feed_body(region, cells, oldest, start)


def write_feed(body, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s.json" % body["region"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def do_build(args, opener=None, sleep=time.sleep, clock=time.monotonic,
             now=None, log=print):
    regions = [args.region] if args.region else sorted(PO.REGIONS)
    for region in regions:
        if region not in PO.REGIONS:
            raise SystemExit("unknown region %s; one of %s"
                             % (region, ", ".join(sorted(PO.REGIONS))))
    current = now or datetime.datetime.now(datetime.timezone.utc)
    start = current.replace(minute=0, second=0, microsecond=0)
    fetcher = Fetcher(args.cache, opener=opener, sleep=sleep, clock=clock,
                      now=lambda: current, log=log)
    refused = []
    for region in regions:
        body = build_region(region, fetcher, start, current)
        if body is None:
            # ZERO CELLS IS BLIND. Nothing written; the published file stays.
            refused.append(region)
            log("REFUSED: %s has no forecast cell at all; nothing written"
                % region)
            continue
        path = write_feed(body, args.out)
        log("%s: %d cells, as of %s -> %s (%d bytes)"
            % (region, len(body["cells"]), body["as_of"], path,
               os.path.getsize(path)))
    log("requests %d (304: %d), from cache %d, failed %d"
        % (fetcher.requests, fetcher.not_modified, fetcher.from_cache,
           fetcher.failed))
    return 1 if refused else 0


# ------------------------------------------------------------- selftest

def selftest(log=print):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s%s" % (name, (": " + detail) if detail else ""))

    check("coordinates carry at most four decimals",
          _coord(52.123456) == "52.1235", _coord(52.123456))
    check("float noise does not reach the URL",
          _coord(0.1 + 0.2) == "0.3", _coord(0.1 + 0.2))
    check("a whole degree is written plainly", _coord(-2.0) == "-2")
    url = point_url(52.5, -1.6)
    check("the URL is the compact endpoint with lat and lon only",
          url == API + "?lat=52.5&lon=-1.6", url)

    pts = grid_points((-1.0, 52.0, 0.0, 52.5))
    check("the grid covers the box on the lattice",
          (52.0, -0.8) in pts and (52.5, 0.0) in pts, repr(pts))
    check("and nothing outside it",
          all(52.0 <= a <= 52.5 and -1.0 <= b <= 0.0 for a, b in pts))
    total = len({p for r in PO.REGIONS.values() for p in grid_points(r)})
    check("the country is a few hundred points, not thousands",
          100 < total < 700, str(total))

    t0 = datetime.datetime(2026, 9, 30, 6, tzinfo=datetime.timezone.utc)
    body = {"properties": {"meta": {"updated_at": "2026-09-30T05:40:00Z"},
                           "timeseries": [
        {"time": "2026-09-30T06:00:00Z", "data": {
            "instant": {"details": {"air_temperature": 9.04,
                                    "wind_speed": 3.2}},
            "next_1_hours": {"details": {"precipitation_amount": 0.0}}}},
        {"time": "2026-09-30T07:00:00Z", "data": {
            "instant": {"details": {"air_temperature": 9.5,
                                    "wind_speed": 3.9}}}},
        {"time": "2026-09-30T09:00:00Z", "data": {
            "instant": {"details": {"air_temperature": 10.0,
                                    "wind_speed": 4.0}},
            "next_6_hours": {"details": {"precipitation_amount": 6.0}}}},
    ]}}
    rain, wind, temp = hourly(body, t0, hours=4)
    check("a real zero stays zero", rain[0] == 0.0, repr(rain))
    check("a missing next_1_hours is null, not zero", rain[1] is None,
          repr(rain))
    check("an hour with no entry at all is null", rain[2] is None and
          wind[2] is None and temp[2] is None, repr((rain, wind, temp)))
    check("a six-hour total is not smeared into one hour", rain[3] is None,
          repr(rain))
    check("values are rounded to a tenth", temp[0] == 9.0, repr(temp))

    for failure in failures:
        log("  FAIL " + failure)
    log("selftest: %s" % ("FAILED %d" % len(failures) if failures else "ok"))
    return 1 if failures else 0


def parse_args(argv):
    parser = argparse.ArgumentParser(description="ride forecast feeds")
    parser.add_argument("--selftest", action="store_true")
    sub = parser.add_subparsers(dest="command")
    b = sub.add_parser("build", help="fetch MET Norway and write the feeds")
    b.add_argument("--region", help="one region; default every region")
    b.add_argument("--out", default="published/forecast")
    b.add_argument("--cache", default="cache/forecast")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.selftest:
        return selftest()
    if args.command == "build":
        return do_build(args)
    raise SystemExit("nothing to do; --selftest or build")


if __name__ == "__main__":
    sys.exit(main())

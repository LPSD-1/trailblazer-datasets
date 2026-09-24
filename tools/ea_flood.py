#!/usr/bin/env python3
"""The Environment Agency's real-time flood-monitoring API, and the one
geometric question two features ask of it: *which gauge, and how far away?*

    python tools/ea_flood.py stations --parameter rainfall --out cache/ea
    python tools/ea_flood.py readings --parameter rainfall --days 2 \
        --stations cache/ea/stations-rainfall.json --out cache/ea
    python tools/ea_flood.py --selftest        # offline, no network

WHY A SHARED FILE. Spec 9.6 C (wet-weather restraint) and 9.6 G (fords and
river levels) are the same pipeline pointed at two parameters: find the EA
stations, read them, tie each of our features to the NEAREST one, and carry the
distance. Written twice they would drift, and the half that matters most - the
distance, and what it licenses us to say - is exactly the half that gets
dropped on the second writing.

THE LICENCE. environment.data.gov.uk/flood-monitoring is Open Government
Licence v3, needs no key and no registration. ~1,400 gauging stations and
~1,800 level-only sites, updated every 15 minutes (spec 9.6 G).

DISTANCE IS PART OF THE ANSWER, NOT METADATA.
Gauges are sparse and essentially never at the ford. Spec 9.6 G says it in as
many words: "a gauge ten miles downstream says less than one above the ford".
So `nearest` returns a (station, metres) pair and every caller is expected to
carry the metres through to whatever the rider reads. A tool that returned only
the station id would be handing the app a measurement it cannot qualify.

NULL IS NOT DRY.
Two absences are represented and neither may be read as a reassurance:

  * no station within range        -> `nearest` returns (None, None)
  * a station with no readings     -> its entry carries `mm` = None, not 0.0

A station that stopped reporting looks exactly like a dry one if you let a
missing total default to zero, and the direction of that error is a rider told
a soft lane is fine. Every accumulator here distinguishes "summed nothing" from
"summed zero", and `readings_count` is carried so the app can tell them apart
too.

JSON-LD SHAPES, AND WHY THE PARSING LOOKS PARANOID.
The EA serves JSON-LD, which collapses a one-element array to a bare value. The
same field is a list on one station and a string on the next - `label`,
`measures` and `riverName` all do this - so every read goes through `_as_list`
or `_first`. This is not defensive style for its own sake: a build that crashed
on the one station in a region with two river names would take the whole
region's fords down with it.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://environment.data.gov.uk/flood-monitoring"
USER_AGENT = "trailblazer-datasets/1.0 (+https://trailblazer.app; EA flood)"

#: Mean Earth radius, IUGG. Metres.
EARTH_R_M = 6371008.8

#: The API pages; 10,000 is the documented ceiling for `_limit`. A national
#: day of 15-minute rainfall is ~300,000 readings, so this is 30 requests
#: rather than 300.
PAGE = 10000

#: A tipping-bucket gauge reports millimetres in a 15-minute period. The UK
#: 15-minute record is under 40 mm; anything past this is a sensor fault or a
#: unit error, and summing it would put a whole region into "do not ride".
#: Rejected values are COUNTED, not silently dropped - see `accumulate`.
MAX_SANE_15MIN_MM = 60.0


# --------------------------------------------------------------- JSON-LD

def _as_list(value):
    """JSON-LD collapses a one-element array to the element. Undo that."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _first(value, key=None):
    """The first of a maybe-collapsed array, optionally a field inside it."""
    items = _as_list(value)
    if not items:
        return None
    item = items[0]
    if key is None:
        return item
    if isinstance(item, dict):
        return item.get(key)
    return None


def _number(value):
    """A float, or None. `None` and `""` are both "the source said nothing"."""
    if value is None or value is True or value is False:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _measure_id(measure):
    """A measure reference, as either a URI string or an object carrying one."""
    if isinstance(measure, dict):
        return measure.get("@id") or measure.get("notation")
    if isinstance(measure, str):
        return measure
    return None


# ------------------------------------------------------------------ geo

def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle metres between two WGS84 points.

    Haversine and not equirectangular: the error of the flat approximation at
    British latitudes reaches ~0.3% at 50 km, and these distances are shown to
    a rider as "12 km away" to justify NOT trusting a reading. A distance used
    to argue for doubt has to be right.
    """
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2.0) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2)
    return 2.0 * EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


class StationIndex(object):
    """Nearest-station lookup over a few thousand stations.

    WHY AN INDEX AT ALL. `build_wet` asks this once per way: 10,342 ways
    against ~3,200 stations is 33 million haversines, which is about half a
    minute of Python per run of a job that runs four times a day.

    WHY THE RING EXPANSION LOOKS OVER-CAREFUL. The obvious version - look in
    the point's own cell, then the eight around it, then stop - is WRONG in the
    case that matters. A ford sitting just inside the eastern edge of its cell
    has its true nearest gauge one cell east at 300 m, while a gauge in its own
    cell sits 12 km north; the naive search finds the 12 km one first and
    returns it, and the answer the rider sees changes from "a gauge by the
    ford" to "a hint from the next valley". So the ring keeps growing until the
    ring's own inner radius exceeds the best distance found, which is the only
    stopping rule that cannot miss a nearer station.
    """

    #: Chosen against the data, not for neatness: ~3,200 stations over England
    #: and Wales is ~0.5 per 0.25-degree cell, so a hit is usually found in the
    #: first two rings, and a cell is ~28 km north-south - comfortably larger
    #: than any distance we would call "near the ford".
    CELL_DEG = 0.25

    #: A ford with no gauge inside this is a ford with no gauge. 50 km is well
    #: past useful - spec 9.6 G already calls ten miles "says less" - but the
    #: cutoff is a termination bound, not a usefulness one, and the caller
    #: decides what distance it will still show.
    MAX_M = 50000.0

    def __init__(self, stations, cell_deg=CELL_DEG, max_m=MAX_M):
        self.cell_deg = cell_deg
        self.max_m = max_m
        self.cells = {}
        self.stations = []
        for station in stations:
            if station.get("lat") is None or station.get("lon") is None:
                # A station with no position cannot answer "how far"; keeping
                # it would let it win a nearest search at distance zero.
                continue
            self.stations.append(station)
            self.cells.setdefault(self._cell(station["lat"], station["lon"]),
                                  []).append(station)

    def _cell(self, lat, lon):
        return (int(math.floor(lat / self.cell_deg)),
                int(math.floor(lon / self.cell_deg)))

    def _ring(self, cy, cx, r):
        if r == 0:
            yield (cy, cx)
            return
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dy), abs(dx)) == r:
                    yield (cy + dy, cx + dx)

    def nearest(self, lat, lon):
        """(station, metres), or (None, None) when nothing is within MAX_M."""
        if lat is None or lon is None:
            return None, None
        cy, cx = self._cell(lat, lon)
        # The metre width of one cell at THIS latitude, which is the shorter of
        # the two axes going north. Using the north-south width (~28 km) as the
        # guarantee would overstate how far a ring reaches in longitude at 55N
        # and let the loop stop one ring early.
        lon_m = self.cell_deg * 111320.0 * max(0.05, math.cos(math.radians(lat)))
        lat_m = self.cell_deg * 110540.0
        step_m = min(lon_m, lat_m)
        best, best_m = None, None
        r = 0
        max_r = int(math.ceil(self.max_m / step_m)) + 1
        while r <= max_r:
            for cell in self._ring(cy, cx, r):
                for station in self.cells.get(cell, ()):
                    metres = haversine_m(lat, lon, station["lat"],
                                         station["lon"])
                    if best_m is None or metres < best_m:
                        best, best_m = station, metres
            # Everything outside ring r is at least (r * step_m) away, so once
            # the best found beats that, no later ring can beat it.
            if best_m is not None and best_m <= r * step_m:
                break
            r += 1
        if best_m is None or best_m > self.max_m:
            return None, None
        return best, best_m


def nearest_brute(lat, lon, stations, max_m=StationIndex.MAX_M):
    """The obvious O(n) answer. Kept because it is what the index is TESTED
    against - an index that is fast and wrong is worse than no index."""
    best, best_m = None, None
    for station in stations:
        if station.get("lat") is None or station.get("lon") is None:
            continue
        metres = haversine_m(lat, lon, station["lat"], station["lon"])
        if best_m is None or metres < best_m:
            best, best_m = station, metres
    if best_m is None or best_m > max_m:
        return None, None
    return best, best_m


# -------------------------------------------------------------- fetching

def _get(url, opener=None, attempts=3, pause=5.0):
    opener = opener or urllib.request.urlopen
    last = None
    for attempt in range(attempts):
        request = urllib.request.Request(url,
                                         headers={"User-Agent": USER_AGENT})
        try:
            with opener(request, timeout=300) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:      # noqa: BLE001 - retried or re-raised
            last = error
            if attempt + 1 < attempts:
                time.sleep(pause * (attempt + 1))
    raise last


def parse_station(raw):
    """One station, in our shape. None when it cannot be placed on the map.

    `stageScale.typicalRangeLow/High` is what turns a level into the sentence
    spec 9.6 G actually wants - "0.8 m above normal" rather than "1.9 mAOD",
    which means nothing to a rider. It is frequently ABSENT, and an absent
    typical range must stay None: inventing one would let the app announce a
    river is high when nobody ever said what normal was.
    """
    notation = raw.get("notation") or raw.get("stationReference")
    if not notation:
        return None
    lat = _number(raw.get("lat"))
    lon = _number(raw.get("long"))
    if lon is None:
        lon = _number(raw.get("lon"))
    scale = raw.get("stageScale")
    if isinstance(scale, list):
        scale = scale[0] if scale else None
    if not isinstance(scale, dict):
        scale = {}
    label = _first(raw.get("label"))
    return {
        "id": str(notation),
        "label": label if isinstance(label, str) else None,
        "lat": lat,
        "lon": lon,
        "river": _first(raw.get("riverName")),
        "catchment": _first(raw.get("catchmentName")),
        "typical_low_m": _number(scale.get("typicalRangeLow")),
        "typical_high_m": _number(scale.get("typicalRangeHigh")),
        "measures": [m for m in (_measure_id(x)
                                 for x in _as_list(raw.get("measures"))) if m],
    }


def fetch_stations(parameter, opener=None, base=BASE, limit=PAGE):
    """Every station reporting `parameter` ('rainfall' or 'level').

    Sorted by id so two runs against an unchanged API produce one file - the
    API's own order is not stable and a reordered cache would republish a
    container that did not change.

    `_view=full` IS NOT OPTIONAL, and this was measured against the live API on
    2026-09-24. Without it the listing serves `stageScale` as a bare URI
    string, so `typicalRangeHigh` is not there to read: the first run of this
    function reported "3600 level stations, 3600 placed, **0 with a typical
    range**". Nothing errored. Every ford in England would have carried a gauge
    with no normal to compare against, and spec 9.6 G's whole sentence - "the
    Dove is running 0.8 m above normal" - would have been unsayable, silently.
    """
    url = ("%s/id/stations?parameter=%s&_view=full&_limit=%d"
           % (base, parameter, limit))
    payload = _get(url, opener=opener)
    out = {}
    for raw in payload.get("items", []):
        station = parse_station(raw)
        if station is None or station["lat"] is None or station["lon"] is None:
            continue
        out[station["id"]] = station
    return [out[key] for key in sorted(out)]


def measure_index(stations):
    """{measure id: station id}.

    Built from the STATION LIST and not by splitting the measure URI on its
    first hyphen. The obvious split works for `E7050-rainfall-...` and fails
    for the several hundred stations whose notation itself contains a hyphen,
    and it fails silently - the reading is attributed to a station that does
    not exist, so the real station looks like it stopped reporting, which is
    exactly the failure this file refuses to let look like "dry".
    """
    out = {}
    for station in stations:
        for measure in station["measures"]:
            out[measure] = station["id"]
    return out


def fetch_readings(parameter, since, until=None, opener=None, base=BASE,
                   limit=PAGE, max_pages=200, log=None):
    """Raw readings for `parameter` covering `since` (a UTC datetime) to now.

    `startdate`/`enddate` AND NOT `since`, measured against the live API on
    2026-09-24. `/data/readings?...&since=2026-09-23T00:00:00Z` answers **HTTP
    400**; `startdate=2026-09-23&enddate=2026-09-24` answers normally. `since`
    is accepted elsewhere in the same API, which is exactly why the wrong one
    looks right - and a 400 here would have taken the whole wet feed down every
    time it ran, while every offline test passed.

    The two parameters are WHOLE DAYS, so this fetches whole days and
    `accumulate` does the hour-level window filtering. Fetching a few extra
    hours costs one more page; guessing at the window boundary would cost a
    silently short 48-hour total.

    THE LOOP STOPS ON AN EMPTY PAGE, NOT A SHORT ONE, AND THIS WAS MEASURED.
    The obvious rule - "a page smaller than `_limit` is the last page" - is
    what every paged API teaches, and it is WRONG here. Against the live API on
    2026-09-24, over `startdate=2026-09-22&enddate=2026-09-24`:

        _offset=40000   ->  9,999 items      (a short page, mid-stream)
        _offset=49000   -> 10,000 items
        _offset=200000  -> 10,000 items
        _offset=250000  ->      0 items      (the actual end)

    The short-page rule stopped the first real run at 49,999 readings over
    **213 of 1,041 rainfall stations**. Nothing failed. The other 828 gauges
    came back silent, and only this file's "silent is unknown, never dry" rule
    stopped that from reading as four-fifths of England having had no rain.
    """
    until = until or datetime.now(timezone.utc)
    start = since.astimezone(timezone.utc).date().isoformat()
    end = until.astimezone(timezone.utc).date().isoformat()
    out = []
    offset = 0
    for _page in range(max_pages):
        url = ("%s/data/readings?parameter=%s&startdate=%s&enddate=%s"
               "&_limit=%d&_offset=%d"
               % (base, parameter, start, end, limit, offset))
        payload = _get(url, opener=opener)
        items = payload.get("items", [])
        out.extend(items)
        if log:
            log("    readings %s +%d -> %d" % (parameter, offset, len(items)))
        if not items:
            break
        offset += limit
    else:
        # Reached only when `max_pages` ran out before an empty page. Said out
        # loud rather than returned quietly: a truncated fetch is a region of
        # gauges reported as silent, and silence is what this whole file
        # refuses to let read as good news.
        raise SystemExit(
            "readings for %s did not end within %d pages of %d - the fetch is "
            "truncated and would report %d stations as silent"
            % (parameter, max_pages, limit,
               len(set(_measure_id(i.get("measure")) for i in out))))
    return out


def fetch_latest(parameter, opener=None, base=BASE, limit=PAGE,
                 max_pages=200, log=None):
    """The most recent reading per measure, in one cheap sweep.

    THE RIGHT FETCH FOR A LEVEL, AND THE WRONG ONE FOR RAINFALL. A river level
    is a STATE - only the latest value means anything, and asking for two days
    of 15-minute levels across 3,600 stations is ~700,000 readings to throw all
    but 3,600 of away. Rainfall is an ACCUMULATION and cannot use this: the
    latest tip is one bucket, not two days of rain.

    Same empty-page stopping rule as `fetch_readings`, for the same measured
    reason.
    """
    out = []
    offset = 0
    for _page in range(max_pages):
        url = ("%s/data/readings?parameter=%s&latest&_limit=%d&_offset=%d"
               % (base, parameter, limit, offset))
        payload = _get(url, opener=opener)
        items = payload.get("items", [])
        out.extend(items)
        if log:
            log("    latest %s +%d -> %d" % (parameter, offset, len(items)))
        if not items:
            break
        offset += limit
    else:
        raise SystemExit("latest %s readings did not end within %d pages"
                         % (parameter, max_pages))
    return out


def _iso(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_when(text):
    """An EA `dateTime`, as an aware UTC datetime, or None."""
    if not isinstance(text, str):
        return None
    body = text.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(body)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def accumulate(readings, index, now, windows_h=(24, 48), latest=False):
    """Per station: the sum over each window, the count, and the last time.

    `windows_h` are hours back from `now`. A station that appears in the
    readings but has nothing inside a window gets None for that window, NOT
    0.0 - see the file header. `latest=True` instead keeps the most recent
    value per station, which is what a river LEVEL is (a level is a state, not
    something you add up).

    Unattributable readings - a measure the station list does not name - are
    counted and returned rather than dropped, because that count going up is
    how we would learn the station list and the readings had drifted apart.
    """
    out = {}
    dropped = {"unknown_measure": 0, "no_time": 0, "no_value": 0, "insane": 0}
    edges = [(hours, now - timedelta(hours=hours)) for hours in windows_h]
    for item in readings:
        station_id = index.get(_measure_id(item.get("measure")))
        if station_id is None:
            dropped["unknown_measure"] += 1
            continue
        when = parse_when(item.get("dateTime"))
        if when is None:
            dropped["no_time"] += 1
            continue
        value = _number(item.get("value"))
        if value is None:
            dropped["no_value"] += 1
            continue
        if not latest and (value < 0.0 or value > MAX_SANE_15MIN_MM):
            dropped["insane"] += 1
            continue
        entry = out.setdefault(station_id, {
            "sums": {hours: None for hours, _ in edges},
            "count": 0, "latest": None, "latest_at": None})
        entry["count"] += 1
        if entry["latest_at"] is None or when > entry["latest_at"]:
            entry["latest_at"] = when
            entry["latest"] = value
        if not latest:
            for hours, edge in edges:
                if when >= edge:
                    current = entry["sums"][hours]
                    entry["sums"][hours] = value + (0.0 if current is None
                                                    else current)
    return out, dropped


def totals(accumulated, hours):
    """{station id: millimetres in the last `hours`}, None where unknown."""
    return {sid: entry["sums"].get(hours)
            for sid, entry in accumulated.items()}


# ------------------------------------------------------------------- cli

def stations_path(out_dir, parameter):
    return os.path.join(out_dir, "stations-%s.json" % parameter)


def readings_path(out_dir, parameter):
    return os.path.join(out_dir, "readings-%s.json" % parameter)


def do_stations(args, log=print, opener=None):
    stations = fetch_stations(args.parameter, opener=opener)
    os.makedirs(args.out, exist_ok=True)
    path = stations_path(args.out, args.parameter)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"parameter": args.parameter,
                   "fetched_at": _iso(datetime.now(timezone.utc)),
                   "stations": stations}, handle, indent=1, sort_keys=True)
    placed = sum(1 for s in stations if s["lat"] is not None)
    ranged = sum(1 for s in stations if s["typical_high_m"] is not None)
    log("%d %s stations, %d placed, %d with a typical range -> %s"
        % (len(stations), args.parameter, placed, ranged, path))
    return 0


def do_readings(args, log=print, opener=None):
    with open(args.stations, encoding="utf-8") as handle:
        stations = json.load(handle)["stations"]
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)
    # Rainfall is accumulated and needs the window; a level is a state and
    # needs only the latest reading. See `fetch_latest`.
    is_rain = args.parameter == "rainfall"
    if is_rain:
        raw = fetch_readings(args.parameter, since, until=now, opener=opener,
                             log=log)
    else:
        raw = fetch_latest(args.parameter, opener=opener, log=log)
    index = measure_index(stations)
    acc, dropped = accumulate(raw, index, now, windows_h=(24, 48),
                              latest=not is_rain)
    os.makedirs(args.out, exist_ok=True)
    path = readings_path(args.out, args.parameter)
    body = {}
    for sid, entry in sorted(acc.items()):
        body[sid] = {
            "mm_24h": entry["sums"].get(24),
            "mm_48h": entry["sums"].get(48),
            "latest": entry["latest"],
            "latest_at": _iso(entry["latest_at"]) if entry["latest_at"]
            else None,
            "readings": entry["count"],
        }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"parameter": args.parameter, "as_of": _iso(now),
                   "since": _iso(since), "dropped": dropped,
                   "stations": body}, handle, indent=1, sort_keys=True)
    log("%d readings over %d stations, dropped %s -> %s"
        % (len(raw), len(body), dropped, path))
    return 0


def selftest(log=print):
    """Offline. Proves the two things a caller cannot check for itself: that
    the index agrees with brute force, and that nothing missing reads as zero.
    """
    import random
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s%s" % (name, (": " + detail) if detail else ""))

    rng = random.Random(20260924)
    stations = [{"id": "s%d" % i, "lat": 50.0 + rng.random() * 5.5,
                 "lon": -5.0 + rng.random() * 6.5, "measures": []}
                for i in range(400)]
    index = StationIndex(stations)
    worst = 0.0
    for _ in range(300):
        lat = 50.0 + rng.random() * 5.5
        lon = -5.0 + rng.random() * 6.5
        got, got_m = index.nearest(lat, lon)
        want, want_m = nearest_brute(lat, lon, stations)
        if want_m is None:
            check("index agrees when nothing is near", got is None)
            continue
        worst = max(worst, abs((got_m or 0.0) - want_m))
        check("index agrees with brute force",
              got is not None and abs(got_m - want_m) < 1e-6,
              "at %.3f,%.3f got %s want %s" % (lat, lon, got_m, want_m))
    check("no drift between index and brute force", worst < 1e-6,
          "worst %g m" % worst)

    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    idx = {"m-a": "A", "m-b": "B"}
    acc, dropped = accumulate([
        {"measure": "m-a", "dateTime": "2026-09-24T11:45:00Z", "value": 0.2},
        {"measure": "m-a", "dateTime": "2026-09-22T11:45:00Z", "value": 9.9},
        {"measure": "m-b", "dateTime": "2026-09-24T11:45:00Z", "value": None},
        {"measure": "m-z", "dateTime": "2026-09-24T11:45:00Z", "value": 1.0},
    ], idx, now)
    check("a reading outside every window does not enter a total",
          abs(acc["A"]["sums"][24] - 0.2) < 1e-9, repr(acc["A"]))
    check("a 48h window excludes a 49h-old reading",
          abs(acc["A"]["sums"][48] - 0.2) < 1e-9, repr(acc["A"]))
    check("a station whose only reading has no value sums to None, not 0",
          "B" not in acc or acc["B"]["sums"][24] is None, repr(acc.get("B")))
    check("an unattributable reading is counted, not dropped quietly",
          dropped["unknown_measure"] == 1, repr(dropped))

    # JSON-LD collapse, on the three fields that actually do it.
    station = parse_station({"notation": "E7050", "lat": 53.0, "long": -1.6,
                             "label": ["Foo", "Bar"], "riverName": "Dove",
                             "measures": {"@id": "m-1"},
                             "stageScale": {"typicalRangeHigh": 1.2}})
    check("a collapsed measures object still yields a measure",
          station["measures"] == ["m-1"], repr(station["measures"]))
    check("a two-element label does not crash and takes the first",
          station["label"] == "Foo", repr(station["label"]))
    check("an absent typical LOW stays None rather than becoming 0",
          station["typical_low_m"] is None, repr(station["typical_low_m"]))

    for failure in failures:
        log("  FAIL " + failure)
    log("selftest: %s" % ("FAILED %d" % len(failures) if failures else "ok"))
    return 1 if failures else 0


def parse_args(argv):
    parser = argparse.ArgumentParser(description="EA flood-monitoring client")
    parser.add_argument("--selftest", action="store_true")
    sub = parser.add_subparsers(dest="command")

    s = sub.add_parser("stations", help="cache the station list")
    s.add_argument("--parameter", default="rainfall",
                   choices=("rainfall", "level"))
    s.add_argument("--out", default="cache/ea")

    r = sub.add_parser("readings", help="cache recent readings")
    r.add_argument("--parameter", default="rainfall",
                   choices=("rainfall", "level"))
    r.add_argument("--stations", required=True)
    r.add_argument("--days", type=int, default=2)
    r.add_argument("--out", default="cache/ea")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.selftest:
        return selftest()
    if args.command == "stations":
        return do_stations(args)
    if args.command == "readings":
        return do_readings(args)
    raise SystemExit("nothing to do; --selftest, stations or readings")


if __name__ == "__main__":
    sys.exit(main())

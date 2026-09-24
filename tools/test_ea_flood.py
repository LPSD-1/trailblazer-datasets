#!/usr/bin/env python3
"""The EA client, offline.

Nothing here touches the network: every fetch is driven through an injected
opener holding a canned payload, so the suite is the same on a laptop with no
signal as it is in CI. The payloads are the SHAPES the EA actually serves,
including the JSON-LD collapse - a test built from a tidied-up version of the
API would pass while the real one crashed.

PREMISE FIRST. Several tests below assert an absence - a total that stays None,
a station that is not returned. Each is preceded by an assertion that the same
machinery CAN produce the thing, because an absence test over an empty result
passes for the wrong reason and goes on passing for ever.
"""
import datetime
import io
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ea_flood as E               # noqa: E402

_passed = 0
_failed = []

NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.timezone.utc)


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


class _Response(object):
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def opener_for(pages):
    """An opener serving `pages` in order, recording the URLs it was asked."""
    seen = []
    queue = list(pages)

    def _open(request, timeout=None):
        seen.append(request.get_full_url())
        return _Response(queue.pop(0) if queue else {"items": []})

    _open.seen = seen
    return _open


def station(notation, lat, lon, **extra):
    raw = {"notation": notation, "lat": lat, "long": lon,
           "measures": ["%s-rainfall-t-15_min-mm" % notation]}
    raw.update(extra)
    return raw


# ------------------------------------------------------------- distance

def test_haversine_matches_a_known_pair():
    """Sanity anchor: the Ordnance Survey distance from Buxton to Bakewell is
    ~17.0 km great-circle. A haversine with a radians/degrees slip is out by a
    factor of 57, which this catches and an eyeball does not."""
    metres = E.haversine_m(53.2590, -1.9110, 53.2130, -1.6750)
    check("Buxton to Bakewell is about 17 km",
          16000 < metres < 18000, "%.0f m" % metres)


def test_distance_is_symmetric_and_zero_on_itself():
    check("a point is zero from itself",
          E.haversine_m(53.0, -1.6, 53.0, -1.6) == 0.0)
    check("distance is symmetric",
          abs(E.haversine_m(53.0, -1.6, 52.0, -2.0)
              - E.haversine_m(52.0, -2.0, 53.0, -1.6)) < 1e-6)


def test_the_index_never_disagrees_with_brute_force():
    """The whole reason the index may exist. 2,000 random probes against 600
    random stations over the England-and-Wales box; any disagreement at all is
    a ford tied to the wrong river."""
    rng = random.Random(7)
    stations = [{"id": "s%d" % i, "lat": 49.9 + rng.random() * 6.0,
                 "lon": -5.4 + rng.random() * 7.3} for i in range(600)]
    index = E.StationIndex(stations)
    worst = 0.0
    disagreed = 0
    for _ in range(2000):
        lat = 49.9 + rng.random() * 6.0
        lon = -5.4 + rng.random() * 7.3
        got, got_m = index.nearest(lat, lon)
        want, want_m = E.nearest_brute(lat, lon, stations)
        if (got is None) != (want is None):
            disagreed += 1
            continue
        if want is None:
            continue
        worst = max(worst, abs(got_m - want_m))
        if got["id"] != want["id"] and abs(got_m - want_m) > 1e-9:
            disagreed += 1
    check("the index picks the same station as brute force every time",
          disagreed == 0, "%d disagreements" % disagreed)
    check("and the same distance", worst < 1e-6, "worst %g m" % worst)


def test_a_station_just_over_a_cell_edge_still_wins():
    """The exact case the naive one-ring search gets wrong, pinned as its own
    test so a future 'optimisation' has to argue with it.

    The probe sits a hair inside a cell edge. Its true nearest gauge is 300 m
    away in the NEXT cell; a decoy 12 km north shares its own cell. A search
    that stops at the first cell holding anything returns the decoy.
    """
    edge = 53.0                       # a 0.25-degree cell boundary
    probe_lat = edge - 0.001
    near = {"id": "near", "lat": edge + 0.002, "lon": -1.6}
    far = {"id": "far", "lat": probe_lat - 0.11, "lon": -1.6}
    index = E.StationIndex([far, near])
    # PREMISE: the decoy really is findable, so "it found `near`" is a choice
    # and not an empty search.
    only_far, only_far_m = E.StationIndex([far]).nearest(probe_lat, -1.6)
    check("the decoy is reachable on its own",
          only_far is not None and only_far["id"] == "far",
          repr(only_far))
    check("and it really is the far one", 10000 < only_far_m < 14000,
          "%.0f m" % only_far_m)
    got, got_m = index.nearest(probe_lat, -1.6)
    check("a gauge over the cell edge beats one in the same cell",
          got is not None and got["id"] == "near", repr(got))
    check("and the distance reported is the near one",
          got_m is not None and got_m < 500, "%.0f m" % (got_m or -1))


def test_nothing_within_range_is_none_and_not_the_far_thing():
    """A ford with no gauge must come back as no gauge. Returning the nearest
    thing regardless of distance is how "the Dove is 0.8 m up" gets said about
    a river in the next county."""
    far = [{"id": "far", "lat": 58.0, "lon": -4.0}]
    # PREMISE: this station is found when the probe is beside it.
    close, _ = E.StationIndex(far).nearest(58.001, -4.0)
    check("the lone station is findable when it is near",
          close is not None and close["id"] == "far", repr(close))
    got, metres = E.StationIndex(far).nearest(50.5, -3.5)
    check("but a station 800 km away is not an answer", got is None,
          repr(got))
    check("and no distance is offered either", metres is None, repr(metres))


def test_a_station_the_ring_search_reaches_but_max_m_forbids_is_refused():
    """MAX_M has to be enforced where the answer is FOUND, not only by how far
    the rings go.

    The ring bound is a square and MAX_M is a circle, so a station on the
    diagonal is inside the last ring and outside the range. Without the range
    check on the result, a gauge 53 km from a ford comes back as that ford's
    gauge - and the app says the river is up about a catchment two valleys
    away. A search-bound test alone (a station 800 km off) never exercises
    this, because the rings never reach it.
    """
    probe = (53.0, -1.6)
    inside = {"id": "in", "lat": 53.30, "lon": -1.6}          # ~33 km north
    diagonal = {"id": "diag", "lat": 53.35, "lon": -1.05}     # ~53 km, corner
    # PREMISE: a station comfortably inside the range IS returned, so a None
    # below is the range check and not an empty index.
    got, metres = E.StationIndex([inside]).nearest(*probe)
    check("a gauge 33 km away is returned",
          got is not None and got["id"] == "in", repr(got))
    check("and really is inside MAX_M",
          metres is not None and metres < E.StationIndex.MAX_M,
          repr(metres))
    # PREMISE: the diagonal station is inside the rings the search walks -
    # brute force finds it, so the rings can reach it.
    brute, brute_m = E.nearest_brute(probe[0], probe[1], [diagonal],
                                     max_m=1e9)
    check("the diagonal gauge is over MAX_M",
          brute_m > E.StationIndex.MAX_M, "%.0f m" % brute_m)
    check("but not by much - it is the corner case, not a distant one",
          brute_m < 2 * E.StationIndex.MAX_M, "%.0f m" % brute_m)
    check("and brute force can see it", brute is not None)
    got, metres = E.StationIndex([diagonal]).nearest(*probe)
    check("the index refuses it", got is None, repr(got))
    check("and offers no distance", metres is None, repr(metres))


def test_a_station_with_no_position_cannot_win():
    """A station with a null lat would sit at distance 0 from everywhere under
    a naive comparison, and would then be attached to every ford in England."""
    stations = [{"id": "placed", "lat": 53.0, "lon": -1.6},
                {"id": "unplaced", "lat": None, "lon": None}]
    got, _ = E.StationIndex(stations).nearest(53.01, -1.61)
    check("an unplaced station is not the nearest one",
          got is not None and got["id"] == "placed", repr(got))


# --------------------------------------------------------------- parsing

def test_a_plain_station_parses():
    """PREMISE for the shape tests below: an ordinary station yields all of it.
    Without this, every 'absent field stays None' test would pass over a parser
    that returned None for everything."""
    got = E.parse_station(station("E7050", 53.0, -1.6, label="Ashbourne",
                                  riverName="Dove",
                                  stageScale={"typicalRangeLow": 0.2,
                                              "typicalRangeHigh": 1.1}))
    check("id carried", got["id"] == "E7050", repr(got))
    check("position carried", (got["lat"], got["lon"]) == (53.0, -1.6))
    check("label carried", got["label"] == "Ashbourne")
    check("river carried", got["river"] == "Dove")
    check("typical range carried",
          (got["typical_low_m"], got["typical_high_m"]) == (0.2, 1.1))
    check("measure carried", len(got["measures"]) == 1)


def test_jsonld_collapses_do_not_crash_or_lose_the_measure():
    got = E.parse_station({"notation": "1029TH", "lat": 51.5, "long": -0.3,
                           "label": ["Kingston", "Kingston upon Thames"],
                           "riverName": ["Thames"],
                           "measures": {"@id": "1029TH-level-stage-i-15_min"},
                           "stageScale": [{"typicalRangeHigh": 2.0}]})
    check("a collapsed measures object is still a measure",
          got["measures"] == ["1029TH-level-stage-i-15_min"],
          repr(got["measures"]))
    check("a listed label takes the first", got["label"] == "Kingston")
    check("a listed river takes the first", got["river"] == "Thames")
    check("a listed stageScale is still read",
          got["typical_high_m"] == 2.0, repr(got))


def test_an_absent_typical_range_stays_none():
    """UNKNOWN IS NOT NORMAL. If an absent typicalRangeHigh became 0.0, every
    level-only site in England would read as 'far above normal' for ever."""
    got = E.parse_station(station("X1", 53.0, -1.6))
    check("no stageScale means no typical high",
          got["typical_high_m"] is None, repr(got))
    check("and no typical low", got["typical_low_m"] is None, repr(got))


def test_a_station_with_no_notation_is_refused():
    check("a station we cannot name is not a station",
          E.parse_station({"lat": 53.0, "long": -1.6}) is None)


def test_fetch_drops_stations_with_no_position():
    """PREMISE: the placed one comes back, so 'the unplaced one did not' is a
    filter doing its job rather than an empty list."""
    op = opener_for([{"items": [station("A", 53.0, -1.6),
                                {"notation": "B", "measures": []}]}])
    got = E.fetch_stations("rainfall", opener=op)
    check("the placed station is returned",
          [s["id"] for s in got] == ["A"], repr(got))


def test_fetch_sorts_so_two_runs_give_one_file():
    """The API's order is not stable. An unsorted cache would rewrite on every
    fetch and republish a container whose content did not change - the
    121-republishes-a-month fault, by another route."""
    op = opener_for([{"items": [station("Z", 53.0, -1.6),
                                station("A", 52.0, -1.6),
                                station("M", 51.0, -1.6)]}])
    got = [s["id"] for s in E.fetch_stations("rainfall", opener=op)]
    check("stations come back in id order", got == ["A", "M", "Z"], repr(got))


# --------------------------------------------------------- measure index

def test_the_measure_index_survives_a_hyphen_in_the_notation():
    """Splitting a measure URI on its first hyphen is the obvious shortcut and
    it is wrong for any notation containing one. It fails SILENTLY: the reading
    is filed under a station that does not exist, so the real station looks
    like it stopped reporting - which, before this file's rule, would have read
    as 'no rain here'."""
    stations = [E.parse_station(
        {"notation": "E-7050", "lat": 53.0, "long": -1.6,
         "measures": ["E-7050-rainfall-t-15_min-mm"]})]
    index = E.measure_index(stations)
    check("the measure maps to the whole notation",
          index.get("E-7050-rainfall-t-15_min-mm") == "E-7050", repr(index))
    check("and not to the part before the first hyphen",
          "E" not in set(index.values()), repr(index))


# ---------------------------------------------------------- accumulation

def _reading(measure, when, value):
    return {"measure": measure, "dateTime": when, "value": value}


def test_rainfall_inside_the_window_is_summed():
    """PREMISE for every 'stays None' test below."""
    acc, _ = E.accumulate([_reading("m", "2026-09-24T11:45:00Z", 0.2),
                           _reading("m", "2026-09-24T11:30:00Z", 0.4)],
                          {"m": "A"}, NOW)
    check("two tips in the last hour sum",
          abs(acc["A"]["sums"][24] - 0.6) < 1e-9, repr(acc["A"]))
    check("and are counted", acc["A"]["count"] == 2, repr(acc["A"]))


def test_the_window_edge_is_inclusive_and_the_far_side_is_not():
    """Stated both ways, because an off-by-one here silently halves or doubles
    a 48-hour total and nothing downstream can notice."""
    on = "2026-09-23T12:00:00Z"         # exactly 24 h before NOW
    over = "2026-09-23T11:59:00Z"       # a minute past it
    acc, _ = E.accumulate([_reading("m", on, 1.0)], {"m": "A"}, NOW)
    check("a reading exactly 24 h old is inside the 24 h window",
          abs(acc["A"]["sums"][24] - 1.0) < 1e-9, repr(acc["A"]))
    acc, _ = E.accumulate([_reading("m", over, 1.0)], {"m": "A"}, NOW)
    check("a reading 24 h and a minute old is not",
          acc["A"]["sums"][24] is None, repr(acc["A"]))
    check("but it is still inside the 48 h window",
          abs(acc["A"]["sums"][48] - 1.0) < 1e-9, repr(acc["A"]))


def test_a_station_that_reported_nothing_usable_sums_to_none_not_zero():
    """THE RULE THIS FILE EXISTS FOR. A gauge that stopped reporting and a
    gauge in a drought produce the same JSON if a missing total defaults to
    0.0, and the direction of that error is a rider told a soft lane is fine
    after a week of rain."""
    acc, dropped = E.accumulate([_reading("m", "2026-09-24T11:45:00Z", None)],
                                {"m": "A"}, NOW)
    check("a null value does not become a zero total",
          "A" not in acc or acc["A"]["sums"][24] is None, repr(acc))
    check("and the null is counted so the gauge can be seen to be silent",
          dropped["no_value"] == 1, repr(dropped))
    check("a real zero reading is still a zero total",
          abs(E.accumulate([_reading("m", "2026-09-24T11:45:00Z", 0.0)],
                           {"m": "A"}, NOW)[0]["A"]["sums"][24]) < 1e-9)


def test_an_absent_station_is_absent_rather_than_dry():
    acc, _ = E.accumulate([], {"m": "A"}, NOW)
    check("a station with no readings at all is not in the result",
          "A" not in acc, repr(acc))


def test_an_impossible_tip_is_rejected_and_counted():
    """A 15-minute tipping-bucket reading past MAX_SANE_15MIN_MM is a sensor
    fault. Summing it would push a whole region into 'do not ride' on one bad
    gauge; dropping it quietly would hide that the gauge is broken."""
    acc, dropped = E.accumulate(
        [_reading("m", "2026-09-24T11:45:00Z", 999.0),
         _reading("m", "2026-09-24T11:30:00Z", 0.2)], {"m": "A"}, NOW)
    check("the impossible value is not in the total",
          abs(acc["A"]["sums"][24] - 0.2) < 1e-9, repr(acc["A"]))
    check("and it is counted as rejected", dropped["insane"] == 1,
          repr(dropped))


def test_a_negative_rainfall_is_rejected():
    acc, dropped = E.accumulate([_reading("m", "2026-09-24T11:45:00Z", -3.0)],
                                {"m": "A"}, NOW)
    check("negative rain does not reduce a total",
          "A" not in acc or acc["A"]["sums"][24] is None, repr(acc))
    check("and is counted", dropped["insane"] == 1, repr(dropped))


def test_a_reading_the_station_list_does_not_name_is_counted():
    """This counter going up is the only way we would learn that the station
    list and the readings feed had drifted apart."""
    _acc, dropped = E.accumulate([_reading("ghost", "2026-09-24T11:45:00Z",
                                           1.0)], {"m": "A"}, NOW)
    check("an unattributable reading is counted",
          dropped["unknown_measure"] == 1, repr(dropped))


def test_a_level_is_the_latest_and_never_a_sum():
    """A river level is a state. Adding up six hours of levels would report the
    Dove at 11 metres."""
    acc, _ = E.accumulate([_reading("m", "2026-09-24T09:00:00Z", 1.2),
                           _reading("m", "2026-09-24T11:45:00Z", 1.9),
                           _reading("m", "2026-09-24T10:00:00Z", 1.5)],
                          {"m": "A"}, NOW, latest=True)
    check("the latest reading wins regardless of order",
          acc["A"]["latest"] == 1.9, repr(acc["A"]))
    check("and nothing was summed", acc["A"]["sums"][24] is None,
          repr(acc["A"]))
    check("the time of that reading is carried, so the app can age it",
          acc["A"]["latest_at"] == datetime.datetime(
              2026, 9, 24, 11, 45, tzinfo=datetime.timezone.utc),
          repr(acc["A"]["latest_at"]))


def test_a_reading_with_an_unparsable_time_is_counted_not_guessed():
    _acc, dropped = E.accumulate([_reading("m", "yesterday", 1.0)],
                                 {"m": "A"}, NOW)
    check("an unparsable timestamp is dropped and counted",
          dropped["no_time"] == 1, repr(dropped))


def test_times_without_a_zone_are_read_as_utc():
    """The EA serves Z-suffixed UTC. A naive datetime compared against an aware
    one raises, which would take the whole accumulation down."""
    when = E.parse_when("2026-09-24T11:45:00")
    check("a zoneless time is read as UTC",
          when == datetime.datetime(2026, 9, 24, 11, 45,
                                    tzinfo=datetime.timezone.utc), repr(when))


# ----------------------------------------------------------------- paging

def test_readings_page_until_an_empty_page():
    """One national day of 15-minute rainfall is ~100,000 readings and a page
    caps at 10,000. A fetch that took the first page only would cover a few
    counties and report the rest as no rain."""
    full = {"items": [_reading("m", "2026-09-24T11:45:00Z", 0.1)] * E.PAGE}
    empty = {"items": []}
    op = opener_for([full, full, empty])
    got = E.fetch_readings("rainfall", NOW - datetime.timedelta(days=2),
                           until=NOW, opener=op)
    check("both full pages were read", len(got) == 2 * E.PAGE, len(got))
    check("three requests were made", len(op.seen) == 3, repr(op.seen))
    check("the second request carried the offset",
          "_offset=%d" % E.PAGE in op.seen[1], op.seen[1])
    check("the first did not", "_offset=0" in op.seen[0], op.seen[0])


def test_a_short_page_mid_stream_does_not_end_the_fetch():
    """MEASURED against the live API, 2026-09-24. The obvious rule - a page
    smaller than `_limit` is the last page - is what every paged API teaches
    and is wrong here:

        _offset=40000   ->  9,999      (short, mid-stream)
        _offset=49000   -> 10,000
        _offset=200000  -> 10,000
        _offset=250000  ->      0      (the end)

    The short-page rule stopped the first real run at 49,999 readings over 213
    of 1,041 rainfall stations. Nothing failed; 828 gauges just came back
    silent.
    """
    full = {"items": [_reading("m", "2026-09-24T11:45:00Z", 0.1)] * E.PAGE}
    short = {"items": [_reading("m", "2026-09-24T11:30:00Z", 0.1)] * 9999}
    empty = {"items": []}
    op = opener_for([full, short, full, empty])
    got = E.fetch_readings("rainfall", NOW - datetime.timedelta(days=2),
                           until=NOW, opener=op)
    check("the fetch continued past the short page",
          len(got) == 2 * E.PAGE + 9999, len(got))
    check("and only stopped on the empty one", len(op.seen) == 4,
          repr(op.seen))


def test_the_latest_sweep_asks_for_latest_and_pages_the_same_way():
    """A river level is a state: two days of 15-minute levels across 3,600
    stations is ~700,000 readings to throw all but 3,600 of away."""
    full = {"items": [_reading("m", "2026-09-24T11:45:00Z", 1.2)] * E.PAGE}
    op = opener_for([full, {"items": []}])
    got = E.fetch_latest("level", opener=op)
    check("it asks the API for the latest", "latest" in op.seen[0],
          op.seen[0])
    check("and not for a date window",
          "startdate=" not in op.seen[0], op.seen[0])
    check("it pages", len(got) == E.PAGE, len(got))
    check("and stops on the empty page", len(op.seen) == 2, repr(op.seen))


def test_the_latest_sweep_also_refuses_to_truncate():
    full = {"items": [_reading("m", "2026-09-24T11:45:00Z", 1.2)] * E.PAGE}

    def _open(_request, timeout=None):
        return _Response(full)

    try:
        E.fetch_latest("level", opener=_open, max_pages=2)
        check("an endless latest sweep is refused", False, "no SystemExit")
    except SystemExit:
        check("an endless latest sweep is refused", True)


def test_a_fetch_that_never_ends_is_refused_rather_than_truncated():
    """A truncated fetch is a region of gauges reported as silent. Returning
    what it got would look exactly like a quiet fortnight."""
    full = {"items": [_reading("m", "2026-09-24T11:45:00Z", 0.1)] * E.PAGE}

    def _open(_request, timeout=None):
        return _Response(full)

    try:
        E.fetch_readings("rainfall", NOW - datetime.timedelta(days=2),
                         until=NOW, opener=_open, max_pages=3)
        check("an endless feed is refused", False, "no SystemExit")
    except SystemExit:
        check("an endless feed is refused", True)


def test_readings_use_startdate_and_never_since():
    """MEASURED against the live API, 2026-09-24.

    `/data/readings?parameter=rainfall&since=2026-09-23T00:00:00Z` answers
    HTTP 400. `startdate=2026-09-23&enddate=2026-09-24` answers normally.
    `since` is accepted elsewhere in the same API, which is why the wrong one
    looks right - and every offline test in this file would have passed while
    the wet feed 400'd on every run in production.
    """
    op = opener_for([{"items": []}])
    E.fetch_readings("rainfall",
                     datetime.datetime(2026, 9, 22, 13, 0,
                                       tzinfo=datetime.timezone.utc),
                     until=datetime.datetime(2026, 9, 24, 12, 0,
                                             tzinfo=datetime.timezone.utc),
                     opener=op)
    url = op.seen[0]
    check("the window is asked for as whole days",
          "startdate=2026-09-22" in url and "enddate=2026-09-24" in url, url)
    check("and `since` is not sent at all", "since=" not in url, url)


def test_stations_are_fetched_with_the_full_view():
    """MEASURED, 2026-09-24: without `_view=full` the listing serves
    `stageScale` as a bare URI and the first real run reported '3600 level
    stations, 3600 placed, 0 with a typical range'. Nothing errored; spec
    9.6 G's 'above normal' sentence was simply unsayable."""
    op = opener_for([{"items": [station("A", 53.0, -1.6)]}])
    E.fetch_stations("level", opener=op)
    check("the full view is requested", "_view=full" in op.seen[0],
          op.seen[0])


def test_the_full_view_typical_range_is_actually_read():
    """The shape `_view=full` returns, with the real nesting: `stageScale` is
    an object carrying a `@id` of its own alongside the numbers."""
    raw = {"notation": "1029TH", "lat": 51.4, "long": -0.3,
           "measures": ["1029TH-level-stage-i-15_min-mASD"],
           "stageScale": {
               "@id": "http://environment.data.gov.uk/.../stageScale",
               "highestRecent": {"value": 0.602},
               "typicalRangeHigh": 0.4, "typicalRangeLow": 0.1}}
    got = E.parse_station(raw)
    check("the typical high is read past the sibling fields",
          got["typical_high_m"] == 0.4, repr(got))
    check("and the typical low", got["typical_low_m"] == 0.1, repr(got))


def test_a_first_empty_page_stops_immediately():
    op = opener_for([{"items": []}])
    E.fetch_readings("rainfall", NOW - datetime.timedelta(days=2), until=NOW,
                     opener=op)
    check("an empty first page is one request", len(op.seen) == 1,
          repr(op.seen))


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    rc = E.selftest(log=lambda msg: _failed.append(msg)
                    if "FAIL" in msg else None)
    check("ea_flood --selftest passes", rc == 0)
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d checks" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

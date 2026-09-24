#!/usr/bin/env python3
"""The POI density budget (spec 6.3) against the download budget (spec 8).

The cost measurements run against a REAL published container when one is on
disk, because the thing being measured - how many bytes SQLite spends on a row,
its index and its r-tree entry - is a property of a real file's page layout and
would be a different number in a two-row fixture. Where no container is
present the cost tests say so and skip rather than pass over nothing.

Overpass is driven through an injected opener; nothing here touches the
network.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_pois as PO            # noqa: E402
import poi_budget as B             # noqa: E402

_passed = 0
_failed = []

ROOT = os.path.dirname(HERE)
REAL = os.path.join(ROOT, "containers", "bicycle-north-york.tbmap")


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


def counting_opener(totals):
    """Serves `out count;` answers in CATEGORIES order; None means a broken
    response rather than a zero."""
    queue = list(totals)
    sent = []

    def _open(request, timeout=None):
        sent.append(request.data.decode("utf-8"))
        total = queue.pop(0) if queue else None
        if total is None:
            return _Response({"elements": []})
        return _Response({"elements": [{"type": "count", "id": 0,
                                        "tags": {"total": str(total)}}]})

    _open.sent = sent
    return _open


# ---------------------------------------------------------------- queries

def test_the_count_query_asks_for_a_count_not_a_body():
    """Fetching the elements to count them means downloading the Midlands' car
    parks to find out how many there are."""
    query = B.count_query([("amenity", "parking")], (-3.25, 51.9, 0.15, 53.6))
    check("it ends in out count", "out count;" in query, query)
    check("and never asks for a body", "out body" not in query, query)
    check("and never asks for tags or centres",
          "out tags" not in query, query)


def test_the_count_query_carries_every_selector_for_a_category():
    """`food` is four OSM tags. A query carrying one of them would report a
    quarter of the cafes and the budget would say food is cheap."""
    selectors = dict(PO.CATEGORIES)["food"]
    query = B.count_query(selectors, (-1.0, 52.0, 0.0, 53.0))
    for key, value in selectors:
        check("%s=%s is in the query" % (key, value),
              '"%s"="%s"' % (key, value) in query, query)


def test_the_bounding_box_is_south_west_north_east():
    """Overpass takes (S,W,N,E) and every other box in this repo is (W,S,E,N).
    Swapping them yields an empty or nonsensical region and Overpass does not
    complain - it answers zero, which the budget would read as 'free'."""
    query = B.count_query([("amenity", "fuel")], (-3.0, 52.0, -1.0, 53.0))
    check("the box is south,west,north,east",
          "52.000000,-3.000000,53.000000,-1.000000" in query, query)


# ----------------------------------------------------------------- counts

def test_a_count_response_parses():
    """PREMISE for the 'unknown is not zero' tests."""
    check("a normal count parses",
          B.parse_count({"elements": [{"type": "count",
                                       "tags": {"total": "1234"}}]}) == 1234)
    check("and a genuine zero is a zero",
          B.parse_count({"elements": [{"type": "count",
                                       "tags": {"total": "0"}}]}) == 0)


def test_an_unrecognised_response_is_unknown_and_not_zero():
    """A category silently counted as zero is a category the budget says is
    free - precisely the wrong answer to hand a density decision."""
    for payload in ({"elements": []}, {}, {"elements": [{"type": "node"}]},
                    {"elements": [{"type": "count", "tags": {}}]},
                    {"elements": [{"type": "count",
                                   "tags": {"total": "lots"}}]},
                    "not json at all"):
        check("%r is unknown" % (payload,), B.parse_count(payload) is None)


def test_every_category_is_asked_about():
    """The nine are the whole list (WAYS-SCHEMA.md). A budget measured over
    eight of them understates by whatever the ninth costs."""
    opener = counting_opener([10] * len(PO.CATEGORIES))
    got = B.fetch_counts("midlands", PO.REGIONS["midlands"], opener=opener,
                         log=lambda _m: None)
    check("one request per category",
          len(opener.sent) == len(PO.CATEGORIES), len(opener.sent))
    check("and a count for each",
          set(got) == set(PO.CATEGORY_NAMES), repr(sorted(got)))


def test_a_failed_request_leaves_the_category_unknown_not_zero():
    def _boom(_request, timeout=None):
        raise IOError("overpass said no")

    got = B.fetch_counts("midlands", PO.REGIONS["midlands"], opener=_boom,
                         log=lambda _m: None)
    check("every category is unknown",
          all(v is None for v in got.values()), repr(got))


def test_one_broken_response_does_not_take_the_others_down():
    totals = [10] * len(PO.CATEGORIES)
    totals[3] = None
    got = B.fetch_counts("midlands", PO.REGIONS["midlands"],
                         opener=counting_opener(totals),
                         log=lambda _m: None)
    unknown = [k for k, v in got.items() if v is None]
    check("exactly one category is unknown", len(unknown) == 1, repr(unknown))
    check("and it is the broken one",
          unknown == [PO.CATEGORY_NAMES[3]], repr(unknown))


# ----------------------------------------------------------------- budget

def test_a_complete_count_inside_the_budget_reads_inside():
    """PREMISE for the two failure verdicts below."""
    got = B.budget({"midlands": {"fuel": 1000}}, 36.0, {"a.tbmap": 1000})
    check("the verdict is inside", got["verdict"] == "inside", repr(got))
    check("the POI bytes are counted", got["poi_bytes"] == 36000, repr(got))
    check("and added to the containers",
          got["total_bytes"] == 37000, repr(got))


def test_an_incomplete_count_is_not_allowed_to_read_inside():
    """A budget that says 'fits' while four categories were never counted is
    exactly the green result this codebase exists to distrust."""
    got = B.budget({"midlands": {"fuel": 1000, "food": None}}, 36.0,
                   {"a.tbmap": 1000})
    check("the verdict is incomplete", got["verdict"] == "incomplete",
          repr(got["verdict"]))
    check("and the number of uncounted categories is reported",
          got["uncounted_categories"] == 1, repr(got))
    check("per region as well",
          got["regions"]["midlands"]["uncounted_categories"] == 1, repr(got))


def test_over_the_budget_reads_over():
    got = B.budget({"midlands": {"fuel": 1}}, 36.0,
                   {"a.tbmap": B.DOWNLOAD_BUDGET_BYTES + 1})
    check("the verdict is over", got["verdict"] == "over", repr(got))
    check("and the headroom is negative, not clamped",
          got["headroom_bytes"] < 0, repr(got["headroom_bytes"]))


def test_the_budget_is_the_one_the_spec_names():
    check("1.5 GB, per spec 8", B.DOWNLOAD_BUDGET_BYTES == 1_500_000_000,
          B.DOWNLOAD_BUDGET_BYTES)


def test_every_input_to_the_verdict_is_visible_in_the_result():
    """A verdict whose inputs are not in the report cannot be argued with."""
    got = B.budget({"midlands": {"fuel": 10}}, 36.0, {"a.tbmap": 1000})
    for key in ("budget_bytes", "container_bytes", "poi_bytes", "total_bytes",
                "headroom_bytes", "pois", "gzip_per_poi",
                "uncounted_categories"):
        check("%s is reported" % key, key in got, repr(sorted(got)))
    check("and the per-category counts survive into the report",
          got["regions"]["midlands"]["by_category"] == {"fuel": 10},
          repr(got["regions"]))


# --------------------------------------------------------------- headroom

def test_headroom_says_how_many_pois_would_fit():
    got = B.headroom({"a.tbmap": 500_000_000}, 40.0)
    check("a gigabyte of headroom", got["headroom_bytes"] == 1_000_000_000,
          repr(got))
    check("at 40 bytes each that is 25 million POIs",
          got["pois_that_fit"] == 25_000_000, repr(got))
    check("and it is not already over", got["already_over"] is False)


def test_containers_already_over_the_budget_are_said_out_loud():
    """A budget already blown is the single most important thing this tool
    could say, and clamping the headroom to zero would hide it."""
    got = B.headroom({"a.tbmap": B.DOWNLOAD_BUDGET_BYTES + 1}, 40.0)
    check("already_over is true", got["already_over"] is True, repr(got))
    check("and the headroom is negative rather than zero",
          got["headroom_bytes"] < 0, repr(got["headroom_bytes"]))


# ------------------------------------------------------------- the cost

def test_a_synthetic_row_is_the_shape_build_pois_writes():
    """If the synthetic row lost a column, the measured cost would be of a row
    the pipeline never writes."""
    row = B.synthetic_pois(1, 16, 1.0)[0]
    check("the keys match build_pois' row",
          set(row) == {"poi_uid", "category", "name", "lat", "lon",
                       "opening_hours", "source_date"}, repr(sorted(row)))
    check("the name is the requested length", len(row["name"]) == 16,
          repr(row["name"]))
    check("and opening_hours is present at 100%",
          row["opening_hours"] is not None, repr(row))


def test_a_bare_shape_really_is_bare():
    rows = B.synthetic_pois(100, 0, 0.0)
    check("no names", all(r["name"] is None for r in rows))
    check("no opening hours", all(r["opening_hours"] is None for r in rows))


def test_the_sweep_spreads_the_points_rather_than_stacking_them():
    """An r-tree over a thousand coincident points compresses to nothing and
    would flatter the measurement into saying POIs are nearly free."""
    rows = B.synthetic_pois(500, 0, 0.0)
    distinct = set((r["lat"], r["lon"]) for r in rows)
    check("the points are distinct", len(distinct) == 500, len(distinct))


def test_the_measured_cost_rises_with_the_row_shape():
    """The whole reason a range is reported rather than one number: a POI with
    a long name and opening hours costs more than a bare one, and the sweep
    must be able to see that. If it could not, the range would be decoration."""
    if not os.path.exists(REAL):
        check("a published container is present to measure against", True,
              "skipped: %s absent" % REAL)
        return
    swept = B.sweep_cost(REAL, count=4000)
    bare = swept["bare"]["plain_per_poi"]
    heavy = swept["heavy"]["plain_per_poi"]
    check("a heavy row costs more than a bare one", heavy > bare,
          "%.1f vs %.1f" % (heavy, bare))
    check("and both are plausible SQLite row costs",
          20 < bare < 400 and 20 < heavy < 600,
          "%.1f, %.1f" % (bare, heavy))


def test_the_compressed_cost_is_below_the_plain_one():
    """1.5 GB is a DOWNLOAD budget and containers ship compressed. Measuring
    plain bytes would leave the budget with headroom it does not have."""
    if not os.path.exists(REAL):
        check("a published container is present to measure against", True,
              "skipped: %s absent" % REAL)
        return
    got = B.measure_cost(REAL, B.synthetic_pois(4000, 16, 0.25))
    check("compression helps", got["gzip_per_poi"] < got["plain_per_poi"],
          "%.1f vs %.1f" % (got["gzip_per_poi"], got["plain_per_poi"]))
    check("and the delta is positive - POIs are not free",
          got["gzip_delta"] > 0, repr(got["gzip_delta"]))


def test_the_container_is_not_modified_by_a_measurement():
    """`measure_cost` writes POIs into a COPY. Writing into the published file
    would put 4,000 fake car parks into a container a rider downloads."""
    if not os.path.exists(REAL):
        check("a published container is present to measure against", True,
              "skipped: %s absent" % REAL)
        return
    before = os.path.getsize(REAL)
    with open(REAL, "rb") as handle:
        head = handle.read(4096)
    B.measure_cost(REAL, B.synthetic_pois(200, 8, 0.0))
    with open(REAL, "rb") as handle:
        after_head = handle.read(4096)
    check("the container's size is unchanged",
          os.path.getsize(REAL) == before, os.path.getsize(REAL))
    check("and its bytes are unchanged", head == after_head)


def test_no_cache_yields_no_cost_rather_than_a_zero():
    """A cost of zero would tell the budget POIs are free."""
    if not os.path.exists(REAL):
        check("a published container is present to measure against", True,
              "skipped: %s absent" % REAL)
        return
    tmp = tempfile.mkdtemp()
    try:
        got = B.cost_from_cache(REAL, os.path.join(tmp, "nope"), "midlands")
        check("an absent cache gives None, not 0", got is None, repr(got))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gzip_sizes_reads_only_containers():
    tmp = tempfile.mkdtemp()
    try:
        for name in ("a.tbmap", "b.tbmap", "a.tbmap.sig", "notes.txt"):
            with open(os.path.join(tmp, name), "wb") as handle:
                handle.write(b"x" * 1000)
        got = B.gzip_sizes(tmp)
        check("only the .tbmap files are measured",
              sorted(got) == ["a.tbmap", "b.tbmap"], repr(sorted(got)))
        check("and each has a size", all(v > 0 for v in got.values()),
              repr(got))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    rc = B.selftest(log=lambda msg: _failed.append(msg) if "FAIL" in msg
                    else None)
    check("poi_budget --selftest passes", rc == 0)
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d checks" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

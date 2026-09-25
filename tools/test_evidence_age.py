#!/usr/bin/env python3
"""Evidence decay: how old the app's answer is.

Every case here passes `as_of` explicitly. A test whose verdict changes at
midnight is one that fails in CI and passes when you look at it - the lesson
`test_poi_staleness.py` already records.
"""
import contextlib
import datetime
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import evidence_age as A           # noqa: E402

_passed = 0
_failed = []

TODAY = datetime.date(2026, 9, 24)


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def days_ago(n):
    return (TODAY - datetime.timedelta(days=n)).isoformat()


WAYS_DDL = """
CREATE TABLE ways (
  way_uid TEXT PRIMARY KEY, way_class TEXT NOT NULL, authority TEXT NOT NULL,
  legal_tier TEXT NOT NULL, source TEXT NOT NULL, source_date TEXT NOT NULL,
  motorbike_ok INTEGER NOT NULL, fourxfour_ok INTEGER NOT NULL,
  access_reason TEXT NOT NULL, access_evidence TEXT NOT NULL,
  length_m REAL NOT NULL, geometry BLOB NOT NULL
);
"""

LANES_DDL = """
CREATE TABLE lanes (lane_uid TEXT PRIMARY KEY, name TEXT, geometry BLOB);
"""


def ways_container(path, dates):
    db = sqlite3.connect(path)
    db.executescript(WAYS_DDL)
    for i, when in enumerate(dates, start=1):
        db.execute(
            "INSERT INTO ways (way_uid, way_class, authority, legal_tier,"
            " source, source_date, motorbike_ok, fourxfour_ok, access_reason,"
            " access_evidence, length_m, geometry)"
            " VALUES (?,'boat','Derbyshire','statutory','rowmaps:x',?,1,1,"
            "'BOAT','statutory',1.0,X'00')", ("w%d" % i, when))
    db.commit()
    db.close()
    return path


# ------------------------------------------------------------ parse_date

def test_a_plain_iso_date_parses():
    """PREMISE: without this, every 'unparsable is unknown' test below would
    pass over a parser that returned None for everything."""
    check("an ISO date parses",
          A.parse_date("2026-09-24") == datetime.date(2026, 9, 24))
    check("and an ISO timestamp is read as its date",
          A.parse_date("2026-09-24T11:45:00Z") == datetime.date(2026, 9, 24))


def test_absent_empty_and_rubbish_are_all_unknown():
    """All three mean the same thing - we do not know when this was read - and
    none of them may quietly become today."""
    for value in (None, "", "   ", "not a date", "2026-13-45", 0):
        check("%r is unknown" % (value,), A.parse_date(value) is None)


# ---------------------------------------------------------- distribution

def test_a_dated_column_produces_a_distribution():
    """PREMISE for the unknown-handling tests."""
    got = A.distribution([days_ago(0), days_ago(100), days_ago(400)], TODAY)
    check("all three are dated", got["dated"] == 3, repr(got))
    check("none unknown", got["unknown"] == 0, repr(got))
    check("the newest is today", got["newest_days"] == 0, repr(got))
    check("the oldest is 400 days", got["oldest_days"] == 400, repr(got))
    check("the median is the middle one", got["median_days"] == 100,
          repr(got))


def test_undated_rows_are_their_own_bucket_and_not_fresh():
    """Counting an undated row as today would flatter every distribution here;
    counting it as ancient would understate our data. It is neither."""
    got = A.distribution([days_ago(0), None, "junk", ""], TODAY)
    check("three rows are undated", got["unknown"] == 3, repr(got))
    check("one is dated", got["dated"] == 1, repr(got))
    check("the row count is the whole column", got["rows"] == 4, repr(got))
    check("and the undated rows are not in any age bucket",
          sum(got["buckets"].values()) == 1, repr(got["buckets"]))


def test_an_empty_column_gives_no_median_rather_than_zero():
    """A zero median would say 'every row was surveyed today', which is the one
    direction this file must not be wrong in."""
    got = A.distribution([], TODAY)
    check("no median", got["median_days"] is None, repr(got))
    check("no p90", got["p90_days"] is None, repr(got))
    check("no oldest", got["oldest_days"] is None, repr(got))
    check("and no newest", got["newest_days"] is None, repr(got))


def test_a_column_of_nothing_but_rubbish_gives_no_median():
    got = A.distribution([None, "junk"], TODAY)
    check("an all-undated column has no median",
          got["median_days"] is None, repr(got))
    check("and says how many it could not read", got["unknown"] == 2,
          repr(got))


def test_the_median_is_used_and_not_the_newest():
    """One authority republishing last week must not make a region of
    five-year-old surveys look current - the fault `poi_staleness.py` guards
    against in the other direction."""
    dates = [days_ago(1)] + [days_ago(2000)] * 9
    got = A.distribution(dates, TODAY)
    check("the newest is a day old", got["newest_days"] == 1, repr(got))
    check("but the median is not", got["median_days"] == 2000, repr(got))
    check("and nine rows are past the warning line", got["over_warn"] == 9,
          repr(got))


def test_the_bucket_boundaries_are_stated_both_ways():
    """An off-by-one here moves thousands of ways between 'under a month' and
    'under three months' and nothing downstream could notice."""
    check("exactly 30 days is under_30d", A.bucket_of(30) == "under_30d")
    check("31 days is not", A.bucket_of(31) == "under_90d")
    check("exactly a year is under_1y", A.bucket_of(365) == "under_1y")
    check("366 days is not", A.bucket_of(366) == "under_2y")
    check("five years exactly is under_5y", A.bucket_of(1825) == "under_5y")
    check("and a day past is over_5y", A.bucket_of(1826) == "over_5y")


def test_every_age_lands_in_exactly_one_bucket():
    """A row counted twice, or not at all, makes the percentages lie."""
    ages = [0, 1, 29, 30, 31, 90, 91, 364, 365, 366, 729, 730, 1824, 1825,
            1826, 99999]
    got = A.distribution([days_ago(a) for a in ages], TODAY)
    check("the buckets hold every dated row",
          sum(got["buckets"].values()) == len(ages),
          "%d vs %d" % (sum(got["buckets"].values()), len(ages)))


def test_a_future_date_is_clamped_and_counted_not_negative():
    """A source_date in the future is a data fault. Letting it through drags a
    median below zero and makes a region look newer than any row in it."""
    got = A.distribution([(TODAY + datetime.timedelta(days=30)).isoformat(),
                          days_ago(100)], TODAY)
    check("no negative age", got["newest_days"] == 0, repr(got))
    check("and the fault is counted", got["future_dated"] == 1, repr(got))


def test_the_warning_count_uses_the_stated_threshold():
    got = A.distribution([days_ago(A.WARN_DAYS),
                          days_ago(A.WARN_DAYS + 1)], TODAY)
    check("exactly the threshold is not past it", got["over_warn"] == 1,
          repr(got))
    check("and the threshold travels in the summary",
          got["warn_days"] == A.WARN_DAYS, repr(got))


def test_percentiles_are_the_values_they_name():
    ages = list(range(1, 101))
    got = A.distribution([days_ago(a) for a in ages], TODAY)
    check("the median of 1..100 is 50", got["median_days"] == 50, repr(got))
    check("the p90 is 90", got["p90_days"] == 90, repr(got))


# ------------------------------------------------------------- containers

def test_a_ways_container_is_measured():
    """PREMISE for the BLIND test: this tool CAN measure a container."""
    tmp = tempfile.mkdtemp()
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"),
                              [days_ago(10), days_ago(900)])
        got = A.read_container(path, TODAY)
        check("ways were measured", got["ways"]["state"] == "measured",
              repr(got["ways"]))
        check("both rows counted", got["ways"]["rows"] == 2, repr(got["ways"]))
        check("and the as-of date is recorded, so the report can be read"
              " months later", got["as_of"] == TODAY.isoformat(), repr(got))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_pre_pivot_lanes_container_is_blind_and_says_so():
    """Every published container on disk today is this shape. Reporting it as
    clean - or silently printing nothing - would read as a clean bill of
    health for data whose age we cannot see at all."""
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "l.tbmap")
        db = sqlite3.connect(path)
        db.executescript(LANES_DDL)
        db.execute("INSERT INTO lanes VALUES ('l1','x',X'00')")
        db.commit()
        db.close()
        got = A.read_container(path, TODAY)
        check("lanes is blind, not absent and not measured",
              got["lanes"]["state"] == "blind", repr(got["lanes"]))
        check("and it says why", "source_date" in got["lanes"]["why"],
              repr(got["lanes"]))
        lines = []
        A.report(got, log=lines.append)
        check("the report says so out loud",
              any("BLIND" in line for line in lines), repr(lines))
        check("and leads with the fact that nothing is dated",
              any("NOTHING DATED" in line for line in lines), repr(lines))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_table_that_is_not_there_is_absent_not_blind():
    """Three different things: measured, blind (there but undated), absent.
    Collapsing absent into blind would report a container as having a problem
    with a table it was never meant to carry."""
    tmp = tempfile.mkdtemp()
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"), [days_ago(1)])
        got = A.read_container(path, TODAY)
        check("pois is absent", got["pois"]["state"] == "absent",
              repr(got["pois"]))
        check("lanes is absent too", got["lanes"]["state"] == "absent",
              repr(got["lanes"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_summary_can_be_written_into_meta_and_read_back():
    """The wiring. A summary the app cannot reach is a measurement nobody
    sees - the defect this codebase names as its most common."""
    tmp = tempfile.mkdtemp()
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"),
                              [days_ago(10), days_ago(400)])
        summary = A.read_dates(path)
        A.write_meta(path, summary)
        db = sqlite3.connect(path)
        raw = db.execute("SELECT value FROM meta WHERE key = ?",
                         (A.META_KEY,)).fetchone()
        db.close()
        check("the key is there", raw is not None)
        check("and it is evidence_dates, the key the app reads",
              A.META_KEY == "evidence_dates", A.META_KEY)
        back = json.loads(raw[0])
        check("and it round-trips", back["ways"]["median"] ==
              summary["ways"]["median"], repr(back["ways"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_writing_twice_leaves_one_key_and_the_same_bytes():
    """`built_at` leaking into a content hash broke reproducibility once
    already (WAYS-SCHEMA.md). This key is derived from the container's own rows
    and must be byte-identical on a rebuild of an unchanged container."""
    tmp = tempfile.mkdtemp()
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"), [days_ago(10)])
        A.write_meta(path)
        db = sqlite3.connect(path)
        first = db.execute("SELECT value FROM meta WHERE key=?",
                           (A.META_KEY,)).fetchone()[0]
        db.close()
        A.write_meta(path)
        db = sqlite3.connect(path)
        rows = db.execute("SELECT value FROM meta WHERE key=?",
                          (A.META_KEY,)).fetchall()
        db.close()
        check("one row", len(rows) == 1, len(rows))
        check("and the same bytes", rows[0][0] == first,
              "%r vs %r" % (rows[0][0][:60], first[:60]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_writing_does_not_disturb_the_rest_of_meta():
    tmp = tempfile.mkdtemp()
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"), [days_ago(10)])
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('built_at','2026-09-16T21:47Z')")
        db.execute("INSERT INTO meta VALUES ('bounds','-1,53,-0.9,54')")
        db.commit()
        db.close()
        A.write_meta(path)
        db = sqlite3.connect(path)
        kept = dict(db.execute("SELECT key, value FROM meta"))
        db.close()
        check("built_at is untouched",
              kept["built_at"] == "2026-09-16T21:47Z", repr(kept))
        check("bounds is untouched", kept["bounds"] == "-1,53,-0.9,54",
              repr(kept))
        check("and the new key is there", A.META_KEY in kept, repr(kept))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------- dates, not ages (cost A)
#
# MEASURED: `meta.evidence_age` held ages in days against an `as_of` pinned to
# the 1st of the month, so every region container's bytes moved on the first
# run of every month with nothing in it changed - and, `built_at` following
# the data, the app saw the build it held under a new hash and fetched the
# region whole: ~94 MB for the six, at least monthly, on the paid tier.

def test_the_stored_dates_do_not_depend_on_the_day_they_were_written():
    """THE REGRESSION TEST FOR COST A. The same rows written by the CLI on
    the 1st of September and the 1st of October are the same bytes."""
    tmp = tempfile.mkdtemp()
    try:
        sept = ways_container(os.path.join(tmp, "sept.tbmap"),
                              [days_ago(10), days_ago(400), "unknown"])
        octo = os.path.join(tmp, "oct.tbmap")
        shutil.copyfile(sept, octo)
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            A.main(["--write", "--as-of", "2026-09-01", sept])
            A.main(["--write", "--as-of", "2026-10-01", octo])
        with open(sept, "rb") as a, open(octo, "rb") as b:
            check("a month later, the same bytes", a.read() == b.read())
        with contextlib.closing(sqlite3.connect(octo)) as db:
            stored = json.loads(dict(db.execute(
                "SELECT key, value FROM meta"))[A.META_KEY])
        check("and nothing in them is a day count or a run date",
              "as_of" not in stored and "median_days" not in stored["ways"],
              repr(stored)[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_dates_carry_every_figure_the_ages_did():
    """What the app computes on the device, checked against the old
    day-count summary for several `as_of`: nothing a rider was told is lost
    by storing dates, and the age now keeps ageing on its own."""
    dates = ([days_ago(a) for a in (0, 3, 40, 100, 364, 365, 366, 900, 2000)]
             + [(TODAY + datetime.timedelta(days=5)).isoformat(), None, "x"])
    got = A.date_summary(dates)
    check("rows, dated and unknown", (got["rows"], got["dated"],
                                      got["unknown"]) == (12, 10, 2), got)
    for shift in (0, 31, 400):
        as_of = TODAY + datetime.timedelta(days=shift)
        ages = A.distribution(dates, as_of)
        age = lambda iso: max(0, (as_of - A.parse_date(iso)).days)
        check("median at +%d" % shift,
              age(got["median"]) == ages["median_days"],
              (got["median"], ages["median_days"]))
        check("p90 at +%d" % shift, age(got["p90"]) == ages["p90_days"])
        check("oldest at +%d" % shift,
              age(got["oldest"]) == ages["oldest_days"])
        over = sum(n for day, n in got["days"].items()
                   if (as_of - A.parse_date(day)).days > A.WARN_DAYS)
        check("over the warning line at +%d" % shift,
              over == ages["over_warn"], (over, ages["over_warn"]))
        future = sum(n for day, n in got["days"].items()
                     if A.parse_date(day) > as_of)
        check("future-dated at +%d" % shift,
              future == ages["future_dated"], (future, ages["future_dated"]))
    check("the per-day counts hold every dated row",
          sum(got["days"].values()) == got["dated"], got["days"])


def test_writing_the_dates_removes_the_age_key():
    """One answer to "how old is this", not two that can disagree."""
    tmp = tempfile.mkdtemp()
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"), [days_ago(10)])
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES (?, '{}')", (A.LEGACY_KEY,))
        db.commit()
        db.close()
        A.write_meta(path)
        with contextlib.closing(sqlite3.connect(path)) as db:
            kept = dict(db.execute("SELECT key, value FROM meta"))
        check("evidence_age is gone", A.LEGACY_KEY not in kept, kept.keys())
        check("evidence_dates is there", A.META_KEY in kept, kept.keys())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_age_report_cannot_be_stored():
    """read_container's summary carries `as_of`; stored, it would put the
    monthly clock straight back into the container."""
    tmp = tempfile.mkdtemp()
    try:
        path = ways_container(os.path.join(tmp, "w.tbmap"), [days_ago(10)])
        try:
            A.write_meta(path, A.read_container(path, TODAY))
            check("refused", False, "it was written")
        except ValueError:
            check("refused", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_real_published_container_reports_blind_rather_than_crashing():
    """The shape actually on disk. This is the one case a fixture cannot
    stand in for."""
    root = os.path.dirname(HERE)
    path = os.path.join(root, "containers", "bicycle-north-york.tbmap")
    if not os.path.exists(path):
        check("a published container is present to check", True,
              "skipped: %s absent" % path)
        return
    got = A.read_container(path, TODAY)
    check("the published container is blind, not crashed",
          got["lanes"]["state"] == "blind", repr(got["lanes"]))
    check("and nothing in it was measured",
          all(got[t]["state"] != "measured" for t in A.TABLES), repr(got))


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    rc = A.selftest(log=lambda msg: _failed.append(msg) if "FAIL" in msg
                    else None)
    check("evidence_age --selftest passes", rc == 0)
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d checks" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

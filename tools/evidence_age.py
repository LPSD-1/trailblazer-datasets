#!/usr/bin/env python3
"""How old is the answer? The age distribution of every derived verdict.

Spec 9.4 and 5.4 both require the app to say when its data was read - "closures
as of 14 March", and a warning past a threshold. Every row in
`docs/WAYS-SCHEMA.md` already carries `source_date`; nothing until now turned
those dates into a sentence the app could say.

    python tools/evidence_age.py containers/*.tbmap
    python tools/evidence_age.py --write containers/ways-midlands.tbmap
    python tools/evidence_age.py --report age.json containers/*.tbmap
    python tools/evidence_age.py --selftest

WHY A DISTRIBUTION AND NOT A DATE.
"Data from 2026-01-14" is what a single `built_at` supports, and it is close to
a lie: a region's ways come from thirty-odd authorities, each of which last
published its definitive map on its own schedule. F-series work found council
files years apart in the same region. A rider asking "is this current?" is
owed the SHAPE of that - the median, the oldest, and how much of the region is
past a threshold - not a build stamp that describes when we ran a script.

THE FIGURE THE APP SHOULD LEAD WITH IS THE MEDIAN, NOT THE NEWEST.
Taking the newest would let one authority that republished last week make a
region of five-year-old surveys look current, which is the same fault
`poi_staleness.py` guards against in the other direction.

UNPARSABLE AND ABSENT ARE THEIR OWN BUCKET, AND IT IS NOT "FRESH".
A row whose `source_date` we cannot read is a row whose age we do not know.
Counting it as today would flatter every distribution here; counting it as
ancient would understate our data. It is counted as `unknown`, reported
separately, and left out of the percentiles - and if that count is large, the
honest thing for the app to say is that it does not know.

PRE-PIVOT CONTAINERS ARE BLIND, AND SAY SO.
Every published container today is the `lanes` shape and has no `source_date`
column at all. This tool reports `blind` for those rather than inventing an
age from `built_at`, because `built_at` is when WE ran, not when the AUTHORITY
surveyed, and conflating the two is exactly the claim the spec forbids.
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys

#: Age buckets, in days, as (label, upper bound inclusive). The last is open.
#:
#: The boundaries are the ones the pipeline already acts on rather than round
#: numbers: 30 days is `poi_staleness.MAX_AGE_DAYS`, 365 is the point past
#: which a definitive-map extract has probably seen a modification order, and
#: 1,825 (five years) is where a surveying authority's file is old enough that
#: the app should say so out loud.
BUCKETS = [("under_30d", 30), ("under_90d", 90), ("under_1y", 365),
           ("under_2y", 730), ("under_5y", 1825), ("over_5y", None)]

#: Tables that carry a `source_date` per WAYS-SCHEMA.md. Checked for, never
#: assumed: a container missing one is reported, not skipped silently.
#:
#: `lanes` is in the list although it has no `source_date` and never will. It
#: is the pre-pivot record table, it is what every published container on disk
#: still holds, and leaving it out made this tool print NOTHING for those files
#: - a silent pass that reads exactly like a clean bill of health. Listed, it
#: reports BLIND, which is the true answer.
TABLES = ("ways", "lanes", "pois", "orders", "fords")

#: Past this, the app is expected to say the answer is old. Not a refusal -
#: an old definitive map is still the definitive map, and refusing to show it
#: would leave a rider with nothing.
WARN_DAYS = 365

META_KEY = "evidence_age"


def parse_date(text):
    """An ISO date, or None. `None` covers absent, empty and malformed alike -
    all three mean the same thing here: we do not know when this was read."""
    if not text:
        return None
    body = str(text).strip()
    if len(body) > 10:
        body = body[:10]
    try:
        return datetime.date.fromisoformat(body)
    except ValueError:
        return None


def bucket_of(days):
    for label, upper in BUCKETS:
        if upper is None or days <= upper:
            return label
    return BUCKETS[-1][0]


def percentile(sorted_days, point):
    """Nearest-rank. Empty in, None out - never a zero, which would read as
    'everything was surveyed today'."""
    if not sorted_days:
        return None
    rank = max(1, int(round(point / 100.0 * len(sorted_days))))
    return sorted_days[min(rank, len(sorted_days)) - 1]


def distribution(dates, as_of):
    """The age summary for one table's `source_date` column.

    `dates` is the raw column, including Nones and rubbish. Ages are clamped at
    zero: a source_date in the future is a data fault, not a negative age, and
    letting it through would drag a median below zero and make a region look
    newer than any row in it.
    """
    buckets = dict((label, 0) for label, _ in BUCKETS)
    ages = []
    unknown = 0
    future = 0
    for raw in dates:
        when = parse_date(raw)
        if when is None:
            unknown += 1
            continue
        days = (as_of - when).days
        if days < 0:
            future += 1
            days = 0
        ages.append(days)
        buckets[bucket_of(days)] += 1
    ages.sort()
    return {
        "rows": len(dates),
        "dated": len(ages),
        "unknown": unknown,
        "future_dated": future,
        "buckets": buckets,
        "newest_days": ages[0] if ages else None,
        "median_days": percentile(ages, 50),
        "p90_days": percentile(ages, 90),
        "oldest_days": ages[-1] if ages else None,
        "over_warn": sum(1 for d in ages if d > WARN_DAYS),
        "warn_days": WARN_DAYS,
    }


def read_container(path, as_of):
    """{table: distribution}, plus why any table is absent or blind.

    Three outcomes per table and they are different things:
      present   - a distribution
      'absent'  - the table is not in this container
      'blind'   - the table is there and has no `source_date` column
    """
    db = sqlite3.connect(path)
    try:
        names = set(row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        out = {}
        for table in TABLES:
            if table not in names:
                out[table] = {"state": "absent"}
                continue
            columns = set(row[1] for row in
                          db.execute("PRAGMA table_info(%s)" % table))
            if "source_date" not in columns:
                out[table] = {"state": "blind",
                              "why": "no source_date column"}
                continue
            dates = [row[0] for row in
                     db.execute("SELECT source_date FROM %s" % table)]
            out[table] = dict(distribution(dates, as_of), state="measured")
        # Named so a report can be read months later without guessing which
        # day "42 days old" was 42 days before.
        out["as_of"] = as_of.isoformat()
        out["container"] = os.path.basename(path)
        return out
    finally:
        db.close()


def write_meta(path, summary):
    """Put the summary in `meta` so the app can read it without a scan.

    `meta` is a key/value table and this adds one key. It deliberately does NOT
    touch `built_at` - WAYS-SCHEMA.md records that a run stamp leaking into a
    content hash broke reproducibility once already, and this key is written
    from the container's own rows, so two builds of one container write the
    same bytes here.
    """
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS meta"
                   " (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                   (META_KEY, json.dumps(summary, sort_keys=True)))
        db.commit()
    finally:
        db.close()


def _days(value):
    return "-" if value is None else "%5d" % value


def report(summary, log=print):
    log("%s (as of %s)" % (summary["container"], summary["as_of"]))
    if not any(summary[t]["state"] == "measured" for t in TABLES):
        # Said out loud. A container this tool can say nothing about must not
        # produce the same empty output as a container with nothing wrong.
        log("    NOTHING DATED - this container carries no source_date at all")
    for table in TABLES:
        entry = summary[table]
        if entry["state"] == "absent":
            continue
        if entry["state"] == "blind":
            log("    %-8s BLIND - %s" % (table, entry["why"]))
            continue
        log("    %-8s %6d rows, %d undated" % (table, entry["rows"],
                                               entry["unknown"]))
        log("             newest %s  median %s  p90 %s  oldest %s days"
            % (_days(entry["newest_days"]), _days(entry["median_days"]),
               _days(entry["p90_days"]), _days(entry["oldest_days"])))
        log("             " + "  ".join(
            "%s %d" % (label, entry["buckets"][label])
            for label, _ in BUCKETS))
        if entry["dated"]:
            log("             %d rows (%.1f%%) older than %d days"
                % (entry["over_warn"],
                   100.0 * entry["over_warn"] / entry["dated"],
                   entry["warn_days"]))


def selftest(log=print):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s%s" % (name, (": " + detail) if detail else ""))

    today = datetime.date(2026, 9, 24)
    got = distribution(["2026-09-24", "2026-08-25", "2020-01-01", None, "junk"],
                       today)
    check("undated rows are counted apart", got["unknown"] == 2, repr(got))
    check("and are not in the dated count", got["dated"] == 3, repr(got))
    check("the newest is today", got["newest_days"] == 0, repr(got))
    check("the oldest is the old one", got["oldest_days"] > 2000, repr(got))
    check("an empty column gives no median, not zero",
          distribution([], today)["median_days"] is None)
    check("a future date is clamped, not negative",
          distribution(["2030-01-01"], today)["newest_days"] == 0)
    check("and counted", distribution(["2030-01-01"], today)["future_dated"]
          == 1)
    check("the bucket boundary is inclusive", bucket_of(30) == "under_30d")
    check("and one day past is the next bucket", bucket_of(31) == "under_90d")
    check("the last bucket is open", bucket_of(100000) == "over_5y")

    for failure in failures:
        log("  FAIL " + failure)
    log("selftest: %s" % ("FAILED %d" % len(failures) if failures else "ok"))
    return 1 if failures else 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="age distribution of every derived verdict")
    parser.add_argument("containers", nargs="*")
    parser.add_argument("--as-of", help="ISO date; defaults to today")
    parser.add_argument("--write", action="store_true",
                        help="store the summary in each container's meta")
    parser.add_argument("--report", help="write every summary as one JSON")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.selftest:
        return selftest()
    if not args.containers:
        raise SystemExit("name at least one container, or --selftest")
    as_of = (datetime.date.fromisoformat(args.as_of) if args.as_of
             else datetime.date.today())
    every = []
    for path in args.containers:
        summary = read_container(path, as_of)
        every.append(summary)
        report(summary)
        if args.write:
            write_meta(path, summary)
            print("    -> meta.%s" % META_KEY)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(every, handle, indent=2, sort_keys=True)
        print("report -> %s" % args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

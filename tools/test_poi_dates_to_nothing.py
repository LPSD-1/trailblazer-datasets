#!/usr/bin/env python3
"""A POI that LOSES a value - its name, its opening hours - is a changed one.

tools/test_poi_dates.py holds the rule: a POI whose every other column equals
the published row keeps the published date; one whose content moved takes the
day it was read. Every "changed" case there changes a value to another value
(a name to a new name, hours to other hours, a category, a position). None
takes a value away. So a comparison that skipped the columns that are None in
the refresh - the easy way to "tolerate a missing column" - would call a POI
whose name was deleted in OSM, or whose opening hours were removed, unchanged,
keep its old date, and leave it out of the month's changeset: every phone
would go on showing the old name and the old hours.

The same blind spot was closed for fords in round 9 (test_ford_dates.py,
"unnamed" and "every gauge withdrawn"); its verifier named this one.

Checked red, in a scratch copy, with stable_ids.keep_dates skipping columns
that are None in the refresh.

Run: python tools/test_poi_dates_to_nothing.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_pois as PO           # noqa: E402
import test_poi_dates as T        # noqa: E402

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": %r" % (detail,)) if detail else ""))


def test_a_poi_that_loses_its_name_or_hours_is_a_changed_one():
    tmp = tempfile.mkdtemp()
    try:
        published = os.path.join(tmp, "published.tbmap")
        T._container(published)
        PO.write_pois(published, [
            T._poi("osm:n1", T.PUBLISHED_DAY, name="Hilltop Garage"),
            T._poi("osm:n2", T.PUBLISHED_DAY, lat=52.51, hours="24/7"),
            T._poi("osm:n3", T.PUBLISHED_DAY, lat=52.52, name="Kept",
                   hours="Mo-Su 07:00-22:00"),
        ])
        nxt = os.path.join(tmp, "next.tbmap")
        T._container(nxt)
        PO.write_pois(nxt, [
            T._poi("osm:n1", T.REFRESH_DAY),
            T._poi("osm:n2", T.REFRESH_DAY, lat=52.51),
            T._poi("osm:n3", T.REFRESH_DAY, lat=52.52, name="Kept",
                   hours="Mo-Su 07:00-22:00"),
        ], previous=published)
        got = T._dates(nxt)
        check("PREMISE: the published POIs carry the published day",
              set(T._dates(published).values()) == {T.PUBLISHED_DAY},
              T._dates(published))
        check("PREMISE: an unchanged POI beside them keeps the published day",
              got.get("osm:n3") == T.PUBLISHED_DAY, got)
        check("a POI whose name was taken away takes the day it was read",
              got.get("osm:n1") == T.REFRESH_DAY, got)
        check("a POI whose opening hours were taken away takes the day it "
              "was read", got.get("osm:n2") == T.REFRESH_DAY, got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("%d passed, %d failed" % (_passed, len(_failed)))
    for failure in _failed:
        print("  FAIL %s" % failure)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

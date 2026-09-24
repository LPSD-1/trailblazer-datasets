"""The rule that decides how often a rider re-downloads an area container.

POIs live inside the area container, so a POI refresh rewrites container bytes
and every rider fetches the area again. The ways build runs four times a day
and the POI fetch must not, or this reproduces the manifest-run-stamp fault -
121 republishes a month for data that did not move - by a different route.

Every case here is a date arithmetic case, so today is passed in rather than
read from the clock: a test whose verdict changes at midnight is one that
fails in CI and passes when you look at it.
"""
import datetime
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import poi_staleness as S          # noqa: E402
import build_packages as P         # noqa: E402
import build_pois as PO            # noqa: E402

_passed = 0
_failed = []

TODAY = datetime.date(2026, 9, 24)
ALL = [r for r, _l, _b in P.REGIONS]


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


def _cache(tmp, per_region):
    """per_region: {region: fetched_at} or {region: {category: fetched_at}}."""
    root = os.path.join(tmp, "pois")
    for region, spec in per_region.items():
        os.makedirs(os.path.join(root, region), exist_ok=True)
        for name, _sel in PO.CATEGORIES:
            when = spec.get(name) if isinstance(spec, dict) else spec
            if when is None:
                continue
            with io.open(PO.cache_path(root, region, name), "w",
                         encoding="utf-8") as fh:
                fh.write(json.dumps({"fetched_at": when, "elements": []}))
    return root


def test_no_cache_means_every_region_is_due():
    tmp = tempfile.mkdtemp()
    try:
        got = S.stale_regions(os.path.join(tmp, "pois"), today=TODAY)
        check("a tree with no POI cache refetches everything", got == ALL,
              "got %r" % got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_fresh_region_is_left_alone():
    tmp = tempfile.mkdtemp()
    try:
        root = _cache(tmp, {r: "2026-09-20" for r in ALL})
        got = S.stale_regions(root, today=TODAY)
        check("four days old is not stale", got == [], "got %r" % got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_thirty_days_is_still_fresh_and_thirty_one_is_not():
    """The boundary, stated both ways, because off-by-one here is a month of
    needless republishing or a month of stale fuel stations."""
    tmp = tempfile.mkdtemp()
    try:
        on = (TODAY - datetime.timedelta(days=30)).isoformat()
        over = (TODAY - datetime.timedelta(days=31)).isoformat()
        root = _cache(tmp, {r: on for r in ALL})
        check("exactly 30 days is not yet due",
              S.stale_regions(root, today=TODAY) == [], "30d came back stale")
        shutil.rmtree(root, ignore_errors=True)
        root = _cache(tmp, {r: over for r in ALL})
        check("31 days is due", S.stale_regions(root, today=TODAY) == ALL,
              "31d came back fresh")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_one_stale_region_does_not_drag_the_others_in():
    tmp = tempfile.mkdtemp()
    try:
        spec = {r: "2026-09-20" for r in ALL}
        spec["wales"] = "2026-01-01"
        got = S.stale_regions(_cache(tmp, spec), today=TODAY)
        check("only the region that is old is refetched", got == ["wales"],
              "got %r" % got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_oldest_category_decides_not_the_newest():
    """A region whose fuel was refreshed yesterday and whose toilets are from
    last year is a year-old region. Taking the newest would let one refreshed
    category hold a region's staleness down for ever."""
    tmp = tempfile.mkdtemp()
    try:
        spec = {name: "2026-09-23" for name, _ in PO.CATEGORIES}
        spec[PO.CATEGORIES[-1][0]] = "2025-09-23"
        per = {r: "2026-09-23" for r in ALL}
        per["north"] = spec
        got = S.stale_regions(_cache(tmp, per), today=TODAY)
        check("one year-old category makes the region due", got == ["north"],
              "got %r" % got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_half_fetched_region_is_stale():
    """`load_cached` refuses to build from a region missing a category, so a
    half-fetched region must be refetched rather than left to fail the build."""
    tmp = tempfile.mkdtemp()
    try:
        spec = {name: "2026-09-23" for name, _ in PO.CATEGORIES}
        spec[PO.CATEGORIES[0][0]] = None       # never written
        per = {r: "2026-09-23" for r in ALL}
        per["midlands"] = spec
        got = S.stale_regions(_cache(tmp, per), today=TODAY)
        check("a region missing one category is refetched", got == ["midlands"],
              "got %r" % got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_unreadable_cache_file_is_stale_not_fresh():
    """A truncated file from a killed run would otherwise sit there for ever
    looking current, because nothing else ever reads it until the build does."""
    tmp = tempfile.mkdtemp()
    try:
        root = _cache(tmp, {r: "2026-09-23" for r in ALL})
        with io.open(PO.cache_path(root, "wales", PO.CATEGORIES[0][0]), "w",
                     encoding="utf-8") as fh:
            fh.write('{"fetched_at": "2026-09-2')      # cut off mid-write
        got = S.stale_regions(root, today=TODAY)
        check("a truncated cache file makes its region due", got == ["wales"],
              "got %r" % got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_regions_are_the_pack_regions():
    """A POI region that is not a pack region fetches POIs into no container."""
    check("the decider walks build_packages.REGIONS",
          ALL == [r for r, _l, _b in P.REGIONS], "got %r" % ALL)


def test_the_output_is_the_shape_actions_reads():
    tmp = tempfile.mkdtemp()
    try:
        root = _cache(tmp, {r: "2026-09-23" for r in ALL})
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            S.main([root])
        line = buf.getvalue().strip()
        check("it prints a GITHUB_OUTPUT assignment",
              line.startswith("regions="), line)
        check("and an empty one when nothing is due", line == "regions=", line)
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

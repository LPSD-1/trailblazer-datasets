"""Which regions need their POIs re-fetched, as a GitHub Actions output.

WHY THIS IS A FILE AND NOT A HEREDOC IN THE WORKFLOW. It decides how often a
rider re-downloads an area container, which is the single biggest recurring
cost this project imposes on anyone. A rule that lives inside YAML cannot be
run, cannot be tested, and is read by nobody until it is wrong - and the last
time a stamp decided republishing without a test in front of it, it cost 121
republishes a month.

THE RULE. POIs travel inside the area container (docs/WAYS-SCHEMA.md), so
re-fetching them rewrites container bytes even when the ways have not moved.
The ways build runs four times a day; POIs must not. A region is refetched
when:

  - it has no cache at all, or
  - one of its cached categories is missing - a half-fetched region is stale,
    because `build_pois.load_cached` refuses to build from one, or
  - its OLDEST `fetched_at` is more than MAX_AGE_DAYS old.

Oldest, not newest: a region whose `fuel` was refreshed yesterday and whose
`toilets` is from last year is a year-old region. Taking the newest would let
one refreshed category hold the whole region's staleness down for ever.

Prints `regions=a,b,c` - empty when nothing is due.
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

#: Long enough that the 4x-daily ways build republishes containers for lane
#: reasons and not POI ones; short enough that a closed fuel station is not
#: still on the map next season.
MAX_AGE_DAYS = 30


def stale_regions(cache="cache/pois", today=None, max_age_days=MAX_AGE_DAYS):
    import build_packages as P
    import build_pois as PO
    today = today or datetime.date.today()
    out = []
    for region, _label, _bbox in P.REGIONS:
        dates = []
        complete = True
        for name, _selectors in PO.CATEGORIES:
            path = PO.cache_path(cache, region, name)
            if not os.path.exists(path):
                complete = False
                break
            try:
                with open(path, encoding="utf-8") as fh:
                    dates.append(json.load(fh)["fetched_at"])
            except (ValueError, KeyError, OSError):
                # UNREADABLE IS STALE, NOT FRESH. A truncated cache file from a
                # killed run would otherwise sit there for ever looking current.
                complete = False
                break
        if not complete or not dates:
            out.append(region)
            continue
        try:
            oldest = datetime.date.fromisoformat(min(dates))
        except ValueError:
            out.append(region)
            continue
        if (today - oldest).days > max_age_days:
            out.append(region)
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cache = argv[0] if argv else os.environ.get("POI_CACHE", "cache/pois")
    print("regions=" + ",".join(stale_regions(cache)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

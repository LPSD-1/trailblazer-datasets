#!/usr/bin/env python3
"""Re-check the cached rowmaps files, and take a newer one when it changed.

    python tools/rowmaps_refresh.py                     # up to --max files
    python tools/rowmaps_refresh.py --only DY,WT --max 0  # these, all due

WHY. fetch_rights_of_way.py downloads a rowmaps file only when the cache
has no copy of it, and the cache is carried from run to run. So a council
whose file rowmaps updated after our first fetch was never read again:
every authority without a live council layer (council_ways.py) stayed as
it was on the day it was first fetched.

WHAT THIS DOES. Each carried file (byways, restricted byways, bridleways -
never footpaths, which the build does not carry) is re-checked once every
RECHECK_DAYS, oldest check first, at most --max a run, with a conditional
GET: rowmaps answers 304 and no body when the file has not changed since
the Last-Modified it gave us, so a re-check of an unchanged file costs it
almost nothing. A changed file is taken the way council_ways takes a
council's layer (council_ways.merge_features): every record the new file
still draws is kept byte for byte, so its lane id is unchanged; a record it
no longer draws is dropped; a new or re-drawn record is added.

KEEP LAST GOOD. The cached file is replaced only by a newer file that
reads, holds at least two thirds of the records the old one did, and
agrees with it well enough to be the same network (the thresholds
council_ways.agrees uses). Anything else leaves the cached file exactly as
it was and is written to --report, which the lane workflow turns into an
issue. Every write is atomic: a crash part way leaves the old file whole.

Through tools/polite_http.py: an honest User-Agent, robots.txt obeyed
(rowmaps has none), paced.
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_ways  # noqa: E402
import polite_http  # noqa: E402
from polite_http import FetchFailed, Refused  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "cache")
CHECKS = "rowmaps-checks.json"
BASE = "https://www.rowmaps.com/jsons/%s/mutated%d.json"

#: The rowmaps types the lane build carries (build_packages.ROW_RULES).
#: Footpaths are not carried, so they are not re-read.
TYPES = {2: "bridleway", 3: "restricted_byway", 4: "byway_open_to_all_traffic"}

RECHECK_DAYS = 7
MAX_PER_RUN = 60
#: Keep-last-good: a new file under this share of the old one's records is
#: refused (an empty or truncated publish, not a county's worth of
#: stopping-up orders).
FLOOR_SHARE = 2.0 / 3.0
FLOOR_MIN = 6


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_atomic(path, data, compact=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        if compact:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, fh, indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


def due(cache, checks, today, codes=None, max_files=MAX_PER_RUN,
        recheck_days=RECHECK_DAYS):
    """[(key, code, type_no)] to re-check this run, oldest check first.

    Only files already in the cache: fetching a missing one is
    fetch_rights_of_way.py's job.
    """
    cutoff = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=recheck_days)).isoformat()
    out = []
    for code in sorted(os.listdir(cache)):
        if codes and code not in codes:
            continue
        if not os.path.isdir(os.path.join(cache, code)):
            continue
        for type_no, name in sorted(TYPES.items()):
            if not os.path.exists(os.path.join(cache, code, name + ".json")):
                continue
            key = "%s/%s" % (code, name)
            checked = (checks.get(key) or {}).get("checked") or ""
            if checked <= cutoff:
                out.append((checked, key, code, type_no))
    out.sort()
    if max_files:
        out = out[:max_files]
    return [(key, code, type_no) for _c, key, code, type_no in out]


def take(old_fc, new_fc):
    """-> (FeatureCollection to keep, report, refusal or None)."""
    old = old_fc.get("features") or []
    new = new_fc.get("features") or []
    if new_fc.get("type") != "FeatureCollection":
        return old_fc, None, "not a FeatureCollection"
    if old and not new:
        return old_fc, None, "the new file holds no records (old: %d)" \
            % len(old)
    if len(old) >= FLOOR_MIN and len(new) < len(old) * FLOOR_SHARE:
        return old_fc, None, ("the new file holds %d records against %d - "
                              "a bad publish, not a quiet month"
                              % (len(new), len(old)))
    features, report = council_ways.merge_features(old, new)
    if old:
        if report["kept"] < council_ways.MIN_KEPT_SHARE * len(old):
            return old_fc, report, (
                "the new file still draws only %d of the %d records held - "
                "too different to be the same network" % (report["kept"],
                                                          len(old)))
        if report["new_share"] > council_ways.MAX_NEW_SHARE:
            return old_fc, report, (
                "%.0f%% of the new file's length is not in the old one"
                % (100 * report["new_share"]))
    out = dict(new_fc)
    out["features"] = features
    return out, report, None


def refresh(client, cache=CACHE, today=None, codes=None,
            max_files=MAX_PER_RUN, log=print):
    """Returns ([(key, problem)], {key: summary}) and updates the checks."""
    today = today or datetime.date.today().isoformat()
    checks_path = os.path.join(cache, CHECKS)
    checks = _read(checks_path, {}) or {}
    problems, done = [], {}
    for key, code, type_no in due(cache, checks, today, codes, max_files):
        path = os.path.join(cache, code, TYPES[type_no] + ".json")
        entry = dict(checks.get(key) or {})
        url = BASE % (code, type_no)
        try:
            changed, body, headers = client.get_if_changed(
                url, last_modified=entry.get("last_modified"))
        except Refused as e:
            problems.append((key, "refused: %s" % e))
            continue
        except FetchFailed as e:
            # A 404 is rowmaps withdrawing the file: keep ours, say so.
            problems.append((key, "could not be read: %s" % e))
            continue
        entry["checked"] = today
        if not changed:
            entry["status"] = "unchanged"
            checks[key] = entry
            done[key] = "unchanged"
            continue
        try:
            new_fc = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            problems.append((key, "the new file is not JSON"))
            checks[key] = dict(entry, status="refused: not JSON")
            continue
        old_fc = _read(path, {"type": "FeatureCollection", "features": []})
        kept_fc, report, refusal = take(old_fc, new_fc)
        if refusal:
            problems.append((key, "kept the cached file: " + refusal))
            # Not marked as taken: the validator stays the old one, so the
            # next re-check reads the file again rather than calling it
            # unchanged.
            checks[key] = dict(entry, status="refused: " + refusal)
            continue
        _write_atomic(path, kept_fc, compact=True)
        entry["last_modified"] = next(
            (v for k, v in (headers or {}).items()
             if k.lower() == "last-modified"), None)
        entry["status"] = "taken"
        entry["taken"] = today
        if report:
            entry["last_change"] = {
                "kept": report["kept"], "dropped": len(report["dropped"]),
                "added": len(report["added"])}
        checks[key] = entry
        done[key] = "taken: %s" % (entry.get("last_change") or "first read")
        log("  %s: newer file taken %s" % (key, entry.get("last_change")))
    _write_atomic(checks_path, checks)
    return problems, done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="comma-separated authority codes")
    ap.add_argument("--max", type=int, default=MAX_PER_RUN,
                    help="most files re-checked this run (0: every one due)")
    ap.add_argument("--today")
    ap.add_argument("--report", help="write the problems here, one a line")
    args = ap.parse_args(argv)
    if not os.path.isdir(CACHE):
        print("no cache/ to re-check")
        return 0
    codes = set(c.strip().upper() for c in (args.only or "").split(",")
                if c.strip())
    problems, done = refresh(polite_http.PoliteClient(min_gap=1.5),
                             today=args.today, codes=codes or None,
                             max_files=args.max)
    taken = sum(1 for v in done.values() if v.startswith("taken"))
    print("rowmaps re-check: %d file(s) checked, %d newer taken, %d "
          "unchanged, %d problem(s)" % (len(done) + len(problems), taken,
                                         len(done) - taken, len(problems)))
    for key, problem in problems:
        print("::warning::rowmaps %s %s" % (key, problem))
    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join("%s: %s\n" % kp for kp in problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())

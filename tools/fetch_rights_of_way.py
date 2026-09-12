#!/usr/bin/env python3
"""Download England & Wales public rights of way from rowmaps.com.

rowmaps.com aggregates the definitive-map data that councils publish under the
Open Government Licence, and republishes it as GeoJSON split by path type. That
is the authoritative legal source for what is a right of way: it is the same
record the council would produce in court.

No part of this comes from any third-party green-laning service or from
anybody's member account. It is public sector information, ours to use and to
redistribute, provided the OGL attribution travels with it — which it does,
carried on every feature and surfaced in the app.

    python fetch_rights_of_way.py                 # everything, resumable
    python fetch_rights_of_way.py --only ON,DN    # just these authorities
    python fetch_rights_of_way.py --types 3,4     # just byways

Output: cache/<AUTHORITY>/<type>.json

Be kind to it. This is one person's site hosting data for 149 councils, and it
is doing us a considerable favour. Requests are paced and the cache means a
re-run costs nothing.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "https://www.rowmaps.com"
INDEX = BASE + "/datasets/"
UA = "TrailBlazer-data/1.0 (rights-of-way packaging; contact via github)"

# rowmaps splits each authority's GeoJSON into four files by path type.
TYPES = {
    1: "footpath",
    2: "bridleway",
    3: "restricted_byway",
    4: "byway_open_to_all_traffic",
}

# One request at a time, spaced. Nothing here is urgent.
REQUEST_GAP_S = 1.5


def cache_dir():
    # The repository root, one level above tools/, and it must stay the same
    # directory build_packages.py reads: the workflow caches <root>/cache and
    # nothing fetched into tools/cache was ever seen again.
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def authorities():
    """Two-letter code -> name, scraped once and cached."""
    path = os.path.join(cache_dir(), "authorities.json")
    if os.path.exists(path):
        with open(path, encoding="utf8") as fh:
            return json.load(fh)

    os.makedirs(cache_dir(), exist_ok=True)
    html = get(INDEX).decode("utf8", "replace")
    pairs = re.findall(r'href="([A-Z0-9]{2})/"[^>]*>(.*?)</a>', html, re.S)
    found = {}
    for code, name in pairs:
        found[code] = re.sub(r"<[^>]+>", "", name).replace("\xa0", " ").strip()
    if not found:
        sys.exit("could not read the authority list - the page markup changed")
    with open(path, "w", encoding="utf8") as fh:
        json.dump(found, fh, indent=1, ensure_ascii=False)
    print("authorities: %d" % len(found))
    return found


def fetch_one(code, type_no):
    """Returns (status, feature_count). Cached files are not re-fetched."""
    out_dir = os.path.join(cache_dir(), code)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "%s.json" % TYPES[type_no])

    if os.path.exists(out) and os.path.getsize(out) > 0:
        try:
            with open(out, encoding="utf8") as fh:
                return "cached", len(json.load(fh).get("features", []))
        except (ValueError, OSError):
            os.remove(out)  # truncated from an interrupted run; refetch

    url = "%s/jsons/%s/mutated%d.json" % (BASE, code, type_no)
    try:
        raw = get(url)
    except urllib.error.HTTPError as e:
        # Not every authority publishes every type. A missing byways file
        # means that council has none, not that the fetch failed.
        return ("none" if e.code == 404 else "http %d" % e.code), 0
    except Exception as e:  # noqa: BLE001 - report and carry on
        return "error: %s" % e, 0

    try:
        parsed = json.loads(raw.decode("utf8", "replace"))
    except ValueError:
        return "not json", 0
    if parsed.get("type") != "FeatureCollection":
        return "not a FeatureCollection", 0

    with open(out, "wb") as fh:
        fh.write(raw)
    return "ok", len(parsed.get("features", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated authority codes")
    ap.add_argument("--types", default="1,2,3,4",
                    help="comma-separated type numbers (default all)")
    args = ap.parse_args()

    wanted_types = [int(t) for t in args.types.split(",") if t.strip()]
    auth = authorities()
    codes = sorted(auth)
    if args.only:
        codes = [c.strip().upper() for c in args.only.split(",") if c.strip()]

    totals = {name: 0 for name in TYPES.values()}
    missing = []
    done = 0
    started = time.time()

    for code in codes:
        name = auth.get(code, code)
        line = []
        for t in wanted_types:
            status, count = fetch_one(code, t)
            if status == "ok":
                time.sleep(REQUEST_GAP_S)
            if status in ("ok", "cached"):
                totals[TYPES[t]] += count
                line.append("%s=%d" % (TYPES[t][:4], count))
            elif status == "none":
                line.append("%s=-" % TYPES[t][:4])
            else:
                line.append("%s=%s" % (TYPES[t][:4], status))
                missing.append((code, TYPES[t], status))
        done += 1
        print("[%3d/%3d] %s %-28s %s"
              % (done, len(codes), code, name[:28], "  ".join(line)))

    print("\n--- totals ---")
    for name, n in totals.items():
        print("  %-26s %7d" % (name, n))
    print("  elapsed: %.0fs" % (time.time() - started))
    if missing:
        print("\n%d fetches did not succeed:" % len(missing))
        for code, t, status in missing[:20]:
            print("   %s %s: %s" % (code, t, status))
        print("  re-run to retry only these; cached files are skipped")


if __name__ == "__main__":
    main()

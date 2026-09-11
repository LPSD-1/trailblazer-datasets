#!/usr/bin/env python3
"""Decide which satellite area to work on, so nobody has to.

    python satellite_plan.py --catalogue catalogue.json

Prints one JSON object on stdout describing today's job, or `{"work": false}`
when there is nothing due. The workflow reads it and does what it says.

WHY THE AREAS ARE NOT LISTED HERE
---------------------------------
They are the catalogue's OWN areas - the same Midlands, The North, Wales a
rider already sees in Downloads - minus the country-wide pseudo-areas that hold
routing tiles and nothing else. So adding a lane area adds a satellite area,
and there is no second list to keep in step. A separate config would have
drifted the first time somebody added a region.

Only areas that publish LANES get imagery. Those are the places this app has
anything to say about; buying a month of tile fetches for a country where we
publish no rights of way would be bytes spent on nothing.

WHY ONE AREA AT A TIME
----------------------
An area is a few thousand tiles - the Midlands is 6,859 at z13 - which is a
single polite run. A whole country in one go is 143,637, which is neither
polite nor recoverable. One area a day covers Britain in under a week and
refreshes the lot comfortably inside a month, and every run either produces a
finished pack or produces nothing: no half-built state to reason about.
"""
import argparse
import datetime as dt
import json
import sys


def areas_with_lanes(catalogue):
    """Every area that publishes lane data, with its bounds."""
    out = []
    for continent in catalogue.get("continents", []):
        for country in continent.get("countries", []):
            for area in country.get("areas", []):
                packs = area.get("packs", [])
                if not any(p.get("kind") == "lanes" for p in packs):
                    continue
                bounds = area.get("bounds")
                if not bounds:
                    continue
                out.append({
                    "id": "%s-satellite" % area["id"],
                    "area": area["id"],
                    "label": "Satellite - %s" % area.get("label", area["id"]),
                    "country": country.get("label", country.get("code", "")),
                    "bounds": bounds,
                })
    return out


def existing_satellite(catalogue):
    """Satellite packs already published, by id, with when they were built."""
    out = {}
    for continent in catalogue.get("continents", []):
        for country in continent.get("countries", []):
            for area in country.get("areas", []):
                for pack in area.get("packs", []):
                    if pack.get("kind") != "basemap":
                        continue
                    out[pack["id"]] = pack.get("generated")
    return out


def age_days(stamp, now):
    if not stamp:
        return None
    try:
        built = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if built.tzinfo is None:
        built = built.replace(tzinfo=dt.timezone.utc)
    return (now - built).total_seconds() / 86400.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalogue", required=True)
    ap.add_argument("--refresh-after-days", type=int, default=25,
                    help="a pack younger than this is left alone, so a full "
                         "cycle lands about once a month")
    ap.add_argument("--max-zoom", type=int, default=13)
    args = ap.parse_args()

    try:
        with open(args.catalogue, encoding="utf-8") as f:
            catalogue = json.load(f)
    except FileNotFoundError:
        print(json.dumps({"work": False, "why": "no catalogue yet"}))
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    published = existing_satellite(catalogue)
    candidates = []
    for area in areas_with_lanes(catalogue):
        age = age_days(published.get(area["id"]), now)
        # Never built comes first, then oldest. `None` sorts ahead of any
        # number, which is what we want and is worth being explicit about.
        candidates.append((0 if age is None else 1,
                           -(age or 0),
                           area, age))

    if not candidates:
        print(json.dumps({"work": False, "why": "no areas publish lanes"}))
        return 0

    candidates.sort(key=lambda c: (c[0], c[1]))
    rank, _, area, age = candidates[0]

    if rank == 1 and age is not None and age < args.refresh_after_days:
        print(json.dumps({
            "work": False,
            "why": "everything was rebuilt within %d days (oldest is %.1f)"
                   % (args.refresh_after_days, age),
        }))
        return 0

    b = area["bounds"]
    print(json.dumps({
        "work": True,
        "id": area["id"],
        "label": area["label"],
        "area": area["area"],
        "country": area["country"],
        "age_days": age,
        "max_zoom": args.max_zoom,
        "bbox": [b["west"], b["south"], b["east"], b["north"]],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

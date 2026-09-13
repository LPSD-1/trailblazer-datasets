#!/usr/bin/env python3
"""Decide which ground-height area to build, so nobody has to.

    python height_plan.py --catalogue catalogue.json

Prints one JSON object on stdout describing today's job, or `{"work": false}`
when there is nothing due. The workflow reads it and does what it says.

THE AREAS ARE THE CATALOGUE'S OWN, exactly as the imagery planner does it - the
same Midlands, The North, Wales a rider already sees in Downloads. Adding a
lane area adds a height area, and there is no second list to drift out of step.
Both planners share `areas_with_lanes` from `satellite_plan` rather than
keeping two copies of the walk, because two copies is how one of them comes to
believe in an area the other does not.

WHY IT IS NOT JUST satellite_plan WITH A FLAG
---------------------------------------------
The only real difference is which published pack counts as "this area is
already done", and that difference is load-bearing: keyed on the wrong kind,
the planner concludes an area has never been built and re-fetches it every
single run, for ever, against a free service. That has already happened once
here, when tiers changed the imagery pack ids. So the answer lives in a named
function with a test over it rather than in a flag.

The refresh interval is deliberately long. Ground height does not change.
Rebuilding is for picking up improvements to the SOURCE data, not for keeping
up with the landscape, so this is months rather than the imagery's weeks - and
every run that finds nothing due costs nothing at all.
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "satellite_plan", os.path.join(HERE, "satellite_plan.py"))
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)

# Height changes when the survey does, which is years. A pack older than this
# is rebuilt to pick up an improved source, and nothing more urgent than that.
DEFAULT_REFRESH_DAYS = 180


def height_id(area_id):
    """The pack id for an area. One place, so the builder and the planner
    cannot disagree about what a finished area is called."""
    return "%s-height" % area_id


def existing_height(catalogue):
    """Area ids that already have a published height pack.

    Keyed on the AREA, not on the pack id, for the reason in the header: an id
    scheme that changes later must not make every area look unbuilt.
    """
    seen = {}
    for continent in catalogue.get("continents", []):
        for country in continent.get("countries", []):
            for area in country.get("areas", []):
                for pack in area.get("packs", []):
                    if pack.get("kind") != "height":
                        continue
                    pid = pack.get("id") or ""
                    # Tolerate a suffix the way the imagery planner has to:
                    # if height ever publishes more than one detail level, the
                    # area is still built.
                    base = pid.split("-height")[0] + "-height" if \
                        "-height" in pid else pid
                    when = pack.get("generated")
                    if base and (base not in seen or
                                 (when or "") > (seen[base] or "")):
                        seen[base] = when
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalogue", default="catalogue.json")
    ap.add_argument("--refresh-after-days", type=int,
                    default=DEFAULT_REFRESH_DAYS)
    ap.add_argument("--max-zoom", type=int, default=12)
    args = ap.parse_args()

    try:
        with open(args.catalogue, encoding="utf-8") as f:
            catalogue = json.load(f)
    except (OSError, ValueError) as e:
        print(json.dumps({"work": False, "why": "no catalogue: %s" % e}))
        return 0

    built = existing_height(catalogue)
    now = dt.datetime.now(dt.timezone.utc)

    candidates = []
    for area in sp.areas_with_lanes(catalogue):
        # `area["id"]` from that helper is ALREADY the satellite pack id - it
        # was written for one caller and names its own output. `area["area"]`
        # is the plain area, which is what a height id is built from. Reading
        # the wrong one produced `gb-east-anglia-satellite-height`, which is
        # nobody's pack, and would have republished every area for ever
        # because nothing by that name could ever appear in the catalogue.
        pid = height_id(area["area"])
        when = built.get(pid)
        age = None
        if when:
            try:
                stamp = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
                age = (now - stamp).total_seconds() / 86400.0
            except ValueError:
                age = None
        # Never built sorts first, then oldest first.
        candidates.append((0 if age is None else 1, -(age or 0), area, age))

    if not candidates:
        print(json.dumps({"work": False, "why": "no areas publish lanes"}))
        return 0

    candidates.sort(key=lambda c: (c[0], c[1]))
    rank, _, area, age = candidates[0]

    if rank == 1 and age is not None and age < args.refresh_after_days:
        print(json.dumps({
            "work": False,
            "why": "every area was built within %d days (oldest is %.1f)"
                   % (args.refresh_after_days, age),
        }))
        return 0

    b = area["bounds"]
    print(json.dumps({
        "work": True,
        "id": height_id(area["area"]),
        # Same reason: the shared helper labels for ITS job.
        "label": "Ground height - %s"
                 % area["label"].replace("Satellite - ", ""),
        "area": area["area"],
        "country": area["country"],
        "age_days": age,
        "max_zoom": args.max_zoom,
        "bbox": [b["west"], b["south"], b["east"], b["north"]],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

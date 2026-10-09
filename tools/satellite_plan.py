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
import os
import subprocess
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
    """When each area's imagery was last built, by the AREA's satellite id.

    Keyed on the area rather than on the pack, because one area now publishes
    one pack per detail tier - `gb-south-east-satellite-standard` and
    `-high` - and the planner asks about `gb-south-east-satellite`. Keyed on
    the pack id it found neither, decided the South East had never been built,
    and would have rebuilt it every run for ever: a 20,000-tile fetch a night,
    against a free service, for imagery already published.

    The NEWEST of an area's tiers wins. They are written by one run and share a
    timestamp today, but a rebuild that failed part way through should leave
    the area looking as old as its oldest half rather than as young as its
    newest - so `min` would be the safer choice if they ever diverge. They
    cannot: record_satellite.py stamps every entry of a run with one value.
    """
    out, tiered = {}, set()
    for key, pid, pack in _satellite_packs(catalogue):
        if pid != key:
            tiered.add(key)
    for key, pid, pack in _satellite_packs(catalogue):
        # AN UNTIERED PACK LEFT BESIDE ITS AREA'S TIERS DOES NOT AGE IT.
        # East Anglia kept its September z13 pack after its z14 tiers were
        # published on 7 October 2026; taking the oldest of the three made
        # the area look 25 days old for ever, so it would have been rebuilt
        # every run while the Midlands waited behind it indefinitely.
        if key in tiered and pid == key:
            continue
        was = out.get(key)
        now = pack.get("generated")
        if was is None or (now is not None and now < was):
            out[key] = now
    return out


def _satellite_packs(catalogue):
    """(area key, pack id, pack) for every basemap pack: `<area>-satellite-
    <tier>` and the older untiered `<area>-satellite` share the key."""
    for continent in catalogue.get("continents", []):
        for country in continent.get("countries", []):
            for area in country.get("areas", []):
                for pack in area.get("packs", []):
                    if pack.get("kind") != "basemap":
                        continue
                    pid = pack.get("id", "")
                    marker = "-satellite"
                    at = pid.find(marker)
                    key = pid[: at + len(marker)] if at >= 0 else pid
                    yield key, pid, pack


def below_zoom(catalogue, max_zoom):
    """Areas with imagery but none at `max_zoom`: built before the z14
    decision, so due now, however recently they were built."""
    best = {}
    for key, _pid, pack in _satellite_packs(catalogue):
        z = pack.get("maxZoom") or 0
        best[key] = max(best.get(key, 0), z)
    return set(k for k, z in best.items() if z < max_zoom)


def wrong_attribution(catalogue, attribution):
    """Areas with any imagery pack published under other words than
    `attribution`: built from another mosaic, so due now, however young.

    This is how the 2024 mosaic, which is CC BY-NC-SA and so cannot ship in
    a paid app, is replaced: its packs say so in their `note`, and every one
    is due the day build_satellite.py's ATTRIBUTION changes, rather than when
    it happens to turn 25 days old.
    """
    return set(key for key, _pid, pack in _satellite_packs(catalogue)
               if pack.get("note") != attribution)


def block_host():
    """The host every imagery tile comes from, which a refusal blocks:
    build_satellite's SOURCE, so there is one definition."""
    import urllib.parse
    return urllib.parse.urlparse(_build_satellite().SOURCE).netloc


def build_satellite_attribution():
    return _build_satellite().ATTRIBUTION


def _build_satellite():
    """build_satellite, loaded here rather than at the top:
    height_plan imports this module, and its job installs no Pillow."""
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "build_satellite", os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "build_satellite.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A REFUSAL IS REMEMBERED. When EOX stops an area, build_satellite.py writes
# the stop to satellite/blocks.json under EOX's HOST (its time, the area, the
# HTTP status or "network", and any Retry-After), and the workflow commits it.
# Without that the next run, twelve hours on, asked the same service again.
# Every area comes from that one host, so none is planned for at least
# BLOCK_DAYS, or until Retry-After when EOX asked for longer; nothing
# shortens either.
BLOCK_DAYS = 7
NEVER = dt.datetime.max.replace(tzinfo=dt.timezone.utc)


def stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(text):
    built = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if built.tzinfo is None:
        built = built.replace(tzinfo=dt.timezone.utc)
    return built


def blocked_until(record):
    """When a host that refused us at record["at"] may be asked again. A
    record whose time cannot be read blocks until a person mends it."""
    try:
        at = _when(record["at"])
        asked = float(record.get("retry_after") or 0)
        until = at + max(dt.timedelta(days=BLOCK_DAYS),
                         dt.timedelta(seconds=asked))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return NEVER
    try:
        until = max(until, _when(record["until"]))
    except (KeyError, TypeError, ValueError):
        pass
    return until


def load_blocks(path):
    """{"hosts": {host: record}} from `path`; no file is no blocks. A
    file that is there and cannot be read raises: taking it as "no blocks"
    would ask again of a service that refused us."""
    if not path or not os.path.exists(path):
        return {"hosts": {}}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("hosts", {}),
                                                    dict):
        raise ValueError("%s is not {\"hosts\": {...}}" % path)
    data.setdefault("hosts", {})
    return data


# THE BLOCK ON MAIN, NOT ONLY THE ONE IN THIS CHECKOUT. A re-run of a refused
# run checks out the commit it started from, and a manual run can start from
# any branch: neither holds a stop committed to main since. So main's latest
# copy is fetched at plan time and read as well, and for each host the record
# that blocks for longer wins. If main's copy cannot be fetched or read,
# nothing is planned: "could not look" is never "no block".
BLOCKS_PATH = "satellite/blocks.json"

# How git is run; the tests swap it for a stand-in.
GIT_RUN = subprocess.run


def _git(run, cwd, *args):
    return run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE,
               stderr=subprocess.PIPE, universal_newlines=True)


def blocks_on(ref, run=None, cwd=None, path=BLOCKS_PATH):
    """{"hosts": {...}} from `ref`'s latest copy of `path` (ref is
    remote/branch), fetched first. Not on `ref` at all is no blocks;
    anything that cannot be fetched or read raises."""
    run = run or GIT_RUN
    remote, _, branch = ref.partition("/")
    if not remote or not branch:
        raise ValueError("%r is not remote/branch" % ref)
    # A shallow clone (actions/checkout's) stays shallow: depth 1 is all
    # this needs. In a full clone --depth would make it shallow, so not there.
    shallow = _git(run, cwd, "rev-parse", "--is-shallow-repository")
    depth = (["--depth=1"] if shallow.returncode == 0
             and shallow.stdout.strip() == "true" else [])
    fetched = _git(run, cwd, "fetch", "--quiet", *depth, remote,
                   "+refs/heads/%s:refs/remotes/%s" % (branch, ref))
    if fetched.returncode != 0:
        raise OSError("cannot fetch %s: %s" % (ref, fetched.stderr.strip()))
    listed = _git(run, cwd, "ls-tree", "--name-only", ref, "--", path)
    if listed.returncode != 0:
        raise OSError("cannot list %s on %s: %s"
                      % (path, ref, listed.stderr.strip()))
    if path not in listed.stdout.splitlines():
        return {"hosts": {}}
    shown = _git(run, cwd, "show", "%s:%s" % (ref, path))
    if shown.returncode != 0:
        raise OSError("cannot read %s on %s: %s"
                      % (path, ref, shown.stderr.strip()))
    data = json.loads(shown.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("hosts", {}),
                                                    dict):
        raise ValueError("%s on %s is not {\"hosts\": {...}}" % (path, ref))
    data.setdefault("hosts", {})
    return data


def merge_blocks(*copies):
    """Every host in any copy; where copies disagree, the record that
    blocks for longer."""
    hosts = {}
    for data in copies:
        for host, rec in data["hosts"].items():
            if (host not in hosts
                    or blocked_until(rec) > blocked_until(hosts[host])):
                hosts[host] = rec
    return {"hosts": hosts}


def live_blocks(local, ref=None, run=None, cwd=None):
    """The blocks that decide: this checkout's file, and `ref`'s latest
    copy when a ref is given. Raises when either cannot be read."""
    copies = [load_blocks(local)]
    if ref:
        copies.append(blocks_on(ref, run=run, cwd=cwd))
    return merge_blocks(*copies)


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
    # 14, which is a decision that was taken and then not carried out: every
    # pack published so far says maxZoom 13 because this default was never
    # moved. Sentinel-2 is 10 m/pixel and z13 already IS that resolution, so
    # z14 adds no optical detail - but a rider looks at RENDERED pixels, and at
    # z13 the phone stretches a 256px JPEG four times while at z14 it is handed
    # twice as many real pixels resampled offline. Zoomed in, which is the
    # condition anyone complains about, z14 is visibly better.
    #
    # It also unlocks the second detail tier: a z0-14 fetch CONTAINS z0-13, so
    # one run now publishes both and the picker in the app finally has
    # something to pick between.
    ap.add_argument("--max-zoom", type=int, default=14)
    ap.add_argument("--blocks",
                    help="satellite/blocks.json: hosts that refused us; "
                         "no area is planned until the block runs out")
    ap.add_argument("--blocks-ref",
                    help="also read the blocks file from this ref's latest "
                         "copy (origin/main), fetched first; the later block "
                         "wins, and a copy that cannot be read plans nothing")
    args = ap.parse_args()

    try:
        blocks = live_blocks(args.blocks, args.blocks_ref)["hosts"]
    except (OSError, ValueError) as e:
        print(json.dumps({"work": False,
                          "why": "cannot read the blocks file: %s" % e}))
        return 1

    try:
        with open(args.catalogue, encoding="utf-8") as f:
            catalogue = json.load(f)
    except FileNotFoundError:
        print(json.dumps({"work": False, "why": "no catalogue yet"}))
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    published = existing_satellite(catalogue)
    short = below_zoom(catalogue, args.max_zoom)
    relabel = wrong_attribution(catalogue, build_satellite_attribution())
    # ONE REFUSAL STOPS EVERY AREA. Every area is fetched from the same
    # host, so planning the next area after EOX said no to this one would
    # be working around the block.
    host = block_host()
    if host in blocks:
        until = blocked_until(blocks[host])
        if until > now:
            print(json.dumps({"work": False, "why": (
                "%s refused us (in %s), so no area is asked for until %s"
                % (host, blocks[host].get("area", "an area"),
                   "a person mends its record in the blocks file"
                   if until == NEVER else stamp(until)))}))
            return 0

    candidates = []
    for area in areas_with_lanes(catalogue):
        age = age_days(published.get(area["id"]), now)
        # Built from another mosaic (the licence) comes first: that imagery
        # is being served under terms we may not use, which is a live
        # breach. Then never built, then built below the agreed zoom, then
        # oldest.
        rank = (0 if area["id"] in relabel else 1 if age is None
                else 2 if area["id"] in short else 3)
        candidates.append((rank, -(age or 0), area, age))

    if not candidates:
        print(json.dumps({"work": False, "why": "no areas publish lanes"}))
        return 0

    candidates.sort(key=lambda c: (c[0], c[1]))
    rank, _, area, age = candidates[0]

    if rank == 3 and age is not None and age < args.refresh_after_days:
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

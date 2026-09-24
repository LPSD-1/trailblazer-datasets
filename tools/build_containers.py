"""Turn the built way packs into the containers the app actually downloads.

THE CUTOVER. `build_packages.py` goes on producing `.tbpack` because that is
where the fetched rows are assembled and checked; this converts its output into
the `.tbmap` containers described in the app's docs/MAP_ARCHITECTURE.md, and
those are what the catalogue publishes.

ONE DATASET, NOT FIVE. Until step 1.2 this built a set of containers per
vehicle, and `bicycle` and `horse` came out byte-identical because they were
the same bridleway data built twice. 109 containers on disk, and a phone asked
to mount them froze. There is now one dataset - `ways` - classed per way, and
the rider's vehicle is a filter over the derived access columns rather than a
partition of the download.

Two kinds come out, and section 19.1 is why:

    ways-<area>.tbmap        z11-z14, individual features, way records
    ways-overview.tbmap      z6-z10, coalesced, no records

There is ONE overview, because there is one dataset. The measured reason it
used to be per vehicle still stands and is now served by not carrying
footpaths at all: a single national overview across every vehicle type put
462,684 points in one z6 tile and took the app to 1.5 GB, and 435,299 of those
ways were footpaths with no bearing on a motor vehicle.

Everything is signed if a key is available; see `sign_release.py`.

Usage:
    python tools/build_containers.py --manifest manifest.json --out dist/containers
"""

import argparse
import collections
import base64
import gzip
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_map_container as B  # noqa: E402

try:
    import sign_release
except Exception:                                    # pragma: no cover
    sign_release = None


def _digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _download_bytes(path):
    """What a rider actually fetches: the container, compressed for transport.

    Reported rather than guessed, because section 18.2 turns on it and the raw
    file size is roughly three times larger.
    """
    with open(path, "rb") as fh:
        return len(gzip.compress(fh.read(), mtime=0))


def _add_pois(target, features, region, cache, log=print):
    """Put the POI tables into an area container, and say how many.

    AN EMPTY TABLE IS NOT A MISSING TABLE, and the app reads the difference.
    `TbMapStore.hasPois` is table presence, so a container with the tables and
    no rows says "we looked here and there is nothing", while a container
    without them says "nobody has fetched this region". A rider looking for
    fuel needs those told apart - the first is an answer and the second is a
    gap - and it is the same distinction `bounds` exists for.

    Returns the POI count, or None when this region has no cache, which is the
    state of every region until `build_pois.py fetch` has been run for it.
    """
    import build_pois as PO
    if not cache or not os.path.isdir(os.path.join(cache, region)):
        return None
    # A HALF-FETCHED REGION IS AN UNFETCHED ONE, not a failed build. Overpass
    # 504s on the dense categories (measured: `food` across the south-west, in
    # the first dry run of the cutover) and the fetch step deliberately shrugs
    # that off - "a region that will not come back does NOT fail the lane
    # build". `load_cached` then refused the incomplete region with
    # SystemExit, so the shrug was undone one step later and the courtesy call
    # to somebody else's server took down every rider's lanes. No tables and a
    # null count is the honest state: "nobody has fetched this", which
    # poi_staleness.py also reads it as, so the next run tries again.
    missing = [name for name, _ in PO.CATEGORIES
               if not os.path.exists(PO.cache_path(cache, region, name))]
    if missing:
        log("    %-40s POIs skipped: no cache for %s"
            % ("", ", ".join(missing)))
        return None
    bounds = B._bounds_of(features)
    if not bounds:
        return None
    # The container's OWN bounds, not the region's nominal box: a POI outside
    # the ways we actually shipped is one the rider cannot reach from anything
    # in this file, and it belongs to whichever area does cover it.
    bbox = tuple(float(v) for v in bounds.split(","))
    pois, _dates = PO.load_cached(cache, region, bbox, log=lambda *a: None)
    PO.write_pois(target, pois)
    log("    %-40s %6d POIs" % ("", len(pois)))
    return len(pois)


def build_all(manifest_path, out_dir, key, signing_key=None, root=".",
              poi_cache=None):
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    os.makedirs(out_dir, exist_ok=True)

    # THE MANIFEST'S OWN STAMP IS THE CLOCK, AND MUST NOT REACH A CONTAINER.
    #
    # `manifest["generated"]` is when this RUN built the manifest. Stamping it
    # into every container made every container new on every run: measured on
    # two CI builds of unchanged council data, one SQLite, `motor-wales.tbmap`
    # differed in FIVE bytes out of 1,114,112 - `built_at` moving from
    # 21:47:46Z to 22:52:13Z - and five bytes move the sha256, so every rider
    # re-downloads all 343 MB for ways that did not change.
    #
    # Each PACK carries its own `generated`, and build_packages keeps that
    # stamp when the ways have not changed - that is the whole mechanism that
    # makes packs rebuild byte-identical. Containers inherit it.
    run_stamp = manifest.get("generated") or ""
    dataset = manifest.get("dataset") or "ways"
    # Step 1.2c travels into every container, so the app can say what it did
    # not look for. Copied from the manifest and never decided here: the scope
    # is what build_packages.py built ('none' - byways only - by default since
    # the owner's decision of 2026-09-24), and an empty one is a build that
    # carried everything.
    context_scope = manifest.get("contextScope") or None
    context_note = manifest.get("contextNote") or None

    sources = []
    stamps = []
    entries = []

    # Which ways already have an area drawing them. Packs are walked in
    # manifest order, which is stable, so the owner of a boundary way is the
    # same on every rebuild - and a rebuild that changed it would rewrite tiles
    # in two areas for no reason.
    claimed = set()

    for pack in manifest.get("packages", []):
        source = os.path.join(root, pack["file"])
        if not os.path.exists(source):
            raise SystemExit("Missing pack: %s" % source)
        sources.append(source)
        # Fall back to the run stamp only for a manifest too old to carry one;
        # that costs one republish, where the clock costs one every month.
        pack_stamp = pack.get("generated") or run_stamp
        stamps.append(pack_stamp)

        name = "%s-%s.tbmap" % (dataset, pack["area"])
        target = os.path.join(out_dir, name)
        features = B.load_features([source], key)
        B.write_container(target, features, "area", B.AREA_ZOOMS, pack_stamp,
                          tile_exclude=claimed,
                          context_scope=context_scope,
                          context_note=context_note)
        claimed.update(f["properties"].get("lane_uid") for f in features)
        # POIs AFTER THE WAYS, into the same file. Same container, own tables -
        # WAYS-SCHEMA.md - so a rider who downloads an area gets the fuel and
        # the toilets with it and there is no second download to forget.
        poi_count = _add_pois(target, features, pack["area"], poi_cache)
        entry = _entry(target, pack, kind="area", dataset=dataset,
                       lane_count=len(features), generated=pack_stamp)
        # NULL, NOT ZERO, when nothing was fetched. The app shows "no POI data
        # for this area" rather than "no fuel in Wales".
        entry["poiCount"] = poi_count
        entries.append(entry)
        print("  %-42s %6d ways  %5.2f MB download"
              % (name, len(features), _download_bytes(target) / 1048576.0))

    # ONE overview, from every pack.
    #
    # THE FLOOR IS MEASURED, NOT CHOSEN. The dataset gets the lowest zoom whose
    # tiles are all under the ceiling, and a dataset that will not fit at any
    # of them gets NO overview rather than one that would take a phone down.
    if sources:
        name = "%s-overview.tbmap" % dataset
        target = os.path.join(out_dir, name)
        features = B.load_features(sources, key)
        floor = B.lowest_zoom_that_fits(
            features, B.OVERVIEW_ZOOMS[0], B.OVERVIEW_ZOOMS[1])
        if floor > B.OVERVIEW_ZOOMS[1]:
            print("  %-42s %6d ways  NO OVERVIEW - too dense to draw even at "
                  "z%d" % (name, len(features), B.OVERVIEW_ZOOMS[1]))
        else:
            # The newest pack it was built from. Deterministic, and it moves
            # only when one of its sources actually did.
            overview_stamp = max(stamps) if stamps else run_stamp
            B.write_container(target, features, "overview",
                              (floor, B.OVERVIEW_ZOOMS[1]), overview_stamp,
                              context_scope=context_scope,
                              context_note=context_note)
            entry = _entry(target, None, kind="overview", dataset=dataset,
                           lane_count=len(features),
                           generated=overview_stamp)
            entry["minZoom"] = floor
            entries.append(entry)
            print("  %-42s %6d ways  %5.2f MB download  (overview from z%d)"
                  % (name, len(features),
                     _download_bytes(target) / 1048576.0, floor))

    if signing_key is not None:
        for entry in entries:
            path = os.path.join(out_dir, os.path.basename(entry["file"]))
            signature, _ = sign_release.sign_file(path, signing_key)
            entry["signature"] = base64.b64encode(signature).decode("ascii")

    # THE NEWEST CONTAINER, NOT THE CLOCK.
    #
    # "nothing hashes it" was true and beside the point. refresh-data.yml's
    # Publish step DIFFS the containers/ tree, so a field that moves every run
    # makes the tree differ every run and publish every run. The top-level
    # manifest.json and catalogue.json are already held back for exactly this
    # reason; this one was missed. Monthly that is 12 needless republishes a
    # year and nobody noticed. On the 4x-daily clock step 1.9 puts the ways
    # build on, it is 121 a month - 121 commits, 121 catalogue rebuilds, and
    # "update available" on every rider's phone four times a day for data that
    # has not moved.
    #
    # So this is derived from the containers themselves. Identical data in,
    # identical manifest out, and the tree becomes a function of the data and
    # nothing else - which is what WAYS-SCHEMA.md asks for when it says
    # `built_at` must not leak into any pack's content hash.
    newest = max((e.get("generated") or "" for e in entries), default="")
    out = {
        "schema": 1,
        "generated": newest or run_stamp,
        "format": "tbmap",
        "dataset": dataset,
        "contextScope": context_scope or "",
        "contextNote": context_note or "",
        "containers": entries,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    return out


def _entry(path, pack, kind, dataset, lane_count, generated):
    """One row in the container manifest.

    NO `vehicle` KEY. Its absence is the step: a pack that names a vehicle is a
    pack that partitions the same way five times. `dataset` replaces it, and it
    is the same value on every row.
    """
    name = os.path.basename(path)
    return {
        "id": "gb-%s" % (pack["area"] if pack else "overview"),
        "kind": kind,
        "dataset": dataset,
        "area": pack["area"] if pack else None,
        "label": pack["label"] if pack else "Overview",
        "note": pack.get("note") if pack else
                "Where the lanes are, before you download an area.",
        "file": "containers/%s" % name,
        "sha256": _digest(path),
        "bytes": os.path.getsize(path),
        "downloadBytes": _download_bytes(path),
        "laneCount": lane_count,
        "generated": generated,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="manifest.json")
    ap.add_argument("--out", default="dist/containers")
    ap.add_argument("--root", default=".")
    ap.add_argument("--key", default=os.environ.get("DATASET_KEY_FILE"))
    # OFF UNLESS POINTED AT A CACHE. Every region without one is built exactly
    # as before, with no POI tables at all, so turning this on is a per-region
    # decision made by whether `build_pois.py fetch` has been run for it.
    ap.add_argument("--poi-cache", default=os.environ.get("POI_CACHE"),
                    help="the build_pois.py cache directory; regions with a "
                         "cache get POI tables in their area container")
    args = ap.parse_args()

    if not args.key:
        raise SystemExit("Set --key or DATASET_KEY_FILE to the pack key file.")
    with open(args.key, "r", encoding="utf-8") as fh:
        key = base64.b64decode(fh.read().strip())

    signing_key = None
    if sign_release is not None and (os.environ.get("TB_SIGNING_KEY")
                                     or os.environ.get("TB_SIGNING_KEY_PEM")):
        signing_key = sign_release._private_key()
    else:
        # SAID OUT LOUD. A build that quietly publishes unsigned containers is
        # a build whose riders cannot tell the publisher's data from anyone's,
        # and nobody would notice until it mattered.
        print("  WARNING: no signing key; containers will be unsigned.")

    print("Building containers")
    out = build_all(args.manifest, args.out, key, signing_key, args.root,
                    poi_cache=args.poi_cache)
    total = sum(e["downloadBytes"] for e in out["containers"])
    print("\n  %d containers, %.1f MB if a rider downloaded every one"
          % (len(out["containers"]), total / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())

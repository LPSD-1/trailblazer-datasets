"""Turn the built lane packs into the containers the app actually downloads.

THE CUTOVER. `build_packages.py` goes on producing `.tbpack` because that is
where the fetched rows are assembled and checked; this converts its output into
the `.tbmap` containers described in the app's docs/MAP_ARCHITECTURE.md, and
those are what the catalogue publishes.

Two kinds come out, and section 19.1 is why:

    <vehicle>-<area>.tbmap        z11-z14, individual features, lane records
    <vehicle>-overview.tbmap      z6-z10, coalesced, no records

The overview is PER VEHICLE, which section 23 established the hard way: one
national overview across every vehicle type put 462,684 points in a single z6
tile and took the app to 1.5 GB. A motorcyclist's whole national overview is
10,583 points and 35.6 kB. There is no reason for them to carry the footpath
network, and every reason not to.

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


def build_all(manifest_path, out_dir, key, signing_key=None, root="."):
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
    # re-downloads all 343 MB for lanes that did not change.
    #
    # It also cost a publish. The catalogue check compares a pack against the
    # file on disk, and containers that were "new" every run made 129 of them
    # disagree with what was published an hour earlier.
    #
    # Each PACK carries its own `generated`, and build_packages keeps that
    # stamp when the lanes have not changed - that is the whole mechanism that
    # makes packs rebuild byte-identical. Containers now inherit it, which is
    # what build_map_container.py has always done when run directly: "From the
    # source, never the clock". This driver was the way round it.
    run_stamp = manifest.get("generated") or ""
    by_vehicle = collections.defaultdict(list)
    stamps_by_vehicle = collections.defaultdict(list)
    entries = []

    # Which lanes already have an area drawing them, per vehicle. Packs are
    # walked in manifest order, which is stable, so the owner of a boundary lane
    # is the same on every rebuild - and a rebuild that changed it would rewrite
    # tiles in two areas for no reason.
    claimed = collections.defaultdict(set)

    for pack in manifest.get("packages", []):
        source = os.path.join(root, pack["file"])
        if not os.path.exists(source):
            raise SystemExit("Missing pack: %s" % source)
        vehicle = pack["package"]
        by_vehicle[vehicle].append(source)
        # Fall back to the run stamp only for a manifest too old to carry one;
        # that costs one republish, where the clock costs one every month.
        pack_stamp = pack.get("generated") or run_stamp
        stamps_by_vehicle[vehicle].append(pack_stamp)

        name = "%s-%s.tbmap" % (vehicle, pack["area"])
        target = os.path.join(out_dir, name)
        features = B.load_features([source], key)
        already = claimed[vehicle]
        B.write_container(target, features, "area", B.AREA_ZOOMS, pack_stamp,
                          tile_exclude=already)
        already.update(f["properties"].get("lane_uid") for f in features)
        entries.append(_entry(target, pack, kind="area", vehicle=vehicle,
                              lane_count=len(features), generated=pack_stamp))
        print("  %-42s %6d lanes  %5.2f MB download"
              % (name, len(features), _download_bytes(target) / 1048576.0))

    # One overview per vehicle, from every pack that vehicle has.
    #
    # THE FLOOR IS MEASURED, NOT CHOSEN. A motorcyclist's national overview fits
    # in five z6 tiles of 35 kB; a walker's does not, and no tolerance fixes it
    # (section 23). So each vehicle gets the lowest zoom whose tiles are all
    # under the ceiling, and a vehicle whose data will not fit at any of them
    # gets NO overview rather than one that would take a phone down.
    for vehicle, sources in sorted(by_vehicle.items()):
        name = "%s-overview.tbmap" % vehicle
        target = os.path.join(out_dir, name)
        features = B.load_features(sources, key)
        floor = B.lowest_zoom_that_fits(
            features, B.OVERVIEW_ZOOMS[0], B.OVERVIEW_ZOOMS[1])
        if floor > B.OVERVIEW_ZOOMS[1]:
            print("  %-42s %6d lanes  NO OVERVIEW - too dense to draw even at "
                  "z%d" % (name, len(features), B.OVERVIEW_ZOOMS[1]))
            continue
        # The newest pack it was built from. Deterministic, and it moves only
        # when one of its sources actually did.
        overview_stamp = max(stamps_by_vehicle[vehicle])             if stamps_by_vehicle[vehicle] else run_stamp
        B.write_container(target, features, "overview",
                          (floor, B.OVERVIEW_ZOOMS[1]), overview_stamp)
        entry = _entry(target, None, kind="overview", vehicle=vehicle,
                       lane_count=len(features), generated=overview_stamp)
        entry["minZoom"] = floor
        entries.append(entry)
        print("  %-42s %6d lanes  %5.2f MB download  (overview from z%d)"
              % (name, len(features), _download_bytes(target) / 1048576.0,
                 floor))

    if signing_key is not None:
        for entry in entries:
            path = os.path.join(out_dir, os.path.basename(entry["file"]))
            signature, _ = sign_release.sign_file(path, signing_key)
            entry["signature"] = base64.b64encode(signature).decode("ascii")

    out = {
        "schema": 1,
        # This one IS the run: it describes when the manifest was written, and
        # nothing hashes it.
        "generated": run_stamp,
        "format": "tbmap",
        "containers": entries,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    return out


def _entry(path, pack, kind, vehicle, lane_count, generated):
    name = os.path.basename(path)
    return {
        "id": "gb-%s-%s" % (pack["area"] if pack else "overview", vehicle),
        "kind": kind,
        "vehicle": vehicle,
        "area": pack["area"] if pack else None,
        "label": pack["label"] if pack else "Overview - %s" % vehicle,
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
    out = build_all(args.manifest, args.out, key, signing_key, args.root)
    total = sum(e["downloadBytes"] for e in out["containers"])
    print("\n  %d containers, %.1f MB if a rider downloaded every one"
          % (len(out["containers"]), total / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())

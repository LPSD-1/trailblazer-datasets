#!/usr/bin/env python3
"""Turn the fetched rights-of-way data into encrypted, timestamped packages.

Input:  cache/<AUTHORITY>/<type>.json   (from fetch_rights_of_way.py)
Output: dist/
          manifest.json
          <vehicle>-<date>.tbpack       encrypted AES-256-GCM
          <vehicle>-<date>.tbpack.sha256

One package per vehicle access type, because that is how a rider chooses: a
motorcyclist has no use for 140,000 footpaths, and downloading them costs them
data and storage for nothing.

    python build_packages.py --key ../trailblazer-keys/dataset-encryption-key-256.b64

The OGL attribution is carried on the collection AND on every feature. That is
a licence condition, not decoration: strip it and we lose the right to use any
of this.
"""
import argparse
import base64
import glob
import gzip
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    sys.exit("pip install cryptography")

# ---- container format -------------------------------------------------------
# Byte-compatible with the app's Tbpack reader (lib/data/crypto/tbpack.dart).
# Do not change one without changing the other, and republish: the magic is
# written into every published pack.
MAGIC = b"TBPK"
VERSION = 1
ALG_AES_GCM_256 = 1
NONCE_LEN = 12

# ---- what each right of way means for a vehicle ------------------------------
# Straight from the Highways Act and the definitive map categories. No judgement
# calls here: a BOAT is open to all traffic, a restricted byway is not open to
# mechanically propelled vehicles, and so on.
ROW_RULES = {
    "byway_open_to_all_traffic": {
        "lane_class": "full-access",
        "designation": "Byway open to all traffic (BOAT)",
        "vehicles": ["motorcycle", "4x4", "bicycle", "horse", "foot"],
    },
    "restricted_byway": {
        "lane_class": "restricted",
        "designation": "Restricted byway",
        "vehicles": ["bicycle", "horse", "foot"],
    },
    "bridleway": {
        "lane_class": "restricted",
        "designation": "Public bridleway",
        "vehicles": ["bicycle", "horse", "foot"],
    },
    "footpath": {
        "lane_class": "restricted",
        "designation": "Public footpath",
        "vehicles": ["foot"],
    },
}

# Which types go into which downloadable package. A rider picks their vehicle
# and gets only what is legally relevant to it.
PACKAGES = {
    "motor": {
        "label": "Motorcycle and 4x4",
        "types": ["byway_open_to_all_traffic"],
        "note": "Byways open to all traffic - the lanes you may legally ride.",
    },
    "bicycle": {
        "label": "Bicycle",
        "types": ["byway_open_to_all_traffic", "restricted_byway", "bridleway"],
        "note": "Byways and bridleways.",
    },
    "horse": {
        "label": "Horse",
        "types": ["byway_open_to_all_traffic", "restricted_byway", "bridleway"],
        "note": "Byways and bridleways.",
    },
    "foot": {
        "label": "On foot",
        "types": list(ROW_RULES),
        "note": "Every recorded right of way.",
    },
}

# These boxes must TILE England and Wales with no hole between them.
#
# They did not. Measured against real places on 2026-09-11, five towns fell
# outside every box, so every right of way in them was published in no package
# at all - present in the source data, absent from the product, and nothing
# anywhere said so:
#
#     Gloucester, Stroud, Cirencester   between South West (north 51.55),
#                                       Wales (east -2.60) and Midlands
#                                       (south 51.90) - the Cotswolds, which
#                                       is as green-lane as England gets
#     Aylesbury                         between South East (north 51.80) and
#                                       Midlands (south 51.90)
#     Skegness and the Lincolnshire     east of The North (east 0.20) and
#     coast                             north of East Anglia (north 53.00)
#
# The edges now meet or overlap on every seam. Overlap is harmless - in_region
# publishes a straddling lane in both packages and the app dedupes on id - so
# when in doubt, overlap. test_build_packages.py checks real towns against
# these, and build_packages refuses to publish if any lane lands outside them.
#
# Must match lib/features/regions/region_providers.dart exactly: the app asks
# for packages by region id, and a mismatch means a silent 404. The same holes
# were in the app's copy, so a rider standing in Gloucester matched no region.
REGIONS = [
    ("south-west", "South West", (-6.45, 49.85, -1.85, 51.95)),
    ("south-east", "South East", (-1.85, 50.50, 1.45, 51.95)),
    ("east-anglia", "East Anglia", (-0.40, 51.50, 1.85, 53.05)),
    ("midlands", "Midlands", (-3.25, 51.90, 0.15, 53.60)),
    ("wales", "Wales", (-5.35, 51.35, -2.60, 53.45)),
    ("north", "The North", (-3.70, 53.00, 1.85, 55.90)),
]

OGL = ("Contains public sector information licensed under the Open Government "
       "Licence v3.0. Source: local highway authority definitive maps, via "
       "rowmaps.com.")

# The largest plaintext a single package may reach, in bytes.
#
# Not a guess. Measured by test/pack_size_probe_test.dart in the app, parsing
# real packages through the app's own reader:
#
#     plain   2.0 MB ->  peak RSS  +16 MB
#     plain  25.6 MB ->  peak RSS +210 MB
#     plain 140.2 MB ->  peak RSS +981 MB
#
# So peak memory runs about 8x the plaintext, because decrypting, decoding and
# parsing all hold their own copy at once. Android gives an app a heap of
# 256-512 MB and the map needs most of it, so a package that costs ~100 MB to
# open is the most we can ask for: 12 MB of plaintext.
#
# Region-sized packages broke this badly - the Midlands on foot was 137 MB of
# JSON and 168,132 ways, which would have killed the app outright on any phone.
MAX_PLAIN_BYTES = 12 * 1024 * 1024


def slugify(text):
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "area"


def feature_bytes(feature):
    """What this one lane costs in the serialised collection."""
    return len(json.dumps(feature, separators=(",", ":")).encode("utf8")) + 1


def split_by_authority(features):
    """Cut one vehicle+region's lanes into pieces a phone can actually open.

    Grouped by highway authority rather than by an arbitrary rectangle, because
    the authority is what a rider recognises: 'Derbyshire' is an answer,
    'Midlands part 7 of 12' is not. Authorities are kept whole where they fit
    and split by number only where a single county is too big on its own.

    Returns [(label, [feature, ...]), ...]; a single unnamed piece when the
    whole lot already fits.
    """
    total = sum(feature_bytes(f) for f in features)
    if total <= MAX_PLAIN_BYTES:
        return [(None, features)]

    by_authority = {}
    for f in features:
        by_authority.setdefault(
            f["properties"]["authority"] or "Unknown", []).append(f)

    pieces = []
    for name in sorted(by_authority):
        owned = by_authority[name]
        size = sum(feature_bytes(f) for f in owned)
        if size <= MAX_PLAIN_BYTES:
            pieces.append(([(name, len(owned))], owned, size))
            continue
        # One county too big by itself. Numbered slices are worse to read than
        # a county name, but they are still a place the rider can point at.
        parts = -(-size // MAX_PLAIN_BYTES)
        per = -(-len(owned) // parts)
        for i in range(parts):
            slice_ = owned[i * per:(i + 1) * per]
            if slice_:
                pieces.append((
                    [("%s (%d of %d)" % (name, i + 1, parts), len(slice_))],
                    slice_,
                    sum(feature_bytes(f) for f in slice_),
                ))

    # Pack small authorities together so a rider is not offered thirty rows of
    # a few hundred paths each.
    merged = []
    for names, feats, size in pieces:
        if merged and merged[-1][2] + size <= MAX_PLAIN_BYTES:
            merged[-1][0].extend(names)
            merged[-1][1].extend(feats)
            merged[-1][2] += size
        else:
            merged.append([list(names), list(feats), size])

    out = []
    for names, feats, _ in merged:
        # Named after the authority with the MOST lanes in the chunk, not the
        # alphabetically first.
        #
        # `sorted(by_authority)` put "Bath and North East Somerset" at the
        # front of every chunk it appeared in, and in_region deliberately
        # pulls in lanes that straddle a boundary - so the published Wales
        # index carried an area called "Bath and North East Somerset and 25
        # more" on the strength of a handful of border lanes. A rider looking
        # for Welsh byways cannot be expected to recognise that, and an area
        # named after somewhere 80 miles inside another country reads as a
        # bug in the data rather than a label.
        ranked = sorted(names, key=lambda nc: (-nc[1], nc[0]))
        shown = [n for n, _ in ranked]
        if len(shown) <= 2:
            label = " and ".join(shown)
        else:
            label = "%s and %d more" % (shown[0], len(shown) - 1)
        out.append((label, feats))
    return out


def cache_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def dist_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")


def load_key(path):
    with open(path, encoding="utf8") as fh:
        key = base64.b64decode(fh.read().strip())
    if len(key) != 32:
        sys.exit("key must be 32 bytes (got %d)" % len(key))
    return key


def pack(payload_bytes, key):
    """Same container the app already reads: prefix is the AAD."""
    compressed = gzip.compress(payload_bytes, mtime=0)
    nonce = os.urandom(NONCE_LEN)
    prefix = MAGIC + bytes([VERSION, ALG_AES_GCM_256]) + nonce
    sealed = AESGCM(key).encrypt(nonce, compressed, prefix)
    return prefix + sealed


def parse_description(desc):
    """rowmaps packs the useful fields into a pipe-separated Description.

    'BO|ON:22|0.144|none|-1.30876|51.66111|...'
      type code, authority:sheet, length km, surface/notes, then endpoints.
    Only the length is worth keeping; the rest we already know or can derive.
    """
    if not isinstance(desc, str):
        return {}
    bits = desc.split("|")
    out = {}
    if len(bits) > 2:
        try:
            out["length_km"] = float(bits[2])
        except (ValueError, IndexError):
            pass
    if len(bits) > 3 and bits[3] and bits[3] != "none":
        out["note"] = bits[3]
    return out


def normalise(feature, authority_code, authority_name, row_type):
    """One council feature -> one lane in the shape the app already parses."""
    geom = feature.get("geometry") or {}
    if geom.get("type") != "LineString":
        return None
    coords = geom.get("coordinates") or []
    if len(coords) < 2:
        return None

    props = feature.get("properties") or {}
    rule = ROW_RULES[row_type]
    extra = parse_description(props.get("Description"))

    # The council's own path number, e.g. 'ON|100|2/10'. Keep it: it is how a
    # rider or a council officer would refer to this specific way.
    ref = props.get("Name") or ""
    path_no = ref.split("|")[-1] if "|" in ref else ref

    # The geometry hash is ALWAYS part of the id, never just a fallback.
    #
    # Path numbers are not unique. Councils reuse them across separate ways -
    # Nottinghamshire has 23 distinct byways all numbered BOAT12 - so an id of
    # authority+number collapses them into one. The app dedupes on this id, to
    # stop a lane published in two neighbouring regions being drawn twice, and
    # that dedupe was silently throwing away 826 of the Midlands' 2,155 byways.
    # 38% of the data, gone, with nothing to show it had happened.
    #
    # Hashing the coordinates fixes both halves at once: distinct ways get
    # distinct ids, and the SAME way in two regions still hashes identically,
    # because region splitting copies geometry rather than clipping it.
    geom_hash = hashlib.sha1(
        json.dumps(coords, separators=(",", ":")).encode()).hexdigest()[:10]
    uid = "%s-%s-%s" % (authority_code, path_no, geom_hash) if path_no \
        else "%s-%s" % (authority_code, geom_hash)

    name = "%s %s" % (rule["designation"], path_no) if path_no \
        else rule["designation"]

    return {
        "type": "Feature",
        "properties": {
            "lane_uid": uid,
            "class": rule["lane_class"],
            "county": authority_name,
            "name": name,
            "designation": rule["designation"],
            "vehicles": rule["vehicles"],
            "rowType": row_type,
            "authority": authority_name,
            "authorityCode": authority_code,
            # Licence condition. Travels with every feature.
            "attribution": OGL,
            **({"lengthKm": extra["length_km"]} if "length_km" in extra else {}),
            **({"description": extra["note"]} if "note" in extra else {}),
        },
        "geometry": {"type": "LineString", "coordinates": coords},
    }


def load_all(authorities):
    """-> {row_type: [feature, ...]}"""
    by_type = {t: [] for t in ROW_RULES}
    skipped = Counter()
    # Ids now carry a geometry hash, so two records sharing one are the same
    # way listed twice by the council - same authority, same path number, same
    # coordinates. Collapsing those is right. What must NEVER be collapsed is
    # two DIFFERENT ways that happen to share a number, which is what the old
    # id did and why write_package still checks.
    seen = set()

    for code in sorted(authorities):
        name = authorities[code]
        for row_type in ROW_RULES:
            path = os.path.join(cache_dir(), code, "%s.json" % row_type)
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf8") as fh:
                    fc = json.load(fh)
            except (ValueError, OSError) as e:
                skipped["unreadable %s/%s" % (code, row_type)] += 1
                continue
            for f in fc.get("features", []):
                lane = normalise(f, code, name, row_type)
                if lane is None:
                    skipped["bad geometry"] += 1
                    continue
                uid = lane["properties"]["lane_uid"]
                if uid in seen:
                    skipped["exact duplicate"] += 1
                    continue
                seen.add(uid)
                by_type[row_type].append(lane)

    if skipped:
        print("  skipped: %s" % dict(skipped))
    return by_type


def report_orphans(orphans):
    """Say WHERE the uncovered lanes are, so the boxes can be fixed."""
    west = min(lon for f in orphans for lon, _ in f["geometry"]["coordinates"])
    east = max(lon for f in orphans for lon, _ in f["geometry"]["coordinates"])
    south = min(lat for f in orphans for _, lat in f["geometry"]["coordinates"])
    north = max(lat for f in orphans for _, lat in f["geometry"]["coordinates"])

    by_authority = {}
    for f in orphans:
        name = f["properties"].get("authority") or "Unknown"
        by_authority[name] = by_authority.get(name, 0) + 1

    print("\n%d lanes matched no region." % len(orphans))
    print("  they span  west %.2f  south %.2f  east %.2f  north %.2f"
          % (west, south, east, north))
    print("  by authority:")
    for name, n in sorted(by_authority.items(), key=lambda kv: -kv[1])[:15]:
        print("    %-40s %d" % (name, n))
    if len(by_authority) > 15:
        print("    ... and %d more authorities" % (len(by_authority) - 15))


def in_region(feature, box):
    """A lane belongs to a region if ANY of it is inside.

    Lanes that straddle a boundary land in both packages. That is deliberate:
    a rider who has downloaded only the Midlands should still see the whole of
    a lane that crosses into Wales, and the app dedupes on id when two
    packages are loaded together.
    """
    west, south, east, north = box
    for lon, lat in feature["geometry"]["coordinates"]:
        if west <= lon <= east and south <= lat <= north:
            return True
    return False


def write_package(pkg_name, region_id, region_label, area_label, features,
                  key, stamp):
    """Seal one downloadable piece.

    [area_label] is None when the whole region fits in one package; then the
    area IS the region and the app shows the region's name.
    """
    spec = PACKAGES[pkg_name]
    area_id = region_id if area_label is None else \
        "%s-%s" % (region_id, slugify(area_label))
    shown = region_label if area_label is None else area_label

    # The app dedupes on lane_uid when it loads neighbouring areas together, so a
    # non-unique id inside one package is data the rider will never see. This
    # cost us 38% of the Midlands once; it does not get to happen quietly again.
    uids = [f["properties"]["lane_uid"] for f in features]
    if len(set(uids)) != len(uids):
        dupes = Counter(uids)
        worst = [u for u, n in dupes.most_common(3) if n > 1]
        sys.exit(
            "FATAL: %s/%s has %d duplicate lane ids (e.g. %s).\n"
            "The app would silently drop them. Fix normalise() before "
            "publishing."
            % (pkg_name, area_id, len(uids) - len(set(uids)), ", ".join(worst)))

    collection = {
        "type": "FeatureCollection",
        "generated": stamp,
        "package": pkg_name,
        "region": region_id,
        "area": area_id,
        "label": "%s - %s" % (spec["label"], shown),
        "note": spec["note"],
        "attribution": OGL,
        "features": features,
    }
    payload = json.dumps(collection, separators=(",", ":")).encode("utf8")
    sealed = pack(payload, key)

    pkg_dir = os.path.join(dist_dir(), "packages")
    os.makedirs(pkg_dir, exist_ok=True)
    date = stamp[:10]
    fname = "%s-%s-%s.tbpack" % (pkg_name, area_id, date)
    out = os.path.join(pkg_dir, fname)
    with open(out, "wb") as fh:
        fh.write(sealed)

    digest = hashlib.sha256(sealed).hexdigest()
    with open(out + ".sha256", "w", encoding="utf8") as fh:
        fh.write("%s  %s\n" % (digest, fname))

    return {
        "package": pkg_name,
        "region": region_id,
        "regionLabel": region_label,
        "area": area_id,
        "areaLabel": shown,
        "label": "%s - %s" % (spec["label"], shown),
        "note": spec["note"],
        "file": "packages/" + fname,
        "sha256": digest,
        "bytes": len(sealed),
        "plainBytes": len(payload),
        "laneCount": len(features),
        "generated": stamp,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="base64 32-byte key file")
    ap.add_argument("--only", help="comma-separated package names")
    ap.add_argument("--allow-orphans", action="store_true",
                    help="publish even though some lanes match no region")
    args = ap.parse_args()

    key = load_key(args.key)
    with open(os.path.join(cache_dir(), "authorities.json"), encoding="utf8") as fh:
        authorities = json.load(fh)

    print("reading cache...")
    by_type = load_all(authorities)
    for t, fs in by_type.items():
        print("  %-26s %7d" % (t, len(fs)))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    wanted = [p.strip() for p in args.only.split(",")] if args.only \
        else list(PACKAGES)

    entries = []
    orphans = []
    print("\nbuilding packages (vehicle x region)...")
    for pkg_name in wanted:
        pool = []
        for t in PACKAGES[pkg_name]["types"]:
            pool.extend(by_type.get(t, []))
        if not pool:
            print("  %-9s SKIPPED - nothing to package" % pkg_name)
            continue
        placed = set()
        for region_id, region_label, box in REGIONS:
            features = [f for f in pool if in_region(f, box)]
            if not features:
                continue
            placed.update(f["properties"]["lane_uid"] for f in features)
            for area_label, part in split_by_authority(features):
                entry = write_package(pkg_name, region_id, region_label,
                                      area_label, part, key, stamp)
                entries.append(entry)
                over = "  OVER BUDGET" \
                    if entry["plainBytes"] > MAX_PLAIN_BYTES else ""
                print("  %-9s %-34s %6d lanes  %5.1f MB sealed "
                      "(%5.1f MB plain)%s"
                      % (pkg_name, entry["area"], entry["laneCount"],
                         entry["bytes"] / 1048576,
                         entry["plainBytes"] / 1048576, over))

        orphans.extend(
            f for f in pool if f["properties"]["lane_uid"] not in placed)

    # A lane that fell outside every region box.
    #
    # There was no check at all here, and REGIONS is six hand-written boxes
    # covering an island with a very awkward shape. Anything they miss is
    # simply not published: it is in the source data, it is in no package, no
    # rider ever sees it, and the build prints a page of healthy-looking
    # numbers either way. That is the worst kind of data bug - the product is
    # quietly smaller than it claims and nothing says so.
    if orphans:
        report_orphans(orphans)
        if not args.allow_orphans:
            sys.exit(
                "refusing to publish: %d lanes belong to no region. Widen the "
                "boxes in REGIONS to cover them, or pass --allow-orphans if "
                "they are genuinely outside the area this dataset serves."
                % len(orphans))

    manifest = {
        "schema": 1,
        "generated": stamp,
        "attribution": OGL,
        "licence": "OGL-3.0",
        "source": "Local highway authority definitive maps via rowmaps.com",
        "authorities": len(authorities),
        "maxPlainBytes": MAX_PLAIN_BYTES,
        "regions": [
            {"id": r, "label": lab,
             "bounds": {"west": b[0], "south": b[1], "east": b[2], "north": b[3]}}
            for r, lab, b in REGIONS
        ],
        "packages": entries,
    }
    with open(os.path.join(dist_dir(), "manifest.json"), "w", encoding="utf8") as fh:
        json.dump(manifest, fh, indent=1)

    print("\nwrote %s" % os.path.join(dist_dir(), "manifest.json"))
    print("timestamp: %s" % stamp)


if __name__ == "__main__":
    main()

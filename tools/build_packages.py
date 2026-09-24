#!/usr/bin/env python3
"""Turn the fetched rights-of-way data into encrypted, timestamped packages.

Input:  cache/<AUTHORITY>/<type>.json   (from fetch_rights_of_way.py)
Output: dist/
          manifest.json
          <vehicle>-<area>.tbpack       encrypted AES-256-GCM
          <vehicle>-<area>.tbpack.sha256

The output is REPRODUCIBLE: build the same council data twice and you get the
same bytes, down to the SHA-256. Nothing else in this file matters as much to
a rider on a phone tethered to their bike - see pack() and write_package().

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
import hmac
import json
import math
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

# ---- what each right of way IS, and what that means for a motor vehicle ----
#
# docs/WAYS-SCHEMA.md is the contract. One dataset, classed per way, replacing
# four overlapping builds of the same ways: `bicycle` and `horse` were
# byte-identical because they WERE the same bridleway data built twice.
#
# Straight from the Highways Act and the definitive map categories. No
# judgement calls: a BOAT is open to all traffic, a restricted byway is not
# open to mechanically propelled vehicles, and a bridleway never was.
#
# FOOTPATHS ARE NOT CARRIED. 435,299 of them, 627 MB, and not one has any
# bearing on where a motor vehicle may legally go. They are read from the cache
# so the build can still say how many it declined, and then dropped.
ROW_RULES = {
    "byway_open_to_all_traffic": {
        "way_class": "boat",
        "designation": "Byway open to all traffic (BOAT)",
        "legal_tier": "statutory",
        "motorbike_ok": 1,
        "fourxfour_ok": 1,
        "access_reason": "Byway open to all traffic: a public right of way "
                         "for every kind of traffic, including motor "
                         "vehicles.",
        "access_evidence": "statutory",
        "carried": True,
        "context": False,
    },
    "restricted_byway": {
        "way_class": "restricted_byway",
        "designation": "Restricted byway",
        "legal_tier": "statutory",
        "motorbike_ok": 0,
        "fourxfour_ok": 0,
        "access_reason": "Restricted byway: no mechanically propelled "
                         "vehicles. Shown so you can see where a byway ends.",
        "access_evidence": "statutory",
        "carried": True,
        "context": True,
    },
    "bridleway": {
        "way_class": "bridleway",
        "designation": "Public bridleway",
        "legal_tier": "statutory",
        "motorbike_ok": 0,
        "fourxfour_ok": 0,
        "access_reason": "Public bridleway: horse, foot and pedal cycle only "
                         "- no mechanically propelled vehicles. Shown so you "
                         "can see where a byway ends.",
        "access_evidence": "statutory",
        "carried": True,
        "context": True,
    },
    #: THE FIFTH CLASS, and the only one not derived from a definitive map.
    #:
    #: Outside England and Wales there is no definitive map to read, so a way
    #: is carried on OSM's word and is drawn amber - "verify locally" - rather
    #: than green. WAYS-SCHEMA.md names the class, the reader maps it and the
    #: compat view maps it; the rules table was the one place it was missing,
    #: which meant nothing in the pipeline could produce a way whose
    #: `legal_tier` was not 'statutory'. The contract test then had a
    #: `legal_tier` check that a reader hard-coding 'statutory' passed.
    #:
    #: `access_evidence` is 'osm', NOT 'statutory'. The distinction is the
    #: whole point of the column: this is a way somebody mapped, not a right
    #: somebody recorded, and the record card must not claim otherwise.
    #:
    #: NOT REACHED BY ANY SOURCE YET - the world tier is phase 7. It is
    #: declared here so the shape exists and can be tested before the data
    #: arrives, not because anything builds it today.
    "osm_track": {
        "way_class": "osm_track",
        "designation": "Track (OpenStreetMap)",
        "legal_tier": "osm",
        "motorbike_ok": 1,
        "fourxfour_ok": 1,
        "access_reason": "Mapped as a track on OpenStreetMap. There is no "
                         "definitive map here: check locally before you ride "
                         "it.",
        "access_evidence": "osm",
        "carried": True,
        "context": False,
    },
    "footpath": {
        "way_class": "footpath",
        "designation": "Public footpath",
        "legal_tier": "statutory",
        "motorbike_ok": 0,
        "fourxfour_ok": 0,
        "access_reason": "Public footpath: on foot only.",
        "access_evidence": "statutory",
        "carried": False,
        "context": False,
    },
}

#: The one dataset. There is no vehicle here and there must not be one: the
#: rider's vehicle is a filter the app applies to the derived access columns,
#: not a partition of the download.
DATASET = "ways"

DATASET_LABEL = "Green lanes and byways"

#: Step 1.2c, DECIDED: carry context ways only where they meet a motor-legal
#: way.
#:
#: MEASURED over the full published population (10,342 BOATs, 89,300 bridleways
#: and restricted byways): 73.6% of context ways are nowhere near a BOAT. They
#: are carried for exactly one job - answering "the byway ends here" at the
#: point where it ends - and that question cannot arise a mile from any byway.
#:
#: WHAT THIS COSTS, AND WHY THE METADATA MUST SAY SO. A rider who looks at a
#: hillside and sees no bridleway must not read that as "there is no bridleway
#: here". There is; we did not carry it. CONTEXT_NOTE travels into every
#: container's meta and the manifest, and the app is required to show it
#: wherever it draws context ways. An absence in our data must never be
#: presented as an absence on the ground.
CONTEXT_SCOPE = "near-byways-only"
CONTEXT_RADIUS_KM = 1.0
CONTEXT_NOTE = (
    "Bridleways and restricted byways are shown only within %g km of a byway "
    "open to all traffic. Where none is shown, this map has not looked - it "
    "does not mean there is none on the ground."
) % CONTEXT_RADIUS_KM

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

# Ground this dataset knowingly does not serve.
#
# Scotland has no equivalent of the English and Welsh definitive map. The Land
# Reform (Scotland) Act 2003 gives a general right of responsible access to
# most land instead of recording individual rights of way, so the source
# carries almost nothing north of the border - one lane, in Highland, at the
# time of writing.
#
# Publishing a "Scotland" region out of that would be this app implying
# knowledge it does not have, in the most damaging direction available: a rider
# would download it, see a single lane, and conclude Scotland has nothing to
# ride - when in fact it has the most generous access rights in Britain, and
# the absence is in the RECORDING rather than on the ground.
#
# So it is excluded deliberately and by name rather than swept up with
# --allow-orphans. Anything that falls outside every region and is NOT here
# still fails the build, which is the entire value of the check: a genuine hole
# opening up in England or Wales must never be hidden by a flag somebody added
# once to get a release out.
UNSERVED = [
    ("Scotland", (-9.00, 55.90, 0.00, 61.00)),
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


# ---- step 1.2c: which context ways are near enough to be worth carrying ----

#: One kilometre, in degrees, at GB latitudes. Latitude is a constant
#: 1/110.574 deg per km; longitude is taken at 54 deg N, the middle of the
#: published coverage, where a degree is 111.320*cos(54) = 65.4 km. Using the
#: MIDDLE latitude makes the cell slightly too wide in Cornwall and slightly
#: too narrow in Northumberland, so the grid is only an index - every candidate
#: pair is then measured properly by [_km_between].
_KM_LAT = 1.0 / 110.574
_KM_LON = 1.0 / 65.40


def _km_between(a, b):
    """Equirectangular distance in km. Good to better than 0.1% over 1 km."""
    (lon1, lat1), (lon2, lat2) = a, b
    mid = math.radians((lat1 + lat2) / 2.0)
    x = math.radians(lon2 - lon1) * math.cos(mid)
    y = math.radians(lat2 - lat1)
    return 6371.0088 * math.hypot(x, y)


def near_motor_ways(context, motor, radius_km=CONTEXT_RADIUS_KM):
    """-> the context ways within [radius_km] of a motor-legal way.

    Vertex to vertex, over a grid of [radius_km] cells so only the nine cells
    around a point are ever searched. Vertex proximity rather than true
    segment distance, which is an approximation in one direction only: it can
    call a way far when a long straight segment passes close between two
    vertices. The source is densely sampled - measured median vertex spacing is
    tens of metres, not kilometres - so the cases where that bites are rare,
    and the error drops a way rather than admitting one.
    """
    if not motor:
        return []
    cells = {}
    for f in motor:
        for lon, lat in f["geometry"]["coordinates"]:
            cells.setdefault(
                (int(lon / (_KM_LON * radius_km)),
                 int(lat / (_KM_LAT * radius_km))), []).append((lon, lat))

    out = []
    for f in context:
        hit = False
        for lon, lat in f["geometry"]["coordinates"]:
            cx = int(lon / (_KM_LON * radius_km))
            cy = int(lat / (_KM_LAT * radius_km))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for point in cells.get((cx + dx, cy + dy), ()):
                        if _km_between((lon, lat), point) <= radius_km:
                            hit = True
                            break
                    if hit:
                        break
                if hit:
                    break
            if hit:
                break
        if hit:
            out.append(f)
    return out


#: A fixed-width stand-in, replaced with the pack's real cut date in
#: sealed_at(). It has to be the same LENGTH as the date it becomes, because
#: split_by_authority decides the container split from the serialised size.
SOURCE_DATE_PLACEHOLDER = "0000-00-00"


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


def repo_root():
    """The checkout, one level above tools/.

    cache/ and dist/ belong to the REPOSITORY, not to this directory. They were
    resolved against tools/ here and in fetch_rights_of_way.py, while every
    workflow, .gitignore and the other tools use the root - so the monthly
    refresh fetched into tools/cache, built into tools/dist, and then looked
    for dist/manifest.json, which was not there. Anchoring both to the root is
    what makes `python tools/build_packages.py` from the checkout agree with
    the workflow that runs it.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cache_dir():
    return os.path.join(repo_root(), "cache")


def dist_dir():
    return os.path.join(repo_root(), "dist")


def published_packages(path):
    """What is already on riders' phones -> {"<package>/<area>": entry}.

    Read from the manifest that is committed at the top of the repository,
    which is the one the app is serving right now. Missing, empty or unreadable
    all mean the same thing and are all fine: every package is then treated as
    new, which is what a first publish is.
    """
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf8") as fh:
            manifest = json.load(fh)
    except (ValueError, OSError) as e:
        print("  could not read %s (%s); every package counts as new"
              % (path, e))
        return {}
    return {
        "%s/%s" % (p.get("package"), p.get("area") or p.get("region")): p
        for p in manifest.get("packages", [])
    }


def load_key(path):
    with open(path, encoding="utf8") as fh:
        key = base64.b64decode(fh.read().strip())
    if len(key) != 32:
        sys.exit("key must be 32 bytes (got %d)" % len(key))
    return key


def pack(payload_bytes, key):
    """Same container the app already reads: prefix is the AAD.

    The nonce is DERIVED FROM THE PAYLOAD, not drawn at random, so sealing the
    same lanes twice produces the same bytes. That is the whole point: with
    os.urandom here, a monthly rebuild of council data nobody had amended still
    produced 97 packages with 97 new hashes, and every rider re-downloaded
    ~100 MB for nothing. Determinism is what makes "councils published nothing
    new" a fact the build can check rather than a hope.

    WHY THIS IS SAFE, since deriving a GCM nonce looks alarming
    -----------------------------------------------------------
    AES-GCM fails catastrophically when one (key, nonce) pair seals two
    DIFFERENT plaintexts: the keystream repeats, so the ciphertexts XOR to the
    plaintexts' XOR, and the GHASH subkey can be recovered from the pair, which
    hands an attacker forgeries for that key. So the property that has to hold
    is "different plaintext, different nonce" - NOT "every encryption gets a
    fresh nonce".

    A PRF of the plaintext gives exactly that property:

      * identical plaintext -> identical nonce. Not reuse across two messages:
        it is the SAME message sealed twice, which yields a byte-for-byte
        identical file and so tells an attacker nothing a second copy of the
        file would not have told them anyway.
      * different plaintext -> different nonce, unless HMAC-SHA256 truncated to
        96 bits collides. That is a birthday bound of 2**48 packages; we publish
        about a hundred a month, and an attacker cannot search for a collision
        without the key.

    This is the synthetic-IV construction that deterministic AEADs (AES-SIV,
    RFC 5297) are built on, with the domain string keeping this use of the key
    from colliding with any other.

    What it gives up is real and is fine here: deterministic encryption leaks
    that two packs have identical CONTENT. These are public rights of way
    published under the Open Government Licence, whose whole purpose is to be
    downloaded - and manifest.json already prints every pack's SHA-256 in the
    clear, so pack equality was public before this line existed. The README is
    blunt about what the sealing is for: "a speed bump, not a lock".

    Nothing on the device changes. The nonce travels in the prefix exactly as
    before and the reader takes it from there; it never recomputes it. Old and
    new packs open identically.
    """
    compressed = gzip.compress(payload_bytes, mtime=0)
    nonce = hmac.new(key, b"tbpack-nonce-v1\x00" + compressed,
                     hashlib.sha256).digest()[:NONCE_LEN]
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
            "class": rule["way_class"],
            "county": authority_name,
            "name": name,
            "designation": rule["designation"],
            "rowType": row_type,
            "authority": authority_name,
            "authorityCode": authority_code,
            # LEGAL PROVENANCE, per way and never per pack. A region may hold
            # statutory and OSM-derived ways side by side once the world tier
            # lands, and the map colours them differently.
            "legal_tier": rule["legal_tier"],
            "source": "rowmaps:%s" % slugify(authority_name),
            # Overwritten with this pack's cut date at seal time; fixed width
            # so the size accounting in split_by_authority stays exact.
            "source_date": SOURCE_DATE_PLACEHOLDER,
            # DERIVED ACCESS, with its reason. Never a bare boolean: a rider
            # who is not shown a lane is owed the sentence saying why.
            "motorbike_ok": rule["motorbike_ok"],
            "fourxfour_ok": rule["fourxfour_ok"],
            "access_reason": rule["access_reason"],
            "access_evidence": rule["access_evidence"],
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


def unserved(feature):
    """Whether this lane is on ground the dataset knowingly does not cover.

    Only ever asked of lanes that already matched no region, so "any part of it
    is in an unserved box" is the right test: a lane straddling the border
    would have been placed in The North and never reach here.
    """
    return any(in_region(feature, box) for _, box in UNSERVED)


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
                  key, stamp, published=None, note=None):
    """Seal one downloadable piece.

    [area_label] is None when the whole region fits in one package; then the
    area IS the region and the app shows the region's name.

    [published] is what is already on riders' phones, from published_packages().
    It is what lets an unchanged package keep the date it was cut - and
    therefore keep its bytes. See the comment on the seal below.
    """
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

    def sealed_at(cut):
        """Exactly the bytes this package publishes as, cut on [cut].

        `source_date` is stamped HERE rather than in normalise(), because it is
        the date this data was cut and that is what the reproducibility
        mechanism below establishes. Setting it earlier would freeze the run
        clock into every way and hand every rider a fresh download a month.
        """
        day = cut[:10]
        for f in features:
            f["properties"]["source_date"] = day
        collection = {
            "type": "FeatureCollection",
            "generated": cut,
            "package": pkg_name,
            "region": region_id,
            "area": area_id,
            "label": "%s - %s" % (DATASET_LABEL, shown),
            "note": note or "",
            "contextScope": CONTEXT_SCOPE,
            "contextNote": CONTEXT_NOTE,
            "attribution": OGL,
            "features": features,
        }
        body = json.dumps(collection, separators=(",", ":")).encode("utf8")
        return body, pack(body, key)

    # An unchanged package keeps the date it was CUT, and so keeps its bytes.
    #
    # A deterministic nonce is not on its own enough to make a rebuild
    # reproducible, because the build stamp is INSIDE the payload: rebuild
    # untouched council data and the timestamp alone gives every package a new
    # plaintext, a new ciphertext and a new hash. So the stamp cannot simply be
    # "now" - it has to be the date this data was actually cut.
    #
    # Which we can establish exactly, without decrypting anything: seal the
    # package with the date the published one claims, and see whether it
    # reproduces the published SHA-256. It can only do that if every lane, its
    # geometry and its label are identical to what is already on riders'
    # phones. Then the pack is republished unchanged and the rider downloads
    # nothing.
    #
    # Riders are shown this date as when their lane data was cut. Advancing it
    # monthly while the data underneath stood still was telling them their map
    # was fresher than it was, which is the one direction a mapping app must
    # never round in.
    #
    # A mismatch is always safe: it means "rebuild it", which is what happened
    # every month before this existed. So a new zlib, a changed field order or
    # a first publish costs one extra republish, never a stale one.
    payload = sealed = None
    previous = (published or {}).get("%s/%s" % (pkg_name, area_id))
    if previous and previous.get("generated") and previous.get("sha256"):
        payload, sealed = sealed_at(previous["generated"])
        if hashlib.sha256(sealed).hexdigest() == previous["sha256"]:
            stamp = previous["generated"]
        else:
            payload = sealed = None
    if sealed is None:
        payload, sealed = sealed_at(stamp)

    pkg_dir = os.path.join(dist_dir(), "packages")
    os.makedirs(pkg_dir, exist_ok=True)
    # No build date in the name.
    #
    # The date made every run write new paths whatever was in them, so git saw
    # a whole new set of files even when the bytes were identical, and the
    # "nothing new" branch in the refresh workflow could never fire. Nothing
    # downstream wants it: the app saves each pack under a dateless local name
    # so a new build supersedes the old copy on the device, and the workflow
    # replaces the packages directory wholesale, so nothing accumulates here
    # either.
    fname = "%s-%s.tbpack" % (pkg_name, area_id)
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
        "label": "%s - %s" % (DATASET_LABEL, shown),
        "note": note or "",
        "contextScope": CONTEXT_SCOPE,
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
    ap.add_argument("--allow-orphans", action="store_true",
                    help="publish even though some lanes match no region")
    ap.add_argument("--context", choices=("near", "all", "none"),
                    default="near",
                    help="step 1.2c. 'near' carries bridleways and restricted "
                         "byways within 1 km of a byway open to all traffic; "
                         "'all' carries every one of them; 'none' carries "
                         "BOATs alone. The decision is 'near', and the "
                         "measurement for all three prints either way.")
    ap.add_argument("--measure-only", action="store_true",
                    help="print the step 1.2c measurement and write nothing")
    ap.add_argument("--previous",
                    default=os.path.join(repo_root(), "manifest.json"),
                    help="the published manifest. A package whose ways have "
                         "not changed since it keeps the date it was cut, and "
                         "so rebuilds byte for byte. Pass '' to rebuild "
                         "everything as new.")
    args = ap.parse_args()

    key = load_key(args.key)
    with open(os.path.join(cache_dir(), "authorities.json"), encoding="utf8") as fh:
        authorities = json.load(fh)

    print("reading cache...")
    by_type = load_all(authorities)
    for t, fs in by_type.items():
        carried = "carried" if ROW_RULES[t]["carried"] else "NOT CARRIED"
        print("  %-26s %7d  %s" % (t, len(fs), carried))

    motor = by_type.get("byway_open_to_all_traffic", [])
    context_all = [f for t, fs in by_type.items() if ROW_RULES[t]["context"]
                   for f in fs]

    # STEP 1.2c, MEASURED BOTH WAYS, ON EVERY RUN.
    #
    # A decision recorded once in a document drifts away from the data under
    # it. This prints the number the decision rests on at the moment the
    # decision is applied, so a build where it stopped being true says so
    # instead of quietly carrying on.
    print("")
    print("step 1.2c - how much context to carry")
    near = near_motor_ways(context_all, motor)
    near_uids = set(f["properties"]["lane_uid"] for f in near)
    far = len(context_all) - len(near)
    pct = (lambda n: 100.0 * n / len(context_all) if context_all else 0.0)
    print("  byways open to all traffic         %7d" % len(motor))
    print("  context ways (bridleway + restr.)  %7d" % len(context_all))
    print("    within %.1f km of a byway         %7d  (%.1f%%)"
          % (CONTEXT_RADIUS_KM, len(near), pct(len(near))))
    print("    nowhere near one                 %7d  (%.1f%%)"
          % (far, pct(far)))
    for name, n in (("none", len(motor)),
                    ("near", len(motor) + len(near)),
                    ("all", len(motor) + len(context_all))):
        mark = "  <- DECIDED (1.2c)" if name == "near" else ""
        print("  option %-4s total ways in dataset  %7d%s" % (name, n, mark))

    if args.context == "near":
        pool = motor + [f for f in context_all
                        if f["properties"]["lane_uid"] in near_uids]
    elif args.context == "all":
        pool = motor + context_all
    else:
        pool = list(motor)
    print("  building with --context %s: %d ways" % (args.context, len(pool)))

    if args.measure_only:
        return

    note = "Byways open to all traffic - the lanes you may legally ride."
    if args.context == "near":
        note = note + " " + CONTEXT_NOTE

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    published = published_packages(args.previous)
    if published:
        print("")
        print("comparing against %d published packages in %s"
              % (len(published), args.previous))

    entries = []
    placed = set()
    print("")
    print("building ONE dataset per region (no vehicle partition)...")
    for region_id, region_label, box in REGIONS:
        features = [f for f in pool if in_region(f, box)]
        if not features:
            continue
        placed.update(f["properties"]["lane_uid"] for f in features)
        for area_label, part in split_by_authority(features):
            entry = write_package(DATASET, region_id, region_label,
                                  area_label, part, key, stamp,
                                  published=published, note=note)
            entries.append(entry)
            over = "  OVER BUDGET" \
                if entry["plainBytes"] > MAX_PLAIN_BYTES else ""
            state = "unchanged" if entry["generated"] != stamp else ""
            print("  %-42s %6d ways  %5.1f MB sealed (%5.1f MB plain) %-9s%s"
                  % (entry["area"], entry["laneCount"],
                     entry["bytes"] / 1048576.0,
                     entry["plainBytes"] / 1048576.0, state, over))

    orphans = [f for f in pool if f["properties"]["lane_uid"] not in placed]

    # A way that fell outside every region box.
    #
    # REGIONS is six hand-written boxes over an island with a very awkward
    # shape. Anything they miss is simply not published: it is in the source
    # data, in no package, no rider ever sees it, and the build prints a page
    # of healthy-looking numbers either way.
    if orphans:
        known = set(f["properties"]["lane_uid"] for f in orphans
                    if unserved(f))
        genuine = [f for f in orphans
                   if f["properties"]["lane_uid"] not in known]

        if known:
            print("")
            print("%d ways are on ground this dataset does not serve "
                  "(see UNSERVED); left out deliberately." % len(known))

        if genuine:
            report_orphans(genuine)
            if not args.allow_orphans:
                sys.exit(
                    "refusing to publish: %d ways belong to no region. Widen "
                    "the boxes in REGIONS to cover them, add them to UNSERVED "
                    "if this dataset genuinely does not serve that ground, or "
                    "pass --allow-orphans for a one-off."
                    % len(genuine))

    by_class = Counter(f["properties"]["class"] for f in pool)
    print("")
    print("ways by class, whole dataset:")
    for name, n in sorted(by_class.items()):
        print("  %-20s %7d" % (name, n))
    not_carried = dict((t, len(fs)) for t, fs in by_type.items()
                       if not ROW_RULES[t]["carried"])
    if not_carried:
        print("not carried: %s" % not_carried)

    manifest = {
        "schema": 1,
        "schemaVersion": 1,
        "generated": stamp,
        "attribution": OGL,
        "licence": "OGL-3.0",
        "source": "Local highway authority definitive maps via rowmaps.com",
        "authorities": len(authorities),
        "maxPlainBytes": MAX_PLAIN_BYTES,
        "dataset": DATASET,
        # Step 1.2c travels WITH THE DATA, not only in a decision document.
        # The app is required to show contextNote wherever it draws context
        # ways, so their absence is never read as absence on the ground.
        "contextScope": CONTEXT_SCOPE if args.context == "near" else args.context,
        "contextRadiusKm": CONTEXT_RADIUS_KM,
        "contextNote": CONTEXT_NOTE if args.context == "near" else "",
        "wayClassCounts": dict(sorted(by_class.items())),
        "notCarried": not_carried,
        "regions": [
            {"id": r, "label": lab,
             "bounds": {"west": b[0], "south": b[1], "east": b[2], "north": b[3]}}
            for r, lab, b in REGIONS
        ],
        "packages": entries,
    }
    with open(os.path.join(dist_dir(), "manifest.json"), "w",
              encoding="utf8") as fh:
        json.dump(manifest, fh, indent=1)

    print("")
    print("wrote %s" % os.path.join(dist_dir(), "manifest.json"))
    print("timestamp: %s" % stamp)

    same = [e for e in entries if e["generated"] != stamp]
    if published:
        print("%d of %d packages are byte-identical to the published build"
              % (len(same), len(entries)))


if __name__ == "__main__":
    main()

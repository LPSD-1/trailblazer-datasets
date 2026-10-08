#!/usr/bin/env python3
"""Turn the fetched rights-of-way data into encrypted, timestamped packages.

Input:  cache/<AUTHORITY>/<type>.json   (from fetch_rights_of_way.py)
        council-ways/<AUTHORITY>.json  (from council_ways.py: the council's
                                        own live byway layer, where it has one)
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
import re
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import council_ucrs  # noqa: E402
import council_ways  # noqa: E402
import duplicate_ways  # noqa: E402
from text_clean import clean_text  # noqa: E402

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
    #: THE SIXTH CLASS, AND THE FIRST NOT READ FROM A RIGHTS OF WAY RECORD.
    #:
    #: An unsurfaced unclassified road is a PUBLIC ROAD: adopted highway the
    #: council maintains and lists in its List of Streets, where the NERC Act
    #: 2006 s67(2)(b) kept its motor vehicle rights. It is on no definitive
    #: map, so no rowmaps file has it; council_ucrs.py reads the councils
    #: that publish theirs (Devon first, on the owner's decision of 8 October
    #: 2026 that every green lane is to be shown).
    #:
    #: `legal_tier` is 'highway_record', NOT 'statutory': the record is the
    #: council's highway record, a different kind of evidence from a
    #: definitive-map BOAT, and the sheet must be able to say which it is
    #: reading. Open to both motor flags because it is a public road; an
    #: order can still close it, exactly as one closes a byway.
    #:
    #: NOT IN `features`: it travels in its own pack member (`ucrFeatures`),
    #: its own container table (`ucr_ways`) and its own tile layer (`ucr`),
    #: because an app built before 119 reads `ucr` as unknown and draws it
    #: red - "you may not ride this" over a public road. See
    #: docs/WAYS-SCHEMA.md "Unsurfaced unclassified roads".
    "unsurfaced_unclassified_road": {
        "way_class": "ucr",
        "designation": "Unsurfaced unclassified road (UCR)",
        "legal_tier": "highway_record",
        "motorbike_ok": 1,
        "fourxfour_ok": 1,
        # {council} is the council whose records it is (normalise_ucr).
        "access_reason": "Unsurfaced unclassified road: a public road the "
                         "council maintains, recorded in {council}'s highway "
                         "records. Being a public road does not on its own "
                         "prove every vehicle may use it - check the signs "
                         "and any traffic orders.",
        "access_evidence": "highway_record",
        "carried": True,
        "context": False,
        "council_record": True,
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

#: Step 1.2c: how much CONTEXT to carry. Context is the bridleways and
#: restricted byways - rights of way no motor vehicle may use (ROW_RULES
#: "context": True).
#:
#: DECIDED BY THE OWNER, 2026-09-24: BYWAYS ONLY, which is `--context none`.
#: His choice, verbatim: "Carry only ways a motor vehicle may use. Smaller
#: download, but the byway-ends warning and red 'no motor vehicles' lanes go."
#: So the dataset carries every byway open to all traffic and every OSM track
#: (motorbike_ok=1, "check locally" - also a way a motor vehicle may use), and
#: no bridleway and no restricted byway at all. Footpaths were never carried.
#:
#: THIS SUPERSEDES 'near', decided and measured earlier the same day: carry
#: context ways only within CONTEXT_RADIUS_KM of a motor-legal way, for one job
#: - answering "the byway ends here" at the point where it ends. The owner
#: traded that job for the smaller download. The measurement is kept, here and
#: in the printout main() makes on every run, because it is the evidence the
#: trade was made against and --context near still builds it:
#:
#: MEASURED over the full published population (10,342 BOATs, 89,300 bridleways
#: and restricted byways): **82.8% of context ways are nowhere near a BOAT**,
#: leaving 15,366 (17.2%) that 'near' would carry - 25,708 distinct ways
#: against 10,342 for 'none', and 31,369 rows across overlapping regions
#: against 12,702.
#:
#: SAID 73.6% UNTIL 2026-09-24, AND THAT FIGURE IS SUPERSEDED. It came from a
#: grid-cell approximation, and the build itself reproduces it at a 1.5 km
#: radius - which is what adjacent ~1 km cells actually measure, not the 1 km
#: 'near' uses. The plan's step 1.2c row carries the correction; this comment,
#: which is the one a reader of the pipeline finds first, did not, and the
#: stale number was copied out of here into three other files before anybody
#: noticed. A measurement lives in one place or it lives in none.
#:
#: WHAT THIS COSTS, AND WHY THE METADATA MUST STILL SAY SO. A rider who looks
#: at a hillside and sees no bridleway must not read that as "there is no
#: bridleway here". There may well be; this map does not carry any. The note
#: for the option built travels into every sealed pack, the manifest and every
#: container's meta, and the app shows it verbatim on the lane sheet and the
#: record card. An absence in our data must never be presented as an absence on
#: the ground - and with NO context carried that is truer than it ever was.
CONTEXT_OPTIONS = ("none", "near", "all")
DEFAULT_CONTEXT = "none"
CONTEXT_RADIUS_KM = 1.0

#: What each option publishes as `contextScope` - in the manifest, every sealed
#: pack body and, as `context_scope`, every container's meta. It names what was
#: BUILT, never what was once decided.
CONTEXT_SCOPES = {
    "none": "none",
    "near": "near-byways-only",
    "all": "all",
}

#: The sentence the app shows verbatim (`context_note`). Empty only for 'all',
#: the one build that left no context way out and so has nothing to say.
CONTEXT_NOTES = {
    "none": (
        "Bridleways and restricted byways are not on this map: it carries "
        "only the ways a motor vehicle may use. Where none is shown, this map "
        "has not looked - it does not mean there is none on the ground."
    ),
    "near": (
        "Bridleways and restricted byways are shown only within %g km of a "
        "byway open to all traffic. Where none is shown, this map has not "
        "looked - it does not mean there is none on the ground."
    ) % CONTEXT_RADIUS_KM,
    "all": "",
}

#: What the DEFAULT build publishes. Derived, so they cannot disagree with
#: DEFAULT_CONTEXT; a build with another --context uses context_fields().
CONTEXT_SCOPE = CONTEXT_SCOPES[DEFAULT_CONTEXT]
CONTEXT_NOTE = CONTEXT_NOTES[DEFAULT_CONTEXT]


def context_fields(context=DEFAULT_CONTEXT):
    """-> the manifest's three step-1.2c fields, for what [context] built.

    `contextRadiusKm` is null unless the build measured a radius: with no
    context carried, or all of it, there is no radius to report, and a 1.0
    there would describe a filter that never ran.
    """
    if context not in CONTEXT_OPTIONS:
        raise ValueError("unknown context option %r" % (context,))
    return {
        "contextScope": CONTEXT_SCOPES[context],
        "contextRadiusKm": CONTEXT_RADIUS_KM if context == "near" else None,
        "contextNote": CONTEXT_NOTES[context],
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


# ---- step 1.2c: which ways the dataset carries -----------------------------
#
# The default carries no context way at all (see DEFAULT_CONTEXT); the
# near-set filter below is what --context near builds, and what the per-run
# measurement prints for every option.

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
        for lon, lat in points_of(f):
            cells.setdefault(
                (int(lon / (_KM_LON * radius_km)),
                 int(lat / (_KM_LAT * radius_km))), []).append((lon, lat))

    out = []
    for f in context:
        hit = False
        for lon, lat in points_of(f):
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


def is_context(feature):
    """A bridleway or a restricted byway: carried, and no motor may use it."""
    return ROW_RULES[feature["properties"]["rowType"]]["context"]


def select_ways(features, context=DEFAULT_CONTEXT, near_uids=None,
                removed=None):
    """Step 1.2c, applied -> the [features] the dataset carries, in input order.

    ONE FUNCTION, CALLED BY main() AND BY golden.py. The golden used to carry
    its own copy of the near filter, so the day the decision moved the golden
    would have gone on building - and blessing - the decision it replaced.

    Footpaths go first, on ROW_RULES "carried". Then, for [context]:

      none  every way a motor vehicle may use - BOATs and OSM tracks - and no
            context way at all. THE DEFAULT, by the owner's decision of
            2026-09-24;
      near  those, plus the context ways within CONTEXT_RADIUS_KM of one;
      all   those, plus every context way.

    [near_uids] is the near set main() has already measured, so the build
    does not walk the grid twice; None computes it here.

    AND ONE RECORD PER WAY. Where two authorities both record the same way -
    a National Park and its county, a county and its abolished predecessor,
    two councils either side of a boundary lane - the map drew both, as two
    lines a metre apart (found on a tablet near Pencelli, 26 Sep 2026).
    duplicate_ways.merge() keeps one, by a rule stated there, and the kept
    record carries the other in `also_recorded_by`. Applied to the whole
    national pool, BEFORE the region split, so a boundary lane is resolved
    the same way in both regions it is published in. [removed], a list, is
    extended with what went: (removed, [kept in its place], within_m).
    """
    if context not in CONTEXT_OPTIONS:
        raise ValueError("unknown context option %r" % (context,))
    carried = [f for f in features
               if ROW_RULES[f["properties"]["rowType"]]["carried"]]
    if context == "all":
        chosen = carried
    elif context == "none":
        chosen = [f for f in carried if not is_context(f)]
    else:
        if near_uids is None:
            motor = [f for f in carried if not is_context(f)]
            near_uids = set(f["properties"]["lane_uid"]
                            for f in near_motor_ways(
                                [f for f in carried if is_context(f)], motor))
        chosen = [f for f in carried
                  if not is_context(f)
                  or f["properties"]["lane_uid"] in near_uids]
    kept, gone = duplicate_ways.merge(chosen)
    if removed is not None:
        removed.extend(gone)
    # AND ONE LANE PER COUNCIL RECORD, joined after the merge, which compares
    # single lines: see join_pieces().
    return join_pieces(kept)


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


#: rowmaps' Description length is in MILES. One statute mile, in km.
KM_PER_MILE = 1.609344


def parse_description(desc):
    """rowmaps packs the useful fields into a pipe-separated Description.

    'BO|ON:22|0.144|none|-1.30876|51.66111|...'
      type code, authority:object id, length MILES, then more fields and the
      endpoints.
    Only the length is worth keeping; the rest we already know or can derive.

    THE LENGTH IS MILES, NOT KILOMETRES. Derbyshire's DY 18/3 says 0.113 and
    its line is about 182 m - 0.113 miles; read as km it was 113 m. Over the
    8,493 published ways of 100 m or more, length_m over the length of the
    way's own geometry had a median of 0.623 (1/1.604), p5-p95 0.617-0.630
    (measured 2 Oct 2026): the mile-per-km factor, not survey noise. Every
    way told every consumer of `length_m` (metres, WAYS-SCHEMA.md) that it
    was 38% shorter than it is. Converted HERE, so `lengthKm` in the pack and
    `length_m` in the container both mean what they say.
    """
    if not isinstance(desc, str):
        return {}
    bits = desc.split("|")
    out = {}
    if len(bits) > 2:
        try:
            out["length_km"] = round(float(bits[2]) * KM_PER_MILE, 6)
        except (ValueError, IndexError):
            pass
    if len(bits) > 3 and bits[3] and bits[3] != "none":
        out["note"] = bits[3]
    return out


#: rowmaps' piece suffix on a council number: 'KT|SR|74#1', 'KT|SR|74#2'.
#: Both pieces carry one source object (Description 'KT:8831#1', 'KT:8831#2')
#: and they meet end to end with 'KT|SR|74' itself: one byway, three records.
_PIECE_SUFFIX = re.compile(r"#\d+$")

#: rowmaps' map-sheet suffix on a parish: 'Great Hucklow-WD41'.
_SHEET_SUFFIX = re.compile(r"\s*-\s*[A-Za-z]{1,4}\d+[A-Za-z]?$")


def council_reference(ref, authority_code=None):
    """rowmaps' Name -> (parish, number), the words a byway is known by.

    'WT|LACO|24' -> ('LACO', '24'); 'DY|Great Hucklow-WD41|18/3' ->
    ('Great Hucklow', '18/3'); 'KT|SR|74#1' -> ('SR', '74'); 'ON|7' -> ('7',).
    Empty parts are left out, so it may be shorter than two.

    THE PARISH IS PART OF THE NUMBER. In most councils a path number is only
    unique within its parish: Wiltshire's reference is LACO24 (Lacock 24),
    Kent's AE25, Derbyshire's "Great Hucklow 18/3". The name used to keep the
    number alone, and measured from the council cache on 2 Oct 2026
    Wiltshire's "BOAT 1" was 21 byways in 21 parishes; across the published
    containers 6,711 of 10,231 ways shared their exact name with another way
    of their authority. A rider could not search for one, quote it to the
    council or set it against a traffic order.

    The map-sheet suffix and rowmaps' "#n" piece suffix are not part of the
    council's reference and are dropped: two records that differ only in
    them are pieces of one way (see join_pieces).

    UNLESS THE NUMBER ALREADY SAYS IT. Gwynedd's number carries its parish:
    'GY|Abermaw|Prow Abermaw Rhif 2' published as "... Abermaw Prow Abermaw
    Rhif 2" (87 of 113 Gwynedd references, measured 3 Oct 2026, every
    'Prow <parish> Rhif N' one), on the sheet, the record card and in search.
    The parish is left out when the number names it as a whole word, so
    that one is ('Prow Abermaw Rhif 2',). Still one name per record: two
    references that differ in parish differ in their number too, or keep it.

    A WHOLE WORD AT EITHER END ONLY WHERE THAT END IS A LETTER. Nottingham-
    shire's 'NT|Gamston (B)|Gamston (B)BOAT4' ends its parish in a bracket
    with the number straight after it; asking for no letter after ")" kept
    it as "Gamston (B) Gamston (B)BOAT4".
    """
    parts = [p.strip() for p in (ref or "").split("|")]
    number = _PIECE_SUFFIX.sub("", parts[-1]).strip()
    parish = _SHEET_SUFFIX.sub("", parts[-2]).strip() if len(parts) >= 3 \
        else ""
    if parish and parish == authority_code:
        parish = ""
    if parish and re.search(
            (r"(?<!\w)" if re.match(r"\w", parish) else "")
            + re.escape(parish)
            + (r"(?!\w)" if re.search(r"\w$", parish) else ""),
            number, re.IGNORECASE):
        parish = ""
    return tuple(p for p in (parish, number) if p)


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
    # SOURCE TEXT ENTERS HERE, and is cleaned here. The authority names come
    # from rowmaps' HTML index, and 50 of its 149 carried `&nbsp;` - so 2,012
    # published ways told the rider they were in `North&nbsp;Lincolnshire`.
    # See text_clean.py.
    authority_name = clean_text(authority_name)
    extra = parse_description(clean_text(props.get("Description")))

    # The council's own path number, e.g. 'ON|100|2/10'. Keep it: it is how a
    # rider or a council officer would refer to this specific way. The NAME
    # carries its parish too (council_reference); the id keeps the number
    # alone, as it always has, so every id a rider has starred still exists.
    ref = clean_text(props.get("Name")) or ""
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

    name = " ".join([rule["designation"]]
                    + list(council_reference(ref, authority_code)))

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
            # A byway read from the council's own live layer says so, and
            # credits that council (council_ways.py); every other way came
            # from the council's definitive map via rowmaps.
            "source": ("council:%s" if props.get("TB_council")
                       else "rowmaps:%s") % slugify(authority_name),
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
            "attribution": clean_text(props.get("TB_attribution")) or OGL,
            **({"lengthKm": extra["length_km"]} if "length_km" in extra else {}),
            **({"description": extra["note"]} if "note" in extra else {}),
        },
        "geometry": {"type": "LineString", "coordinates": coords},
    }


#: The one rowmaps file a council's own layer can stand in for.
COUNCIL_LAYER_TYPE = "byway_open_to_all_traffic"
#: The authorities whose byways this build took from the council's layer.
COUNCIL_LAYERS_USED = []


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
            if ROW_RULES[row_type].get("council_record"):
                # Not a rowmaps file: council_ucrs.py, through ucr_lanes().
                continue
            path = os.path.join(cache_dir(), code, "%s.json" % row_type)
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf8") as fh:
                    fc = json.load(fh)
            except (ValueError, OSError) as e:
                skipped["unreadable %s/%s" % (code, row_type)] += 1
                continue
            features = fc.get("features", [])
            if row_type == COUNCIL_LAYER_TYPE:
                # The council's live layer decides which byways exist, where
                # it publishes one; rowmaps stays the fallback. Unchanged
                # ways keep their rowmaps record, so their ids do not move.
                rowmaps = features
                features, _report = council_ways.byways_for(code, rowmaps)
                if features is not rowmaps:
                    COUNCIL_LAYERS_USED.append(name)
            for f in features:
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


#: The row type council_ucrs.py's roads are built as.
UCR_TYPE = "unsurfaced_unclassified_road"

#: THE NERC TEST. The Natural Environment and Rural Communities Act 2006 s67
#: extinguished the motor vehicle rights over a way recorded on the
#: definitive map as a footpath, bridleway or restricted byway; s67(2)(b)
#: saves only a way that was on the List of Streets and NOT on the
#: definitive map. So a UCR section that IS also one of those definitive-map
#: ways has no motor rights, and is not drawn as a green lane. Lincolnshire
#: says so on its own rights of way page: "Legislation has extinguished the
#: rights of motorists if these are also shown as a: public footpath, public
#: bridleway, restricted byway".
UCR_NERC_TYPES = ("footpath", "bridleway", "restricted_byway")

#: A section lies ALONG a way when UCR_ALONG_SHARE of its length is within
#: UCR_ALONG_M of that way AND RUNS WITH IT: the way's segment there within
#: UCR_ALONG_DEG of the section's own bearing. Never a touch, and never a
#: crossing: before the bearing test 59 sections under 53 m were dropped for
#: a path that crossed them (the second review, 8 Oct 2026: Stokenham 315,
#: 14 m at 75 degrees, a whole road lost). The same test, per section, takes
#: a section lying on a BOAT out of the drawn roads (the byway's definitive
#: record is the stronger one). 20 m: the distance two records of one road
#: sit apart. MEASURED 8 Oct 2026 over 4,415 sections: the share within 20 m
#: is two humps, 1,101 sections under 0.1 and 362 at 0.9 or more.
UCR_ALONG_M = 20.0
UCR_ALONG_SHARE = 0.75
UCR_ALONG_DEG = 30.0

#: Below the share but along this much of a long section: listed for the
#: owner to look at (the build's report), never dropped (a path may run
#: along part of a road and leave it; the road part keeps its rights).
UCR_PARTLY_ON_PATH = 0.3
UCR_PARTLY_ON_PATH_MIN_M = 100.0

#: Where the build writes every section the NERC and byway tests took out,
#: and every one partly along a path: dist/, which the publish step never
#: moves - a report for the owner, not data for riders. The log prints the
#: counts and points here.
UCR_REPORT = "ucr-report.json"

#: Cells, in degrees, for picking the definitive-map ways near any UCR before
#: indexing them: 435,299 footpaths are far too many to index for 4,000 roads.
_NEAR_CELL_DEG = 0.02


def ucr_uid(authority_code, *key):
    """A UCR's id: the council's own unique reference, nothing else.

    `key` is the route's key (council_ucrs.routes_of): [parish, number] in
    Devon ('DN-UCR-abbotsham-301'), [district, number] in North Yorkshire
    ('NY-UCR-richmondshire-u1057'), [number] where the council's numbers are
    county-wide ('SU-UCR-d262', 'ON-UCR-41697255'). Never a field that read
    order or an optional attribute could move, so a section re-drawn, added
    or dropped, or a council filling in a village, keeps the road's id and a
    rider's star stays on it. Pieces of one key far apart are told apart in
    ucr_lanes, without moving the longest's id."""
    return "-".join([authority_code, "UCR"] +
                    [slugify(k) for k in key if k])


def _route_key(route):
    """The route's key; [parish, number] for a file written before keys."""
    key = route.get("key")
    if key:
        return [clean_text(k) or "" for k in key]
    return [clean_text(route.get("parish")) or "",
            clean_text(route.get("number")) or ""]


def normalise_ucr(route, source, authority_code, authority_name):
    """One council_ucrs route -> one lane, in the shape normalise() writes.

    THE ROUTE IS ONE LANE. Councils draw a road in sections; a route is the
    council's reference, and every section of it is a line of one lane - the
    shape join_pieces() gives a byway drawn in pieces.

    THE ID is the council's reference (ucr_uid), so it does not move when the
    geometry does: 'DN-UCR-abbotsham-301'.

    THE NAME is what the council calls the road, with its reference, so a
    rider searching "Rocky Lane" or "Abbotsham 301" finds it:
    "Rocky Lane (Abbotsham UCR 301)"; with no name, the designation and the
    reference, as a byway is named.
    """
    rule = ROW_RULES[UCR_TYPE]
    parts = sorted([[float(p[0]), float(p[1])] for p in l]
                   for l in route.get("lines") or [] if len(l) >= 2)
    if not parts:
        return None
    parish = clean_text(route.get("parish")) or ""
    number = clean_text(route.get("number")) or ""
    uid = ucr_uid(authority_code, *_route_key(route))
    road = clean_text(route.get("name")) or ""
    ref = " ".join(x for x in (parish, "UCR", number) if x)
    name = "%s (%s)" % (road, ref) if road else " ".join(
        x for x in (rule["designation"], parish, number) if x)
    council = clean_text(source.get("council")) or authority_name
    authority_name = clean_text(authority_name)
    length_km = sum(council_ways._length(l) for l in parts) / 1000.0
    props = {
        "lane_uid": uid,
        "class": rule["way_class"],
        "county": authority_name,
        "name": name,
        "designation": rule["designation"],
        "rowType": UCR_TYPE,
        "authority": authority_name,
        "authorityCode": authority_code,
        "legal_tier": rule["legal_tier"],
        # The council's highway records, named by the council, so the sheet
        # can credit it: 'highway-records:devon-county-council'.
        "source": "highway-records:%s" % slugify(council),
        "source_date": SOURCE_DATE_PLACEHOLDER,
        "motorbike_ok": rule["motorbike_ok"],
        "fourxfour_ok": rule["fourxfour_ok"],
        "access_reason": rule["access_reason"].format(council=council),
        "access_evidence": rule["access_evidence"],
        "attribution": clean_text(source.get("attribution")) or council,
        "lengthKm": round(length_km, 6),
    }
    geometry = {"type": "LineString", "coordinates": parts[0]} \
        if len(parts) == 1 else {"type": "MultiLineString",
                                 "coordinates": parts}
    return {"type": "Feature", "properties": props, "geometry": geometry}


def _near_cells(lines, pad=1):
    out = set()
    for line in lines:
        for lon, lat in line:
            cx = int(math.floor(lon / _NEAR_CELL_DEG))
            cy = int(math.floor(lat / _NEAR_CELL_DEG))
            for i in range(-pad, pad + 1):
                for j in range(-pad, pad + 1):
                    out.add((cx + i, cy + j))
    return out


def nerc_indexes(paths, near):
    """{row type: council_ways._Index} of the definitive-map footpaths,
    bridleways and restricted byways [paths] with a point in a cell of
    [near] (the cells around every UCR)."""
    by_type = dict((t, []) for t in UCR_NERC_TYPES)
    for f in paths:
        t = f["properties"].get("rowType")
        if t not in by_type:
            continue
        lines = [[tuple(p) for p in l] for l in lines_of(f)]
        if _near_cells(lines, pad=0) & near:
            by_type[t].extend(lines)
    return dict((t, council_ways._Index(ls)) for t, ls in by_type.items())


def _axis(a, b):
    """A segment's bearing as an undirected axis, 0-180 degrees."""
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0


def _turn(a, b):
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


#: How far past a segment's end a point may fall and still be BESIDE it.
_BESIDE_SLACK_M = 2.0


def _beside(x, y, a, b):
    """The distance from (x, y) square across to segment a-b, or None where
    the point is not beside the segment but beyond one end of it. A path that
    ends where a road begins, running on in the same line, is near the
    road's first metres but never beside them (the review's Stokenham 315:
    14 m, a footpath arriving from the north and ending at its start)."""
    (x0, y0), (x1, y1) = a, b
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    t = ((x - x0) * dx + (y - y0) * dy) / (length * length)
    slack = _BESIDE_SLACK_M / length
    if t < -slack or t > 1.0 + slack:
        return None
    t = max(0.0, min(1.0, t))
    return math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))


def along_share(line, indexes, d=UCR_ALONG_M, max_deg=UCR_ALONG_DEG):
    """-> (share, {index name: share}): how much of `line` lies within `d`
    of a segment of one of `indexes` THAT RUNS WITH IT (within `max_deg` of
    the line's own bearing there) AND IS BESIDE IT (_beside: square across
    from it, not beyond its end). Sampled every council_ways.STEP_M; a
    sample counts once, for the index whose aligned segment is nearest.

    A path crossing a road is near it for 2d of its length but at a wide
    angle, so it never counts: the review's Stokenham 315 (14 m, a path
    across it at 75 degrees) and Wainfleet St Mary 72G500 (25 m at 72)."""
    pts = [council_ways._xy(tuple(p)) for p in line]
    if len(pts) < 2:
        return 0.0, {}
    samples = []
    for i in range(1, len(pts)):
        (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg == 0:
            continue
        axis = _axis(pts[i - 1], pts[i])
        n = max(1, int(seg // council_ways.STEP_M))
        for k in range(n + (1 if i == len(pts) - 1 else 0)):
            t = min(1.0, k * council_ways.STEP_M / seg)
            samples.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, axis))
    if not samples:
        return 0.0, {}
    hits = Counter()
    for x, y, axis in samples:
        best = None
        for name, ix in (indexes or {}).items():
            r = int(math.ceil(d / ix.cell))
            gx, gy = int(math.floor(x / ix.cell)), int(math.floor(y / ix.cell))
            for i in range(gx - r, gx + r + 1):
                for j in range(gy - r, gy + r + 1):
                    for a, b in ix.grid.get((i, j), ()):
                        if a == b or _turn(axis, _axis(a, b)) > max_deg:
                            continue
                        dist = _beside(x, y, a, b)
                        if dist is not None and dist <= d and                                 (best is None or dist < best[0]):
                            best = (dist, name)
        if best:
            hits[best[1]] += 1
    total = float(len(samples))
    return (sum(hits.values()) / total,
            dict((k, v / total) for k, v in hits.items()))


def nerc_sections(route, indexes, byways=None):
    """-> (kept lines, [(line, row type, share, metres)] dropped,
    [(line, share, metres)] partly on a path).

    Section by section: a council draws a road in sections, and the NERC Act
    takes the motor rights off the section the definitive map records as a
    path, not off the rest of the road. A section lying along a BOAT
    (`byways`, an index) is dropped the same way, its row type
    'byway_open_to_all_traffic': the byway's definitive record wins."""
    kept, dropped, partly = [], [], []
    for line in route.get("lines") or []:
        pts = [tuple(p) for p in line]
        if len(pts) < 2:
            continue
        metres = council_ways._length(pts)
        both, shares = along_share(pts, indexes)
        if both >= UCR_ALONG_SHARE:
            kind = max(shares, key=lambda t: shares[t])
            dropped.append((line, kind, round(both, 3), round(metres)))
            continue
        if byways is not None:
            on, _s = along_share(pts, {"byway_open_to_all_traffic": byways})
            if on >= UCR_ALONG_SHARE:
                dropped.append((line, "byway_open_to_all_traffic",
                                round(on, 3), round(metres)))
                continue
        if both >= UCR_PARTLY_ON_PATH and metres >= UCR_PARTLY_ON_PATH_MIN_M:
            partly.append((line, round(both, 3), round(metres)))
        kept.append(line)
    return kept, dropped, partly


def _piece_tag(lane):
    """Six hex naming a piece by where it is (its south-west corner to
    0.01 degrees, about 1 km): moves only if the piece moves that far."""
    xs = [p[0] for l in lines_of(lane) for p in l]
    ys = [p[1] for l in lines_of(lane) for p in l]
    return hashlib.sha1(("%.2f,%.2f" % (min(xs), min(ys))).encode()) \
        .hexdigest()[:6]


def _settle_ids(lanes):
    """Ids unique within one council's lanes, moving none that need not.

    Two lanes share an id only when one reference is several pieces far
    apart (council_ucrs.MAX_GAP_M) or the council gave a number twice. The
    LONGEST keeps the plain id - the one a single-piece road was published
    under, so a rider's star stays on it - and each other piece gets a
    suffix from where it is (_piece_tag), never from read order."""
    by_id = {}
    for lane in lanes:
        by_id.setdefault(lane["properties"]["lane_uid"], []).append(lane)
    taken = set(by_id)
    for uid, same in by_id.items():
        if len(same) < 2:
            continue
        same.sort(key=lambda l: (-l["properties"]["lengthKm"],
                                 _piece_tag(l)))
        for lane in same[1:]:
            new = "%s-%s" % (uid, _piece_tag(lane))
            n = 2
            while new in taken:
                new = "%s-%s-%d" % (uid, _piece_tag(lane), n)
                n += 1
            taken.add(new)
            lane["properties"]["lane_uid"] = new
    return lanes


def _has_definitive_map(paths):
    """Authority codes with ALL THREE of footpaths, bridleways and
    restricted byways in the build: one file missing (a fetch that failed)
    is a definitive map the NERC test cannot be trusted against."""
    held = {}
    for f in paths:
        p = f["properties"]
        if p.get("rowType") in UCR_NERC_TYPES:
            held.setdefault(p.get("authorityCode"), set()).add(p["rowType"])
    return set(c for c, ts in held.items() if ts >= set(UCR_NERC_TYPES))


def ucr_lanes(authorities, byways, paths, out_dir=None, today=None,
              log=print):
    """-> ([UCR lane], [source], report): every council's unsurfaced roads,
    as lanes, less every section the definitive map records as a footpath,
    bridleway or restricted byway (the NERC test) or as a BOAT - each
    judged by along_share, section by section (nerc_sections).

    `authorities` is rowmaps' {code: name}, which names the authority as
    every byway of it is named ("Devon"), so the app's authority filter and
    county chips treat a UCR and a byway of one council alike. It is also
    the set of authorities this build builds: a council file for one it
    does not (a test's fixture cache, a partial run) is left alone.

    `paths` are the build's definitive-map ways (normalise()d rowmaps
    footpaths, bridleways and restricted byways of every authority: a road
    near a county or park boundary may lie on its neighbour's path). A
    council without all three of its own (_has_definitive_map) CANNOT be
    tested, and its roads are held
    back, not drawn unchecked: report["unchecked"].

    Each source is the council file's `source` with `since` and `count`
    added - what a pack and a container say about where the roads came from.

    report: routes (read), on_path [(code, name, row type, share, metres)],
    on_byway [(code, name, share, metres)], partly_on_path [(code, name,
    share, metres)], unchecked [code], per_council {code: {...counts}}.
    """
    boats = council_ways._Index(
        [tuple(p) for p in l] for f in byways for l in lines_of(f))
    held = [(source, since, routes) for source, since, routes
            in council_ucrs.held(out_dir, today=today, log=log)
            if source["code"] in authorities]
    has_paths = _has_definitive_map(paths)
    near = _near_cells([l for _s, _d, routes in held for r in routes
                        for l in r.get("lines") or []])
    indexes = nerc_indexes(paths, near) if near else {}
    lanes, sources = [], []
    report = {"routes": 0, "on_path": [], "on_byway": [],
              "partly_on_path": [], "unchecked": [], "per_council": {}}
    for source, since, routes in held:
        code = source["code"]
        name = clean_text(authorities[code]) or source.get("authority") \
            or code
        counts = {"routes": len(routes), "lanes": 0, "on_path_sections": 0,
                  "on_path_routes": 0, "on_byway_sections": 0,
                  "on_byway_routes": 0, "km": 0.0}
        report["per_council"][code] = counts
        report["routes"] += len(routes)
        if code not in has_paths:
            report["unchecked"].append(code)
            log("::warning::%s: the build lacks %s's definitive-map "
                "footpaths, bridleways or restricted byways, so its "
                "unsurfaced roads cannot be tested against them (NERC 2006 "
                "s67) and are not published" % (code, name))
            continue
        mine = []
        for route in routes:
            kept, dropped, partly = nerc_sections(route, indexes, boats)
            if dropped or partly:
                whole = normalise_ucr(route, source, code, name)
                label = whole["properties"]["name"] if whole else code
            on_path = [d for d in dropped if d[1] in UCR_NERC_TYPES]
            on_boat = [d for d in dropped if d[1] not in UCR_NERC_TYPES]
            for _line, kind, share, metres in on_path:
                report["on_path"].append((code, label, kind, share, metres))
            for _line, _kind, share, metres in on_boat:
                report["on_byway"].append((code, label, share, metres))
            for _line, share, metres in partly:
                report["partly_on_path"].append((code, label, share, metres))
            counts["on_path_sections"] += len(on_path)
            counts["on_byway_sections"] += len(on_boat)
            if not kept:
                counts["on_byway_routes" if on_boat and not on_path
                       else "on_path_routes"] += 1
                continue
            lane = normalise_ucr(dict(route, lines=kept), source, code, name)
            if lane is not None:
                mine.append(lane)
        _settle_ids(mine)
        counts["lanes"] = len(mine)
        counts["km"] = round(sum(l["properties"]["lengthKm"]
                                 for l in mine), 1)
        lanes.extend(mine)
        sources.append(dict(source, since=since, count=len(mine)))
        log("  %s: %d unsurfaced roads (%.1f km) from %s; %d sections lie "
            "along a footpath, bridleway or restricted byway and are not "
            "drawn (NERC 2006 s67; %d roads wholly); %d along a byway, left "
            "to it (%d roads wholly)" % (
                code, len(mine), counts["km"], source.get("council"),
                counts["on_path_sections"], counts["on_path_routes"],
                counts["on_byway_sections"], counts["on_byway_routes"]))
    return lanes, sources, report


def log_ucr_report(report, path, log=print):
    """Every section the tests took out or flagged, in the log AND in
    `path` (dist/ucr-report.json): none is left off a list. `path` None
    (a --measure-only run, which writes nothing) logs them only."""
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf8", newline="\n") as fh:
            json.dump(report, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
    for code in report["unchecked"]:
        log("  NOT PUBLISHED, no full definitive map to test them against: "
            "%s" % code)
    for code, n, kind, share, metres in report["on_path"]:
        log("    along a %s (%.0f%% of %d m): %s %s"
            % (kind.replace("_", " "), share * 100, metres, code, n))
    for code, n, share, metres in report["on_byway"]:
        log("    along a byway (%.0f%% of %d m): %s %s"
            % (share * 100, metres, code, n))
    for code, n, share, metres in report["partly_on_path"]:
        log("    partly along a path, kept (%.0f%% of %d m): %s %s"
            % (share * 100, metres, code, n))
    if path:
        log("  every one of these is in %s" % path)


def attach_ucrs(parts, ucrs):
    """Each UCR to the area piece of its region that holds its authority's
    byways, or to the first piece when none does. -> [[ucr], ...] by piece.
    A region that fits one package (all six today) has one piece."""
    out = [[] for _ in parts]
    for f in ucrs:
        authority = f["properties"].get("authority")
        k = next((i for i, (_label, feats) in enumerate(parts)
                  if any(g["properties"].get("authority") == authority
                         for g in feats)), 0)
        out[k].append(f)
    return out


def lines_of(feature):
    """A lane's lines: one for a LineString, several for a joined record."""
    g = feature["geometry"]
    if g.get("type") == "MultiLineString":
        return g["coordinates"]
    return [g["coordinates"]]


def points_of(feature):
    """Every vertex of a lane, whichever of the two shapes it has."""
    return [p for line in lines_of(feature) for p in line]


def _line_key(line):
    return json.dumps([list(p) for p in line], separators=(",", ":"))


def join_pieces(features):
    """-> [features], each council record drawn in pieces made ONE lane.

    THE DEFECT. rowmaps delivers many byways as several LineString pieces
    under one council reference, usually split where the way crosses a road
    or a parish line, end touching end: Kent's KT|AW|339 is 16 pieces,
    Surrey's 526 is 19. Each piece's id carries a hash of its own coordinates
    (normalise), so each piece was published as its own lane with the same
    name. A rider who tapped one was told the length of that fragment, not
    the byway; starring, My lanes and "take me to this lane" held a fragment;
    and every count was inflated. Measured 2 Oct 2026: Kent's 234 records
    were 800 lanes, Wiltshire's 92 multi-piece records 203; across the
    published containers 627 byways whose same-name pieces meet end to end
    were 2,113 lanes.

    The app was built for the other shape: Lane.lengthM sums `lines` because
    "a lane uid with several parts is one right of way the source has drawn
    in pieces", and the container blob carries several lines per way. So the
    pieces become one MultiLineString feature.

    ONE RECORD IS ONE AUTHORITY, ONE ROW TYPE AND ONE NAME. The name is the
    council's reference, parish and number (council_reference), with the
    map-sheet and "#n" piece suffixes already gone - 'KT|SR|74#1', '#2' and
    'KT|SR|74' are one chain of one byway. A way with no number has no
    reference to join on and is left alone.

    THE ID. A record that was one piece keeps its id exactly, so a star on it
    survives. A joined record's id is the authority, the number and a hash
    over ALL its parts, sorted, so it is the same whatever order the source
    lists the pieces in, and it still differs from any one piece's id.

    Run in select_ways() AFTER duplicate_ways.merge(), which compares single
    lines: joined first, every record drawn in pieces would be invisible to
    it and drawn twice where a neighbouring authority records it too. The
    length is the sum of the parts' own lengths; a part lying exactly on one
    already taken counts once.
    """
    groups = {}
    order = []
    for f in features:
        p = f["properties"]
        code = p.get("authorityCode") or ""
        row_type = p.get("rowType")
        designation = ROW_RULES.get(row_type, {}).get("designation")
        if not code or not p.get("name") or p.get("name") == designation:
            key = ("", id(f))
        else:
            key = (code, row_type, p["name"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)
    return [_joined(groups[key]) for key in order]


def _joined(members):
    if len(members) == 1:
        return members[0]
    first = members[0]["properties"]
    code = first["authorityCode"]

    lines = {}
    length_km = 0.0
    lengths_known = True
    descriptions = set()
    also = {}
    for m in members:
        p = m["properties"]
        fresh = [l for l in lines_of(m) if _line_key(l) not in lines]
        for l in fresh:
            lines[_line_key(l)] = [list(pt) for pt in l]
        if fresh:
            if p.get("lengthKm") is None:
                lengths_known = False
            else:
                length_km += p["lengthKm"]
        descriptions.add(p.get("description"))
        for e in p.get("also_recorded_by") or []:
            also.setdefault(e.get("way_uid"), e)

    parts = [lines[k] for k in sorted(lines)]
    uid = first["lane_uid"]
    # normalise wrote '<code>-<number>-<hash of 10>'; the number may itself
    # hold '-', so it is cut from both ends rather than split.
    number = uid[len(code) + 1:-11] if uid.startswith(code + "-") \
        and len(uid) > len(code) + 12 else ""
    number = _PIECE_SUFFIX.sub("", number)
    geom_hash = hashlib.sha1(
        json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:10]

    props = dict(first)
    props["lane_uid"] = "%s-%s-%s" % (code, number, geom_hash) if number \
        else "%s-%s" % (code, geom_hash)
    props.pop("lengthKm", None)
    if lengths_known:
        props["lengthKm"] = round(length_km, 6)
    props.pop("description", None)
    if len(descriptions) == 1 and None not in descriptions:
        props["description"] = descriptions.pop()
    # EVERY PIECE'S OWN ID IS NAMED ON THE LANE IT BECAME, twice over.
    #
    # Each piece was published as a lane of its own until 2 Oct 2026, so
    # riders hold notes, stars, photographs and plans against piece ids that
    # leave the data the day this joins them. Nothing else maps one to the
    # other: the joined id hashes ALL the parts, and a rowmaps "#n" piece's
    # number ('KT-74#1-...') is not even the joined lane's ('KT-74-...').
    # Measured on the first joined build: of 2,588 piece ids, the app's
    # path-number rule could follow 1,226 - and only on a phone holding every
    # area - and moved 5 onto a byway of the same number in another parish,
    # up to 58 km away.
    #
    #   * `also_recorded_by`, because that is what SHIPPED apps follow: a
    #     uid named there moves onto the record carrying it, for any set of
    #     areas (the app's planLaneRepoint folds). Authority, code and name
    #     are set, because the app's parser drops an entry with no authority.
    #   * `joined_from`, so an app that knows it can tell "this lane's own
    #     pieces" from "another council's record": it moves several pieces
    #     one rider holds onto the one lane, and does not print the lane's own
    #     pieces as "Also recorded by". The container carries it as meta
    #     `joined_from` (build_map_container.py, docs/WAYS-SCHEMA.md).
    pieces = sorted(set(m["properties"]["lane_uid"] for m in members)
                    - {props["lane_uid"]})
    for m in members:
        p = m["properties"]
        if p["lane_uid"] in pieces:
            also.setdefault(p["lane_uid"], {
                "way_uid": p["lane_uid"],
                "authority": p.get("authority"),
                "authority_code": p.get("authorityCode") or code,
                "name": p.get("name"),
            })
    props.pop("also_recorded_by", None)
    if also:
        props["also_recorded_by"] = [also[k] for k in sorted(
            also, key=lambda u: u or "")]
    props.pop("joined_from", None)
    if pieces:
        props["joined_from"] = pieces
    if len(parts) == 1:
        geometry = {"type": "LineString", "coordinates": parts[0]}
    else:
        geometry = {"type": "MultiLineString", "coordinates": parts}
    return {"type": "Feature", "properties": props, "geometry": geometry}


def distinct_line_km(features):
    """Kilometres of line in [features], each distinct line counted ONCE.

    THE MEASURE check_build.py gates on, and why it is not the row count. A
    build that publishes one council record drawn in 16 pieces as one lane,
    or one line recorded twice as one way, has fewer rows and exactly the
    same byways: on 3 Oct 2026 the first such build went 12,576 -> 10,375
    rows (-17.5%, the South East -27%) and refused itself as "data loss" with
    every metre of every byway still in it. The length of line it carries is
    what a rider gets, and it only falls when a way does.

    Distinct, so a line two records both carry (the byte-identical doubles
    duplicate_ways folds) is not counted twice on one side of the comparison
    and not on the other. From the geometry, never from `lengthKm`, whose
    unit changed once already (rowmaps' miles, read as km until 2 Oct 2026).
    """
    seen = {}
    for f in features:
        for line in lines_of(f):
            key = _line_key(line)
            if key in seen:
                continue
            seen[key] = sum(_km_between(line[k], line[k + 1])
                            for k in range(len(line) - 1))
    return sum(seen.values())


#: Pieces of one joined lane further apart than this are listed for the
#: owner to look at. Not refused: a byway broken by a stretch of road is one
#: record and one legal way, and the council's reference is what joins it.
JOINED_FAR_APART_KM = 1.0

#: Two pieces whose nearest vertices lie within this are one chain.
_PIECES_TOUCH_KM = 0.025


def joined_gap_km(feature):
    """The widest gap, in km, between the pieces of a joined lane: 0 when
    every part chains to the others within [_PIECES_TOUCH_KM].

    join_pieces joins by authority, row type and council reference, NOT by
    the pieces touching. Measured on the first joined build: 720 of 812
    joined lanes are one connected chain, 92 are not, and 7 have pieces more
    than 1 km apart (Cambridgeshire's Balsham 4 by 1.8 km). Most will be one
    way interrupted by a road; any that are two ways sharing a number are
    for the owner to find, and this is how they are found.
    """
    parts = lines_of(feature)
    if len(parts) < 2:
        return 0.0

    def apart(a, b):
        return min(_km_between(p, q) for p in a for q in b)

    owner = list(range(len(parts)))

    def root(i):
        while owner[i] != i:
            owner[i] = owner[owner[i]]
            i = owner[i]
        return i

    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            if root(i) != root(j) and apart(parts[i], parts[j]) \
                    <= _PIECES_TOUCH_KM:
                owner[root(i)] = root(j)
    groups = {}
    for i in range(len(parts)):
        groups.setdefault(root(i), []).append(parts[i])
    if len(groups) == 1:
        return 0.0
    chains = list(groups.values())
    worst = 0.0
    for k, chain in enumerate(chains):
        nearest = min(apart(a, b)
                      for other in chains[:k] + chains[k + 1:]
                      for a in chain for b in other)
        worst = max(worst, nearest)
    return worst


def joined_far_apart(features, limit_km=JOINED_FAR_APART_KM):
    """-> [{way_uid, authority, name, pieces, gap_km, length_km}], the
    joined lanes whose pieces lie more than [limit_km] apart, widest first."""
    out = []
    for f in features:
        p = f["properties"]
        if not p.get("joined_from"):
            continue
        gap = joined_gap_km(f)
        if gap > limit_km:
            out.append({"way_uid": p["lane_uid"],
                        "authority": p.get("authority"),
                        "name": p.get("name"),
                        "pieces": len(lines_of(f)),
                        "joined_from": list(p["joined_from"]),
                        "gap_km": round(gap, 3),
                        "length_km": round(distinct_line_km([f]), 3)})
    out.sort(key=lambda r: (-r["gap_km"], r["way_uid"]))
    return out


#: Where build_packages leaves the far-apart list for check_build.py.
JOINED_REPORT = os.path.join("reports", "joined-far-apart.json")


def report_orphans(orphans):
    """Say WHERE the uncovered lanes are, so the boxes can be fixed."""
    west = min(lon for f in orphans for lon, _ in points_of(f))
    east = max(lon for f in orphans for lon, _ in points_of(f))
    south = min(lat for f in orphans for _, lat in points_of(f))
    north = max(lat for f in orphans for _, lat in points_of(f))

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
    for lon, lat in points_of(feature):
        if west <= lon <= east and south <= lat <= north:
            return True
    return False


def write_package(pkg_name, region_id, region_label, area_label, features,
                  key, stamp, published=None, note=None,
                  context=DEFAULT_CONTEXT, ucrs=None, ucr_sources=None):
    """Seal one downloadable piece.

    [context] is the step 1.2c option these [features] were selected under,
    and it decides the scope and the note sealed into the pack. It used to be
    read from module constants, so a --context all build sealed "shown only
    within 1 km" into packs that carried every bridleway.

    [area_label] is None when the whole region fits in one package; then the
    area IS the region and the app shows the region's name.

    [published] is what is already on riders' phones, from published_packages().
    It is what lets an unchanged package keep the date it was cut - and
    therefore keep its bytes. See the comment on the seal below.

    [ucrs] are this piece's unsurfaced unclassified roads (ucr_lanes), and
    [ucr_sources] the councils they came from. They are sealed in their OWN
    member, `ucrFeatures`, never in `features`: an app before 119 reads a
    pack's `features` on its GeoJSON path and would draw a `ucr` red. A
    package with none is sealed exactly as before, byte for byte.
    """
    ucrs = list(ucrs or [])
    area_id = region_id if area_label is None else \
        "%s-%s" % (region_id, slugify(area_label))
    shown = region_label if area_label is None else area_label
    scope = context_fields(context)

    # The app dedupes on lane_uid when it loads neighbouring areas together, so a
    # non-unique id inside one package is data the rider will never see. This
    # cost us 38% of the Midlands once; it does not get to happen quietly again.
    uids = [f["properties"]["lane_uid"] for f in features + ucrs]
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
        for f in features + ucrs:
            f["properties"]["source_date"] = day
        collection = {
            "type": "FeatureCollection",
            "generated": cut,
            "package": pkg_name,
            "region": region_id,
            "area": area_id,
            "label": "%s - %s" % (DATASET_LABEL, shown),
            "note": note or "",
            "contextScope": scope["contextScope"],
            "contextNote": scope["contextNote"],
            "attribution": OGL,
            "features": features,
        }
        if ucrs:
            collection["ucrFeatures"] = ucrs
            collection["ucrSources"] = _sources_in(ucr_sources, ucrs)
            # The Open Government Licence sentence is the rights of way's,
            # not the councils' highway records' (dataset_attribution).
            collection["attribution"] = dataset_attribution(
                OGL, collection["ucrSources"])
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
        "contextScope": scope["contextScope"],
        "file": "packages/" + fname,
        "sha256": digest,
        "bytes": len(sealed),
        "plainBytes": len(payload),
        "laneCount": len(features),
        # WHAT check_build.py GATES ON: km of line, each distinct line once
        # (distinct_line_km). A row count cannot tell sixteen pieces joined
        # into one lane from fifteen byways lost.
        "lengthKm": round(distinct_line_km(features), 3),
        "generated": stamp,
        # Only where there are any, so a manifest entry for a package with
        # none is what it always was.
        **({"ucrCount": len(ucrs),
            "ucrLengthKm": round(distinct_line_km(ucrs), 3)} if ucrs else {}),
    }


def _councils(sources):
    names = [clean_text(s.get("council")) or s.get("code") for s in sources]
    return names[0] if len(names) == 1 else         ", ".join(names[:-1]) + " and " + names[-1]


def dataset_licence(ucr_sources):
    """The manifest's `licence`: "OGL-3.0" while every way is from the
    definitive map, as it always was; with council highway records in the
    build, what each part is published under, honestly. Never "OGL-3.0" over
    a council layer that states no licence."""
    used = [s for s in ucr_sources or [] if s.get("count")]
    if not used:
        return "OGL-3.0"
    ogl = [s for s in used if s.get("licence") == "OGL-3.0"]
    none = [s for s in used if s.get("licence") != "OGL-3.0"]
    out = "OGL-3.0 for the rights of way"
    if ogl:
        out += ("; OGL-3.0 for the unsurfaced unclassified roads of %s"
                % _councils(ogl))
    if none:
        out += ("; the unsurfaced unclassified roads of %s are council "
                "highway records published without a stated licence, on the "
                "owner's decision of 8 October 2026, credited to each "
                "council (ucrSources)" % _councils(none))
    return out


def dataset_attribution(rights_of_way, ucr_sources):
    """The manifest's `attribution`: the definitive-map credit as it always
    was, and, where there are UCRs, each council's own credit after it -
    labelled, so the Open Government Licence sentence is not read as
    covering the councils' highway records."""
    used = [s for s in ucr_sources or [] if s.get("count")]
    if not used:
        return rights_of_way
    return ("Rights of way: " + rights_of_way + " Unsurfaced unclassified "
            "roads: " + " ".join(s["attribution"] for s in used))


def _sources_in(sources, ucrs):
    """The councils these roads came from, each with how many are here."""
    counts = Counter(f["properties"].get("authorityCode") for f in ucrs)
    out = []
    for source in sources or []:
        if counts.get(source.get("code")):
            out.append(dict(source, count=counts[source["code"]]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="base64 32-byte key file")
    ap.add_argument("--allow-orphans", action="store_true",
                    help="publish even though some lanes match no region")
    ap.add_argument("--context", choices=CONTEXT_OPTIONS,
                    default=DEFAULT_CONTEXT,
                    help="step 1.2c. 'none' carries only ways a motor vehicle "
                         "may use - BOATs and OSM tracks; 'near' adds "
                         "bridleways and restricted byways within 1 km of "
                         "one; 'all' adds every one of them. The decision is "
                         "'none' (the owner, 2026-09-24; it superseded "
                         "'near'), and the measurement for all three prints "
                         "either way.")
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

    # In ROW_RULES order, so the pool is the same list on every run.
    every = [f for t in ROW_RULES for f in by_type.get(t, [])]
    # EVERY way a motor vehicle may use, not the BOATs alone. This read
    # by_type["byway_open_to_all_traffic"], so an OSM track - carried, and
    # motorbike_ok=1 - reached no pool under any option.
    motor = [f for f in every
             if ROW_RULES[f["properties"]["rowType"]]["carried"]
             and not is_context(f)]
    context_all = [f for f in every if is_context(f)]

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
    print("  motor-legal ways (BOAT + OSM track) %7d" % len(motor))
    print("  context ways (bridleway + restr.)  %7d" % len(context_all))
    print("    within %.1f km of a byway         %7d  (%.1f%%)"
          % (CONTEXT_RADIUS_KM, len(near), pct(len(near))))
    print("    nowhere near one                 %7d  (%.1f%%)"
          % (far, pct(far)))
    for name, n in (("none", len(motor)),
                    ("near", len(motor) + len(near)),
                    ("all", len(motor) + len(context_all))):
        mark = ("  <- DECIDED (owner, 2026-09-24)"
                if name == DEFAULT_CONTEXT else "")
        print("  option %-4s total ways in dataset  %7d%s" % (name, n, mark))

    duplicates = []
    pool = select_ways(every, args.context, near_uids=near_uids,
                       removed=duplicates)
    print("  building with --context %s: %d ways" % (args.context, len(pool)))

    # ONE RECORD PER WAY, and what it cost, printed on every run.
    print("")
    print("the same way recorded by two authorities (duplicate_ways.py)")
    print("  records removed, each carried by the one kept  %5d"
          % len(duplicates))
    pairs = Counter("%s over %s" % (
        "+".join(sorted(set(duplicate_ways._code(k) for k in keeps))),
        duplicate_ways._code(r)) for r, keeps, _ in duplicates)
    for label, n in pairs.most_common():
        print("    %5d  kept %s" % (n, label))
    if duplicates:
        print("  furthest any removed line lies from the kept one: %.1f m "
              "(limit %.0f m)" % (max(w for _, _, w in duplicates),
                                  duplicate_ways.MATCH_M))

    # ONE LANE PER COUNCIL RECORD, and how many pieces that took.
    joined = [f for f in pool if len(lines_of(f)) > 1]
    print("")
    print("council records the source drew in pieces (join_pieces)")
    print("  records now one lane each  %5d, from %d pieces"
          % (len(joined), sum(len(lines_of(f)) for f in joined)))
    # JOINED BY REFERENCE, NOT BY TOUCHING, so the ones whose pieces lie far
    # apart are listed: for the owner to look at, never a reason to refuse.
    far_apart = joined_far_apart(joined)
    print("  of them, pieces more than %.0f km apart  %5d  (listed, not "
          "refused)" % (JOINED_FAR_APART_KM, len(far_apart)))
    for r in far_apart:
        print("    %6.2f km apart  %-34s %-24s %s (%d pieces, %.2f km)"
              % (r["gap_km"], r["way_uid"], r["authority"], r["name"],
                 r["pieces"], r["length_km"]))

    # THE COUNCILS' UNSURFACED ROADS, beside the byways and never merged
    # with them (council_ucrs.py). Read after the pool, so a road lying on a
    # published byway is left to the byway.
    print("")
    print("unsurfaced unclassified roads (council_ucrs.py)")
    # The definitive map's footpaths, bridleways and restricted byways,
    # read from the cache though none is carried: the NERC test needs them.
    nerc_paths = [f for t in UCR_NERC_TYPES for f in by_type.get(t, [])]
    ucr_pool, ucr_sources, ucr_report = ucr_lanes(authorities, pool,
                                                  nerc_paths)
    print("  %d roads from %d council layers; %d sections lie along a "
          "footpath, bridleway or restricted byway and are not drawn (NERC); "
          "%d along a byway, left to it; %d partly along a path, kept"
          % (len(ucr_pool), len(ucr_sources), len(ucr_report["on_path"]),
             len(ucr_report["on_byway"]), len(ucr_report["partly_on_path"])))
    # EVERY ONE, here and in dist/ucr-report.json (the second review: the
    # log listed 40 of 432 and 40 of 115).
    log_ucr_report(ucr_report, None if args.measure_only
                   else os.path.join(dist_dir(), UCR_REPORT))

    if args.measure_only:
        return

    # For check_build.py, which prints it and puts it in the job summary.
    # Under dist/, which the publish step never moves: a report for the
    # owner, not data for riders.
    report = os.path.join(dist_dir(), JOINED_REPORT)
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", encoding="utf8") as fh:
        json.dump({"limit_km": JOINED_FAR_APART_KM,
                   "joined": len(joined),
                   "far_apart": far_apart}, fh, indent=1)
        fh.write("\n")

    scope = context_fields(args.context)
    note = "Byways open to all traffic - the lanes you may legally ride."
    if scope["contextNote"]:
        note = note + " " + scope["contextNote"]

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
        ucrs = [f for f in ucr_pool if in_region(f, box)]
        placed.update(f["properties"]["lane_uid"] for f in features + ucrs)
        parts = split_by_authority(features)
        for (area_label, part), part_ucrs in zip(parts,
                                                 attach_ucrs(parts, ucrs)):
            entry = write_package(DATASET, region_id, region_label,
                                  area_label, part, key, stamp,
                                  published=published, note=note,
                                  context=args.context, ucrs=part_ucrs,
                                  ucr_sources=ucr_sources)
            entries.append(entry)
            over = "  OVER BUDGET" \
                if entry["plainBytes"] > MAX_PLAIN_BYTES else ""
            state = "unchanged" if entry["generated"] != stamp else ""
            print("  %-42s %6d ways  %5.1f MB sealed (%5.1f MB plain) %-9s%s"
                  % (entry["area"], entry["laneCount"],
                     entry["bytes"] / 1048576.0,
                     entry["plainBytes"] / 1048576.0, state, over))

    orphans = [f for f in pool + ucr_pool
               if f["properties"]["lane_uid"] not in placed]

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
        "attribution": dataset_attribution(OGL + (
            " Byways for %s read from the councils' own live layers."
            % ", ".join(sorted(set(clean_text(n) for n in
                                   COUNCIL_LAYERS_USED)))
            if COUNCIL_LAYERS_USED else ""), ucr_sources),
        "licence": dataset_licence(ucr_sources),
        "source": "Local highway authority definitive maps via rowmaps.com"
                  + ("; byways for %d authorities from the council's own "
                     "live layer" % len(COUNCIL_LAYERS_USED)
                     if COUNCIL_LAYERS_USED else ""),
        **({"councilLayers": sorted(set(clean_text(n) for n in
                                        COUNCIL_LAYERS_USED))}
           if COUNCIL_LAYERS_USED else {}),
        "authorities": len(authorities),
        "maxPlainBytes": MAX_PLAIN_BYTES,
        "dataset": DATASET,
        # Step 1.2c travels WITH THE DATA, not only in a decision document,
        # and says what THIS build carried: the app shows contextNote verbatim,
        # so their absence is never read as absence on the ground.
        **scope,
        "wayClassCounts": dict(sorted(by_class.items())),
        # The unsurfaced roads, counted apart: they are not in any pack's
        # `features` (write_package), so not in wayClassCounts either.
        **({"ucrCount": len(ucr_pool),
            "ucrSources": [s for s in ucr_sources if s["count"]]}
           if ucr_pool else {}),
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

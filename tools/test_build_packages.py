#!/usr/bin/env python3
"""Whether every lane we hold actually reaches a package.

    python tools/test_build_packages.py

REGIONS is six hand-written boxes over an island with an awkward shape, and
until this existed nothing checked that they cover it. A lane outside all six
is not an error, not a warning and not in the product: it is in the source
data, in no package, and the build prints a page of healthy numbers regardless.
The dataset is quietly smaller than it claims and nothing says so.

No framework, matching test_build_trips.py: the repo has none and this build
has to run unattended.
"""
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_packages  # noqa: E402

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s\n    got  %r\n    want %r" % (name, got, want))


def check_true(name, got):
    check(name, bool(got), True)


def lane(coords, authority="Somewhere", uid=None):
    return {
        "geometry": {"coordinates": coords},
        "properties": {
            "lane_uid": uid or ("u%d" % (len(coords) + hash(str(coords)) % 9999)),
            "authority": authority,
        },
    }


def placed_regions(feature):
    """Every region box the feature would be published in."""
    return [r for r, _, box in build_packages.REGIONS
            if build_packages.in_region(feature, box)]


# --- in_region ---------------------------------------------------------------

# A lane wholly inside one box belongs to it.
check("a Midlands lane is in the Midlands",
      placed_regions(lane([(-1.7, 52.5), (-1.6, 52.6)])), ["midlands"])

# Straddling is deliberate: the lane is published whole in both, and the app
# dedupes on id. Losing half a lane at a boundary is worse than 200 KB twice.
check_true("a lane crossing a boundary lands in both",
           len(placed_regions(lane([(-3.3, 52.5), (-3.2, 52.5)]))) >= 2)

# One point inside is enough, even when the rest is far outside.
check("one vertex inside is enough",
      placed_regions(lane([(-20.0, 52.5), (-1.7, 52.5)])), ["midlands"])


# --- the coverage gate -------------------------------------------------------

# The case the gate exists for: somewhere in Great Britain that no box reaches
# is reported rather than dropped. Far north Scotland is the clearest example -
# "The North" stops at 55.85 and there is nothing above it.
far_north = lane([(-4.0, 57.5), (-4.1, 57.6)], authority="Highland")
check("a lane north of every box is in no region",
      placed_regions(far_north), [])

# And the detection the build now runs: anything whose uid no region claimed.
pool = [
    lane([(-1.7, 52.5)], uid="in-midlands"),
    far_north,
]
placed = set()
for _region, _label, _box in build_packages.REGIONS:
    placed.update(f["properties"]["lane_uid"]
                  for f in pool if build_packages.in_region(f, _box))
orphans = [f for f in pool if f["properties"]["lane_uid"] not in placed]

check("exactly the uncovered lane is flagged",
      [f["properties"]["lane_uid"] for f in orphans],
      [far_north["properties"]["lane_uid"]])

# It has to say WHERE, or the box cannot be fixed. This only has to not throw
# and to name the authority; the exact wording is not the contract.
try:
    build_packages.report_orphans(orphans)
    check_true("report_orphans survives a real orphan list", True)
except Exception as exc:  # noqa: BLE001
    check("report_orphans survives a real orphan list", repr(exc), "no error")


# --- chunk labels ------------------------------------------------------------

# A chunk is named after the authority with the most lanes in it.
#
# It used to be named after the alphabetically first, and because in_region
# deliberately pulls in lanes that straddle a boundary, the published Wales
# index carried an area called "Bath and North East Somerset and 25 more" on
# the strength of a few border lanes. A rider looking for Welsh byways has no
# way to read that as anything but broken data.
# The real budget is 12.5 MB, which would need ~23,000 fixture lanes. Lower it
# for this check; the number is not what is under test, the naming is.
_real_max = build_packages.MAX_PLAIN_BYTES
build_packages.MAX_PLAIN_BYTES = 1200


def sized(authority, n):
    return [
        lane([(-3.0, 52.0)] * 4, authority=authority, uid="%s-%d" % (authority, i))
        for i in range(n)
    ]


# "Aardvark" sorts first and contributes almost nothing; "Powys" owns the
# chunk. The old code named it after Aardvark.
labels = [label for label, _ in
          build_packages.split_by_authority(sized("Aardvark Council", 1)
                                            + sized("Powys", 20))]

build_packages.MAX_PLAIN_BYTES = _real_max

check_true("the split actually happened", len(labels) >= 2)
check_true(
    "a chunk is named after its biggest authority, not the first alphabetically",
    all(not str(l).startswith("Aardvark") for l in labels))


# --- coverage over real ground ------------------------------------------------

# Real towns, chosen ON the seams between boxes.
#
# Five of these fell outside every region until 2026-09-11, so every right of
# way in them was published in no package at all: present in the source data,
# absent from the product, and nothing anywhere said so. Gloucester, Stroud and
# Cirencester sat in the hole between South West, Wales and Midlands - the
# Cotswolds, which is as green-lane as England gets.
#
# A grid sweep would mostly report sea. Named places are the honest check.
PLACES = {
    # (lon, lat) — the seams first, because those are what broke.
    "Gloucester": (-2.24, 51.86),
    "Stroud": (-2.22, 51.74),
    "Cirencester": (-1.97, 51.72),
    "Aylesbury": (-0.81, 51.82),
    "Skegness": (0.34, 53.14),
    "Swindon": (-1.78, 51.56),
    "Chippenham": (-2.12, 51.46),
    "Bicester": (-1.15, 51.90),
    "Boston": (-0.02, 52.98),
    "Louth": (-0.00, 53.37),
    "Grimsby": (-0.08, 53.57),
    # And the ordinary interior, so a fix that shrinks a box is caught too.
    "Truro": (-5.05, 50.26),
    "Exeter": (-3.53, 50.72),
    "Salisbury": (-1.80, 51.07),
    "Dover": (1.31, 51.13),
    "Oxford": (-1.26, 51.75),
    "Cambridge": (0.12, 52.21),
    "Norwich": (1.30, 52.63),
    "King's Lynn": (0.40, 52.75),
    "Birmingham": (-1.90, 52.48),
    "Shrewsbury": (-2.75, 52.71),
    "Cardiff": (-3.18, 51.48),
    "Aberystwyth": (-4.08, 52.41),
    "Bangor": (-4.13, 53.23),
    "Manchester": (-2.24, 53.48),
    "Leeds": (-1.55, 53.80),
    "Hull": (-0.33, 53.74),
    "Whitby": (-0.61, 54.49),
    "Scarborough": (-0.40, 54.28),
    "Carlisle": (-2.94, 54.89),
    "Newcastle": (-1.61, 54.98),
    "Berwick": (-2.00, 55.77),
}

for _name, (_lon, _lat) in sorted(PLACES.items()):
    _in = [r for r, _l, (w, s_, e, n) in build_packages.REGIONS
           if w <= _lon <= e and s_ <= _lat <= n]
    check_true("%s belongs to a region" % _name, _in)


# --- REGIONS itself ----------------------------------------------------------

# Ids are what the app asks for by name. A typo here is a silent 404 on the
# phone, which looks to a rider exactly like an area with no data published.
APP_REGION_IDS = ["south-west", "south-east", "east-anglia", "midlands",
                  "wales", "north"]
check("region ids match the ones the app asks for",
      [r for r, _, _ in build_packages.REGIONS], APP_REGION_IDS)

for _r, _lab, (_w, _s, _e, _n) in build_packages.REGIONS:
    check_true("%s box is the right way round" % _r, _w < _e and _s < _n)


# --- ground we knowingly do not serve ----------------------------------------

# The monthly refresh calls build_packages with NO --allow-orphans, and it had
# never run: on 1 October it would have met a single Scottish lane, refused to
# publish, and shipped nothing at all. The obvious fix under time pressure is
# to add the flag - which would then hide every REAL hole for ever after. So
# Scotland is named, and everything else still fails the build.

# Highland, which is what the live data actually contains.
check_true("a Scottish lane is recognised as unserved",
           build_packages.unserved(lane([(-4.05, 57.55), (-4.00, 57.60)])))

# The cases that must NOT be swallowed: a hole anywhere the dataset claims to
# cover. Each of these sits inside no region box.
check("a gap in England is not excused as unserved",
      build_packages.unserved(lane([(-2.00, 54.20), (-1.98, 54.22)])),
      False)
check("nor one in the far south west",
      build_packages.unserved(lane([(-6.50, 49.90), (-6.48, 49.92)])),
      False)
check("nor one off the Welsh coast",
      build_packages.unserved(lane([(-5.40, 52.00), (-5.38, 52.02)])),
      False)

# Northumberland reaches 55.8 and the border is not a straight line, so nothing
# English may fall into the Scottish exclusion.
check("Berwick-upon-Tweed is English and stays served",
      build_packages.unserved(lane([(-2.00, 55.77), (-1.99, 55.78)])),
      False)
check_true("and it is placed in a region rather than orphaned",
           placed_regions(lane([(-2.00, 55.77), (-1.99, 55.78)])))

for _name, (_w, _s, _e, _n) in build_packages.UNSERVED:
    check_true("%s box is the right way round" % _name, _w < _e and _s < _n)


# --- reproducibility ----------------------------------------------------------

# The property the monthly refresh is built on: identical council data must
# rebuild to identical bytes.
#
# It did not. pack() drew a random nonce, so the same lanes sealed twice gave
# two different ciphertexts and two different hashes, and write_package put the
# build date in the filename, so every run wrote new paths regardless. The
# workflow's "councils published nothing new" branch therefore could not fire
# once: every monthly run republished ~100 MB and every rider re-downloaded the
# lot to end up with the lanes they already had.
#
# Each check below fails if any one of those three causes comes back - the
# random nonce, the dated filename, or the build stamp sealed into the payload.

# Any 32 bytes will do; this is a fixture, not a secret, and it is not the key
# anything is published with.
KEY = bytes(range(32))
OTHER_KEY = bytes(range(32, 64))


def payload(n):
    return json.dumps({"features": list(range(n))}).encode("utf8")


check("the same payload seals to the same bytes",
      build_packages.pack(payload(50), KEY),
      build_packages.pack(payload(50), KEY))

# The other half, and the half that makes the first one worth anything. A pack()
# that returned a constant would pass the check above and be catastrophic: with
# AES-GCM, one nonce over two different plaintexts leaks their XOR and hands an
# attacker the authentication subkey.
_a = build_packages.pack(payload(50), KEY)
_b = build_packages.pack(payload(51), KEY)
check_true("different payloads seal to different bytes", _a != _b)
check_true("and to different nonces", _a[6:18] != _b[6:18])

# The nonce is derived from the payload UNDER THE KEY, so it cannot be computed
# by anyone who does not hold the key.
check_true("a different key gives a different nonce",
           build_packages.pack(payload(50), OTHER_KEY)[6:18] != _a[6:18])

# Deterministic is no good if the app can no longer open it. This is the
# reader's side, exactly as check_build.py and the device do it: nonce taken
# from the prefix, whole prefix as the AAD.
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _sealed = build_packages.pack(payload(7), KEY)
    _prefix, _body = _sealed[:18], _sealed[18:]
    _plain = gzip.decompress(AESGCM(KEY).decrypt(_prefix[6:18], _body, _prefix))
    check("a pack still opens the way the app opens it", _plain, payload(7))
    check("and carries the container header", _prefix[:4], b"TBPK")
except ImportError:  # pragma: no cover - build_packages would have exited
    FAILURES.append("cryptography missing; the reader check did not run")


# --- an unchanged package keeps its bytes and its cut date --------------------

_real_dist = build_packages.dist_dir
_tmp = tempfile.mkdtemp(prefix="tbpack-test-")
build_packages.dist_dir = lambda: _tmp


def build_one(features, stamp, published=None):
    """One package, as main() would write it -> (entry, sealed bytes)."""
    entry = build_packages.write_package(
        "motor", "midlands", "Midlands", None, features, KEY, stamp,
        published=published)
    with open(os.path.join(_tmp, "packages",
                           os.path.basename(entry["file"])), "rb") as fh:
        return entry, fh.read()


LANES = [lane([(-1.70, 52.50), (-1.69, 52.51)], uid="a"),
         lane([(-1.60, 52.60), (-1.59, 52.61)], uid="b")]
CHANGED = LANES + [lane([(-1.50, 52.40), (-1.49, 52.41)], uid="c")]

JAN = "2026-01-01T03:17:00Z"
FEB = "2026-02-01T03:17:00Z"

_first, _first_bytes = build_one(LANES, JAN)

# The published name carries no date. The device already saves each pack under
# a dateless local name, so the date bought nothing and cost the ability to
# tell a rebuild from a change.
check("the published name carries no build date",
      _first["file"], "packages/motor-midlands.tbpack")

# Keyed the way build_packages keys it, via the manifest reader itself - a
# mismatch between the two would silently turn the comparison off and leave
# every rebuild looking new, which is the bug this whole test exists for.
_manifest = os.path.join(_tmp, "manifest.json")
with open(_manifest, "w", encoding="utf8") as fh:
    json.dump({"packages": [_first]}, fh)
PUBLISHED = build_packages.published_packages(_manifest)
check_true("the published manifest indexes the package we just built",
           "motor/midlands" in PUBLISHED)

# A month later, nothing amended: same bytes, same hash, same cut date.
_again, _again_bytes = build_one(LANES, FEB, PUBLISHED)
check("a rebuild of unchanged lanes is byte-identical",
      hashlib.sha256(_again_bytes).hexdigest(),
      hashlib.sha256(_first_bytes).hexdigest())
check("and keeps the date the data was actually cut",
      _again["generated"], JAN)
check("and so publishes the same hash", _again["sha256"], _first["sha256"])

# And the half that must still fail: a council amends its map.
_changed, _changed_bytes = build_one(CHANGED, FEB, PUBLISHED)
check_true("a changed lane changes the bytes", _changed_bytes != _first_bytes)
check_true("and the hash", _changed["sha256"] != _first["sha256"])
check("and the cut date moves to this build", _changed["generated"], FEB)

# A published hash that does not reproduce means "rebuild it", never "publish
# the old date anyway". That is what keeps a zlib upgrade or a changed field
# from freezing the cut date on data that really did move.
_stale = {"motor/midlands": dict(_first, sha256="0" * 64)}
_rebuilt, _ = build_one(LANES, FEB, _stale)
check("a hash that does not reproduce falls back to this build's date",
      _rebuilt["generated"], FEB)

build_packages.dist_dir = _real_dist
shutil.rmtree(_tmp, ignore_errors=True)


# --- ONE DATASET, CLASSED PER WAY (step 1.2, docs/WAYS-SCHEMA.md) ------------
#
# What this replaced: PACKAGES = motor/bicycle/horse/foot, four separate builds
# of overlapping ways. `bicycle` and `horse` were byte-identical, because they
# were the same bridleway data built twice, and the four of them produced 109
# containers on disk.

check_true("there is no vehicle partition left to build",
           not hasattr(build_packages, "PACKAGES"))
check("there is one dataset and it is named for what it holds",
      build_packages.DATASET, "ways")

# The classes, exactly as the schema words them. A rename here silently
# restyles the map: the one rule the schema exists to enforce is written
# against `way_class = 'boat'`.
check("the schema's classes are what the builder emits",
      sorted(r["way_class"] for r in build_packages.ROW_RULES.values()),
      ["boat", "bridleway", "footpath", "restricted_byway"])

# FOOTPATHS ARE NOT CARRIED. 435,299 of them, 627 MB, and not one has any
# bearing on where a motor vehicle may legally go.
check("footpaths are not carried",
      build_packages.ROW_RULES["footpath"]["carried"], False)
for _t in ("byway_open_to_all_traffic", "restricted_byway", "bridleway"):
    check_true("%s is carried" % _t, build_packages.ROW_RULES[_t]["carried"])

# Context is what the near-set filter applies to, and it must never include a
# BOAT: filtering the rideable ways by proximity to themselves would drop
# isolated byways, which are the ones a rider travels for.
check("only bridleways and restricted byways are context",
      sorted(t for t, r in build_packages.ROW_RULES.items() if r["context"]),
      ["bridleway", "restricted_byway"])
check("a byway open to all traffic is never context",
      build_packages.ROW_RULES["byway_open_to_all_traffic"]["context"], False)


# --- the derived access columns, and the rule the schema exists to enforce ---

# > A way is drawn rideable only when way_class = 'boat' and
# > legal_tier = 'statutory'.
check("only a BOAT is open to a motorbike",
      sorted(r["way_class"] for r in build_packages.ROW_RULES.values()
             if r["motorbike_ok"]), ["boat"])
check("only a BOAT is open to a 4x4",
      sorted(r["way_class"] for r in build_packages.ROW_RULES.values()
             if r["fourxfour_ok"]), ["boat"])

# > access_evidence must never be 'none' on a way where fourxfour_ok = 0 -
# > hiding a lane requires evidence, and F1 measured that we have it for under
# > 10%.
for _t, _r in sorted(build_packages.ROW_RULES.items()):
    if not _r["fourxfour_ok"]:
        check("%s is closed to a 4x4 on recorded evidence" % _t,
              _r["access_evidence"] != "none", True)
    check_true("%s says why, in words a rider can read" % _t,
               len(_r["access_reason"]) > 20)


# --- normalise() writes the schema, not the old lane shape -------------------

_raw = {
    "geometry": {"type": "LineString",
                 "coordinates": [(-1.70, 52.50), (-1.69, 52.51)]},
    "properties": {"Name": "ON|100|2/10", "Description": "BO|ON:22|0.144|none"},
}
_row = build_packages.normalise(_raw, "DE", "Derbyshire",
                                "byway_open_to_all_traffic")["properties"]
check("the class is the schema's class", _row["class"], "boat")
check("the legal tier is carried per way", _row["legal_tier"], "statutory")
check("the source names the authority it came from",
      _row["source"], "rowmaps:derbyshire")
check("the source date is a fixed-width placeholder until the pack is sealed",
      len(_row["source_date"]), 10)
check("a BOAT is open to a motorbike", _row["motorbike_ok"], 1)
check("and to a 4x4", _row["fourxfour_ok"], 1)
check("with statutory evidence", _row["access_evidence"], "statutory")
check_true("the vehicle list is gone with the partition",
           "vehicles" not in _row)

_bw = build_packages.normalise(_raw, "DE", "Derbyshire",
                               "bridleway")["properties"]
check("a bridleway is closed to a motorbike", _bw["motorbike_ok"], 0)
check("and to a 4x4", _bw["fourxfour_ok"], 0)
check_true("and says so in a sentence, not a boolean",
           "no mechanically propelled vehicles" in _bw["access_reason"])


# --- step 1.2c: the near-set filter ------------------------------------------
#
# MEASURED over the full published population: 10,342 BOATs and 89,300
# bridleways and restricted byways, of which 15,366 (17.2%) lie within 1 km of
# a BOAT and 73,934 (82.8%) do not. Carrying only the near set takes the
# dataset from 99,642 ways to 25,708.
#
# These checks are the mechanism, not the national figure: a filter that
# returned everything, or nothing, or measured degrees as kilometres would pass
# none of them.

_boat = lane([(-1.700, 52.500), (-1.699, 52.500)], uid="boat")
_touching = lane([(-1.699, 52.500), (-1.698, 52.501)], uid="touching")
_near = lane([(-1.700, 52.5045), (-1.699, 52.5045)], uid="near")   # ~500 m N
_far = lane([(-1.700, 52.600), (-1.699, 52.600)], uid="far")       # ~11 km N

_kept = [f["properties"]["lane_uid"] for f in
         build_packages.near_motor_ways([_touching, _near, _far], [_boat])]
check("a context way that meets a byway is carried - the whole reason it is "
      "carried at all", "touching" in _kept, True)
check("one 500 m away is still carried", "near" in _kept, True)
check("one 11 km away is not", "far" in _kept, False)
check("and nothing else came along with them", sorted(_kept),
      ["near", "touching"])

# The boundary, both sides of it, on the same axis so the distance is exact.
# 1 km north is 0.009044 deg of latitude.
_just_in = lane([(-1.700, 52.500 + 0.00895)], uid="in")
_just_out = lane([(-1.700, 52.500 + 0.00915)], uid="out")
check("just inside the radius is carried",
      [f["properties"]["lane_uid"] for f in
       build_packages.near_motor_ways([_just_in], [_boat])], ["in"])
check("just outside it is not",
      [f["properties"]["lane_uid"] for f in
       build_packages.near_motor_ways([_just_out], [_boat])], [])

# The grid is an index over nine cells, so a way in a DIAGONAL neighbour cell
# must still be measured. A filter that only looked in its own cell would pass
# every check above and drop these.
_diag = lane([(-1.700 + 0.0100, 52.500 + 0.0060)], uid="diag")   # ~0.9 km
check("a way in a diagonal neighbouring cell is still found",
      [f["properties"]["lane_uid"] for f in
       build_packages.near_motor_ways([_diag], [_boat])], ["diag"])

# Longitude is not latitude. At 52.5 N a degree of longitude is 68 km and a
# degree of latitude is 111 km, so a filter using one grid step for both is
# wrong by 60% in one direction.
_east = lane([(-1.700 + 0.0133, 52.500)], uid="east")            # ~0.9 km E
check("the radius is kilometres, not degrees, going east",
      [f["properties"]["lane_uid"] for f in
       build_packages.near_motor_ways([_east], [_boat])], ["east"])
_east_far = lane([(-1.700 + 0.0200, 52.500)], uid="east-far")    # ~1.35 km E
check("and a way 1.35 km east is out, though it is closer in degrees than "
      "the 1 km one to the north",
      [f["properties"]["lane_uid"] for f in
       build_packages.near_motor_ways([_east_far], [_boat])], [])

# THE GRID STEP IS A KILOMETRE ON BOTH AXES, AND ONE CONSTANT FOR BOTH IS A
# SILENT UNDER-COUNT.
#
# A degree of latitude is 111 km everywhere; a degree of longitude is 65 km at
# GB latitudes. A filter that used the latitude step for both still measures
# each candidate pair correctly - so every single-pair check above passes - and
# quietly builds a grid too fine in the east-west direction, whose nine cells
# no longer reach a kilometre. The ways it then drops depend on where the cell
# boundaries happen to fall, which is why this sweeps the base longitude
# instead of testing one pair: MEASURED, the wrong constant misses 481 of 3,800
# genuinely-near pairs, and there are base longitudes at which it misses none.
_km_per_deg_lon = build_packages._km_between((-1.70, 54.0),
                                             (-1.70 + build_packages._KM_LON,
                                              54.0))
check_true("one grid step east is one kilometre, not one of latitude",
           0.97 <= _km_per_deg_lon <= 1.03)

_missed = []
for _j in range(60):
    _base = -1.70 + _j * 0.0007
    _b = lane([(_base, 52.50)], uid="sweep-boat")
    for _km in (0.80, 0.85, 0.90, 0.95):
        _c = lane([(_base + _km / 65.40, 52.50)], uid="sweep-%s" % _km)
        if not build_packages.near_motor_ways([_c], [_b]):
            _missed.append((round(_base, 4), _km))
check("no way inside the radius is missed, wherever the cells fall",
      _missed, [])

# No byways at all is not "carry everything": it is a region with nothing to
# ride, and the context has nothing to give context to.
check("no byways means no context carried",
      build_packages.near_motor_ways([_touching, _near], []), [])


# --- the absence must be sayable ---------------------------------------------
#
# A rider who looks at a hillside and sees no bridleway must not read that as
# "there is no bridleway here". There is; we did not carry it. If this string
# ever goes missing the app has nothing to show, and our gap becomes a claim
# about the ground.
check("the carried scope is named", build_packages.CONTEXT_SCOPE,
      "near-byways-only")
check_true("and there is a sentence the app can show a rider",
           "does not mean there is none on the ground"
           in build_packages.CONTEXT_NOTE)
check_true("which says how far we looked",
           "1 km" in build_packages.CONTEXT_NOTE)


# --- the pack's sealed body carries the schema and the scope -----------------

_dist2 = tempfile.mkdtemp(prefix="tbways-test-")
_real_dist2 = build_packages.dist_dir
build_packages.dist_dir = lambda: _dist2
_entry = build_packages.write_package(
    build_packages.DATASET, "midlands", "Midlands", None,
    [build_packages.normalise(_raw, "DE", "Derbyshire",
                              "byway_open_to_all_traffic")],
    KEY, "2026-03-04T05:06:07Z", note="n")
with open(os.path.join(_dist2, "packages",
                       os.path.basename(_entry["file"])), "rb") as fh:
    _blob = fh.read()
from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _A
_body = json.loads(gzip.decompress(
    _A(KEY).decrypt(_blob[6:18], _blob[18:], _blob[:18])))
check("the pack is named for the dataset, not a vehicle",
      _entry["file"], "packages/ways-midlands.tbpack")
check("the sealed pack records what scope of context it holds",
      _body["contextScope"], "near-byways-only")
check("the source date on a way is the date the pack was cut",
      _body["features"][0]["properties"]["source_date"], "2026-03-04")
build_packages.dist_dir = _real_dist2
shutil.rmtree(_dist2, ignore_errors=True)


if FAILURES:
    print("FAILED (%d)\n" % len(FAILURES))
    for f in FAILURES:
        print(f + "\n")
    sys.exit(1)
print("build_packages: all checks passed")

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
import os
import sys

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


if FAILURES:
    print("FAILED (%d)\n" % len(FAILURES))
    for f in FAILURES:
        print(f + "\n")
    sys.exit(1)
print("build_packages: all checks passed")

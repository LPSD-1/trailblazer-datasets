#!/usr/bin/env python3
"""Build the national Traffic Regulation Order pack from D-TRO.

    python tools/build_tro.py --key ../trailblazer-keys/dataset-encryption-key-256.b64
    python tools/build_tro.py --key KEY --csv path/to/dtros_all.csv   (offline)

WHY THIS IS ONE NATIONAL PACK AND NOT ONE PER REGION
----------------------------------------------------
Measured against the whole published corpus on 14 September 2026:

    146,202 records published
     36,821 live restrictions worth carrying
    216,663 coordinate pairs, about 1.7 MB before gzip

The entire country fits in a couple of megabytes. Splitting that into six
regional packs would save a rider nothing worth having and would cost them the
thing that matters: an order does not stop at a regional boundary, and a rider
who has downloaded the Midlands should still be told about the closure two
miles into Wales.

WHY IT IS BUILT TWICE A DAY
---------------------------
Every other dataset here is a snapshot of something that changes slowly -
council definitive maps are amended over months, OS place names over years. A
traffic regulation order is the opposite: the ones that matter most are the
ones made last week, and a road closure a rider finds out about by arriving at
it is exactly the failure this is meant to prevent. Small and perishable, so
it is fetched often and fetched first.

WHAT IT IS NOT
--------------
It is not the legal record and it does not make the app's caveat go away. Not
every authority publishes - 91 of them appear in the corpus, out of about 174 -
so an empty stretch of map means "nothing published here", never "nothing in
force here". The sign on the post still wins. See docs/TRO_SPEC.md.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_packages import load_key, pack  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402
from tro import features  # noqa: E402

csv.field_size_limit(2**31 - 1)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# SRID=27700;LINESTRING(e n, e n) — also POINT, POLYGON and the MULTI forms.
#
# CASE-INSENSITIVE, AND IT TOLERATES A DIMENSION TAG. `POINT Z (x y z)` is
# ordinary WKT and the service is free to start sending it; the old pattern
# wanted a bare word hard against the bracket, so every such order would have
# vanished from the pack with nothing logged anywhere. The same for a
# lowercase `srid=`.
_WKT = re.compile(r"SRID=(\d+);\s*([A-Za-z]+)\s*(?:Z|M|ZM)?\s*\((.*)\)\s*$",
                  re.S | re.I)

# The National Grid's own extent, in metres, generously.
#
# CHECKED ON THE EASTING AND NORTHING, not on the degrees that come out.
# (0, 0) — far and away the commonest missing value — converts to a point in
# the Celtic Sea about 130 km southwest of Land's End, which sits comfortably
# inside any box drawn round the British Isles and sailed straight through the
# check that used to be here. Inside a linestring it was worse: a 130 m closure
# near Sheffield became a 400 km V out into the Atlantic and back, and
# everything downstream that reads a bounding box then covered half of England.
_GRID = (0.0, 0.0, 800000.0, 1400000.0)

# Great Britain, generously, as a second net under the first.
_GB = (-9.0, 49.0, 2.5, 61.5)


def in_grid(easting, northing):
    """Whether a pair could have come from the National Grid at all.

    (0, 0) IS REJECTED EXPLICITLY. It is a real corner of the grid — the
    southwest of square SV, out in the sea beyond Scilly — so a range test
    admits it, and it is also the value a missing number arrives as far more
    often than it is a place anybody has closed a road. Treating the origin as
    the sentinel it is in practice costs nothing real and is the whole reason
    this function exists.
    """
    if easting == 0 and northing == 0:
        return False
    return (_GRID[0] <= easting <= _GRID[2]
            and _GRID[1] <= northing <= _GRID[3])

# Five decimal places is about a metre. The datum shift in osgb.py is good to
# about five, so more places would be recording noise - and every digit is
# bytes in a file riders fetch twice a day.
_PLACES = 5


# ---------------------------------------------------------------------------
# ORDER TYPE, AND WHICH VEHICLE IT BITES
# ---------------------------------------------------------------------------
#
# PIVOT-SPEC.md §5.3, transcribed into a table this file can be checked
# against, because "there is an order here" is not a fact a 4x4 filter can use.
#
# Until now the pack carried `code` (the D-TRO enum) and `label` (words for a
# human). Both reach the app; neither tells it whether the order is about the
# rider's vehicle. A width limit and a pedestrian zone are the same shade of
# red on the map today, and one of them is the ONLY official evidence we hold
# that a lane is closed to a 4x4 and open to a motorbike.
#
# THE THREE VERDICTS, and why there are three and not two:
#
#   "yes"        the order binds this vehicle, whatever it is.
#   "sometimes"  it binds it depending on the individual vehicle - its weight,
#                its width, its height. A verdict that cannot be taken from
#                the order alone.
#   "no"         it cannot bind this vehicle at all.
#
# §5.3 words the middle one three ways - "rarely", "often", "maybe" - and
# those are priors, not rules: a 3.5 t limit stops almost every 4x4 with a
# trailer and no motorbike ever, and neither fact is in the order. So the
# spec's own word is carried as prose in `note`, for the rider to read, and
# the computable verdict stays three-valued. Collapsing "often" into "yes"
# would hide legal lanes; collapsing it into "no" would send a rider up one.
ORDER_TYPES = {
    # --- the five §5.3 rows that are a KIND of restriction -----------------
    "prohibition": {
        "label": "Prohibition of driving",
        "effect": "Way shut to motors",
        "bike": "yes", "bike_note": "yes",
        "x4": "yes", "x4_note": "yes",
    },
    "weight": {
        "label": "Weight limit",
        "effect": "Vehicles over N tonnes",
        "bike": "sometimes", "bike_note": "rarely",
        "x4": "sometimes", "x4_note": "often",
    },
    "width": {
        "label": "Width limit",
        "effect": "Vehicles over N metres",
        "bike": "no", "bike_note": "no",
        "x4": "sometimes", "x4_note": "often",
    },
    "height": {
        "label": "Height limit",
        "effect": "Under a structure",
        "bike": "no", "bike_note": "no",
        "x4": "sometimes", "x4_note": "maybe",
    },
    "oneway": {
        "label": "One way",
        "effect": "Direction of travel",
        "bike": "yes", "bike_note": "yes",
        "x4": "yes", "x4_note": "yes",
    },
    # --- kinds the corpus carries that §5.3 does not name -----------------
    # Left out of §5.3's table but present in tro.INTERESTING, so they must
    # land somewhere. Each is placed by the same question: can it stop this
    # vehicle, and does that depend on the vehicle?
    "turn": {
        "label": "Banned turn",
        "effect": "Direction of travel at a junction",
        "bike": "yes", "bike_note": "yes",
        "x4": "yes", "x4_note": "yes",
    },
    "length": {
        "label": "Length limit",
        "effect": "Vehicles over N metres long",
        "bike": "no", "bike_note": "no",
        "x4": "sometimes", "x4_note": "maybe - a 4x4 with a trailer",
    },
    "speed": {
        "label": "Speed limit",
        "effect": "How fast, not whether",
        "bike": "yes", "bike_note": "yes",
        "x4": "yes", "x4_note": "yes",
    },
    "footway": {
        "label": "Footway or cycle lane closed",
        "effect": "Not the carriageway",
        "bike": "no", "bike_note": "no",
        "x4": "no", "x4_note": "no",
    },
    # A SUSPENSION LIFTS A RESTRICTION. It is carried because a rider who has
    # been told about a weight limit needs to know when it stops applying —
    # but drawing it as a restriction in its own right would shut a road the
    # authority has just reopened.
    "suspension": {
        "label": "Restriction suspended",
        "effect": "Lifts an earlier order; imposes nothing",
        "bike": "no", "bike_note": "no",
        "x4": "no", "x4_note": "no",
    },
    # THE FAIL-SAFE, and it fails towards telling the rider. Anything
    # reaching here has already passed tro.INTERESTING, whose whole test is
    # "can it stop a vehicle, turn it round, or catch it by its size" — so an
    # unmapped code is a restriction of a kind this build has not met, not a
    # parking bay. "no" here would silently drop a new closure kind from every
    # 4x4's map on the day D-TRO started publishing it.
    "other": {
        "label": "Restriction",
        "effect": "A kind this build does not recognise",
        "bike": "sometimes", "bike_note": "unrecognised - read the order",
        "x4": "sometimes", "x4_note": "unrecognised - read the order",
    },
}

# D-TRO's own enum, from tro.INTERESTING, to the type above.
#
# KEPT HERE RATHER THAN IN tro.py deliberately: that module's job is to say
# what a record contains, and this one's is to say what it means for a rider.
_CODE_TYPE = {
    "miscRoadClosure": "prohibition",
    "miscLaneClosure": "prohibition",
    "movementOrderProhibitedAccess": "prohibition",
    "miscPedestrianZone": "prohibition",
    "miscFootwayClosure": "footway",
    "miscCycleLaneClosure": "footway",
    "mandatoryDirectionOneWay": "oneway",
    "miscContraflow": "oneway",
    "bannedMovementNoRightTurn": "turn",
    "bannedMovementNoLeftTurn": "turn",
    "bannedMovementNoUTurn": "turn",
    "dimensionMaximumWeightStructural": "weight",
    "dimensionMaximumWidth": "width",
    "dimensionMaximumHeightStructural": "height",
    "dimensionMaximumLength": "length",
    "miscSuspensionOfOneWay": "suspension",
    "miscSuspensionOfWeightRestriction": "suspension",
    "speedLimitValueBased": "speed",
}

# §5.3's other two rows — Seasonal/TTRO and Experimental — are not a KIND of
# restriction, they are the FORM of the instrument. A temporary order shuts a
# road exactly as a permanent one does; what differs is that it stops.
#
# Splitting them out is not tidiness, it is the rule in §5.4 that a dated
# seasonal way is NOT shut. A seasonal prohibition folded into `prohibition`
# would draw a lane closed all year for a restriction that runs in May.
ORDER_FORMS = {
    "permanent": {
        "label": "Permanent",
        "when": "in force between its dates",
    },
    "seasonal": {
        "label": "Seasonal or temporary (TTRO)",
        "when": "inside its dates only - outside them the way is not shut",
    },
    "experimental": {
        "label": "Experimental",
        "when": "in force now, but a trial that may lapse",
    },
}

# THE FORM IS READ FROM THE ORDER'S OWN TITLE, and that is a weaker signal
# than the rest of this file uses. D-TRO has no published field saying "this
# is an experimental order" that reaches tools/tro.py, so the only evidence
# available here is how the instrument names itself — and legal instruments
# are titled by statute, so "(Experimental) Order 2026" is a real signal
# rather than a guess at prose.
#
# It is a heuristic and it is written down as one. The structural fix belongs
# in tools/tro.py, which reads the record: see the note in main().
_EXPERIMENTAL = re.compile(r"\bexperimental\b", re.I)
_TEMPORARY = re.compile(r"\b(seasonal|temporar(?:y|ily)|ttro|t\.t\.r\.o)\b",
                        re.I)


def order_type(code):
    """The D-TRO regulation code as one of §5.3's kinds."""
    return _CODE_TYPE.get(code, "other")


def order_form(feature):
    """Whether the instrument is permanent, seasonal/temporary, or a trial.

    Title first, dates second. An order that names itself experimental is
    experimental whatever its dates say; an order with both a start AND an end
    is time-bounded, which is what a TTRO is, whatever it calls itself.
    """
    title = " ".join(str(feature.get(key) or "") for key in ("name", "ref"))
    if _EXPERIMENTAL.search(title):
        return "experimental"
    if _TEMPORARY.search(title):
        return "seasonal"
    if feature.get("start") and feature.get("end"):
        return "seasonal"
    return "permanent"


def vehicles_for(otype, oform="permanent"):
    """Which vehicles an order of this type and form bites.

    The one function the app, the ways builder and the check below all read,
    so the table cannot drift between them.
    """
    row = dict(ORDER_TYPES.get(otype) or ORDER_TYPES["other"])
    form = ORDER_FORMS.get(oform) or ORDER_FORMS["permanent"]
    row["form"] = oform if oform in ORDER_FORMS else "permanent"
    row["when"] = form["when"]
    return row


# The only order types that are OFFICIAL evidence a lane is shut to a 4x4 and
# open to a motorbike (spec §5.3: "Weight and width orders feed §4 directly —
# they are the only official source of 4x4-specific restriction we have").
#
# F1 measured that OSM gives us width on ~10% of byways and a 4x4-blocking
# barrier on under 2%, so this short list is most of what the 4x4 filter can
# stand on. Height and length are NOT in it: §5.3 says "maybe" for both, and
# a bridge limit is typically over three metres, which stops no 4x4. They are
# carried as advice, which is what an unquantified maybe is worth.
HIDES_FOURXFOUR = ("weight", "width")
ADVISES_FOURXFOUR = ("height", "length")
SHUTS_TO_MOTORS = ("prohibition",)


def way_access(orders):
    """The WAYS-SCHEMA verdict for a way these orders touch.

    `orders` is an iterable of (otype, oform) pairs - every order matched to
    the way. Returns the four schema columns as a dict:
    motorbike_ok, fourxfour_ok, access_reason, access_evidence.

    WHY THIS LIVES HERE AND NOT IN THE WAYS BUILDER. WAYS-SCHEMA.md says
    `access_evidence` must never be 'none' on a way where fourxfour_ok = 0 -
    hiding a lane requires evidence. That rule is only keepable if the thing
    that decides to hide and the thing that records why are the same call.

    A SEASONAL OR EXPERIMENTAL ORDER DOES NOT SHUT THE WAY HERE. Spec §5.4:
    a dated seasonal way is not shut. The dates travel with the feature and
    the app applies them against the day being ridden, which is the only place
    that knows what day that is. Deciding it in a build would decide it once,
    on a server, for a file somebody opens a fortnight later - the same
    mistake the corpus-date comment above this file exists to prevent.
    """
    motorbike_ok, fourxfour_ok = 1, 1
    reasons, evidence = [], "none"
    for otype, oform in orders:
        row = ORDER_TYPES.get(otype) or ORDER_TYPES["other"]
        dated = oform in ("seasonal", "experimental")
        if otype in SHUTS_TO_MOTORS and not dated:
            motorbike_ok, fourxfour_ok = 0, 0
            reasons.append(row["label"])
            evidence = "order"
        elif otype in HIDES_FOURXFOUR and not dated:
            fourxfour_ok = 0
            reasons.append("%s (%s for a 4x4)" % (row["label"], row["x4_note"]))
            evidence = "order"
        elif otype in SHUTS_TO_MOTORS or otype in HIDES_FOURXFOUR:
            reasons.append("%s, %s" % (row["label"],
                                       ORDER_FORMS[oform]["when"]))
            evidence = "order"
        elif otype in ADVISES_FOURXFOUR:
            reasons.append("%s (%s)" % (row["label"], row["x4_note"]))
            evidence = "order"
    return {
        "motorbike_ok": motorbike_ok,
        "fourxfour_ok": fourxfour_ok,
        "access_reason": "; ".join(reasons) if reasons
                         else "No order restricts this way",
        # Never 'none' where fourxfour_ok is 0: the branch that sets the 0 is
        # the branch that sets the evidence, three lines apart, so the two
        # cannot come adrift. The check below asserts it anyway.
        "access_evidence": evidence,
    }


def parse_wkt(text):
    """`SRID=27700;LINESTRING(...)` to (kind, [(easting, northing), ...])."""
    match = _WKT.match((text or "").strip())
    if not match:
        return None, []
    srid, kind, body = match.group(1), match.group(2).upper(), match.group(3)
    if srid != "27700":
        # Everything published so far is 27700. Anything else is a change in
        # the service, and guessing at it would silently misplace the order.
        return None, []
    def read(run):
        points = []
        for chunk in run.split(","):
            parts = chunk.split()
            if len(parts) < 2:
                continue
            try:
                points.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
        return points

    # THE MULTI FORMS KEEP THEIR PARTS APART. Flattened into one run of
    # points — which is what happened — two closed stretches a mile apart get
    # joined by a straight line drawn along roads that are open.
    if kind.startswith("MULTI") or kind == "GEOMETRYCOLLECTION":
        parts = [read(run) for run in re.findall(r"\(([^()]*)\)", body)]
        return kind, [p for p in parts if p]

    return kind, read(body.replace("(", " ").replace(")", " "))


def to_wgs84(points):
    """National Grid pairs to rounded [lon, lat], dropping anything absurd."""
    out = []
    for easting, northing in points:
        # The grid check FIRST — see the note on _GRID. The degree check below
        # cannot catch (0, 0) because (0, 0) converts to somewhere plausible.
        if not in_grid(easting, northing):
            continue
        lon, lat = grid_to_wgs84(easting, northing)
        if not (_GB[0] <= lon <= _GB[2] and _GB[1] <= lat <= _GB[3]):
            continue
        point = [round(lon, _PLACES), round(lat, _PLACES)]
        # Consecutive duplicates survive the rounding and carry no shape.
        if out and out[-1] == point:
            continue
        out.append(point)
    return out


def geojson(feature, dtro_id=None):
    """One normalised restriction as a GeoJSON feature, or None."""
    kind, points = parse_wkt(feature["wkt"])
    if not points:
        return None

    # A MULTI form arrives as a list of runs. Its parts stay apart: joining
    # them draws a line along roads that are open.
    if kind and (kind.startswith("MULTI") or kind == "GEOMETRYCOLLECTION"):
        parts = [to_wgs84(run) for run in points if isinstance(run, list)]
        parts = [p for p in parts if len(p) >= 2]
        if not parts:
            return None
        geometry = {"type": "MultiLineString", "coordinates": parts}
        return _wrap(geometry, parts[0][0], feature, dtro_id)

    coords = to_wgs84(points)
    if not coords:
        return None

    if kind == "POINT" or len(coords) == 1:
        geometry = {"type": "Point", "coordinates": coords[0]}
    elif kind == "POLYGON":
        # Closed ring, which GeoJSON requires and WKT does not always carry.
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        if len(coords) < 4:
            return None
        geometry = {"type": "Polygon", "coordinates": [coords]}
    else:
        if len(coords) < 2:
            return None
        geometry = {"type": "LineString", "coordinates": coords}

    return _wrap(geometry, coords[0], feature, dtro_id)


def _wrap(geometry, first, feature, dtro_id):
    """Geometry plus the properties every feature carries."""
    properties = {
        "code": feature["code"],
        "label": feature["label"],
    }
    # THE TYPE, CARRIED SEPARATELY FROM THE CODE.
    #
    # `code` is D-TRO's enum and `label` is words for a human; neither is a
    # thing the 4x4 filter can switch on without holding its own copy of §5.3,
    # which is how two copies of a safety table drift apart. `otype` is §5.3's
    # row, resolved here, once, beside the table it comes from.
    otype = order_type(feature["code"])
    oform = order_form(feature)
    properties["otype"] = otype
    # Only when it is not the default. Written on every feature it would add
    # about a quarter of a megabyte to say "permanent" 33,000 times.
    if oform != "permanent":
        properties["oform"] = oform
    # The order this came from. Carried so a delta can REPLACE everything one
    # order contributed, rather than trying to match feature by feature: an
    # amended order routinely changes how many stretches it covers, and a
    # merge that cannot delete what is gone leaves ghosts on the map.
    if dtro_id:
        properties["dtro"] = dtro_id
    # Only what is actually there. An empty string for every absent field
    # would add a hundred kilobytes to say nothing.
    for key in ("name", "where", "start", "end", "ref", "tra"):
        value = feature.get(key)
        if value not in (None, ""):
            properties[key] = value

    # A stable id, so the app can tell "this order again" from "a new order"
    # across two days' packs without the service offering one that survives an
    # amendment.
    #
    # SEEDED FROM WHAT THE ORDER ACTUALLY SAYS. It used to be
    # (ref, code, where, first coordinate) under a comment claiming "an order
    # that has not changed keeps its id and one that has gets a new one" — and
    # it was neither. Amending the dates kept the id, so an app caching by uid
    # went on showing the old ones; extending the geometry by a hundred
    # kilometres kept it too; and two different closures at one junction with
    # no ref and no road name collided into one, so one of them simply
    # disappeared from the rider's map.
    #
    # The dates and the whole geometry are in the seed now. The rounding in
    # to_wgs84 is what keeps it stable: identical published geometry gives
    # identical coordinates gives an identical id, which is what lets an
    # unchanged cut rebuild to identical bytes.
    seed = "|".join([
        str(feature.get("ref")),
        str(feature["code"]),
        str(feature.get("where")),
        str(feature.get("start")),
        str(feature.get("end")),
        geometry["type"],
        json.dumps(geometry["coordinates"], separators=(",", ":")),
    ])
    properties["tro_uid"] = hashlib.sha256(seed.encode()).hexdigest()[:16]
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def read_corpus(path, today, keep_expired=False):
    """Every live restriction in the national CSV extract."""
    kept, records, skipped = [], 0, 0
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            records += 1
            try:
                record = json.loads(row["Data"])
            except (ValueError, KeyError):
                skipped += 1
                continue
            dtro_id = row.get("Id")
            for feature in features(record, today=today,
                                    keep_expired=keep_expired):
                built = geojson(feature, dtro_id)
                if built is not None:
                    kept.append(built)
    return kept, records, skipped


def previous_count(index_path):
    """How many restrictions the last published pack carried, or 0.

    Read from the committed index rather than from the pack itself, because
    every job can read the index and only the job that built the pack has the
    pack. See the note where the index is written.
    """
    try:
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)
        for pack in index.get("packs", []):
            count = pack.get("features")
            if isinstance(count, int) and count > 0:
                return count
    except (IOError, ValueError, KeyError):
        pass
    return 0


def corpus_date(path):
    """The day the extract was cut, from its own filename.

    `/dtros/all` hands back a URL like `dtros_20260906_010013.csv`, and that
    date - not today's - is what goes in the pack. The pack is then a function
    of the data alone, so rebuilding an unchanged extract produces identical
    bytes and riders do not re-download a file that has not changed.
    """
    match = re.search(r"(\d{4})(\d{2})(\d{2})", os.path.basename(path))
    if match:
        return "-".join(match.groups())
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# THE CHECK: one order of each §5.3 type, through the real build
# ---------------------------------------------------------------------------
#
# Run with `--check-order-types`. It writes a corpus CSV in the shape D-TRO
# publishes, feeds it to `read_corpus` — the same function the national build
# calls, not a reimplementation — and asserts what each order resolves to.
#
# It lives in this file rather than in tools/test_build_tro.py because the
# table it checks lives in this file, and a safety table and the assertion
# that it is right should not be able to move apart by one of them being
# edited and the other not being run.
_CHECK_TODAY = "2026-06-01"
_CHECK_LINE = ("SRID=27700;LINESTRING(464946.33 293262.84, "
               "464918.98 293239.27)")


def _check_record(code, name, start="2026-05-01T00:00:00", end=None,
                  speed=None):
    """One D-TRO record, trimmed to the fields tools/tro.py reads."""
    validity = {"start": start}
    if end is not None:
        validity["end"] = end
    regulation = {"timeZone": "Europe/London",
                  "condition": [{"timeValidity": validity}]}
    if speed is not None:
        regulation["speedLimitValueBased"] = {"type": "maximumSpeedLimit",
                                              "mphValue": speed}
    else:
        regulation["generalRegulation"] = {"regulationType": code}
    return {"source": {
        "troName": name,
        "reference": "149816802",
        "traCreator": 2460,
        "currentTraOwner": 2460,
        "provision": [{
            "reference": "149816802/44819603",
            "regulation": [regulation],
            "regulatedPlace": [{
                "type": "regulationLocation",
                "description": "Hooton Lane",
                "linearGeometry": {"linestring": _CHECK_LINE},
            }],
        }],
    }}


# Every row of PIVOT-SPEC.md §5.3, and what it must come out as.
#   (case, record, expected otype, oform, bike verdict, 4x4 verdict)
_CHECK_CASES = [
    ("prohibition of driving",
     ("miscRoadClosure", "THE DERBYSHIRE (HOOTON LANE) ORDER 2026", None),
     "prohibition", "permanent", "yes", "yes"),
    ("weight limit",
     ("dimensionMaximumWeightStructural",
      "THE DERBYSHIRE (HOOTON LANE) ORDER 2026", None),
     "weight", "permanent", "sometimes", "sometimes"),
    ("width limit",
     ("dimensionMaximumWidth", "THE DERBYSHIRE (HOOTON LANE) ORDER 2026",
      None),
     "width", "permanent", "no", "sometimes"),
    ("height limit",
     ("dimensionMaximumHeightStructural",
      "THE DERBYSHIRE (HOOTON LANE) ORDER 2026", None),
     "height", "permanent", "no", "sometimes"),
    ("one-way",
     ("mandatoryDirectionOneWay", "THE DERBYSHIRE (HOOTON LANE) ORDER 2026",
      None),
     "oneway", "permanent", "yes", "yes"),
    ("seasonal / TTRO",
     ("miscRoadClosure",
      "THE DERBYSHIRE (HOOTON LANE) (TEMPORARY PROHIBITION) ORDER 2026",
      "2026-11-01T00:00:00"),
     "prohibition", "seasonal", "yes", "yes"),
    ("experimental",
     ("miscRoadClosure",
      "THE DERBYSHIRE (HOOTON LANE) (EXPERIMENTAL) ORDER 2026", None),
     "prohibition", "experimental", "yes", "yes"),
]


def check_order_types(mutate=False):
    """Prove an order of each §5.3 type resolves to the right vehicles.

    `mutate` is the falsifier, and it exists for the same reason golden.py's
    does: a check nobody has watched go red is a check nobody has watched. It
    misfiles a width limit as a road closure - the single most dangerous
    confusion in the table, because it turns "no 4x4s" into "nobody at all"
    and hides a lane a motorbike may legally ride.
    """
    import tempfile

    if mutate:
        _CODE_TYPE["dimensionMaximumWidth"] = "prohibition"

    rows = []
    for _name, (code, title, end), _t, _f, _b, _x in _CHECK_CASES:
        speed = None
        rows.append(_check_record(code, title, end=end, speed=speed))

    handle, path = tempfile.mkstemp(suffix=".csv", prefix="tro-check-")
    os.close(handle)
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Id", "SchemaVersion", "Data"])
            for i, record in enumerate(rows):
                writer.writerow(["check-%d" % i, "3.4.0",
                                 json.dumps(record)])
        built, records, skipped = read_corpus(path, _CHECK_TODAY)
    finally:
        os.unlink(path)

    print("order types - %d records in, %d features out, %d unreadable"
          % (records, len(built), skipped))
    if len(built) != len(_CHECK_CASES):
        print("FAIL: %d features from %d cases - an order was dropped "
              "before it could be typed" % (len(built), len(_CHECK_CASES)))
        return 1

    failures = []
    width = max(len(case[0]) for case in _CHECK_CASES)
    print("%-*s  %-12s %-12s  %-9s %-9s" % (width, "spec 5.3 order", "otype",
                                            "form", "bike", "4x4"))
    for case, feature in zip(_CHECK_CASES, built):
        name, _record, want_type, want_form, want_bike, want_x4 = case
        props = feature["properties"]
        got_type = props.get("otype")
        got_form = props.get("oform", "permanent")
        verdict = vehicles_for(got_type, got_form)
        got_bike, got_x4 = verdict["bike"], verdict["x4"]
        ok = (got_type == want_type and got_form == want_form
              and got_bike == want_bike and got_x4 == want_x4)
        print("%-*s  %-12s %-12s  %-9s %-9s  %s"
              % (width, name, got_type, got_form, got_bike, got_x4,
                 "ok" if ok else "WRONG"))
        if not ok:
            failures.append(
                "%s: wanted (%s, %s, bike=%s, 4x4=%s), got (%s, %s, bike=%s,"
                " 4x4=%s)" % (name, want_type, want_form, want_bike, want_x4,
                              got_type, got_form, got_bike, got_x4))

    # THE SECOND HALF, and the one the ways schema turns on: weight and width
    # must reach `access_evidence = 'order'` with fourxfour_ok = 0, because
    # they are the only official 4x4-specific restriction we hold.
    print()
    expected_access = {
        "weight": (1, 0, "order"),
        "width": (1, 0, "order"),
        "height": (1, 1, "order"),      # §5.3 says "maybe" - advice, not a hide
        "prohibition": (0, 0, "order"),
        "oneway": (1, 1, "none"),       # direction, not access
    }
    for otype, (want_bike_ok, want_x4_ok, want_ev) in sorted(
            expected_access.items()):
        got = way_access([(otype, "permanent")])
        ok = (got["motorbike_ok"] == want_bike_ok
              and got["fourxfour_ok"] == want_x4_ok
              and got["access_evidence"] == want_ev)
        print("way_access(%-12s) -> motorbike_ok=%d fourxfour_ok=%d "
              "evidence=%-9s %s  [%s]"
              % (otype, got["motorbike_ok"], got["fourxfour_ok"],
                 got["access_evidence"], "ok" if ok else "WRONG",
                 got["access_reason"]))
        if not ok:
            failures.append(
                "way_access(%s): wanted (%d, %d, %s), got (%d, %d, %s)"
                % (otype, want_bike_ok, want_x4_ok, want_ev,
                   got["motorbike_ok"], got["fourxfour_ok"],
                   got["access_evidence"]))

    # A seasonal prohibition is NOT shut (spec §5.4), and it still carries its
    # evidence, so the rider is told why the lane is amber rather than green.
    seasonal = way_access([("prohibition", "seasonal")])
    if seasonal["fourxfour_ok"] != 1 or seasonal["access_evidence"] != "order":
        failures.append("a seasonal prohibition shut the way: %r" % seasonal)
    else:
        print("way_access(seasonal prohibition) -> fourxfour_ok=1 "
              "evidence=order  ok  [%s]" % seasonal["access_reason"])

    # WAYS-SCHEMA.md: access_evidence must never be 'none' where
    # fourxfour_ok = 0. Asserted over every type and form this build can emit,
    # not over the cases above, because the rule is about the whole table.
    for otype in ORDER_TYPES:
        for oform in ORDER_FORMS:
            got = way_access([(otype, oform)])
            if got["fourxfour_ok"] == 0 and got["access_evidence"] == "none":
                failures.append(
                    "(%s, %s) hides a lane from a 4x4 with no evidence"
                    % (otype, oform))

    # Every code tools/tro.py can emit must have a home in the table. A new
    # one appearing upstream would otherwise land in "other" silently.
    from tro import INTERESTING, SPEED_LIMIT
    unmapped = sorted(set(list(INTERESTING) + [SPEED_LIMIT]) - set(_CODE_TYPE))
    if unmapped:
        failures.append("codes tro.py emits with no spec-5.3 type: %s"
                        % ", ".join(unmapped))

    print()
    if failures:
        for line in failures:
            print("FAIL: %s" % line)
        print("%d of %d checks failed" % (len(failures),
                                          len(_CHECK_CASES)
                                          + len(expected_access)))
        return 1
    print("all %d spec-5.3 order types resolve to the right vehicles; "
          "weight and width reach access_evidence='order'" % len(_CHECK_CASES))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--key", help="base64 32-byte key file")
    ap.add_argument("--csv", help="a local dtros_all.csv; otherwise fetched")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "tro"))
    ap.add_argument("--index", default=os.path.join(ROOT, "tro", "index.json"),
                    help="the committed record of what was published")
    ap.add_argument("--base", default="https://github.com/lpsd-1/"
                    "trailblazer-datasets/releases/download/tro/",
                    help="where the sealed pack is hosted")
    ap.add_argument("--today", help="override the expiry date, for testing")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="publish even if the count has collapsed against the "
                         "last run - for a genuine change, never to get a red "
                         "build green")
    ap.add_argument("--check-order-types", action="store_true",
                    help="run one order of each spec 5.3 type through the "
                         "real build and print what each resolves to. No key, "
                         "no network, no corpus")
    ap.add_argument("--mutate", action="store_true",
                    help="with --check-order-types: misfile a width limit as "
                         "a road closure, to show the check can go red")
    args = ap.parse_args()

    if args.check_order_types:
        return check_order_types(mutate=args.mutate)

    # Checked here rather than by `required=True`, so --check-order-types can
    # run in a checkout with no secrets - which is what makes it a check the
    # tests can run and not just the publishing job.
    if not args.key:
        ap.error("--key is required to build the pack "
                 "(--check-order-types needs no key)")

    path = args.csv
    if not path:
        from dtro_fetch import download_corpus  # local import: needs .env
        path = download_corpus(os.path.join(args.out, "_corpus"))

    cut = corpus_date(path)
    # FILTERED AGAINST THE EXTRACT'S OWN DATE, not today's.
    #
    # `corpus_date` promises two lines up that "the pack is then a function of
    # the data alone, so rebuilding an unchanged extract produces identical
    # bytes and riders do not re-download a file that has not changed" — and
    # passing today's date here broke that promise every midnight. The job runs
    # four times a day against an extract DfT re-cuts every few days, so a
    # rider was handed a fresh 3.7 MB pack each morning because a handful of
    # orders had rolled past their end date, not because anything new had been
    # published.
    #
    # The orders that expire between the cut and the rider looking are handled
    # where they should be: the app filters `inForceOn(today)` before it draws
    # anything, so a lapsed order is carried and not shown. Deciding that here
    # would mean deciding it once, on a build server, for a file somebody opens
    # a fortnight later.
    day = args.today or cut
    print("corpus  %s (cut %s, filtered against %s)"
          % (os.path.basename(path), cut, day))

    built, records, skipped = read_corpus(path, day)
    print("records %d, unreadable %d" % (records, skipped))
    print("live restrictions %d" % len(built))
    if not built:
        sys.exit("Nothing to publish. Refusing to write an empty pack: an "
                 "empty TRO pack is indistinguishable from 'no orders in "
                 "force anywhere', which is never true.")

    # A FLOOR, NOT JUST A ZERO CHECK.
    #
    # The line above catches the one case that cannot happen quietly. What can
    # happen quietly is a pack with four hundred restrictions in it instead of
    # thirty-four thousand: a truncated extract, a schema change that makes
    # `parse_wkt` drop a geometry shape, an authority's rows failing to parse.
    # Every one of those publishes cleanly and shows almost every closed road
    # in Great Britain as open, and nothing in the pipeline could tell that
    # pack from a genuinely quiet day.
    #
    # Two thirds of what was published last time, because the real count moves
    # with the ninety-day horizon and with how much each authority has filed —
    # day to day that is a few per cent. A third of the country disappearing
    # between two cuts is not a quiet day, it is a broken read.
    previous = previous_count(args.index)
    if previous and len(built) < previous * 2 // 3 and not args.allow_shrink:
        sys.exit(
            "REFUSING TO PUBLISH: %d restrictions, against %d last time.\n"
            "That is not a quiet day, it is a bad read - a truncated "
            "extract, or geometry this build no longer understands.\n"
            "%d of %d records were unreadable.\n"
            "Pass --allow-shrink if the drop is genuine."
            % (len(built), previous, skipped, records))

    # Unreadable rows are a signal in their own right: the corpus is machine
    # written, so a sudden crop of them means the shape changed under us.
    if records and skipped > records // 10 and not args.allow_shrink:
        sys.exit("REFUSING TO PUBLISH: %d of %d records could not be read. "
                 "The extract's shape has probably changed."
                 % (skipped, records))

    # Sorted, so the file is a function of its contents and not of the order
    # the service happened to return them in.
    built.sort(key=lambda f: f["properties"]["tro_uid"])

    collection = {
        "type": "FeatureCollection",
        "generated": cut,
        "package": "tro",
        "label": "Traffic regulation orders - Great Britain",
        "note": "Orders published to the DfT D-TRO service. Not every "
                "authority publishes, so blank ground means nothing has been "
                "published there - not that nothing is in force. Follow the "
                "signs on the road.",
        "attribution": "Contains public sector information licensed under the "
                       "Open Government Licence v3.0. Source: Department for "
                       "Transport D-TRO service.",
        # THE RESOLUTION TABLE TRAVELS WITH THE PACK.
        #
        # Every feature carries `otype`, and this says what each one means for
        # each vehicle. Shipping the table beside the data rather than baking
        # it into the app is what stops the two versions of §5.3 — the one the
        # builder applied and the one the app believes — from ever disagreeing:
        # an app reading a pack built before a type existed sees the type it
        # was told about, not a type it has to guess at.
        #
        # It costs about a kilobyte, once, against 36,000 features.
        "order_types": ORDER_TYPES,
        "order_forms": ORDER_FORMS,
        "features": built,
    }

    body = json.dumps(collection, separators=(",", ":")).encode("utf8")
    sealed = pack(body, load_key(args.key))

    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, "gb-tro.tbpack")
    with open(out, "wb") as handle:
        handle.write(sealed)
    digest = hashlib.sha256(sealed).hexdigest()
    with open(out + ".sha256", "w", encoding="utf-8") as handle:
        handle.write(digest + "\n")

    # A COMMITTED INDEX, and the sealed pack published as a release asset.
    #
    # Both halves of that matter. The pack is three megabytes and is rebuilt
    # several times a day, so committing the binary would add a gigabyte a year
    # to a repository riders clone nothing from — releases carry it instead,
    # exactly as the mirrored routing tiles are carried.
    #
    # But the catalogue is rebuilt FROM SCRATCH by whichever job runs, and a
    # flag pointing at a file that job does not have is a flag it silently
    # skips — which deletes this pack from every rider's Downloads screen with
    # no code having changed. That has happened three times in this repository
    # to other kinds; see tools/rebuild_catalogue.sh. So the size and hash live
    # in a small file that IS committed, the way satellite and height already
    # do it, and every job can read it whether or not it built the pack.
    index = {
        "generated": cut,
        "packs": [{
            "id": "gb-tro",
            "kind": "tro",
            "label": "Traffic orders",
            "file": args.base.rstrip("/") + "/" + os.path.basename(out),
            "sha256": digest,
            "bytes": len(sealed),
            "generated": cut,
            # What the next run's floor check compares against. Not read by
            # the app; the catalogue builder ignores fields it does not know.
            "features": len(built),
        }],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.index)), exist_ok=True)
    with open(args.index, "w", encoding="utf-8") as handle:
        json.dump(index, handle, indent=2, sort_keys=True)
        handle.write(chr(10))

    print("wrote %s" % out)
    print("  %.2f MB plain, %.2f MB sealed"
          % (len(body) / 1e6, len(sealed) / 1e6))
    print("  sha256 %s" % digest)
    print("wrote %s" % args.index)
    return 0


if __name__ == "__main__":
    sys.exit(main())

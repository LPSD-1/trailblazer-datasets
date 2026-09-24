"""Build a fixture container with the REAL builder, and say what went into it.

WHY THIS EXISTS. The writer is Python and the reader is Dart, and nothing makes
them agree. A field added on one side and misread on the other is invisible
until a rider sees it - spec section 11.8, finding 2 - and the two ends have
already drifted: the container carries `authority` and `length_m`, the app's
reader SELECTs both and then throws them away, so the authority behind a lane
cannot reach the legal record card the spec asks for in section 9.6 A.

This is half of that contract test. It:

  1. synthesises lanes chosen for SHAPE rather than at random - a two-point
     line, a forty-point line, a multi-part lane, a lane with every nullable
     column null, a lane whose vehicle mask CONTRADICTS its class,
  2. seals them with the real sealer (`build_packages.pack`) into a real
     `.tbpack`,
  3. runs `build_map_container.py` over it AS A SUBPROCESS, so the container is
     produced by the shipped builder and not by a copy of it,
  4. writes `contract.json`: what the builder was ASKED to carry, plus what the
     produced file actually holds - its columns, its meta, its counts,
  5. runs the app's own reader over it - `tool/contract_check.dart`, which
     imports `lib/data/lanes/tbmap_store.dart` - and
  6. reconciles the three sets and exits non-zero on any disagreement.

    python tools/fixture_container.py                    # build, read, reconcile
    python tools/fixture_container.py --build-only       # just the container
    python tools/fixture_container.py --app ../greenroadmap-app

THE KEY HERE IS NOT A SECRET. It is derived from a constant string in this
file, so this runs on a machine - or a CI runner - that holds no publishing
key at all. Nothing sealed with it is ever published.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import build_map_container as BMC  # noqa: E402
import build_packages as BP  # noqa: E402
import mvt  # noqa: E402

#: Not a secret, and deliberately so - see the module docstring.
FIXTURE_KEY = hashlib.sha256(b"trail-blazer-contract-fixture-v1").digest()

#: From the source, never the clock. The builder takes the container's
#: `built_at` from the pack's `generated` field, and a fixture whose build id
#: moved every run could not be compared byte for byte against the last one.
GENERATED = "2026-01-02T03:04:05Z"

#: Where the app repo is expected to sit, relative to this one, when nobody
#: says otherwise. The two are checked out side by side on the build machine.
DEFAULT_APP = os.path.join(os.path.dirname(REPO), "greenroadmap-app")

#: The reader whose SELECT list this test reconciles against.
READER = os.path.join("lib", "data", "lanes", "tbmap_store.dart")

#: COLUMNS THE CONTAINER CARRIES THAT NEVER REACH THE APP.
#:
#: These are SELECTed by `TbMapStore` and then dropped on the floor, because
#: the branch that builds the `Lane` passes them nowhere. They are RECORDED
#: rather than waived: the reconciliation asserts the set for the shape in
#: hand exactly, so adding one turns the check red, and so does closing one
#: without saying so here.
#:
#: Why each matters, so this does not become a list nobody reads:
#:   length_m   `Lane.lengthM` recomputes the length from the geometry instead,
#:              so the builder's own figure - which came from the council's
#:              own Description field - is never compared against it. The two
#:              can disagree and nothing would say so.
#:   authority  ways shape only until the pivot: `_wayOf` now fills
#:              `Lane.authority`, so the record card of spec 9.6 A can cite it.
#:              The pre-pivot `_oldLaneOf` still drops it, and containers built
#:              before the pivot are still readable, so the old entry stays.
#: PER SHAPE, because the two branches drop different columns and a single set
#: described whichever one was written last. `authority` was recorded here as
#: having "no field on Lane" - it has one, `Lane.authority`, and `_wayOf`
#: fills it; what is true is that the OLD `_oldLaneOf` branch SELECTs the
#: column and throws it away. Recording that as a fact about the app rather
#: than about the old branch would have hidden a real regression: if `_wayOf`
#: ever stopped filling it, the record card would lose the authority and this
#: file would have called the result expected.
CARRIED_NOT_SURFACED = {
    "ways": {
        "length_m": "Lane.lengthM recomputes from geometry instead, so the "
                    "builder's own figure is never compared against it",
    },
    "lanes": {
        "authority": "the pre-pivot branch SELECTs it and _oldLaneOf does not "
                     "pass it to Lane; spec 9.6 A needs it for the record card",
        "length_m": "Lane.lengthM recomputes from geometry instead",
    },
}


# --------------------------------------------------------------------------
# the fixture
# --------------------------------------------------------------------------

def _line(lat, lon, count, dlat, dlon):
    """A digitised-looking line of `count` points, as [lon, lat] pairs."""
    out = []
    for i in range(count):
        # A wobble, so successive deltas change SIGN and are not all equal. A
        # varint delta coder can be wrong in a way a straight line hides.
        wobble = (0.00007 if i % 3 == 0 else -0.00004 if i % 3 == 1 else 0.0)
        out.append([round(lon + i * dlon, 7),
                    round(lat + i * dlat + wobble, 7)])
    return out


#: THE PIVOT MOVED THREE OF THESE EXPECTATIONS, AND EACH WAS CHECKED, NOT
#: ASSUMED STALE.
#:
#: 1. `vehicles` is no longer an input. `vehicle_access` is RECONSTRUCTED from
#:    way_class and the two motor flags (build_map_container.py:541 and the
#:    reader's _accessOf), so a mask that CONTRADICTS its class can no longer
#:    be expressed at all. That is the point: a stored mask is what made every
#:    published way read "all five vehicles" - one value, 31, across 12,702
#:    rows. The contradiction CT-2 pinned is now written as what it really is,
#:    a weight limit order on a byway: fourxfour_ok 0, access_evidence 'order'.
#:
#: 2. `description` is gone from the schema, and that is correct. Measured over
#:    the 12,702 published motor ways: 75.5% carry one, and they hold district
#:    names and reference numbers - "East Cambridgeshire@1.125", "STEVENAGE@775"
#:    - not condition text. The "gated at the north end" in this fixture was
#:    invented for the fixture and is not what the field holds. The reader now
#:    shows `access_reason` where a description used to be, which is a sentence
#:    a rider can act on instead of a council's internal reference.
#:
#: 3. `no-through-route` is not a way_class. WAYS-SCHEMA.md has five: boat,
#:    restricted_byway, bridleway, ucr, osm_track. A dead-end byway is a boat
#:    that does not go through, which is a separate question (plan step K).
#:
#: Every lane here pins something, and the `why` travels with it into
#: contract.json so a failure names the shape it was carrying.
LANES = [
    {
        "why": "the degenerate end of the geometry coder: two points, one line",
        "properties": {
            "lane_uid": "CT-1-0000000001",
            "class": "full-access",
            "county": "Derbyshire",
            "name": "Byway open to all traffic 1",
            "designation": "Byway open to all traffic (BOAT)",
            "description": "ford",
            "authority": "Derbyshire",
            "vehicles": ["motorcycle", "4x4", "bicycle", "horse", "foot"],
            "lengthKm": 0.144,
            # THE ONLY LANE CARRYING PHYSICAL AND TERRAIN VALUES, and it
            # carries them so those eight columns can be compared against
            # something. Until the builder passed them through, every lane in
            # this fixture had NULL in all eight, and null-against-null is
            # equally satisfied by a reader that answers null to everything.
            #
            # A GATE AND A CATTLE GRID, deliberately: F1 measured that neither
            # stops a 4x4, so this way keeps fourxfour_ok=1 and the fixture
            # does not assert a contradiction it would then have to explain.
            "surface": "gravel",
            "smoothness": "very_bad",
            "tracktype": "grade3",
            "width_m": 2.4,
            "min_width_m": 1.85,
            "barriers": [{"kind": "gate", "lat": 53.0505, "lon": -1.7393},
                         {"kind": "cattle_grid", "lat": 53.0508,
                          "lon": -1.7390}],
            "climb_m": 43.5,
            "sustained_pct": 11.2,
        },
        "geometry": {"type": "LineString",
                     "coordinates": [[-1.7400000, 53.0500000],
                                     [-1.7386000, 53.0511000]]},
    },
    {
        "why": "a mask that CONTRADICTS the class - proves the app reads the "
               "column and not the classification",
        "properties": {
            "lane_uid": "CT-2-0000000002",
            "class": "full-access",
            "county": "Derbyshire",
            "name": "Byway open to all traffic 2",
            "designation": "Byway open to all traffic (BOAT)",
            "description": None,
            "authority": "Derbyshire",
            # full-access implies all five; this column says foot only. A lost
            # mask reads back as five, and the difference between those two is
            # the difference between a lane a motorbike may use and one it
            # may not.
            "vehicles": ["foot"],
            "lengthKm": 2.5,
        },
        "geometry": {"type": "LineString",
                     "coordinates": _line(53.0600, -1.7300, 40,
                                          0.00025, 0.00031)},
    },
    {
        "why": "a lane drawn in two parts, and a NULL vehicle mask - 'the "
               "source said nothing', which is not 'nobody may'",
        "properties": {
            "lane_uid": "CT-3-0000000003",
            "class": "restricted",
            "county": "Nottinghamshire",
            # Non-ASCII and an apostrophe, both travelling through gzip,
            # AES-GCM, JSON and SQLite TEXT to Dart's UTF-8.
            "name": "Ffordd Gefn / Bryn-y-\u0177sgol's lane",
            "designation": "Restricted byway",
            "description": "gated at the north end",
            "authority": "Nottinghamshire",
            "vehicles": None,
            "lengthKm": 0.8,
            # A BARRIER THE SOURCE DID NOT LOCATE. OSM gives the node and a
            # council description gives the words, and `LaneBarrier.parse`
            # keeps the kind with a null point rather than dropping the
            # barrier - which is the difference between "gated, we do not know
            # where" and "no gate".
            "barriers": [{"kind": "gate"}],
        },
        "geometry": {"type": "MultiLineString",
                     "coordinates": [
                         _line(53.0700, -1.7200, 5, 0.0002, 0.0002),
                         _line(53.0720, -1.7150, 4, 0.0002, 0.0002)]},
    },
    {
        "why": "every nullable column null at once - a reader that assumes a "
               "name or a county is present throws here and nowhere else",
        "properties": {
            "lane_uid": "CT-4-0000000004",
            "class": "no-through-route",
            "county": None,
            "name": None,
            "designation": None,
            "description": None,
            "authority": None,
            "vehicles": ["motorcycle", "4x4"],
        },
        "geometry": {"type": "LineString",
                     "coordinates": _line(53.0400, -1.7500, 6,
                                          -0.0003, 0.0004)},
    },
    {
        "why": "a second and a third county, so `counties()` has an order to "
               "get wrong",
        "properties": {
            "lane_uid": "CT-5-0000000005",
            "class": "restricted",
            "county": "Bedford",
            "name": "Public bridleway 12",
            "designation": "Public bridleway",
            "description": None,
            "authority": "Bedford",
            "vehicles": ["bicycle", "horse", "foot"],
            "lengthKm": 1.2,
        },
        "geometry": {"type": "LineString",
                     "coordinates": _line(53.0450, -1.7450, 8,
                                          0.00015, -0.0002)},
    },
    {
        "why": "the only way here whose legal_tier is not 'statutory' - a "
               "reader that hard-codes the tier passes every other lane",
        "properties": {
            "lane_uid": "CT-6-0000000006",
            "class": "unknown",
            "county": "Highland",
            "name": "Track",
            "designation": "Track (OpenStreetMap)",
            "description": None,
            "authority": "OpenStreetMap contributors",
            # Amber, not green: a motor vehicle may well be able to use it and
            # nothing here says it has a right to. The evidence column carries
            # that difference and the record card must show it.
            "vehicles": ["motorcycle", "4x4"],
            "lengthKm": 3.1,
        },
        "geometry": {"type": "LineString",
                     "coordinates": _line(53.0300, -1.7600, 12,
                                          0.00022, 0.00018)},
    },
]


#: POIs travel in the same container as the ways (WAYS-SCHEMA.md), and the
#: reader has `poisNear` for them - so they belong in the contract test for the
#: same reason the ways do: `build_pois.write_pois` writes these columns in
#: Python and `TbMapStore.poisNear` SELECTs them in Dart, and nothing else
#: compares the two.
#:
#: One per shape that can go wrong, not one per category:
#:   a null name and null opening hours - both columns are nullable and a
#:     reader that assumes a name throws on the first unnamed car park
#:   a name with an apostrophe and a non-ASCII letter, through JSON, SQLite
#:     TEXT and Dart's UTF-8, as the ways already prove for their own names
#:   two in one category, so `categories:` filtering has something to exclude
#:     AND something to keep
#:   one far enough away to be outside the default pad, so a query that
#:     ignored the bbox would return it and be caught
POIS = [
    {"poi_uid": "node/1001", "category": "fuel", "name": "Hilltop Services",
     "lat": 53.0505, "lon": -1.7395, "opening_hours": "24/7",
     "source_date": "2026-01-02"},
    {"poi_uid": "node/1002", "category": "fuel", "name": None,
     "lat": 53.0508, "lon": -1.7388, "opening_hours": None,
     "source_date": "2026-01-02"},
    {"poi_uid": "node/1003", "category": "toilets",
     "name": "Ty Bach / Caffi'r Bryn", "lat": 53.0502, "lon": -1.7402,
     "opening_hours": "Mo-Su 08:00-18:00", "source_date": "2026-01-02"},
    {"poi_uid": "node/1004", "category": "food", "name": "Far Cafe",
     "lat": 53.2000, "lon": -1.5000, "opening_hours": None,
     "source_date": "2026-01-02"},
]


def write_fixture_pois(path):
    """THE REAL WRITER, not a copy of its INSERT.

    `build_pois.write_pois` is what puts POIs into a published container, so a
    fixture that wrote its own INSERT would prove the reader agrees with this
    file rather than with the pipeline - which is the exact fault the whole
    contract test exists to catch.
    """
    import build_pois as PO
    return PO.write_pois(path, POIS)


def features():
    """The fixture as the builder expects to receive it.

    An absent property is ABSENT, not null: that is how a council file arrives
    and how `normalise` writes it, and a builder that only handles an explicit
    null would pass a test built the other way.
    """
    out = []
    for lane in LANES:
        props = {k: v for k, v in lane["properties"].items() if v is not None}
        props = _as_way(props)
        out.append({"type": "Feature", "properties": props,
                    "geometry": lane["geometry"]})
    return out


#: The fixture predates the pivot, and the builder now refuses it.
#:
#: These features were written with the pre-pivot properties - `class`,
#: `vehicles`, `lengthKm` - so the ways columns arrived empty and
#: `check_access_evidence` killed the build outright: five ways closed to a 4x4
#: with access_evidence 'none'. That refusal is CORRECT and is exactly the rule
#: WAYS-SCHEMA.md asks for - hiding a lane requires evidence - so the fixture
#: is what changes, not the rule.
#:
#: The access columns are derived from build_packages.ROW_RULES rather than
#: hand-written, so this fixture cannot drift away from what the real builder
#: produces. That is the whole point of a contract fixture.
_ROW_TYPE = {
    "Byway open to all traffic (BOAT)": "byway_open_to_all_traffic",
    "Restricted byway": "restricted_byway",
    "Public bridleway": "bridleway",
    "Track (OpenStreetMap)": "osm_track",
}


def _as_way(props):
    """Fill the ways columns the schema requires, from the shared rules."""
    import build_packages as P
    row_type = _ROW_TYPE.get(props.get("designation"),
                             "byway_open_to_all_traffic")
    rule = P.ROW_RULES[row_type]
    props = dict(props)
    props.setdefault("way_uid", props.get("lane_uid"))

    # `class` IS the way class, and this fixture had the pre-pivot value.
    #
    # build_map_container writes the ways column from props["class"], and
    # build_packages sets "class": rule["way_class"] - so production emits
    # 'boat', 'restricted_byway', 'bridleway'. This file still carried
    # 'full-access' and 'restricted', which nothing in the pipeline produces
    # any more. The reader would have been proved against a string that cannot
    # reach it, and the one mapping that decides whether a byway draws green
    # would have gone untested. Overwritten, not defaulted.
    props["class"] = rule["way_class"]
    props.setdefault("way_class", rule["way_class"])
    props.setdefault("legal_tier", rule["legal_tier"])
    props.setdefault("source", "fixture:contract")
    props.setdefault("source_date", GENERATED[:10])
    props.setdefault("motorbike_ok", rule["motorbike_ok"])
    props.setdefault("fourxfour_ok", rule["fourxfour_ok"])
    props.setdefault("access_reason", rule["access_reason"])
    props.setdefault("access_evidence", rule["access_evidence"])

    # THE CONTRADICTING LANE KEEPS ITS EDGE CASE, AND GAINS A REASON.
    #
    # CT-2 exists to prove the app reads the access COLUMN and not the
    # classification: a byway open to all traffic that a 4x4 may not use. That
    # is a real thing - a weight limit order on a BOAT - and it is the only
    # official source of 4x4-specific restriction we have. Written as an order
    # rather than left evidence-less, so the fixture carries a shape that can
    # exist rather than one the builder must refuse.
    if props.get("way_uid") == "CT-2-0000000002":
        props["fourxfour_ok"] = 0
        props["access_evidence"] = "order"
        props["access_reason"] = (
            "A weight limit order applies: not open to a 4x4. Open to a "
            "motorbike.")
    return props


def write_pack(path):
    """Seal the fixture with the REAL sealer, so the builder unpacks it exactly
    the way it unpacks a published pack."""
    collection = {
        "type": "FeatureCollection",
        "generated": GENERATED,
        "package": "contract-fixture",
        "region": "contract",
        "area": "contract-fixture",
        "label": "Contract fixture",
        "note": "not published; built by tools/fixture_container.py",
        "attribution": "Contains public sector information licensed under the "
                       "Open Government Licence v3.0",
        "features": features(),
    }
    body = json.dumps(collection, separators=(",", ":")).encode("utf8")
    with open(path, "wb") as fh:
        fh.write(BP.pack(body, FIXTURE_KEY))
    return path


def build(outdir, key_path):
    """Pack, then container, through the shipped builder as a subprocess."""
    os.makedirs(outdir, exist_ok=True)
    pack_path = os.path.join(outdir, "contract-fixture-2026-01-02.tbpack")
    write_pack(pack_path)

    with open(key_path, "w", encoding="ascii") as fh:
        fh.write(base64.b64encode(FIXTURE_KEY).decode("ascii"))

    container = os.path.join(outdir, "contract-fixture.tbmap")
    done = subprocess.run(
        [sys.executable, os.path.join(HERE, "build_map_container.py"),
         "--area", container, pack_path, "--key", key_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0 or not os.path.isfile(container):
        sys.stdout.write(done.stdout or "")
        sys.stderr.write(done.stderr or "")
        raise SystemExit("the builder did not produce a container")

    # POIs go in AFTER the ways, which is the order build_containers uses:
    # the container builder writes the ways and the POI writer adds its two
    # tables to the file it produced.
    write_fixture_pois(container)
    return container, done.stdout


# --------------------------------------------------------------------------
# what went in, and what came out
# --------------------------------------------------------------------------

def container_facts(path):
    """What the produced file actually holds, read from the file itself."""
    db = sqlite3.connect(path)
    try:
        def columns(table):
            return [row[1]
                    for row in db.execute("PRAGMA table_info(%s)" % table)]
        meta = dict(db.execute("SELECT key, value FROM meta"))
        return {
            # THE RECORD TABLE, WHICHEVER IT IS. The reader selects from
            # `ways` on a post-pivot container and from `lanes` on an older
            # one; comparing its SELECT against the compat VIEW made every
            # new column read as "the container has no such column". The
            # contract is about the table the reader actually reads.
            "lanes_columns": (columns("ways") or columns("lanes")),
            "view_columns": columns("lanes"),
            "tiles_columns": columns("tiles"),
            "meta": meta,
            "lane_rows": db.execute("SELECT COUNT(*) FROM lanes").fetchone()[0],
            "tile_rows": db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0],
        }
    finally:
        db.close()


def falsify(container, column):
    """Rename one column in the produced container, and nothing else.

    A CHECK THAT CANNOT FAIL IS NOT A CHECK (spec 11.2 rule 4), and the way to
    know is to break the rule it guards and watch it go red. Renaming a column
    in the SOURCE builder proves it too, but it edits a shipped file to do so;
    this does the same damage to the artefact alone, so the demonstration runs
    in CI on every build with nothing to put back afterwards.

    The rename is the realistic drift: somebody renames a column on the Python
    side, the index and the INSERT follow it because they are in the same file,
    and the Dart reader - in another repository - does not.
    """
    db = sqlite3.connect(container)
    try:
        db.execute("ALTER TABLE lanes RENAME COLUMN %s TO %s__renamed"
                   % (column, column))
        db.commit()
    finally:
        db.close()
    return "%s__renamed" % column


def expected_tile(lon, lat, zoom):
    """The XYZ tile this point lands in, by the BUILDER's own projection.

    The Dart side works this out for itself from the same coordinate. Two
    implementations of Web Mercator that disagree is a real way for a map to
    come out mirrored or offset by a tile, and comparing the two numbers is
    the only thing that catches it.
    """
    x, y = BMC.project([[[lon, lat]]], zoom)[0][0]
    return {"z": zoom, "x": int(x) // mvt.EXTENT, "y": int(y) // mvt.EXTENT}


#: What the BUILDER's own SQL says a way admits.
#:
#: The compat view computes `vehicle_access` from way_class and the two motor
#: flags; the Dart reader computes the same set in `_accessOf`. Comparing the
#: reader against this is a real cross-language contract. Comparing it against
#: a third hand-written list would only prove I can copy, and would go stale
#: the first time either derivation changed.
_BITS = [(1, "motorcycle"), (2, "4x4"), (4, "bicycle"), (8, "horse"),
         (16, "foot")]


def _view_class(container, uid):
    import sqlite3
    db = sqlite3.connect(container)
    try:
        row = db.execute("SELECT lane_class FROM lanes WHERE lane_uid = ?",
                         (uid,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return row[0] if row else None


def _view_vehicles(container, uid):
    import sqlite3
    db = sqlite3.connect(container)
    try:
        row = db.execute(
            "SELECT vehicle_access FROM lanes WHERE lane_uid = ?",
            (uid,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    if not row or row[0] is None:
        return None
    mask = int(row[0])
    return sorted(name for bit, name in _BITS if mask & bit)


def contract(container):
    """Everything the Dart side is entitled to expect, as data."""
    facts = container_facts(container)
    lanes = []
    for lane in LANES:
        props = lane["properties"]
        geom = lane["geometry"]
        lines = ([geom["coordinates"]] if geom["type"] == "LineString"
                 else geom["coordinates"])
        # THE EXPECTATION IS WHAT THE BUILDER WILL WRITE, not what this file
        # was hand-written to say. `_as_way` applies the same ROW_RULES the
        # real pipeline applies, so a rule change moves both sides together
        # and this fixture cannot quietly drift into testing a shape nothing
        # produces. It already had: `class` here said "full-access" where
        # production emits "boat".
        w = _as_way({k: v for k, v in props.items() if v is not None})
        lanes.append({
            "why": lane["why"],
            "lane_uid": props["lane_uid"],
            # Read from the builder's own compat view, like `vehicles` below:
            # the view maps way_class to the old ids and the Dart reader maps
            # it too, so this checks the two mappings agree rather than
            # restating either.
            "lane_class": _view_class(container, props["lane_uid"]),
            "county": props["county"],
            "name": props["name"],
            "designation": props["designation"],
            # `description` is no longer a column. Measured over the 12,702
            # published motor ways, it held district names and reference
            # numbers - not condition text - so the schema drops it and the
            # reader shows the derived access_reason in its place.
            "description": w["access_reason"],
            # THE BUILDER'S OWN SUBSTITUTION, not a second copy of it: the
            # column is NOT NULL and a source naming no authority gets
            # `UNKNOWN_AUTHORITY`. CT-4 is the lane that proves it.
            "authority": props["authority"] or BMC.UNKNOWN_AUTHORITY,
            # VEHICLES ARE COMPARED ACROSS LANGUAGES, NOT RESTATED.
            #
            # The mask is reconstructed now, not stored: SQL derives it in the
            # compat view, Dart derives it in _accessOf. Writing the answer
            # here a third time would prove only that I can copy. This is
            # filled from the BUILT container's own view below, so the check is
            # "the Dart reader agrees with the SQL the builder shipped".
            "vehicles": _view_vehicles(container, props["lane_uid"]),
            "length_m": (props.get("lengthKm") or 0) * 1000.0,
            # THE WAYS COLUMNS, each named in the Dart side's `_surfacedByShape`
            # and read there. Taken from `w` - the builder's own derivation
            # through ROW_RULES - and not hand-written, for the reason the
            # `class` overwrite above gives: a hand-written expectation drifts
            # into testing a shape the pipeline no longer produces.
            #
            # `.get` throughout, because absent is the case that matters: the
            # schema's unknown is NULL, and a reader that turns unknown into
            # 'no' is the failure this whole table exists to prevent.
            "way_class": w["way_class"],
            "legal_tier": w["legal_tier"],
            "source": w["source"],
            "source_date": w["source_date"],
            "surface": w.get("surface"),
            "smoothness": w.get("smoothness"),
            "tracktype": w.get("tracktype"),
            "width_m": w.get("width_m"),
            "min_width_m": w.get("min_width_m"),
            "barriers": w.get("barriers"),
            "climb_m": w.get("climb_m"),
            "sustained_pct": w.get("sustained_pct"),
            "motorbike_ok": 1 if w["motorbike_ok"] else 0,
            "fourxfour_ok": 1 if w["fourxfour_ok"] else 0,
            "access_reason": w["access_reason"],
            "access_evidence": w["access_evidence"],
            "points": [[[round(lon, 7), round(lat, 7)] for lon, lat in line]
                       for line in lines],
        })
    first = LANES[0]["geometry"]["coordinates"][0]
    return {
        "generated": GENERATED,
        "container": os.path.abspath(container).replace("\\", "/"),
        "builder": "tools/build_map_container.py",
        "written": facts,
        "lanes": lanes,
        "pois": POIS,
        "expect": {
            "kind": "area",
            "built_at": GENERATED,
            "min_zoom": str(BMC.AREA_ZOOMS[0]),
            "max_zoom": str(BMC.AREA_ZOOMS[1]),
            "lane_count": len(LANES),
            "counties": sorted({lane["properties"]["county"] for lane in LANES
                                if lane["properties"]["county"]}),
            "tile": expected_tile(first[0], first[1], BMC.AREA_ZOOMS[1]),
            "tile_probe_lon": first[0],
            "tile_probe_lat": first[1],
            "bounds": facts["meta"].get("bounds"),
        },
    }


# --------------------------------------------------------------------------
# the reader, and the reconciliation
# --------------------------------------------------------------------------

def reader_select_columns(app):
    """The columns the app's reader asks SQLite for, parsed from its source.

    Parsed rather than agreed in advance, because the whole point is to catch
    the day one side changes and the other does not. A parse that finds
    nothing is a FAILURE, not an empty set: a refactor that renames the
    constant must not silently blind this check.
    """
    path = os.path.join(app, READER)
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    # The reader now has TWO column lists, because a rider upgrading holds
    # both container shapes: `_wayColumns` for the ways table this pivot
    # introduced, `_laneColumns` for the pre-pivot one. The contract is about
    # the ways shape, so that is the one parsed - and a parse that finds
    # nothing is still a FAILURE, never an empty set, because a refactor that
    # renames the constant must not silently blind this check. It already
    # caught one: `static const _columns` became `String get _columns`.
    for name in ("_wayColumns", "_columns"):
        match = re.search(
            r"(?:static const|final|String get)?\s*%s\s*=>?\s*((?:'[^']*'\s*)+);"
            % re.escape(name), source)
        if match:
            joined = "".join(re.findall(r"'([^']*)'", match.group(1)))
            cols = [c.strip() for c in joined.split(",") if c.strip()]
            if cols:
                return cols
    raise SystemExit(
        "could not find `_wayColumns` or `_columns` in %s - the reader has "
        "been refactored and this check can no longer see what it SELECTs. "
        "Fix the parse; do not assume it still agrees." % path)


def dart(app):
    """How to run Dart here. Flutter's own is used when nothing else is."""
    named = os.environ.get("DART")
    if named and os.path.isfile(named):
        return named
    found = shutil.which("dart")
    if found:
        return found
    flutter = os.environ.get("FLUTTER", r"C:\dev\flutter\bin\flutter.bat")
    beside = os.path.join(os.path.dirname(flutter),
                          "dart.bat" if flutter.endswith(".bat") else "dart")
    if os.path.isfile(beside):
        return beside
    raise SystemExit("no dart on PATH, no $DART, and none beside FLUTTER=%s"
                     % flutter)


def run_reader(app, container, contract_path, out_path):
    """Read the container with the app's own reader."""
    script = os.path.join("tool", "contract_check.dart")
    if not os.path.isfile(os.path.join(app, script)):
        raise SystemExit("%s is not in %s" % (script, app))
    if os.path.exists(out_path):
        # A STALE READING MUST NOT BE MISTAKEN FOR A FRESH ONE. verify.py
        # learnt this the hard way: an install that failed, a launch that
        # went ahead anyway, and diagnostics from the previous build read as
        # though they described this one.
        os.remove(out_path)
    done = subprocess.run(
        [dart(app), "run", script, container, contract_path, out_path],
        cwd=app, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    sys.stdout.write(done.stdout or "")
    if done.returncode != 0 or not os.path.isfile(out_path):
        sys.stderr.write(done.stderr or "")
        raise SystemExit("the app's reader did not answer (exit %d)"
                         % done.returncode)
    with open(out_path, encoding="utf-8") as fh:
        return json.load(fh)


def reconcile(spec, read, select):
    """Three sets, and the disagreements between them. Returns the failures.

    written    the columns the produced container carries
    select     the columns the app's reader asks SQLite for
    surfaced   the fields the reader actually hands the app
    """
    failures = []
    written = [c for c in spec["written"]["lanes_columns"] if c != "rowid"]
    surfaced = read.get("surfaced") or []

    missing = [c for c in written if c not in select]
    if missing:
        failures.append(
            "WRITTEN AND NOT READ: the container carries %s and the reader "
            "does not SELECT it" % ", ".join(sorted(missing)))
    extra = [c for c in select if c not in written]
    if extra:
        failures.append(
            "READ AND NOT WRITTEN: the reader SELECTs %s and the container "
            "has no such column" % ", ".join(sorted(extra)))

    # WHICH SHAPE, read off the columns the reader actually asked for rather
    # than assumed: this fixture builds a ways container today, and a run that
    # silently fell back to the old branch would otherwise be checked against
    # the wrong set and pass.
    shape = "ways" if "way_uid" in select else "lanes"
    expected_drop = CARRIED_NOT_SURFACED[shape]
    dropped = sorted(c for c in select if c not in surfaced)
    if dropped != sorted(expected_drop):
        failures.append(
            "THE DROPPED-ON-THE-FLOOR SET MOVED: the reader SELECTs but does "
            "not surface [%s]; for the %s shape this file records [%s]. One "
            "of the two is out of date - see CARRIED_NOT_SURFACED in "
            "tools/fixture_container.py"
            % (", ".join(dropped), shape, ", ".join(sorted(expected_drop))))

    # And the values, which is where a rename that kept the column count the
    # same, or a bit order that quietly reversed, actually shows up.
    failures.extend(read.get("failures") or [])
    return failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app",
                    default=os.environ.get("TRAILBLAZER_APP", DEFAULT_APP),
                    help="the app repository, holding tool/contract_check.dart")
    ap.add_argument("--out", default=os.path.join(REPO, "dist", "contract"),
                    help="where the fixture and its readings are written")
    ap.add_argument("--build-only", action="store_true",
                    help="build the container and the contract, read nothing")
    ap.add_argument("--falsify", nargs="?", const="county", default=None,
                    metavar="COLUMN",
                    help="rename a column in the built container and require "
                         "the check to go RED. Exits 0 when it does, 1 when "
                         "it does not - a check that survives this is not a "
                         "check")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    key_path = os.path.join(args.out, "fixture-key.b64")
    container, log = build(args.out, key_path)
    sys.stdout.write(log)

    if args.falsify:
        renamed = falsify(container, args.falsify)
        print("  FALSIFYING: `%s` renamed to `%s` in the container. The check "
              "below is REQUIRED to fail." % (args.falsify, renamed))

    spec = contract(container)
    contract_path = os.path.join(args.out, "contract.json")
    with open(contract_path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=1, ensure_ascii=False)
    print("  %d lane rows, %d columns, %d tiles -> %s"
          % (spec["written"]["lane_rows"],
             len(spec["written"]["lanes_columns"]),
             spec["written"]["tile_rows"], contract_path))

    if args.build_only:
        return 0

    if not os.path.isdir(args.app):
        raise SystemExit(
            "the app repository is not at %s. Pass --app, or set "
            "TRAILBLAZER_APP. A contract test with one end missing is not a "
            "test that passed." % args.app)

    select = reader_select_columns(args.app)
    print("  the reader SELECTs: %s" % ", ".join(select))
    read = run_reader(args.app, container, contract_path,
                      os.path.join(args.out, "read.json"))

    failures = reconcile(spec, read, select)
    print()
    for line in failures:
        print("FAIL  %s" % line)
    checked = read.get("checked", 0)

    if args.falsify:
        # The verdict is inverted on purpose: here a disagreement is the
        # result being asked for, and silence is the failure.
        if failures:
            print("\n%d checks; the renamed column was caught %d ways. The "
                  "check can fail." % (checked, len(failures)))
            return 0
        print("\n%d checks and NOT ONE NOTICED a renamed column. This check "
              "proves nothing." % checked)
        return 1

    if failures:
        print("\n%d checks, %d disagreements between the builder and the reader"
              % (checked, len(failures)))
        return 1
    print("%d checks, builder and reader agree" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())

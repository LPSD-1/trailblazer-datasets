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
#: Both are SELECTed by `TbMapStore._columns` and then dropped on the floor by
#: `_laneOf`, because `Lane` has no field for either. They are RECORDED rather
#: than waived: the reconciliation asserts this set exactly, so adding a third
#: one turns the check red, and so does closing one of these without saying so
#: here.
#:
#: Why each matters, so this does not become a list nobody reads:
#:   authority  spec 9.6 A - the legal record card must cite the authority, and
#:              it cannot, because the authority stops at the SQL. `county`
#:              happens to carry the same string today (build_packages.py:440
#:              and :445 both write the authority name), which is why nobody
#:              has noticed.
#:   length_m   `Lane.lengthM` recomputes the length from the geometry instead,
#:              so the builder's own figure - which came from the council's
#:              own Description field - is never compared against it.
CARRIED_NOT_SURFACED = {
    "authority": "no field on Lane; spec 9.6 A needs it for the record card",
    "length_m": "Lane.lengthM recomputes from geometry instead",
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
]


def features():
    """The fixture as the builder expects to receive it.

    An absent property is ABSENT, not null: that is how a council file arrives
    and how `normalise` writes it, and a builder that only handles an explicit
    null would pass a test built the other way.
    """
    out = []
    for lane in LANES:
        props = {k: v for k, v in lane["properties"].items() if v is not None}
        out.append({"type": "Feature", "properties": props,
                    "geometry": lane["geometry"]})
    return out


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
            "lanes_columns": columns("lanes"),
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


def contract(container):
    """Everything the Dart side is entitled to expect, as data."""
    facts = container_facts(container)
    lanes = []
    for lane in LANES:
        props = lane["properties"]
        geom = lane["geometry"]
        lines = ([geom["coordinates"]] if geom["type"] == "LineString"
                 else geom["coordinates"])
        lanes.append({
            "why": lane["why"],
            "lane_uid": props["lane_uid"],
            "lane_class": props["class"],
            "county": props["county"],
            "name": props["name"],
            "designation": props["designation"],
            "description": props["description"],
            "authority": props["authority"],
            # What the app must come back with: the mask where there is one,
            # and null where the source said nothing - which the app is then
            # required to answer for from the CLASS instead.
            "vehicles": props["vehicles"],
            "length_m": (props.get("lengthKm") or 0) * 1000.0,
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
    match = re.search(r"static const _columns\s*=\s*((?:'[^']*'\s*)+);", source)
    if not match:
        raise SystemExit(
            "could not find `static const _columns` in %s - the reader has "
            "been refactored and this check can no longer see what it "
            "SELECTs. Fix the parse; do not assume it still agrees." % path)
    joined = "".join(re.findall(r"'([^']*)'", match.group(1)))
    return [c.strip() for c in joined.split(",") if c.strip()]


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

    dropped = sorted(c for c in select if c not in surfaced)
    if dropped != sorted(CARRIED_NOT_SURFACED):
        failures.append(
            "THE DROPPED-ON-THE-FLOOR SET MOVED: the reader SELECTs but does "
            "not surface [%s]; this file records [%s]. One of the two is out "
            "of date - see CARRIED_NOT_SURFACED in tools/fixture_container.py"
            % (", ".join(dropped), ", ".join(sorted(CARRIED_NOT_SURFACED))))

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

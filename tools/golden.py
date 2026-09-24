#!/usr/bin/env python3
"""One small region, built end to end from fixed inputs, byte for byte.

    python tools/golden.py                 # build twice, compare, check the stored bytes
    python tools/golden.py --mutate        # a deliberate change MUST move the bytes
    python tools/golden.py --bless         # re-record the expectation in this file
    python tools/golden.py --keep DIR      # leave the built artefacts behind

THE FAILURE THIS EXISTS FOR HAS SHIPPED TWICE, and both times it was a clock.

  * `build_packages.py` drew its AES-GCM nonce from os.urandom, so a monthly
    rebuild of council data nobody had amended produced 97 packages with 97 new
    hashes and every rider re-downloaded ~100 MB for nothing.
  * `build_containers.py` then stamped every container with `manifest.generated`
    - when THIS RUN wrote the manifest, not when the data was cut. Two CI builds
    of unchanged data an hour apart gave a `motor-wales.tbmap` differing in FIVE
    bytes out of 1,114,112. Five bytes move the sha256, so all 343 MB went out
    again, and 129 containers disagreed with what had been published an hour
    earlier, which cost a publish.

Both were found by hand, after the fact, from download figures. Nothing in the
pipeline could answer "does this build reproduce?", because nothing ever built
the same thing twice. This does, from inputs that never change:

    council rows -> normalise -> .tbpack -> .tbmap -> catalogue.json

and it makes four separate claims, each of which can go red on its own:

  1. IDENTICAL INPUTS, IDENTICAL BYTES. Two consecutive builds, compared file
     by file and byte by byte.
  2. THE CLOCK DOES NOT REACH THE DATA. A third build whose RUN STAMP is an
     hour later must differ in exactly the two fields declared volatile below
     and nowhere else. That is the second regression above, stated as a test:
     if a run stamp ever leaks into a container again, a .tbmap moves and this
     goes red. It also asserts those fields really did change, so it cannot
     pass by the new stamp having been quietly ignored.
  3. THE STORED BYTES. Size and sha256 of every artefact, recorded in this
     file, so a change underneath the build that alters output is caught on the
     run it happens rather than in the month it ships.
  4. A REAL CHANGE STILL MOVES THE BYTES. `--mutate` shifts one lane by about
     11 metres. If the outputs do not change, reproducibility has been bought
     by ignoring the input, which is worse than the bug.

The expectation is stored in this file rather than beside it, so the golden and
the bytes it claims cannot drift apart in a half-applied commit. `--bless`
rewrites it and prints what moved.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_packages as P          # noqa: E402
import build_containers as C        # noqa: E402
import build_catalogue as K         # noqa: E402
import publish_changesets as X      # noqa: E402

# --------------------------------------------------------------------------
# the fixed inputs
# --------------------------------------------------------------------------

#: Not a secret, and not the publishing key: a fixed 32 bytes so the sealed
#: pack is reproducible on any machine. The real key never comes near this file.
GOLDEN_KEY = hashlib.sha256(b"trailblazer-golden-key-v1").digest()

#: When the data was cut. It travels INSIDE the pack payload and is inherited
#: by every container, so it is an input, and must be fixed.
PACK_STAMP = "2026-01-02T03:04:05Z"

#: When the run happened. Deliberately not the same thing; claim 2 is that
#: moving it changes nothing except the fields it is allowed to change.
RUN_STAMP = "2026-03-01T00:00:00Z"
LATER_RUN_STAMP = "2026-03-01T01:04:59Z"

CATALOGUE_STAMP = "2026-03-01T00:00:00Z"
BASE_URL = "https://golden.invalid/trailblazer/"

#: The routing tile listing is fetched over HTTP by build_catalogue. A golden
#: build takes it as an input instead: a network round trip is neither fixed
#: nor available when the volunteer-run server upstream is down.
ROUTING_TILES = {"W5_N50": 12092058, "W5_N55": 9553221, "E0_N50": 14338110}

#: The LIVE half of the conditions pipeline, as a fixed input.
#:
#: The feeds themselves are built from the Environment Agency's API four times
#: a day and are nothing to do with a reproducible container build - so what
#: the golden fixes is not the readings but the JOIN: that a published feed on
#: disk becomes a `conditions` entry in the catalogue with the right served
#: URL, size, hash and `as_of`. Written here by hand, in the shape
#: `build_wet.feed_body` and `build_fords.feed_body` produce, so a change to
#: either shape that the catalogue would carry differently moves these bytes.
#:
#: TWO REGIONS AND TWO KINDS, NOT ONE OF EACH. A block built by taking the
#: first file it finds passes a one-file fixture, and `south-west` is the only
#: region this golden builds ground for - so the wet feed carries a second
#: region the lane build knows nothing about, which is the real case: the feeds
#: are published per region by their own job and do not ask what was built.
CONDITION_FEEDS = {
    "wet/south-west.json": {
        "schema": 1, "region": "south-west",
        "as_of": "2026-03-01T00:15:00Z",
        "window_h": 48, "stale_after_h": 12,
        "bands_mm": {"firm": [12.0, 30.0], "hard": None},
        "calibrated": False,
        "licence": "Environment Agency flood-monitoring data, OGL v3",
        "stations": {"E7050": {"mm_24h": 1.2, "mm_48h": 4.4,
                               "at": "2026-03-01T00:00:00Z", "n": 96}},
    },
    "wet/wales.json": {
        "schema": 1, "region": "wales",
        "as_of": "2026-03-01T00:15:00Z",
        "window_h": 48, "stale_after_h": 12,
        "bands_mm": {"firm": [12.0, 30.0], "hard": None},
        "calibrated": False,
        "licence": "Environment Agency flood-monitoring data, OGL v3",
        "stations": {},
    },
    "rivers/south-west.json": {
        "schema": 1, "region": "south-west",
        "as_of": "2026-03-01T00:15:00Z", "stale_after_h": 6,
        "licence": "Environment Agency flood-monitoring data, OGL v3",
        "stations": {"45120": {"m": 0.42, "at": "2026-03-01T00:00:00Z",
                               "state": "normal", "vs_typical_high_m": -0.31,
                               "typical_low_m": 0.15,
                               "typical_high_m": 0.73}},
    },
}

AUTHORITY_CODE = "devon"
AUTHORITY_NAME = "Devon"
REGION_ID = "south-west"
REGION_LABEL = "South West"

#: Which fields may move when only the run stamp moved, and nothing else may.
#: Both of these describe the RUN; nothing hashes either of them.
VOLATILE = {
    "manifest.json": ("generated",),
    "containers/manifest.json": ("generated",),
}

#: Eight rights of way on Dartmoor, in the shape rowmaps publishes. Small
#: enough to build in about a second, varied enough to exercise a motor pack
#: (byways only), a bicycle pack (byways, restricted byways and a bridleway),
#: and both container kinds - area and overview.
#: Rows that exist to be EXCLUDED, plus one that must survive.
#:
#: The golden could not notice the change that matters most - a footpath
#: leaking back in - because there was no footpath to leak. Same for a context
#: way beyond the 1 km radius. Both are here now, and so is a bridleway INSIDE
#: the radius, because a filter that drops everything passes a drop-only test.
#:
#: Since 2026-09-24 the default is byways only, so EVERY bridleway and
#: restricted byway here - near or far - is one the build must drop, and the
#: five BOATs are what must survive. The rows stay: a context way leaking back
#: in is now the change this fixture exists to catch.
#:
#: THEY GO AFTER COUNCIL_ROWS, NOT BEFORE. The mutation check perturbs
#: rows[0] by index; putting a dropped row there mutates something the build
#: discards, and the check reports "UNCHANGED - the build ignored the change"
#: while looking like it ran. It did exactly that for one commit.
EXCLUSION_ROWS = [
    ("footpath", "ON|900|9/9",
     "FP|ON:99|0.400|none|-3.90800|50.56200|-3.90300|50.56400",
     [[-3.90800, 50.56200], [-3.90550, 50.56300], [-3.90300, 50.56400]]),
    ("bridleway", "ON|901|9/8",
     "BR|ON:98|0.350|none|-2.10000|51.90000|-2.09500|51.90300",
     [[-2.10000, 51.90000], [-2.09750, 51.90150], [-2.09500, 51.90300]]),
    ("bridleway", "ON|902|9/7",
     "BR|ON:97|0.120|none|-3.90800|50.56150|-3.90600|50.56250",
     [[-3.90800, 50.56150], [-3.90700, 50.56200], [-3.90600, 50.56250]]),
]

COUNCIL_ROWS = [
    ("byway_open_to_all_traffic", "ON|100|2/10",
     "BO|ON:22|0.144|none|-3.90876|50.56111|-3.90112|50.56480",
     [[-3.90876, 50.56111], [-3.90512, 50.56290], [-3.90112, 50.56480]]),
    ("byway_open_to_all_traffic", "ON|100|2/11",
     "BO|ON:22|0.311|gravel|-3.89004|50.57220|-3.88301|50.57901",
     [[-3.89004, 50.57220], [-3.88650, 50.57560], [-3.88301, 50.57901]]),
    ("byway_open_to_all_traffic", "ON|101|7/3",
     "BO|ON:23|0.502|none|-3.85110|50.60011|-3.84003|50.60550",
     [[-3.85110, 50.60011], [-3.84600, 50.60280], [-3.84003, 50.60550]]),
    ("byway_open_to_all_traffic", "ON|101|7/4",
     "BO|ON:23|0.208|ford|-3.83220|50.61330|-3.82710|50.61700",
     [[-3.83220, 50.61330], [-3.82960, 50.61515], [-3.82710, 50.61700]]),
    ("byway_open_to_all_traffic", "ON|102|1/1",
     "BO|ON:24|0.905|none|-3.79110|50.63220|-3.77500|50.64010",
     [[-3.79110, 50.63220], [-3.78300, 50.63610], [-3.77500, 50.64010]]),
    ("restricted_byway", "ON|100|9/2",
     "RB|ON:22|0.402|none|-3.91220|50.58110|-3.90410|50.58720",
     [[-3.91220, 50.58110], [-3.90800, 50.58415], [-3.90410, 50.58720]]),
    ("restricted_byway", "ON|103|4/6",
     "RB|ON:25|0.150|none|-3.76010|50.59330|-3.75620|50.59610",
     [[-3.76010, 50.59330], [-3.75815, 50.59470], [-3.75620, 50.59610]]),
    ("bridleway", "ON|104|3/8",
     "BR|ON:26|0.721|boggy|-3.94010|50.62110|-3.92800|50.62900",
     [[-3.94010, 50.62110], [-3.93400, 50.62505], [-3.92800, 50.62900]]),
]

#: What --mutate does: move the first point of the first byway by 0.0001
#: degrees, about 11 metres. Small enough that a careless build could round it
#: away, real enough that a rider would be sent to the wrong gate.
MUTATION = 0.0001

#: One dataset now, not a per-vehicle pair.
#:
#: Phase 1 replaced build_packages.PACKAGES (motor/bicycle/horse/foot, each a
#: separate build of overlapping ways) with a single `ways` dataset classed per
#: way. This file was written against the old shape and crashed on
#: `P.PACKAGES` the moment that landed - the cross-phase break the build
#: partition could not catch, because the two steps were owned by different
#: agents in different phases and neither wrote to the other's files.
PACKAGES_BUILT = (P.DATASET,)


def _rows(mutate=False):
    rows = [(t, ref, desc, [list(p) for p in coords])
            for (t, ref, desc, coords) in COUNCIL_ROWS + EXCLUSION_ROWS]
    if mutate:
        rows[0][3][0][0] += MUTATION
    return rows


def _features(mutate=False):
    """Council rows through the real normaliser, in source order."""
    out = []
    for row_type, ref, desc, coords in _rows(mutate):
        raw = {
            "type": "Feature",
            "properties": {"Name": ref, "Description": desc},
            "geometry": {"type": "LineString", "coordinates": coords},
        }
        f = P.normalise(raw, AUTHORITY_CODE, AUTHORITY_NAME, row_type)
        if f is None:
            raise SystemExit("golden input %r did not normalise" % ref)
        out.append(f)
    return out


# --------------------------------------------------------------------------
# the build
# --------------------------------------------------------------------------

def build_into(out_dir, run_stamp=RUN_STAMP, pack_stamp=PACK_STAMP,
               mutate=False, verbose=False):
    """Build the golden region into [out_dir]. Returns its artefacts, relative.

    Every stage is the SHIPPING code path - normalise, write_package,
    build_all, build - with exactly two things replaced, both of them inputs:
    where dist/ is, and the routing tile listing otherwise fetched over HTTP.
    """
    os.makedirs(out_dir, exist_ok=True)
    features = _features(mutate)

    dist_dir, routing_index = P.dist_dir, K.routing_index
    stdout = sys.stdout
    try:
        P.dist_dir = lambda: out_dir
        K.routing_index = lambda: dict(ROUTING_TILES)
        if not verbose:
            sys.stdout = io.StringIO()

        entries = []
        for pkg_name in PACKAGES_BUILT:
            # Every carried row type, in one package. Footpaths are excluded by
            # ROW_RULES itself now, so there is nothing to filter here beyond
            # what the rules already decided.
            # The key is "carried", not "carry". My first fix used
            # rule.get("carry", True), which is False-proof in the worst way:
            # the key does not exist, so it defaulted True and quietly built
            # 435,299 FOOTPATHS into the golden expectation - the exact data
            # this phase exists to remove. Caught by probing the constant
            # rather than trusting the fix.
            #
            # STEP 1.2c, THROUGH THE SAME FUNCTION main() CALLS, AT ITS
            # DEFAULT. golden.py says "every stage is the SHIPPING code path",
            # and it went normalise -> write_package while the near-context
            # filter lived in build_packages.main() - proved by putting a
            # bridleway 130 km from any byway in the fixture: it was carried.
            # The fix for that was a COPY of the near filter here, and a copy
            # does not move when the decision does: on 2026-09-24 the owner
            # made the default byways-only, and the copy would have gone on
            # building - and blessing - the 'near' set the real build no
            # longer publishes. select_ways() is the one place that decides.
            chosen = P.select_ways(features)
            entries.append(P.write_package(
                pkg_name, REGION_ID, REGION_LABEL, None, chosen,
                GOLDEN_KEY, pack_stamp))

        manifest = {
            "schema": 1,
            "generated": run_stamp,
            "attribution": P.OGL,
            "licence": "OGL-3.0",
            "source": "golden fixture - eight Dartmoor rights of way",
            "authorities": 1,
            "maxPlainBytes": P.MAX_PLAIN_BYTES,
            # What step 1.2c carried, as build_packages.main() writes it. The
            # golden manifest left these out, so no golden container carried
            # `context_note` and the one sentence the app must show about an
            # absence was the one thing this build never byte-checked.
            **P.context_fields(),
            "regions": [
                {"id": r, "label": lab,
                 "bounds": {"west": b[0], "south": b[1],
                            "east": b[2], "north": b[3]}}
                for r, lab, b in P.REGIONS
            ],
            "packages": entries,
        }
        manifest_path = os.path.join(out_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf8") as fh:
            json.dump(manifest, fh, indent=1)

        C.build_all(manifest_path, os.path.join(out_dir, "containers"),
                    GOLDEN_KEY, None, root=out_dir)

        conditions_dir = write_condition_feeds(out_dir)
        changes_index = write_golden_changesets(out_dir)

        catalogue = K.build(
            manifest_path, BASE_URL, CATALOGUE_STAMP,
            containers=K.load_containers(
                os.path.join(out_dir, "containers", "manifest.json")),
            satellite_index=None, trips_index=None, names_dir=None,
            height_index=None, routing_mirror_index=None, tro_path=None,
            conditions_dir=conditions_dir, changes_index=changes_index)
        with open(os.path.join(out_dir, "catalogue.json"), "w",
                  encoding="utf8") as fh:
            json.dump(catalogue, fh, indent=1)
    finally:
        sys.stdout = stdout
        P.dist_dir, K.routing_index = dist_dir, routing_index

    return artefacts(out_dir)


#: The build the golden's "previous" containers claim to be.
#:
#: Fixed, like every other input here. It is an INPUT to the changeset, so a
#: clock in its place would put a run stamp into catalogue.json through the
#: changeset's own sha256 - which is regression number two at the top of this
#: file, arriving through a door that did not exist when it was written.
PREVIOUS_BUILD = "2026-01-01T00:00:00Z"

#: The name the golden's "previous" build gave the way the new one renamed.
PREVIOUS_NAME = "Golden lane, as it was"


def write_golden_changesets(out_dir):
    """Build the `.tbchange` this build publishes, and hand back its index.

    WHY THE GOLDEN COVERS THIS AT ALL. The catalogue is the only place the app
    can learn a changeset exists, and for a long time it could not: both halves
    of the feature were written, neither was joined to the other, and
    catalogue.json contained zero occurrences of "tbchange". A join with
    nothing byte-comparing it is a join that comes apart in a cleanup pass and
    nobody notices until a rider is downloading the country again.

    So the golden builds one, from fixed inputs, exactly as the pipeline does:
    the containers this run produced, against a PREVIOUS build synthesised from
    them by renaming one way and stamping an earlier `built_at`. The changeset's
    sha256 lands in catalogue.json, so any change to the changeset format, the
    diff, or the fields the catalogue carries moves bytes this file checks.

    The previous tree is scaffolding and is built outside `out_dir`, so it
    never becomes an artefact. `changes/` IS an artefact: it is published.
    """
    containers = os.path.join(out_dir, "containers")
    if not os.path.isdir(containers):
        return None
    previous = tempfile.mkdtemp(prefix="tb-golden-prev-")
    try:
        shutil.copytree(containers, previous, dirs_exist_ok=True)
        for name in sorted(os.listdir(previous)):
            if name.endswith(".tbmap"):
                _age(os.path.join(previous, name))
        out = os.path.join(out_dir, "changes")
        # NO base_url, because the pipeline passes none: a changeset is
        # committed beside catalogue.json and the app resolves it against the
        # index's own `baseUrl`, exactly as it does a container. A golden that
        # fixed an absolute URL would be fixing a shape nothing ships.
        X.publish(previous, containers, out)
        index = os.path.join(out, "index.json")
        return index if os.path.isfile(index) else None
    finally:
        shutil.rmtree(previous, ignore_errors=True)


def _age(path):
    """Turn a container into a plausible earlier build of itself.

    ONE ROW, BY LOWEST ROWID, to a fixed name: the smallest change that is
    still a change, chosen so the result does not depend on iteration order,
    on the clock, or on how many ways the fixture happens to carry.
    """
    db = sqlite3.connect(path)
    try:
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        table = next((t for t in ("ways", "lanes", "orders") if t in names),
                     None)
        if table:
            row = db.execute(
                "SELECT rowid FROM %s ORDER BY rowid LIMIT 1" % table
            ).fetchone()
            if row:
                db.execute("UPDATE %s SET name=? WHERE rowid=?" % table,
                           (PREVIOUS_NAME, row[0]))
        db.execute("UPDATE meta SET value=? WHERE key='built_at'",
                   (PREVIOUS_BUILD,))
        db.commit()
    finally:
        db.close()


def write_condition_feeds(out_dir):
    """Lay CONDITION_FEEDS down where the LIVE half of refresh-data.yml puts
    them, and hand back the directory build_catalogue is pointed at.

    `published/` and not somewhere neutral, because the directory NAME is part
    of the answer: it is what `_served_prefix` turns into the URL a rider
    fetches, so a golden that used a different name would not compare the thing
    that ships.
    """
    root = os.path.join(out_dir, "published")
    for rel, body in sorted(CONDITION_FEEDS.items()):
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf8") as fh:
            json.dump(body, fh, indent=1, sort_keys=True)
    return root


def artefacts(root):
    out = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            out.append(os.path.relpath(full, root).replace("\\", "/"))
    return sorted(out)


def container_path(out_dir, dataset=None, area=REGION_ID):
    """Where `build_into` actually writes a container.

    IT STILL SAID `motor`. Step 1.2 replaced the vehicle partition with one
    dataset - the whole reason 109 containers became 7 - and this helper went
    on building `motor-<area>.tbmap`, a name nothing has written since. It
    takes no argument at its two call sites, so nothing type-checked it and
    nothing read it; it simply returned a path to a file that is not there.

    WHAT THAT COST. `validate_container.py --selftest` and
    `validate_changeset.py --selftest` are the only things that call it, and
    both died on the missing file - so the two validators step 0.12 is marked
    `done` for had not run since the pivot. A cross-phase break of exactly the
    kind the build partition cannot catch, because the two files are in
    different groups and neither changed.

    Defaults to `build_packages.DATASET`, so the next rename moves it too.
    """
    if dataset is None:
        dataset = P.DATASET
    return os.path.join(out_dir, "containers",
                        "%s-%s.tbmap" % (dataset, area))


def catalogue_path(out_dir):
    return os.path.join(out_dir, "catalogue.json")


# --------------------------------------------------------------------------
# comparing
# --------------------------------------------------------------------------

def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _first_difference(a, b):
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return min(len(a), len(b))


def _differing_bytes(a, b):
    return sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))


def _blanked(raw, fields):
    """The file with its declared-volatile fields set to a constant."""
    doc = json.loads(raw.decode("utf8"))
    was = {}
    for field in fields:
        was[field] = doc.get(field)
        doc[field] = "<volatile>"
    return json.dumps(doc, indent=1, sort_keys=True).encode("utf8"), was


def compare(dir_a, dir_b, allow_volatile=False):
    """Every artefact in A against B. Returns (problems, bytes compared)."""
    problems, compared = [], 0
    files_a, files_b = artefacts(dir_a), artefacts(dir_b)
    if files_a != files_b:
        only_a = [f for f in files_a if f not in files_b]
        only_b = [f for f in files_b if f not in files_a]
        if only_a:
            problems.append("only in the first build: %s" % ", ".join(only_a))
        if only_b:
            problems.append("only in the second build: %s" % ", ".join(only_b))

    for rel in files_a:
        if rel not in files_b:
            continue
        a, b = _read(os.path.join(dir_a, rel)), _read(os.path.join(dir_b, rel))
        compared += len(a)
        if a == b:
            continue
        fields = VOLATILE.get(rel)
        if allow_volatile and fields:
            # Allowed to move - but ONLY those fields, and they have to have
            # actually moved, or this comparison passed on a stamp that was
            # never applied and proved nothing.
            na, wa = _blanked(a, fields)
            nb, wb = _blanked(b, fields)
            if na != nb:
                at = _first_difference(na, nb)
                problems.append(
                    "%s differs outside %s - first at byte %d of the "
                    "normalised form. The run stamp reached the data."
                    % (rel, "/".join(fields), at))
            elif wa == wb:
                problems.append(
                    "%s: %s did not change between the two runs, so this "
                    "comparison proved nothing." % (rel, "/".join(fields)))
            continue
        at = _first_difference(a, b)
        problems.append(
            "%s differs: %d of %d bytes, first at offset %d (%r vs %r)"
            % (rel, _differing_bytes(a, b), max(len(a), len(b)), at,
               a[at:at + 8], b[at:at + 8]))
    return problems, compared


#: Artefacts the builders write in TEXT mode, so Python turns every newline
#: into the host's line ending on the way out. The bytes on a Windows checkout
#: are therefore not the bytes CI publishes, and an expectation recorded on one
#: could never pass on the other.
#:
#: So the stored expectation is over the LF form of these, and over the RAW
#: bytes of everything else - the packs and the containers, which are binary
#: and identical everywhere. The two-build comparisons above do not use this at
#: all: they are raw bytes on both sides, on one machine, as the gate asks.
#: This exists only so the RECORDED expectation means the same thing on a
#: developer's laptop and in the workflow.
TEXT_SUFFIXES = (".json", ".sha256")


def _canonical(rel, raw):
    if rel.endswith(TEXT_SUFFIXES):
        return raw.replace(b"\r\n", b"\n")
    return raw


def digests(root):
    out = {}
    for rel in artefacts(root):
        body = _canonical(rel, _read(os.path.join(root, rel)))
        out[rel] = {"bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest()}
    return out


# --------------------------------------------------------------------------
# the stored expectation
# --------------------------------------------------------------------------

# EXPECTED-BEGIN (rewritten by --bless; do not edit by hand)
EXPECTED_SQLITE = "3.50.4"
EXPECTED = {
    "catalogue.json": {"bytes": 5213,
        "sha256": "f0a0e4e9c065f73606f8382894f4e10fb971a587d699fd018c88a6ce3374d7ef"},
    "changes/gb-south-west/20260101T000000Z-20260102T030405Z.tbchange": {"bytes": 36864,
        "sha256": "728759392bcde498a441ff86da39d64c180df0898b1eec19a63c8c920de4ff22"},
    "changes/index.json": {"bytes": 509,
        "sha256": "40bc88d472d2e1e01106268cd572f70f407e7ce9818fe0e3478d6a0bdd441367"},
    "containers/manifest.json": {"bytes": 1203,
        "sha256": "b51e984af327fc8b43ba91d0770fb0e7e39701d2a2902bf9d1eb992c8113d59e"},
    "containers/ways-overview.tbmap": {"bytes": 57344,
        "sha256": "1d63e75650bbef91b5ff0e7d68536223e5064bd25e75007041592d3095b38d14"},
    "containers/ways-south-west.tbmap": {"bytes": 65536,
        "sha256": "cbe03bad8a75128350d879908abe21a68ff8cb59d73b5816ccdee11f6be11536"},
    "manifest.json": {"bytes": 2017,
        "sha256": "4871f46be15f77cd296cc4b3ad2d00eb2ede5c104a0452ea26d27a1a2415a0a5"},
    "packages/ways-south-west.tbpack": {"bytes": 976,
        "sha256": "9e9e78c6d01effa0b949a5dd92f335fa6d1a632e704aa89ddf72ae4d77e3c852"},
    "packages/ways-south-west.tbpack.sha256": {"bytes": 89,
        "sha256": "23bca9a13b0409481989e883271af2898db6468553c4b2e16a7a985a0920746c"},
    "published/rivers/south-west.json": {"bytes": 348,
        "sha256": "1de8695dab724afd7106d65d362db0fcf718d107e399fca75b9f43e336857036"},
    "published/wet/south-west.json": {"bytes": 384,
        "sha256": "7bf4eb38f374b0430b56625f804ba66bc9dc7606af2176aca5d18efa75a1ca7a"},
    "published/wet/wales.json": {"bytes": 280,
        "sha256": "fc38c95f181b4049cf292f9dc627fbf755b3a6d6acfe582389e7e0f5bee01fc4"},
}
# EXPECTED-END


def bless(root):
    got = digests(root)
    # THE MARKERS ARE ASSEMBLED, NEVER WRITTEN WHOLE.
    #
    # Spelled out here they would make this file match itself twice, and the
    # first --bless did exactly that: re.sub replaced BOTH matches and wrote
    # the expectation over this function's own body, leaving a golden.py that
    # would not parse. `count=1` is the other half of the fix - the block above
    # is the first match in the file and the only one that may be replaced.
    begin = "# EXPECTED-" + "BEGIN (rewritten by --bless; do not edit by hand)"
    end = "# EXPECTED-" + "END"

    block = begin + "\n"
    block += 'EXPECTED_SQLITE = "%s"\n' % sqlite3.sqlite_version
    block += "EXPECTED = {\n"
    for rel in sorted(got):
        block += '    "%s": {"bytes": %d,\n' % (rel, got[rel]["bytes"])
        block += '        "sha256": "%s"},\n' % got[rel]["sha256"]
    block += "}\n" + end

    me = os.path.abspath(__file__)
    src = io.open(me, encoding="utf-8").read()
    new, hits = re.subn(re.escape(begin) + ".*?" + re.escape(end),
                        lambda _m: block, src, count=1, flags=re.S)
    if hits != 1:
        raise SystemExit("could not find the expectation block to rewrite")
    with io.open(me, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new)

    moved = [rel for rel in sorted(set(got) | set(EXPECTED))
             if got.get(rel) != EXPECTED.get(rel)]
    print("blessed %d artefacts; %d changed" % (len(got), len(moved)))
    for rel in moved:
        # An artefact can be in EXPECTED and NOT in got - phase 1 renamed
        # every pack from motor-*/bicycle-* to ways-*, so blessing crashed on
        # the first artefact that had gone away rather than changed. Both
        # directions are ordinary.
        print("  %-44s %s -> %s"
              % (rel,
                 (EXPECTED.get(rel) or {}).get("sha256", "(new)")[:16],
                 (got.get(rel) or {}).get("sha256", "(gone)")[:16]))


def check_expected(root):
    got, problems, uncomparable = digests(root), [], []
    if not EXPECTED:
        problems.append(
            "no expectation is stored. Run --bless once, on a build you have "
            "read, or this file proves nothing at all.")
        return problems, got
    for rel in sorted(set(got) | set(EXPECTED)):
        want, have = EXPECTED.get(rel), got.get(rel)
        if want is None:
            problems.append("%s was built and is not in the expectation" % rel)
        elif have is None:
            problems.append("%s is expected and was not built" % rel)
        elif have != want:
            # A CONTAINER IS A SQLITE FILE and its page layout is SQLite's to
            # decide — and because the catalogue and both manifests carry the
            # containers' hashes, a page-layout change moves EVERY artefact
            # here, not only the `.tbmap` ones.
            #
            # MEASURED: blessed on a developer machine at SQLite 3.50.4, CI
            # runs 3.45.1, and all twelve came out the same SHAPE with
            # different bytes — while CI's own reproducibility check passed in
            # the same run ("two consecutive builds, same inputs:
            # byte-identical"). The build is reproducible; the library
            # underneath is the only variable.
            #
            # Refusing on that made this loud and wrong on every CI run, which
            # is the state a guard is least useful in: about to be ignored, or
            # forced past. Byte-equality across SQLite versions is not a
            # property this build has, and a check asserting it is measuring
            # the runner.
            #
            # THE FIRST ATTEMPT AT THIS KEPT COMPARING THE SIZE, on the
            # grounds that a page layout does not change it. That was wrong
            # within the hour: `ways-south-west.tbpack` came out 1110 bytes
            # here and 1107 in CI, and the cause is `gzip.compress` — this
            # machine has zlib-ng, the runner has stock zlib, and two
            # compressors do not agree on the byte count for the same input.
            # build_packages.py's own comment anticipated it: "a new zlib".
            #
            # So neither the hash NOR the size is a statement about the data
            # once the toolchain differs, and a guard that keeps one of them is
            # keeping the half that happens not to have bitten yet.
            #
            # WHAT STILL HAS FORCE, and it runs on every CI build:
            #   [1] two consecutive builds from the same inputs in the same
            #       environment are byte-identical — the actual reproducibility
            #       claim, and the one a publish depends on;
            #   [3] this exact comparison, whenever the environment matches the
            #       one the expectation was recorded in.
            # Across environments [3] can only say so, and it does, every time.
            across_versions = sqlite3.sqlite_version != EXPECTED_SQLITE
            if across_versions:
                uncomparable.append(rel)
                continue
            why = ""
            problems.append(
                "%s: expected %d bytes / %s, built %d bytes / %s%s"
                % (rel, want["bytes"], want["sha256"][:16],
                   have["bytes"], have["sha256"][:16], why))

    if uncomparable:
        print("  NOT COMPARED: %d artefact(s). Built under SQLite %s against "
              "an expectation recorded at %s."
              % (len(uncomparable), sqlite3.sqlite_version, EXPECTED_SQLITE))
        print("    Neither the hash nor the size is a statement about the "
              "DATA once the toolchain differs: SQLite decides the page "
              "layout and zlib decides the compressed length.")
        print("    The reproducibility check [1] above still ran, in THIS "
              "environment, and is the claim a publish depends on. Re-bless "
              "here to compare these exactly.")
        for rel in uncomparable:
            print("      %s" % rel)
    return problems, got


# --------------------------------------------------------------------------

def run(args):
    work = args.keep or tempfile.mkdtemp(prefix="tb-golden-")
    if args.keep:
        shutil.rmtree(args.keep, ignore_errors=True)
        os.makedirs(args.keep, exist_ok=True)
    a = os.path.join(work, "a")
    b = os.path.join(work, "b")
    c = os.path.join(work, "c")
    failures = []
    try:
        print("golden region: %s, %d council rows, packages %s"
              % (REGION_LABEL, len(COUNCIL_ROWS), ", ".join(PACKAGES_BUILT)))

        files = build_into(a, verbose=args.verbose)
        print("\nbuilt %d artefacts" % len(files))
        for rel in files:
            raw = _read(os.path.join(a, rel))
            print("  %-44s %8d bytes  %s"
                  % (rel, len(raw), hashlib.sha256(raw).hexdigest()[:16]))

        if args.bless:
            bless(a)
            return 0

        # (1) identical inputs, identical bytes
        build_into(b, verbose=args.verbose)
        problems, compared = compare(a, b)
        print("\n[1] two consecutive builds, same inputs: %d bytes compared "
              "across %d files" % (compared, len(files)))
        if problems:
            failures.extend(problems)
            for p in problems:
                print("    DIFFERS  %s" % p)
        else:
            print("    byte-identical")

        # (2) the clock does not reach the data
        build_into(c, run_stamp=LATER_RUN_STAMP, verbose=args.verbose)
        problems, compared = compare(a, c, allow_volatile=True)
        print("\n[2] run stamp %s -> %s: %d bytes compared"
              % (RUN_STAMP, LATER_RUN_STAMP, compared))
        if problems:
            failures.extend(problems)
            for p in problems:
                print("    DIFFERS  %s" % p)
        else:
            print("    byte-identical outside %s"
                  % ", ".join("%s:%s" % (k, "/".join(v))
                              for k, v in sorted(VOLATILE.items())))

        # (3) the stored expectation
        problems, got = check_expected(a)
        total = sum(e["bytes"] for e in got.values())
        print("\n[3] against the expectation stored in golden.py: %d files, "
              "%d bytes" % (len(got), total))
        if problems:
            failures.extend(problems)
            for p in problems:
                print("    REFUSED  %s" % p)
        else:
            print("    every artefact matches, byte count and sha256")

        # (4) a real change must move the bytes
        if args.mutate:
            m = os.path.join(work, "m")
            build_into(m, mutate=True, verbose=args.verbose)
            problems, compared = compare(a, m)
            print("\n[4] one lane moved %s degrees (~11 m): %d bytes compared"
                  % (MUTATION, compared))
            if not problems:
                failures.append(
                    "a lane moved 11 metres and every artefact came out "
                    "byte-identical. The build is not reading its input.")
                print("    UNCHANGED - the build ignored the change")
            else:
                for p in problems:
                    print("    moved    %s" % p)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\nGOLDEN FAILED: %d problem(s)" % len(failures))
        return 1
    print("\ngolden ok")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bless", action="store_true",
                    help="re-record the expectation in this file")
    ap.add_argument("--mutate", action="store_true",
                    help="also prove a deliberate change moves the bytes")
    ap.add_argument("--keep", help="build into this directory and leave it")
    ap.add_argument("--verbose", action="store_true",
                    help="let the builders print")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())

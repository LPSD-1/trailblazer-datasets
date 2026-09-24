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
            carried = {t for t, rule in P.ROW_RULES.items() if rule["carried"]}
            chosen = [f for f in features
                      if f["properties"]["rowType"] in carried]

            # STEP 1.2c, APPLIED HERE TOO - and it was not, which this file
            # claimed otherwise about.
            #
            # golden.py says "every stage is the SHIPPING code path", and it
            # went normalise -> write_package while the near-context filter
            # lives in build_packages.main(). So the golden could not have
            # noticed the far-context rule breaking. Proved by putting a
            # bridleway 130 km from any byway in the fixture: it was carried.
            motor = [f for f in chosen
                     if not P.ROW_RULES[f["properties"]["rowType"]]["context"]]
            context = [f for f in chosen
                       if P.ROW_RULES[f["properties"]["rowType"]]["context"]]
            near = P.near_motor_ways(context, motor)
            near_uids = {f["properties"]["lane_uid"] for f in near}
            chosen = [f for f in chosen
                      if not P.ROW_RULES[f["properties"]["rowType"]]["context"]
                      or f["properties"]["lane_uid"] in near_uids]
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

        catalogue = K.build(
            manifest_path, BASE_URL, CATALOGUE_STAMP,
            containers=K.load_containers(
                os.path.join(out_dir, "containers", "manifest.json")),
            satellite_index=None, trips_index=None, names_dir=None,
            height_index=None, routing_mirror_index=None, tro_path=None)
        with open(os.path.join(out_dir, "catalogue.json"), "w",
                  encoding="utf8") as fh:
            json.dump(catalogue, fh, indent=1)
    finally:
        sys.stdout = stdout
        P.dist_dir, K.routing_index = dist_dir, routing_index

    return artefacts(out_dir)


def artefacts(root):
    out = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            out.append(os.path.relpath(full, root).replace("\\", "/"))
    return sorted(out)


def container_path(out_dir, vehicle="motor", area=REGION_ID):
    return os.path.join(out_dir, "containers",
                        "%s-%s.tbmap" % (vehicle, area))


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
    "catalogue.json": {"bytes": 3697,
        "sha256": "5443d7283b15961b8f0c3b96658a7b51d4478e2107afd7a070a046e47a49813d"},
    "containers/manifest.json": {"bytes": 982,
        "sha256": "f7cac79e19e3cf83523df320cfbf568e801dc30b574ecc2db805eac648ff9126"},
    "containers/ways-overview.tbmap": {"bytes": 57344,
        "sha256": "d23343592dadec69f070bfc22a6d7dbeb49008939e0cd9e8b0edccd0eb16b38f"},
    "containers/ways-south-west.tbmap": {"bytes": 65536,
        "sha256": "2dfefdcbbe90c0c18a0196aaf86a2fb3e5132d114ccb6ea64d49ea407abe3d20"},
    "manifest.json": {"bytes": 1763,
        "sha256": "a3ed35b8e5901205aff544954fadc557a9b4daa42595140eab69506f39510b71"},
    "packages/ways-south-west.tbpack": {"bytes": 1110,
        "sha256": "bf64666f28b1ed5af9634fda6560252472f63eb4178a98eb771b92c0e31c7e04"},
    "packages/ways-south-west.tbpack.sha256": {"bytes": 89,
        "sha256": "d77ef6c295afaf208f0be0045d5f44817c7c151d7c997ee99061a5fce5ab7cc4"},
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
    got, problems = digests(root), []
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
            why = ""
            # A container is a SQLite file and its page layout is SQLite's to
            # decide. Said out loud rather than left as a mystery diff, because
            # "the library under us changed" and "the data changed" want
            # completely different responses and look identical here.
            if rel.endswith(".tbmap") \
                    and sqlite3.sqlite_version != EXPECTED_SQLITE:
                why = (" (recorded with SQLite %s, built with %s - re-bless "
                       "only after checking the lanes did not move)"
                       % (EXPECTED_SQLITE, sqlite3.sqlite_version))
            problems.append(
                "%s: expected %d bytes / %s, built %d bytes / %s%s"
                % (rel, want["bytes"], want["sha256"][:16],
                   have["bytes"], have["sha256"][:16], why))
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

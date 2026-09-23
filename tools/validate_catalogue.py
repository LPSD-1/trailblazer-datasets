#!/usr/bin/env python3
"""Refuse to publish a catalogue.json the app cannot act on.

    python tools/validate_catalogue.py catalogue.json
    python tools/validate_catalogue.py --selftest      # prove it can refuse

THE CATALOGUE IS THE ONLY THING EVERY RIDER READS. It is fetched before any
pack, and every download in the product is chosen from it. A pack that is wrong
spoils one area; a catalogue that is wrong spoils all of them, and it is a
120 kB JSON file assembled by eight jobs on five clocks.

THE FAULT THIS IS BUILT AROUND: THE SCHEMA NUMBER. The index has had two
shapes, and the app reads shape 2. Fed shape 1 it does not error, warn or
degrade - it finds no countries where it looks for them and draws NO LANES AT
ALL, which presents as "there are no rights of way in England" rather than as a
bad download. That is the same class as the PMTiles root that check_pmtiles.py
exists for: a file that arrives perfectly and is never usable.

The rest come from things that have actually gone wrong in this repository:

  * A LANE PACK POINTING AT NOTHING. `build_catalogue.py` deliberately writes
    `file: null` when the container build did not produce a container, rather
    than quietly falling back to a format the app can no longer read. That is
    the right thing to write and the wrong thing to publish.
  * A CATALOGUE THAT LOST A KIND. It is rebuilt from scratch, so anything a job
    forgets to pass is DELETED from it silently. That has happened three times:
    the monthly refresh would have wiped imagery, the daily imagery job did
    wipe the ready-made trips, and the same job collapsed every mirrored
    routing URL to a bare filename. `verify_catalogue.py` compares against each
    index; this file asks the cheaper question that needs no index - is there
    anything here at all, and is every address still an address.
  * LEGAL BASIS. `real_catalogue_test.dart` asserts every lane pack in a GB
    region says `official`, and caught one of ours that did not. That is not
    bookkeeping: it is the difference between the definitive map and somebody's
    idea of where a byway goes.

WHAT THIS IS NOT. `verify_catalogue.py` checks the catalogue against the files
and indexes BESIDE it - that imagery is still listed, that a pack's sha256
matches the bytes on disk. This validates the artefact ON ITS OWN, the way
check_pmtiles.py validates an archive: structure, addresses, and the fields the
app's behaviour turns on. Run both.
"""
import argparse
import json
import os
import re
import sys

#: The shape the app reads. See the docstring: the wrong one draws no lanes.
SCHEMA = 2

REQUIRED_TOP = ("schema", "generated", "attribution", "baseUrl", "continents",
                "updates")

#: How often the app checks each kind. Published in the catalogue so it can be
#: changed without shipping an app, which means a typo here changes behaviour
#: on every phone and nothing on the device can tell it from a policy.
CADENCES = ("sixHourly", "daily", "weekly", "monthly", "quarterly", "never")

#: Routing tiles are served by brouter.de, which publishes no checksums; every
#: other kind is ours and must carry one. 716 of the 732 routing packs in the
#: live catalogue have no sha256 and are correct; 16 are mirrored by us and do.
NO_CHECKSUM_KINDS = ("routing",)

LANE_KINDS = ("lanes", "overview")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _bounds_problem(box, where):
    if not isinstance(box, dict):
        return "%s has no bounds object" % where
    for k in ("west", "south", "east", "north"):
        if not _is_number(box.get(k)):
            return "%s bounds has no numeric %s" % (where, k)
    if box["west"] > box["east"] or box["south"] > box["north"]:
        return ("%s bounds is inside out (w%s e%s, s%s n%s): the app uses this "
                "box to decide what to offer a rider where they are standing"
                % (where, box["west"], box["east"], box["south"], box["north"]))
    if not (-180 <= box["west"] <= 180 and -180 <= box["east"] <= 180
            and -90 <= box["south"] <= 90 and -90 <= box["north"] <= 90):
        return "%s bounds is off the earth" % where
    return None


def _address_problem(pack, where):
    """Is `file` something the app can actually fetch?"""
    file = pack.get("file")
    if file is None:
        return ("%s has file: null. build_catalogue writes that when the "
                "container build produced nothing - it is a broken build, not "
                "a mixed one, and the app has nothing to download." % where)
    if not isinstance(file, str) or not file.strip():
        return "%s has an empty file" % where
    if "\\" in file:
        return "%s file %r is a Windows path, not a URL or a posix path" \
               % (where, file)
    if file.startswith("//") or file.startswith("/"):
        return "%s file %r is rooted at a host this catalogue does not name" \
               % (where, file)
    if "://" in file and not file.startswith(("http://", "https://")):
        return "%s file %r is not http(s)" % (where, file)
    # A bare filename with no directory is how the mirrored routing URLs
    # collapsed: every tile still listed, every address useless.
    if "://" not in file and "/" not in file:
        return ("%s file %r is a bare filename with no path; relative packs "
                "live under a directory beside the catalogue" % (where, file))
    return None


def _pack_problems(pack, where, country_code, out):
    if not isinstance(pack, dict):
        out.append("%s is not an object" % where)
        return
    for key in ("id", "kind", "label"):
        if not pack.get(key):
            out.append("%s has no %s" % (where, key))
    kind = pack.get("kind")
    name = "%s (%s)" % (where, pack.get("id") or "no id")

    problem = _address_problem(pack, name)
    if problem:
        out.append(problem)

    if not _is_number(pack.get("bytes")) or pack.get("bytes", 0) <= 0:
        out.append("%s says bytes=%r; a rider is shown that number before "
                   "they choose to download" % (name, pack.get("bytes")))

    if "downloadBytes" in pack:
        dl = pack["downloadBytes"]
        if not _is_number(dl) or dl <= 0:
            out.append("%s says downloadBytes=%r, so the size the rider is "
                       "shown is nothing" % (name, dl))

    if kind not in NO_CHECKSUM_KINDS:
        sha = pack.get("sha256")
        if not sha or not HEX64.match(str(sha)):
            out.append("%s has no usable sha256 (%r). Nothing on the device "
                       "can tell a truncated download from a whole one."
                       % (name, sha))

    if kind in LANE_KINDS:
        if pack.get("format") != "tbmap":
            out.append("%s is a lane pack in format %r; the app reads tbmap"
                       % (name, pack.get("format")))
        basis = pack.get("legalBasis")
        if not basis:
            out.append("%s has no legalBasis" % name)
        elif country_code == "GB" and basis != "official":
            out.append(
                "%s is a GB lane pack claiming legalBasis %r. England and "
                "Wales publish a legal register of rights of way and this is "
                "built from it; saying anything else here is the difference "
                "between the definitive map and somebody's idea of where a "
                "byway goes." % (name, basis))


def problems_with(path):
    if not os.path.isfile(path):
        return ["missing"]
    try:
        with open(path, encoding="utf8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        return ["not valid JSON: %s" % e]
    if not isinstance(doc, dict):
        return ["the catalogue is a %s, not an object" % type(doc).__name__]

    out = []
    for key in REQUIRED_TOP:
        if key not in doc:
            out.append("no %s at the top level" % key)

    # THE ONE THAT DRAWS NO LANES AND SAYS NOTHING.
    if doc.get("schema") != SCHEMA:
        out.append(
            "schema is %r and the app reads %d. Fed the other shape the app "
            "does not error - it finds no countries where it looks and draws "
            "NO LANES AT ALL." % (doc.get("schema"), SCHEMA))

    base = doc.get("baseUrl")
    if base is not None and base != "" and not str(base).startswith(
            ("http://", "https://")):
        out.append("baseUrl %r is not an http(s) URL" % base)

    updates = doc.get("updates")
    if not isinstance(updates, dict) or not updates:
        out.append("updates is %r; the app reads its check frequencies from "
                   "here" % updates)
    else:
        for kind, cadence in sorted(updates.items()):
            if cadence not in CADENCES:
                out.append("updates.%s is %r, which is not one of %s"
                           % (kind, cadence, ", ".join(CADENCES)))

    continents = doc.get("continents")
    if not isinstance(continents, list) or not continents:
        out.append("continents is %r: there is nothing to browse"
                   % type(continents).__name__)
        return out

    lane_packs = 0
    seen_continents = set()
    for ci, continent in enumerate(continents):
        where = "continent %d" % ci
        if not isinstance(continent, dict):
            out.append("%s is not an object" % where)
            continue
        cid = continent.get("id")
        if not cid:
            out.append("%s has no id" % where)
        elif cid in seen_continents:
            out.append("two continents share the id %r" % cid)
        seen_continents.add(cid)
        where = "continent %s" % (cid or ci)

        countries = continent.get("countries")
        if not isinstance(countries, list) or not countries:
            out.append("%s has no countries" % where)
            continue

        seen_countries = set()
        for country in countries:
            if not isinstance(country, dict):
                out.append("%s holds a country that is not an object" % where)
                continue
            code = country.get("code")
            cwhere = "%s/%s" % (where, code or "no code")
            if not code:
                out.append("%s has a country with no code" % where)
            elif code in seen_countries:
                out.append("%s lists %r twice" % (where, code))
            seen_countries.add(code)
            if not country.get("label"):
                out.append("%s has no label" % cwhere)
            problem = _bounds_problem(country.get("bounds"), cwhere)
            if problem:
                out.append(problem)

            areas = country.get("areas")
            if not isinstance(areas, list):
                out.append("%s has no areas list" % cwhere)
                continue

            seen_areas = set()
            for area in areas:
                if not isinstance(area, dict):
                    out.append("%s holds an area that is not an object"
                               % cwhere)
                    continue
                aid = area.get("id")
                awhere = "%s/%s" % (cwhere, aid or "no id")
                if not aid:
                    out.append("%s has an area with no id" % cwhere)
                elif aid in seen_areas:
                    out.append("%s lists the area %r twice" % (cwhere, aid))
                seen_areas.add(aid)
                if not area.get("label"):
                    out.append("%s has no label" % awhere)
                problem = _bounds_problem(area.get("bounds"), awhere)
                if problem:
                    out.append(problem)

                packs = area.get("packs")
                if not isinstance(packs, list) or not packs:
                    out.append("%s offers no packs: a browse entry a rider "
                               "can open and download nothing from" % awhere)
                    continue

                seen_ids = set()
                for pi, pack in enumerate(packs):
                    _pack_problems(pack, "%s pack %d" % (awhere, pi), code,
                                   out)
                    if isinstance(pack, dict):
                        pid = pack.get("id")
                        if pid and pid in seen_ids:
                            out.append("%s lists the pack id %r twice; the "
                                       "app keys a download by that id"
                                       % (awhere, pid))
                        seen_ids.add(pid)
                        if pack.get("kind") in LANE_KINDS:
                            lane_packs += 1

    # THE WIPE. Rebuilt from scratch means a job that forgets an input deletes
    # its packs with no error anywhere.
    if not lane_packs:
        out.append(
            "not one lane pack in the whole catalogue. The catalogue is "
            "rebuilt from scratch, so a job that did not pass its input "
            "deletes everything it owns and the build still succeeds.")
    return out


def report(paths):
    bad = 0
    for path in paths:
        found = problems_with(path)
        name = os.path.basename(path)
        if found:
            bad += 1
            print("REFUSED  %s" % name)
            for p in found[:40]:
                print("           %s" % p)
            if len(found) > 40:
                print("           ... and %d more" % (len(found) - 40))
        else:
            print("ok       %s" % name)
    if bad:
        print("\n%d catalogue(s) would misdirect every rider who read them."
              % bad)
    return bad


# --------------------------------------------------------------------------
# proving it can refuse
# --------------------------------------------------------------------------

def _gb_lane_pack(doc):
    for continent in doc["continents"]:
        for country in continent["countries"]:
            if country.get("code") != "GB":
                continue
            for area in country["areas"]:
                for pack in area["packs"]:
                    if pack.get("kind") in LANE_KINDS:
                        return area, pack
    raise SystemExit("the golden catalogue has no GB lane pack to corrupt")


def _corruptions():
    def edit(fn):
        def apply(doc):
            fn(doc)
            return doc
        return apply

    def set_schema(v):
        return edit(lambda d: d.__setitem__("schema", v))

    def on_pack(fn):
        def go(d):
            _area, pack = _gb_lane_pack(d)
            fn(pack)
        return edit(go)

    def drop_lane_packs(d):
        for continent in d["continents"]:
            for country in continent["countries"]:
                for area in country["areas"]:
                    area["packs"] = [p for p in area["packs"]
                                     if p.get("kind") not in LANE_KINDS]

    def duplicate_id(d):
        area, pack = _gb_lane_pack(d)
        twin = dict(area["packs"][-1])
        twin["id"] = pack["id"]
        area["packs"].append(twin)

    def invert_bounds(d):
        area, _pack = _gb_lane_pack(d)
        b = area["bounds"]
        b["west"], b["east"] = b["east"], b["west"]

    def empty_area(d):
        area, _pack = _gb_lane_pack(d)
        area["packs"] = []

    return [
        ("the other schema", set_schema(1)),
        ("no schema at all", edit(lambda d: d.pop("schema"))),
        ("a lane pack pointing at nothing",
         on_pack(lambda p: p.__setitem__("file", None))),
        ("a lane pack addressed by bare filename",
         on_pack(lambda p: p.__setitem__("file", "motor-south-west.tbmap"))),
        ("a lane pack with a Windows path",
         on_pack(lambda p: p.__setitem__(
             "file", "containers\\motor-south-west.tbmap"))),
        ("a lane pack with no checksum",
         on_pack(lambda p: p.__setitem__("sha256", ""))),
        ("a lane pack of zero bytes",
         on_pack(lambda p: p.__setitem__("bytes", 0))),
        ("a lane pack whose download size is nothing",
         on_pack(lambda p: p.__setitem__("downloadBytes", 0))),
        ("a lane pack in a format the app cannot read",
         on_pack(lambda p: p.__setitem__("format", "tbpack"))),
        ("a GB lane pack that is not the definitive map",
         on_pack(lambda p: p.__setitem__("legalBasis", "community"))),
        ("a GB lane pack with no legal basis at all",
         on_pack(lambda p: p.pop("legalBasis"))),
        ("two packs with one id", edit(duplicate_id)),
        ("an area offering nothing", edit(empty_area)),
        ("an area whose box is inside out", edit(invert_bounds)),
        ("every lane pack gone", edit(drop_lane_packs)),
        ("baseUrl collapsed",
         edit(lambda d: d.__setitem__("baseUrl", "packs/"))),
        ("an update cadence nothing implements",
         edit(lambda d: d["updates"].__setitem__("lanes", "sometimes"))),
        ("no continents", edit(lambda d: d.__setitem__("continents", []))),
    ]


def selftest():
    import shutil
    import tempfile
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import golden

    work = tempfile.mkdtemp(prefix="tb-validate-catalogue-")
    failures = []
    try:
        built = os.path.join(work, "good")
        golden.build_into(built)
        good = golden.catalogue_path(built)
        with open(good, encoding="utf8") as fh:
            original = json.load(fh)
        print("golden catalogue: %d bytes, %d packs"
              % (os.path.getsize(good),
                 sum(len(a["packs"]) for c in original["continents"]
                     for co in c["countries"] for a in co["areas"])))

        found = problems_with(good)
        if found:
            failures.append("the GOOD catalogue was refused: %s"
                            % "; ".join(found))
            print("  FAIL   a correct catalogue was refused")
        else:
            print("  ok     a correct catalogue passes")

        bad_path = os.path.join(work, "bad.json")
        for name, corrupt in _corruptions():
            doc = corrupt(json.loads(json.dumps(original)))
            with open(bad_path, "w", encoding="utf8") as fh:
                json.dump(doc, fh, indent=1)
            found = problems_with(bad_path)
            if not found:
                failures.append("NOT CAUGHT: %s" % name)
                print("  MISSED %s" % name)
            else:
                print("  caught %-46s %s" % (name, found[0][:84]))

        # And the one that is not a document at all.
        with open(bad_path, "w", encoding="utf8") as fh:
            fh.write("<html>404</html>")
        if not problems_with(bad_path):
            failures.append("NOT CAUGHT: an error page served as a catalogue")
            print("  MISSED an error page served as a catalogue")
        else:
            print("  caught %-46s %s" % ("an error page served as a catalogue",
                                         problems_with(bad_path)[0][:84]))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\nSELFTEST FAILED: %d" % len(failures))
        for f in failures:
            print("  " + f)
        return 1
    print("\nselftest ok: 1 good catalogue accepted, %d corruptions refused"
          % (len(_corruptions()) + 1))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("catalogues", nargs="*")
    ap.add_argument("--selftest", action="store_true",
                    help="build a golden catalogue, corrupt it, and prove "
                         "every corruption is refused")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.catalogues:
        ap.error("give at least one catalogue, or --selftest")
    return 1 if report(args.catalogues) else 0


if __name__ == "__main__":
    sys.exit(main())

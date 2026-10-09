#!/usr/bin/env python3
"""Only the CC BY imagery layer is built or published; the 2024 packs stay out.

    python tools/imagery_withdrawn.py --source-check
    python tools/imagery_withdrawn.py --entry dist/satellite/entry.json
    python tools/imagery_withdrawn.py --index satellite/index.json \
        --catalogue dist/catalogue.json

WHY
---
Every satellite pack published up to 9 Oct 2026 was cut from EOX's 2024
mosaic. EOX license every year after 2016 as CC BY-NC-SA 4.0, and this app is
paid, so the owner withdrew the lot that day ("Withdraw now"). The packs' own
notes said CC BY 4.0; they were wrong.

THE ALLOWLIST IS HERE, IN CODE. ALLOWED_LAYERS names the one EOX layer that is
CC BY 4.0 (s2cloudless_3857, the 2016 mosaic). Anything else - another year,
another projection, a misspelling, no layer at all - is refused:

- `--source-check` and build_satellite.py refuse a SOURCE or LAYER off the
  list before a tile is fetched;
- `--entry` refuses a freshly built entry off the list before it is uploaded;
- `--index` and `--catalogue` refuse to let a run commit an index or a
  rebuilt catalogue that lists a withdrawn asset (by sha256, or by a note
  naming a later data year), an index pack whose recorded `layer` is off the
  list or missing, or a catalogue imagery pack the index does not vouch for
  with the same sha256 on an allowed layer.

THE WITHDRAWN LEDGER. satellite/index.json keeps `packs` (what is published)
and `withdrawn` (what was, with each asset's file, sha256 and layer). The
release assets themselves are deleted by the owner with
tools/delete_withdrawn_assets.py, which deletes only ids on the list
deletion_list() generates from that ledger and the release's own listing.

The hashes are the 14 assets the `satellite` release held on 9 Oct 2026: the
12 indexed tiers and two older untiered packs (East Anglia, Midlands).
"""
import argparse
import json
import os
import re
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))

# The ONLY imagery layers that may be fetched, built or published. Matched
# exactly: no case folding, no trimming, no prefix.
ALLOWED_LAYERS = frozenset({"s2cloudless_3857"})

# sha256 of every 2024 asset in the `satellite` release on 9 Oct 2026.
WITHDRAWN_SHA256 = frozenset({
    "17c5c1beaf70b0c3fb6b361ca7905171155c08cf8ae3f4dc92d1a58967fe562b",
    "6673677db5ee0f4f4e33dd218d0f3cfe0d2a5fdb7375c8f5e96eb32fdaed7fe3",
    "58bc66847f6df7e264a6d71828d6252985ab514dd2e2be35c628c482f209c9c8",
    "a7f7fa09514f44f8110d118d47549d47902056b335b84c45409ee62c8ad13e44",
    "d3224686c0eda4f507e92878d0526cbe38d05ce80705aad21ab28900edb9218f",
    "deb8c911254a3916612064c65917eacf5255d5151c2a3fb0c1356bc3b72e19a7",
    "4af55d52fda1c515663fed3de62a2b8992e536f05f4055fa9ec9a889071db245",
    "dcc22402009a847ba93e92aae0e78574275c8a38f75d21b63203e53516beb680",
    "b3e15da5c3b50ad997bad5c77e643d76cdbee9f835818098a515159c1a225b54",
    "64757e2a949d59529d9f1f8fdf76462cbba190112b07a47ac6aa934ee683860f",
    "8d6022e3379d603521666d108899ca1e2cfa723179ebc26d0981c0c5ed28ccf3",
    "f7e9f30d078f54b64651ce4fb31c04a1611ea6a2dfeb1ec1bee6ab4851408d3c",
    "6a072c001332e7b65408da86f15fce4e89a3d7479abd32eafba94250452a11b4",
    "6f3f6100e33cff2ad7bcbf1c10fd48373967835872d3c6103c1fb27ab33e43b7",
})

# The 2016 mosaic's data years are 2016 and 2017. A note or attribution that
# names any later year describes a NonCommercial mosaic.
LATER_YEAR = re.compile(r"\b20(?:1[89]|[2-9]\d)\b")

# The release every imagery pack is served from.
SATELLITE_RELEASE = "satellite"


def layer_refused(layer):
    """Why `layer` may not be used, or None if it is on the allowlist."""
    if isinstance(layer, str) and layer in ALLOWED_LAYERS:
        return None
    return ("imagery layer %r is not on the allowlist %s"
            % (layer, sorted(ALLOWED_LAYERS)))


def layer_of_source(source):
    """The WMTS layer a tile URL asks for, or None if it names none."""
    m = re.search(r"/wmts/1\.0\.0/([^/]+)/", source or "")
    return m.group(1) if m else None


def source_refused(source, attribution=""):
    """Why this imagery source may not be built, or None if it may."""
    why = layer_refused(layer_of_source(source))
    if why:
        return "%s refused: %s" % (source, why)
    if LATER_YEAR.search(attribution or ""):
        return "the attribution names a NonCommercial year: %s" % attribution
    return None


def _pack_problems(pack, where):
    """Why one index pack or fresh entry may not be published."""
    out = []
    name = "%s: %s" % (where, pack.get("id", "?"))
    if pack.get("sha256") in WITHDRAWN_SHA256:
        out.append("%s is withdrawn imagery (sha256 %s)"
                   % (name, pack.get("sha256")))
    if LATER_YEAR.search(str(pack.get("note", ""))):
        out.append("%s carries a NonCommercial note: %s"
                   % (name, pack.get("note")))
    why = layer_refused(pack.get("layer"))
    if why:
        out.append("%s: %s" % (name, why))
    return out


def catalogue_packs(catalogue):
    for continent in catalogue.get("continents", []):
        for country in continent.get("countries", []):
            for area in country.get("areas", []):
                for pack in area.get("packs", []):
                    yield pack
    for pack in catalogue.get("overviews", []) or []:
        yield pack


def _is_imagery(pack):
    return (pack.get("kind") == "basemap"
            or "/releases/download/%s/" % SATELLITE_RELEASE
            in str(pack.get("file", "")))


def publish_problems(index=None, catalogue=None, entries=None):
    """Every reason the index, catalogue or fresh entries may not be published.

    An empty list means they may. `index` is satellite/index.json, `catalogue`
    a catalogue about to be committed, `entries` what build_satellite.py just
    wrote. A catalogue is only checked against an index: a catalogue imagery
    pack is published only if the index lists it with the same sha256 on an
    allowed layer.
    """
    out = []
    for entry in entries or []:
        out.extend(_pack_problems(entry, "entry"))
    vouched = {}
    if index is not None:
        for pack in index.get("packs", []):
            out.extend(_pack_problems(pack, "index"))
            if not layer_refused(pack.get("layer")):
                vouched[pack.get("id")] = pack.get("sha256")
    if catalogue is not None:
        if index is None:
            out.append("catalogue: cannot be checked without the index")
        for pack in catalogue_packs(catalogue):
            name = "catalogue: %s" % pack.get("id", "?")
            if pack.get("sha256") in WITHDRAWN_SHA256:
                out.append("%s is withdrawn imagery (sha256 %s)"
                           % (name, pack.get("sha256")))
            if not _is_imagery(pack):
                # Lanes, heights, names: their notes carry their own years.
                continue
            if LATER_YEAR.search(str(pack.get("note", ""))):
                out.append("%s carries a NonCommercial note: %s"
                           % (name, pack.get("note")))
            if (
                    pack.get("sha256") is None
                    or vouched.get(pack.get("id")) != pack.get("sha256")):
                out.append("%s is imagery the index does not list with the "
                           "same sha256 on an allowed layer" % name)
    return out


def deletion_list(index, release):
    """The release assets that are withdrawn imagery, and nothing else.

    `index` is satellite/index.json, whose `withdrawn` ledger records each
    withdrawn asset's file, sha256 and layer. `release` is GitHub's listing of
    the `satellite` release (`gh api repos/.../releases/tags/satellite`),
    whose assets carry an id, a name and a `sha256:` digest.

    An asset is listed only when ONE ledger record vouches for both its name
    (the record's file) and its bytes (the record's sha256), that record's
    layer is recorded and off the allowlist, the sha256 is one of the 14
    known withdrawn ones, and the index does not still publish those bytes.
    A 2016 pack uploaded under a 2024 pack's name has other bytes and a new
    id, so it can never be listed. Names alone never select anything.
    """
    if release.get("tag_name") != SATELLITE_RELEASE:
        raise ValueError("not the %r release: %r"
                         % (SATELLITE_RELEASE, release.get("tag_name")))
    live = {p.get("sha256") for p in index.get("packs", [])}
    vouch = {}
    for rec in index.get("withdrawn", []):
        sha = rec.get("sha256")
        layer = rec.get("layer")
        if (sha in WITHDRAWN_SHA256 and sha not in live
                and isinstance(layer, str) and layer
                and layer_refused(layer)):
            name = urlparse(str(rec.get("file", ""))).path.rsplit("/", 1)[-1]
            vouch[(name, sha)] = layer
    out = []
    for asset in release.get("assets", []):
        digest = str(asset.get("digest") or "")
        algo, _, sha = digest.partition(":")
        if algo != "sha256":
            continue
        layer = vouch.get((asset.get("name"), sha))
        if layer is None or not isinstance(asset.get("id"), int):
            continue
        out.append({"id": asset["id"], "name": asset["name"],
                    "sha256": sha, "layer": layer})
    out.sort(key=lambda d: d["id"])
    return out


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-check", action="store_true",
                    help="refuse if build_satellite.py's SOURCE or LAYER is "
                         "off the allowlist")
    ap.add_argument("--entry", help="the entry build_satellite.py wrote, "
                                    "checked before it is uploaded")
    ap.add_argument("--index", help="satellite/index.json to check")
    ap.add_argument("--catalogue", help="a catalogue about to be committed; "
                                        "needs --index")
    args = ap.parse_args(argv)
    if not (args.source_check or args.entry or args.index or args.catalogue):
        ap.print_usage(sys.stderr)
        print("nothing to check", file=sys.stderr)
        return 2

    problems = []
    if args.source_check:
        sys.path.insert(0, HERE)
        import build_satellite
        why = (layer_refused(build_satellite.LAYER)
               or source_refused(build_satellite.SOURCE,
                                 build_satellite.ATTRIBUTION))
        if why:
            problems.append("refusing to build imagery: " + why)
    try:
        entries = index = catalogue = None
        if args.entry:
            entries = _load(args.entry)
            if not isinstance(entries, list):
                entries = [entries]
        if args.index:
            index = _load(args.index)
        if args.catalogue:
            catalogue = _load(args.catalogue)
    except (OSError, ValueError) as e:
        problems.append("cannot read what is to be checked: %s" % e)
    else:
        problems.extend(publish_problems(index, catalogue, entries))

    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print("imagery: only the allowed layer, no withdrawn asset")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

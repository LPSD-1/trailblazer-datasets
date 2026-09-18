#!/usr/bin/env python3
"""Checks on the imagery tiers, run by hand and by the satellite workflow.

    python tools/test_build_satellite.py

WHAT THIS IS REALLY GUARDING
----------------------------
One number in this repository decides how big a download every rider gets by
default, and it is never shown to anybody.

The app offers imagery at more than one detail level and remembers which the
rider picked. Until they pick, `effectiveImageryDetailProvider` takes the
COARSEST - smallest, and the one every area has - and it works out which that
is by sorting the published tiers on `groundMetresPerPixel` and taking the
last. So the ordering of two numbers in `build_satellite.py`, numbers that
appear on no screen, is what stands between "everything for this county" being
about 100 MB and being about 460 MB.

The app has its own test that the coarsest wins. It builds its own fixtures, so
it proves the mechanism and cannot see these values at all. This is the other
end of that coupling - the same shape as the gazetteer's folding, where one
side indexed and the other queried and only a test across both would have
noticed.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "build_satellite", os.path.join(HERE, "build_satellite.py"))
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def main():
    tiers = bs.TIERS
    check(len(tiers) >= 2, "there is only one tier; there is no choice to make")

    # Coarsest first in this file, and the app sorts finest first. Neither
    # order is wrong; what matters is that they disagree consistently.
    zooms = [t["zoom"] for t in tiers]
    check(zooms == sorted(zooms),
          "TIERS is not ordered by zoom, so reading it is guesswork")

    # THE ONE THAT MATTERS. More zoom is more rendered pixels, so fewer metres
    # of ground per pixel. If this were ever written the other way round, the
    # app would sort the tiers backwards, decide the SHARPEST was the coarsest,
    # and hand every rider who has expressed no preference the largest file
    # published - silently, because the number is never displayed.
    for finer, coarser in zip(tiers[1:], tiers[:-1]):
        check(
            finer["groundMetresPerPixel"] < coarser["groundMetresPerPixel"],
            "tier %r is the higher zoom but does not claim finer pixels "
            "(%s vs %s) - the app would default every rider to the big one"
            % (finer["id"], finer["groundMetresPerPixel"],
               coarser["groundMetresPerPixel"]))

    # And the smallest pack really is the one the app will pick. Tile counts
    # are what decides the bytes, so this asserts against the tile maths rather
    # than against the claim.
    box = (-3.25, 51.9, 0.15, 53.6)   # the Midlands
    counts = {t["id"]: len(list(bs.tiles_in(box, 0, t["zoom"]))) for t in tiers}
    default_id = max(tiers, key=lambda t: t["groundMetresPerPixel"])["id"]
    smallest_id = min(counts, key=lambda k: counts[k])
    check(default_id == smallest_id,
          "the app defaults to %r but %r is the smaller download (%s)"
          % (default_id, smallest_id, counts))

    # Every tier needs an id and a label or the app cannot remember or offer
    # it - `ImageryDetail.fromJson` drops a tier missing either, and a dropped
    # tier is one the rider can never choose.
    for t in tiers:
        check(bool(t.get("id")), "a tier has no id")
        check(bool(t.get("label")), "tier %r has no label" % t.get("id"))
        check(bool(t.get("description")),
              "tier %r has no description; the picker would show a bare label"
              % t["id"])

    ids = [t["id"] for t in tiers]
    check(len(set(ids)) == len(ids), "two tiers share an id: %s" % ids)

    # THE PLANNER MUST RECOGNISE ITS OWN OUTPUT. One area now publishes one
    # pack per tier, with the tier in the id, and the planner asks about the
    # area. Keyed on the pack id it found neither, concluded the area had never
    # been built, and would have re-fetched it every run for ever against a
    # free service.
    plan = importlib.util.spec_from_file_location(
        "satellite_plan", os.path.join(HERE, "satellite_plan.py"))
    sp = importlib.util.module_from_spec(plan)
    plan.loader.exec_module(sp)

    built = {
        "continents": [{"countries": [{"areas": [{
            "packs": [
                {"kind": "basemap", "id": "gb-south-east-satellite-standard",
                 "generated": "2026-09-12T21:21:00Z"},
                {"kind": "basemap", "id": "gb-south-east-satellite-high",
                 "generated": "2026-09-12T21:21:00Z"},
            ]}]}]}],
    }
    seen = sp.existing_satellite(built)
    check("gb-south-east-satellite" in seen,
          "the planner cannot see its own tiered packs: %s" % sorted(seen))

    # And the shape published before tiers existed still counts.
    old = {"continents": [{"countries": [{"areas": [{"packs": [
        {"kind": "basemap", "id": "gb-midlands-satellite",
         "generated": "2026-09-12T19:06:35Z"}]}]}]}]}
    check("gb-midlands-satellite" in sp.existing_satellite(old),
          "an untiered pack published earlier stopped counting")

    if failures:
        for f in failures:
            print("FAIL: %s" % f, file=sys.stderr)
        return 1

    print("build_satellite: %d tiers, %s" % (
        len(tiers), ", ".join("%s z0-%d (%s tiles)"
                              % (t["id"], t["zoom"], f"{counts[t['id']]:,}")
                              for t in tiers)))
    print("  default tier is %r, which is the smaller download" % default_id)
    print("build_satellite: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# --- the root directory limit ------------------------------------------------
#
# SIX OF TEN PUBLISHED IMAGERY PACKS COULD NOT BE OPENED, and nothing said so.
# They downloaded, they matched their sha256, they took up 1.1 GB on the phone,
# and the app refused every one of them with "CorruptArchive: Root directory is
# out of bounds" - the reader enforcing the PMTiles v3 rule that the header and
# the whole root directory must fit in the first 16,384 bytes.
#
# The builder put every entry in the root and wrote leaf_length as 0, so the
# root grew with the tile count. The bias is the cruel part: the bigger the
# pack, the more certain it was to fail, so every high-detail pack and the
# whole of the North were dead while the four smallest worked.

def _entries(n):
    """`n` entries that cannot be run-length collapsed into fewer."""
    # Non-consecutive ids and varying lengths, so the directory is genuinely
    # large rather than compressing down to nothing.
    return [(i * 7, i * 1000, 500 + (i % 97), 1) for i in range(n)]


check_true(
    "a small archive keeps everything in the root",
    build_satellite.build_directories(_entries(50))[1] == b"",
)

_root, _leaves, _count = build_satellite.build_directories(_entries(200000))
check_true(
    "a big archive spills into leaves",
    _leaves != b"" and _count > 1,
)
check_true(
    "and the root then fits the spec's 16,384 bytes",
    build_satellite.HEADER_LENGTH + len(_root) <= build_satellite.ROOT_LIMIT,
)

# The check that would have caught it: EVERY pack this repo publishes, measured
# against the limit a reader will actually apply.
for _n in (1000, 50000, 143637, 400000):
    _r, _l, _c = build_satellite.build_directories(_entries(_n))
    check_true(
        "%d entries produce a conformant root" % _n,
        build_satellite.HEADER_LENGTH + len(_r) <= build_satellite.ROOT_LIMIT,
    )

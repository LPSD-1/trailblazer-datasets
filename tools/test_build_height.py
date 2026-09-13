#!/usr/bin/env python3
"""Checks on the height packs, run by hand and by the height workflow.

    python tools/test_build_height.py

WHAT THIS IS REALLY GUARDING
----------------------------
Two things, and both of them are silent failures that cost real bandwidth or
draw a wrong map.

1. THE PLANNER MUST RECOGNISE ITS OWN OUTPUT. If the id it asks the catalogue
   about is not the id the builder publishes, every area looks unbuilt for
   ever: the job re-fetches the same thousands of tiles every run, against a
   free service, and nothing anywhere reports a problem. This has already
   happened once in this repository, to the imagery planner, when detail tiers
   changed the pack ids.

   It nearly happened again here. `areas_with_lanes` is shared with the
   imagery planner, and the `id` it hands back is ALREADY the satellite pack
   id - so the obvious reading produced `gb-east-anglia-satellite-height`,
   which is nobody's pack. Caught by running it. Pinned by this.

2. THE ENCODING AND THE QUANTISER. Height is packed into colour channels, so
   an error here does not fail - it renders a landscape with the wrong shape.
   The quantiser must not move the ground by more than it claims to, and it
   must be idempotent, because a staging directory is written over several
   runs and half of it may have been written yesterday.
"""
import importlib.util
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bh = load("build_height")
hp = load("height_plan")

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def a_tile(heights):
    """A Terrarium tile whose rows step through `heights` metres."""
    from PIL import Image
    image = Image.new("RGB", (16, 16))
    px = image.load()
    for y in range(16):
        m = heights[y % len(heights)]
        v = int(round((m + 32768) * 256))
        r, g, b = (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF
        for x in range(16):
            px[x, y] = (r, g, b)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def heights_of(blob, stride=1):
    from PIL import Image
    image = Image.open(io.BytesIO(blob)).convert("RGB")
    return [(r * 256 + g + b / 256.0) - 32768
            for r, g, b in list(image.getdata())[::stride]]


def main():
    # --- the planner and the builder must agree on a name -------------------
    catalogue = {"continents": [{"countries": [{
        "label": "United Kingdom",
        "areas": [{
            "id": "gb-midlands",
            "label": "Midlands",
            "bounds": {"west": -3.25, "south": 51.9, "east": 0.15,
                       "north": 53.6},
            "packs": [{"kind": "lanes", "id": "gb-midlands-lanes"}],
        }],
    }]}]}

    # Driven through the planner's own functions rather than its command line:
    # what the workflow rests on is that these two agree, and a subprocess
    # would only prove that argparse works.
    plan_areas = hp.sp.areas_with_lanes(catalogue)
    check(len(plan_areas) == 1, "the shared area walk found %d areas, wanted 1"
          % len(plan_areas))
    if plan_areas:
        area = plan_areas[0]
        pid = hp.height_id(area["area"])
        check(pid == "gb-midlands-height",
              "the planner would ask about %r, which is not a height pack id"
              % pid)
        # And the round trip: a catalogue carrying exactly that pack must make
        # the area count as built. This is the half that was broken.
        published = {"continents": [{"countries": [{"areas": [{
            "packs": [{"kind": "height", "id": pid,
                       "generated": "2026-09-13T00:00:00Z"}]}]}]}]}
        seen = hp.existing_height(published)
        check(pid in seen,
              "the planner cannot see its own published pack: %s"
              % sorted(seen))

    # A pack of some OTHER kind must not count. Imagery and height sit in the
    # same area and are both raster `.pmtiles`; counting one as the other
    # would mean an area with satellite never gets height at all.
    only_imagery = {"continents": [{"countries": [{"areas": [{
        "packs": [{"kind": "basemap", "id": "gb-midlands-satellite-standard",
                   "generated": "2026-09-13T00:00:00Z"}]}]}]}]}
    check(not hp.existing_height(only_imagery),
          "an imagery pack was counted as a height pack")

    # --- the quantiser ------------------------------------------------------
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Pillow is not installed; skipping the encoding checks",
              file=sys.stderr)
    else:
        original = a_tile([0.0, 12.25, 12.5, 137.125, -3.75, 636.0])
        before = heights_of(original)

        half = bh.quantise(original, 0.5)
        after = heights_of(half)
        worst = max(abs(a - b) for a, b in zip(before, after))
        check(worst <= 0.5 + 1e-6,
              "half-metre steps moved the ground by %.3f m" % worst)

        whole = bh.quantise(original, 1.0)
        worst1 = max(abs(a - b) for a, b in zip(before, heights_of(whole)))
        check(worst1 <= 1.0 + 1e-6,
              "one-metre steps moved the ground by %.3f m" % worst1)

        # Smaller, or there was no reason to do any of this.
        check(len(half) < len(original),
              "quantising to 1/2 m did not make the tile smaller (%d vs %d)"
              % (len(half), len(original)))
        check(len(whole) < len(half),
              "1 m is not smaller than 1/2 m (%d vs %d)"
              % (len(whole), len(half)))

        # IDEMPOTENT. A staging directory is filled over several runs, and a
        # tile quantised yesterday must be byte-identical to the same tile
        # quantised today or a rebuild silently republishes a changed pack.
        check(bh.quantise(half, 0.5) == half,
              "quantising twice does not give the same bytes")

        # Rounding DOWN, never up past the true height: a DEM that reads high
        # is the direction that matters when somebody is judging a climb.
        check(all(a >= b - 1e-9 for a, b in zip(before, after)),
              "quantising rounded some heights UP")

        # And the sanity check the builder refuses to package without.
        #
        # `stride=1` here because this fixture is 16x16 and its rows repeat
        # every 6, which aliases exactly against the default stride of 97:
        # every sampled pixel landed on the same row and the range came back
        # 0..0. That is a property of a tiny synthetic tile, not of a real
        # 256x256 one, where 97 still samples 676 pixels spread over the
        # whole image - and the check it feeds only has to tell ground from
        # an error page, not find the true summit.
        lo, hi = bh.height_range(original, stride=1)
        check(lo is not None and -10 < lo < 1 and 600 < hi < 700,
              "height_range read %s..%s from a tile of known ground"
              % (lo, hi))

    # --- the attribution is not optional ------------------------------------
    check("Environment Agency" in bh.ATTRIBUTION,
          "the required attribution has been edited out")
    check(bh.ENCODING == "terrarium",
          "the encoding no longer matches what the app is told to decode with")
    check(bh.TILE_TYPE_PNG == 2,
          "the PMTiles tile type is not PNG; Terrarium cannot survive JPEG")

    if failures:
        for f in failures:
            print("FAIL: %s" % f, file=sys.stderr)
        return 1
    print("build_height: planner ids round-trip, quantiser is honest and "
          "idempotent, attribution intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Cut the same patch of ground at each level of detail, for the picker.

    python sample_imagery.py --lat 52.12 --lon 1.40 --out ../satellite/samples

WHY THIS EXISTS
---------------
"10 metres per pixel" means nothing to anybody. A rider deciding whether a
four-times-larger download is worth the space on their phone can settle it in a
second by looking at the same field twice, and the app's imagery picker shows
exactly that: one photograph per level, of the same ground.

It is honest in the direction that matters. Sentinel-2 is 10 m/pixel and at
British latitudes zoom 13 IS that resolution, so zoom 14 is 2x oversampled —
four times the bytes for no new information. These samples show that plainly,
which is a better argument than any sentence about metres per pixel and stops
anybody going looking for a sharper version that does not exist.

POLITE BY DESIGN
----------------
Five tile requests per sample: one at z13 and the four at z14 that cover the
same ground. The tiles come from build_satellite.SOURCE, so the plates are
the same mosaic the packs are: EOxCloudless 2016, the year EOX license
CC BY 4.0 (2018 onwards is NonCommercial; see build_satellite.py). That
licence covers redistributing the imagery and does not entitle anyone to
hammer EOX's free tile service; a handful of tiles to build a comparison
picture is well inside what that service is for.
"""

import argparse
import hashlib
import importlib.util
import io
import json
import math
import os
import sys
import time
import urllib.request

try:
    from PIL import Image, ImageFilter
except ImportError:  # pragma: no cover - a clearer message than a stack trace
    print("Pillow is needed: python -m pip install Pillow", file=sys.stderr)
    raise SystemExit(2)

# The packs' own source, so a plate can never show a different (or
# differently licensed) mosaic year from the imagery it is a sample of.
_spec = importlib.util.spec_from_file_location(
    "build_satellite",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "build_satellite.py"))
_bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bs)
SOURCE = _bs.SOURCE

# Every plate this makes, and so every plate satellite/samples may hold:
# tools/test_imagery_licence.py checks the folder against this list and
# against the index written beside it.
SAMPLE_NAMES = ("standard", "standard-sharpened", "detailed",
                "zoom-standard", "zoom-sharpened", "zoom-detailed")
TILE = 256


def tile_of(lat, lon, z):
    """The XYZ tile containing a point."""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n)
    return x, y


def ground_metres_per_pixel(lat, z):
    """What one screen pixel covers on the ground, at this latitude."""
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z) / (TILE / 256)


# The packs' own fetcher: the same User-Agent and rate, no redirect followed,
# and a refusal from EOX stops the run rather than being retried around.
FETCHER = _bs.Fetcher(sharpen=False)

# A refusal recorded in satellite/blocks.json holds here as it holds the
# nightly build: this checkout's copy and main's latest (fetched now) are both
# read, and if main's cannot be, nothing is asked of EOX.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCKS = os.path.join(ROOT, "satellite", "blocks.json")
GIT_RUN = None   # how git is run; None is the planner's own (subprocess)


def refused_by_block():
    """Why no tile may be asked for now, or None if they may."""
    plan = _bs._planner()
    try:
        blocks = plan.live_blocks(BLOCKS, "origin/main", run=GIT_RUN,
                                  cwd=ROOT)["hosts"]
    except (OSError, ValueError) as e:
        return "cannot read satellite/blocks.json on main (%s)" % e
    host = plan.block_host()
    if host in blocks:
        import datetime as dt
        until = plan.blocked_until(blocks[host])
        if until > dt.datetime.now(dt.timezone.utc):
            return "%s refused us, and is not asked again until %s" % (
                host, "a person mends its record" if until == plan.NEVER
                else plan.stamp(until))
    return None


def fetch(z, x, y):
    body = FETCHER.get(z, x, y)
    if body is None:
        raise SystemExit(f"could not fetch z{z}/{x}/{y}: "
                         f"{FETCHER.stopped or FETCHER.failed[-1:]}")
    return Image.open(io.BytesIO(body)).convert("RGB")


def mosaic(z, x0, y0, across):
    """`across` x `across` tiles, starting at (x0, y0), as one image."""
    out = Image.new("RGB", (TILE * across, TILE * across))
    for dx in range(across):
        for dy in range(across):
            out.paste(fetch(z, x0 + dx, y0 + dy), (dx * TILE, dy * TILE))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--out", required=True, help="directory for the samples")
    ap.add_argument("--size", type=int, default=512,
                    help="pixel size of each sample (default 512)")
    args = ap.parse_args()

    why = refused_by_block()
    if why:
        print("REFUSED: %s. Nothing fetched." % why, file=sys.stderr)
        return _bs.BLOCKED_EXIT

    os.makedirs(args.out, exist_ok=True)

    # One z13 tile is the ground every sample covers. The four z14 tiles under
    # it cover exactly the same ground, so the comparison is like for like.
    x13, y13 = tile_of(args.lat, args.lon, 13)
    base = mosaic(13, x13, y13, 1)
    finer = mosaic(14, x13 * 2, y13 * 2, 2)

    written = write_plates(args.out, plates(base, finer, args.size), {
        "lat": args.lat, "lon": args.lon, "z13_tile": [x13, y13]})

    print(f"Same ground in every one: z13 tile {x13},{y13} at "
          f"{args.lat},{args.lon}")
    print(f"  z13 is {ground_metres_per_pixel(args.lat, 13):.1f} m per pixel "
          f"(Sentinel-2 is 10 m, so this is its native resolution)")
    print(f"  z14 is {ground_metres_per_pixel(args.lat, 14):.1f} m per pixel "
          f"(oversampled: no new information, four times the tiles)")
    for name, path, size in written:
        print(f"  {name:20s} {size / 1024:6.1f} KB  {path}")


def _centre(img):
    """The centre quarter of the ground: half the width, half the height."""
    w, h = img.size
    return img.crop((w // 4, h // 4, w - w // 4, h - h // 4))


def plates(base, finer, size):
    """name -> image for every plate in SAMPLE_NAMES.

    `base` is the z13 tile, `finer` the four z14 tiles under it as one image.
    All are rendered at the SAME size, which is how they will be compared.
    Upscaling the coarse one is not cheating: it is what the map does when a
    rider zooms past the level a pack holds. The zoom plates are the centre
    quarter of the same ground blown up - one lane rather than one county.
    """
    def sharpen(img):
        return img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=60,
                                                  threshold=3))

    def fit(img):
        return img.resize((size, size), Image.LANCZOS)

    plain = fit(base)
    zoomed = fit(_centre(base))
    return {
        "standard": plain,
        "standard-sharpened": sharpen(plain),
        "detailed": fit(finer),
        "zoom-standard": zoomed,
        "zoom-sharpened": sharpen(zoomed),
        "zoom-detailed": fit(_centre(finer)),
    }


def write_plates(out, made, where=None):
    """Write each plate and an index.json saying which layer they came from,
    with each file's sha256, so a plate cut from any other mosaic year shows.
    Returns [(name, path, bytes)]."""
    os.makedirs(out, exist_ok=True)
    written, files = [], {}
    for name in SAMPLE_NAMES:
        path = os.path.join(out, f"{name}.jpg")
        made[name].save(path, format="JPEG", quality=85, optimize=True,
                        progressive=True)
        with open(path, "rb") as f:
            files[f"{name}.jpg"] = hashlib.sha256(f.read()).hexdigest()
        written.append((name, path, os.path.getsize(path)))
    index = {"layer": _bs.LAYER, "source": SOURCE,
             "attribution": _bs.ATTRIBUTION}
    index.update(where or {})
    index["files"] = files
    with open(os.path.join(out, "index.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(index, f, indent=2, sort_keys=True)
        f.write("\n")
    return written


if __name__ == "__main__":
    sys.exit(main())

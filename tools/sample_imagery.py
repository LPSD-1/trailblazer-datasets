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
same ground. The CC BY licence covers the DATA and does not entitle anyone to
hammer EOX's free tile service; a handful of tiles to build a comparison
picture is well inside what that service is for.
"""

import argparse
import io
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

SOURCE = ("https://tiles.maps.eox.at/wmts/1.0.0/"
          "s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg")
USER_AGENT = "trailblazer-offline-maps dataset builder (contact: the repo owner)"
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


def fetch(z, x, y, retries=4):
    url = SOURCE.format(z=z, x=x, y=y)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read()
            if not body:
                raise ValueError("empty body")
            return Image.open(io.BytesIO(body)).convert("RGB")
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise SystemExit(f"could not fetch z{z}/{x}/{y}: {e}")
            time.sleep(1.5 * (attempt + 1))


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

    os.makedirs(args.out, exist_ok=True)

    # One z13 tile is the ground every sample covers. The four z14 tiles under
    # it cover exactly the same ground, so the comparison is like for like.
    x13, y13 = tile_of(args.lat, args.lon, 13)
    base = mosaic(13, x13, y13, 1)
    finer = mosaic(14, x13 * 2, y13 * 2, 2)

    # Rendered at the SAME size on screen, which is how they will be compared.
    # Upscaling the coarse one is not cheating: it is what the map does when a
    # rider zooms past the level a pack holds, so this is the honest picture of
    # what they would actually see.
    plain = base.resize((args.size, args.size), Image.LANCZOS)
    sharp = plain.filter(ImageFilter.UnsharpMask(radius=1.0, percent=60,
                                                 threshold=3))
    detail = finer.resize((args.size, args.size), Image.LANCZOS)

    written = []
    for name, img in (("standard", plain), ("standard-sharpened", sharp),
                      ("detailed", detail)):
        path = os.path.join(args.out, f"{name}.jpg")
        img.save(path, format="JPEG", quality=85, optimize=True,
                 progressive=True)
        written.append((name, path, os.path.getsize(path)))

    print(f"Same ground in every one: z13 tile {x13},{y13} at "
          f"{args.lat},{args.lon}")
    print(f"  z13 is {ground_metres_per_pixel(args.lat, 13):.1f} m per pixel "
          f"(Sentinel-2 is 10 m, so this is its native resolution)")
    print(f"  z14 is {ground_metres_per_pixel(args.lat, 14):.1f} m per pixel "
          f"(oversampled: no new information, four times the tiles)")
    for name, path, size in written:
        print(f"  {name:20s} {size / 1024:6.1f} KB  {path}")


if __name__ == "__main__":
    main()

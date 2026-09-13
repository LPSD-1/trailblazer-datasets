#!/usr/bin/env python3
"""Build a ground-height pack: shaded relief and 3D ground, carried offline.

    python build_height.py --bbox -3.25 51.9 0.15 53.6 \
        --id gb-midlands-height --label "Ground height - Midlands" \
        --staging staging/height/midlands --budget 4000

    python build_height.py ... --staging staging/height/midlands \
        --package --out dist/packages/gb-midlands-height.pmtiles

Produces a PMTiles v3 archive of Terrarium-encoded PNG tiles, which is what
MapLibre's `raster-dem` source reads, plus the catalogue entry to publish.

WHY THIS IS THE SAME MACHINERY AS THE IMAGERY
---------------------------------------------
A DEM tile IS a raster tile. Height is packed into the colour channels -
Terrarium is `(R * 256 + G + B / 256) - 32768` metres - so this is the same
`.pmtiles` container, the same loopback server on the phone, the same download
queue, the same SHA-256 and the same Update button as satellite. The PMTiles
writer and the slippy-map arithmetic are IMPORTED from `build_satellite`
rather than written twice: two tilers is how a builder and an app come to
disagree about which tiles a pack contains.

Only two things differ, and both are real: the tiles are PNG rather than JPEG
(Terrarium cannot survive a lossy codec - the low bits ARE the height), and
they are re-encoded on the way in. See below.

WHERE THE HEIGHTS COME FROM
---------------------------
The AWS Open Data "Terrain Tiles" set, which is free, needs no key, and is
already tiled and Terrarium-encoded in Web Mercator - so no GDAL, no British
National Grid reprojection, and no unpacking a couple of hundred megabytes of
ASCII grids.

For the United Kingdom the underlying data is the ENVIRONMENT AGENCY's, which
is the best free height data for this country and far better than the 30 m
global sets. ATTRIBUTION IS REQUIRED, travels with the pack in the archive's
own metadata, and is shown by the app. The wording in `ATTRIBUTION` below is
the provider's own, from tilezen/joerd's `docs/attribution.md`; it is a licence
term, not a credit line to tidy up.

WHY THE FRACTIONAL METRES ARE THROWN AWAY
-----------------------------------------
Terrarium's blue channel is the fraction of a metre, in 1/256ths. Over natural
ground that is noise, and noise is the one thing PNG cannot compress. Measured
over seven tiles spread across Britain - fell, peak, fen, mid-Wales, Dartmoor,
the Weald, the Highlands - at zoom 12:

    as published                105,838 bytes/tile
    quantised to 1/2 m           30,522 bytes/tile     3.5x smaller
    quantised to 1 m             20,858 bytes/tile     5.1x smaller

Which decides whether this ships at all. At full precision The North is 353 MB,
which is imagery-sized for data nobody looks at directly; at 1/2 m it is 102 MB
and at 1 m it is 70 MB.

1/2 m is the default, and the thing being traded is not accuracy but TERRACING.
Hillshade is computed from the slope between neighbouring pixels, so quantising
height quantises slope, and on genuinely flat ground - the Fens, the Somerset
Levels - coarse steps can draw contour-like terraces that are an artefact of
the encoding rather than a feature of the ground. At 1/2 m over a 38 m pixel
the smallest slope that survives is about 1.3%, below anything a rider would
call a hill. `--vertical-step 1` is there for anyone who would rather have the
smaller pack.

WHY ZOOM 12
-----------
z12 is 38 m per pixel at British latitudes. MapLibre overzooms a `raster-dem`
happily - relief is smooth, so a z12 DEM shades a z15 map perfectly well - and
z13 would quadruple every figure above for a difference nobody can see under a
hillshade.

MEASURED PACK SIZES at z0-12, 1/2 m steps:

    East Anglia      706 tiles     22 MB
    Midlands       1,789 tiles     55 MB
    South East     1,827 tiles     56 MB
    South West     2,397 tiles     73 MB
    The North      3,340 tiles    102 MB
    Wales          1,896 tiles     58 MB
    All Britain   36,326 tiles  1,109 MB

A NOTE ON PULLING THE TILES
---------------------------
The same courtesy the imagery builder extends, for the same reason: a free
public service, fetched a budget at a time into a staging directory that
survives between runs. Nothing is packaged until every tile is present, so a
pack never ships with holes in it.
"""
import argparse
import hashlib
import importlib.util
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image
except ImportError:
    print("This needs Pillow:  pip install Pillow", file=sys.stderr)
    raise

HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location(
    "build_satellite", os.path.join(HERE, "build_satellite.py"))
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)

SOURCE = ("https://s3.amazonaws.com/elevation-tiles-prod/"
          "terrarium/{z}/{x}/{y}.png")

ATTRIBUTION = ("United Kingdom terrain data (c) Environment Agency copyright "
               "and/or database right 2015. All rights reserved. "
               "Terrain Tiles hosted by AWS Open Data.")

# What MapLibre must be told to decode the pixels with. Written into the
# archive rather than assumed at the reading end: getting it wrong does not
# fail, it renders a landscape with the wrong shape and no error anywhere.
ENCODING = "terrarium"

# PMTiles v3 tile type. 2 is PNG.
TILE_TYPE_PNG = 2

USER_AGENT = ("trailblazer-offline-maps dataset builder "
              "(contact: the repo owner)")

DEFAULT_ZOOM = 12
DEFAULT_VERTICAL_STEP = 0.5


def staged_path(staging, z, x, y):
    """Where a fetched tile lives between runs.

    `.png`, and its own function rather than build_satellite's, which hardcodes
    `.jpg`. Sharing that one would have written PNG bytes into files named
    `.jpg` - which works, right up until somebody looks in the directory.
    """
    return os.path.join(staging, str(z), str(x), "%d.png" % y)


def already_staged(staging, z, x, y):
    path = staged_path(staging, z, x, y)
    # Zero length means an earlier run left a stub. Treated as missing so it is
    # fetched again rather than baked into the pack as a hole.
    return os.path.exists(path) and os.path.getsize(path) > 0


def stage(staging, z, x, y, blob):
    path = staged_path(staging, z, x, y)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)


def quantise(blob, step_metres):
    """Re-encode a Terrarium tile with a coarser vertical step.

    Red and green - whole metres - are untouched. Only blue, the fraction of a
    metre, is rounded to a multiple of `step`. The result is still an ordinary
    Terrarium tile and needs no flag at the reading end.

    Rounded DOWN rather than to nearest, so the operation is idempotent: a
    staging directory half-written by an earlier run at the same step is not
    subtly different from one written today.
    """
    if step_metres <= 0:
        return blob
    step = 256 if step_metres >= 1 else max(1, int(round(step_metres * 256)))
    if step <= 1:
        return blob

    image = Image.open(io.BytesIO(blob)).convert("RGB")
    red, green, blue = image.split()
    # A 256-entry lookup applied to the whole channel at once. Per-pixel Python
    # over 65,536 pixels a tile, times several thousand tiles, is hours of CPU;
    # this is milliseconds.
    blue = blue.point([(v // step) * step for v in range(256)])
    out = io.BytesIO()
    Image.merge("RGB", (red, green, blue)).save(out, format="PNG",
                                                optimize=True)
    return out.getvalue()


def height_range(blob, stride=97):
    """Lowest and highest metres in a tile, as a sanity check.

    Cheap, and it catches the failure that is otherwise invisible: a fetch that
    returned a perfectly valid PNG of something that is not a DEM. An error
    page rendered as an image decodes to heights nowhere near the ground.
    """
    image = Image.open(io.BytesIO(blob)).convert("RGB")
    lo = hi = None
    for r, g, b in list(image.getdata())[::stride]:
        m = (r * 256 + g + b / 256.0) - 32768
        lo = m if lo is None else min(lo, m)
        hi = m if hi is None else max(hi, m)
    return lo, hi


class Fetcher:
    """Polite, retrying, threaded. Its own rather than build_satellite's.

    That one formats a module-level URL and applies an unsharp mask on the way
    through - both right for photography and both wrong here. Sharpening a DEM
    would invent cliffs.
    """

    def __init__(self, retries=4, workers=4):
        self.retries = retries
        self.workers = workers
        self.lock = threading.Lock()
        self.failed = []

    def get(self, z, x, y):
        url = SOURCE.format(z=z, x=x, y=y)
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=40) as r:
                    body = r.read()
                if not body:
                    raise ValueError("empty body")
                return body
            except Exception as e:  # noqa: BLE001 - anything is worth a retry
                if attempt == self.retries - 1:
                    with self.lock:
                        self.failed.append((z, x, y, str(e)))
                    return None
                # Backoff, and do not stampede a free service.
                time.sleep(1.5 * (attempt + 1))
        return None

    def fetch_all(self, tiles, on_tile):
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for (z, x, y), blob in zip(
                    tiles, pool.map(lambda t: self.get(*t), tiles)):
                if blob is not None:
                    on_tile(z, x, y, blob)


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bbox", nargs=4, type=float, required=True,
                   metavar=("W", "S", "E", "N"))
    p.add_argument("--id", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--staging", required=True)
    p.add_argument("--max-zoom", type=int, default=DEFAULT_ZOOM)
    p.add_argument("--vertical-step", type=float,
                   default=DEFAULT_VERTICAL_STEP,
                   help="metres per encoded step (default 0.5); 1 is a third "
                        "smaller again at the cost of terracing on flat ground")
    p.add_argument("--budget", type=int, default=4000,
                   help="how many tiles to fetch this run")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--package", action="store_true")
    p.add_argument("--out")
    p.add_argument("--entry-out",
                   help="write the catalogue entry here, for "
                        "record_satellite.py to file into an index")
    args = p.parse_args()

    bbox = tuple(args.bbox)
    tiles = list(bs.tiles_in(bbox, 0, args.max_zoom))
    staging = args.staging
    os.makedirs(staging, exist_ok=True)

    missing = [t for t in tiles if not already_staged(staging, *t)]
    print("%s: %s tiles to z%d, %s staged, %s missing"
          % (args.id, f"{len(tiles):,}", args.max_zoom,
             f"{len(tiles) - len(missing):,}", f"{len(missing):,}"))

    if missing and not args.package:
        todo = missing[:args.budget]
        fetcher = Fetcher(workers=args.workers)
        staged = [0]

        def keep(z, x, y, blob):
            stage(staging, z, x, y, quantise(blob, args.vertical_step))
            staged[0] += 1

        fetcher.fetch_all(todo, keep)
        left = len(missing) - staged[0]
        print("staged %s this run; %s still missing"
              % (f"{staged[0]:,}", f"{left:,}"))
        if fetcher.failed:
            print("%s tiles failed after retries; they will be tried again "
                  "next run" % f"{len(fetcher.failed):,}", file=sys.stderr)
        if left > 0:
            print("Not packaging: the set is incomplete. Run again.")
            return 0

    still = [t for t in tiles if not already_staged(staging, *t)]
    if still:
        print("%s tiles still missing; not packaging." % f"{len(still):,}",
              file=sys.stderr)
        return 1 if args.package else 0

    if not args.package:
        print("Everything is staged. Run again with --package --out <path>.")
        return 0
    if not args.out:
        print("--package needs --out", file=sys.stderr)
        return 2

    # A DEM that decodes to nothing renders a flat world with no error
    # anywhere, so one real tile at the deepest zoom is checked before a
    # hundred megabytes are written.
    deepest = max(t[0] for t in tiles)
    probe = next(t for t in tiles if t[0] == deepest)
    with open(staged_path(staging, *probe), "rb") as fh:
        lo, hi = height_range(fh.read())
    if lo is None or lo < -500 or hi > 5000:
        print("A staged tile decodes to %s..%s m, which is not ground. "
              "Refusing to package." % (lo, hi), file=sys.stderr)
        return 1
    print("sample tile decodes to %.0f..%.0f m" % (lo, hi))

    blobs = {}
    for z, x, y in tiles:
        with open(staged_path(staging, z, x, y), "rb") as fh:
            blobs[bs.tile_id(z, x, y)] = fh.read()

    bs.write_pmtiles(
        args.out, blobs, bbox, 0, args.max_zoom,
        {
            "name": args.label,
            "format": "png",
            "attribution": ATTRIBUTION,
            "encoding": ENCODING,
            "vertical_step_m": args.vertical_step,
        },
        tile_type=TILE_TYPE_PNG)

    size = os.path.getsize(args.out)
    with open(args.out, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    print("wrote %s  (%.1f MB)" % (args.out, size / 1e6))

    # The shape `record_satellite.py` files into an index. Reused rather than
    # given a recorder of its own: that tool cares about `id`, `file` and
    # `bytes` and nothing about what is inside the archive, so a second copy of
    # it would only be a second place for the URL-joining rule to go wrong.
    entry = {
        "id": args.id,
        "kind": "height",
        "label": args.label,
        # Filled in by whoever uploads it, like the imagery: these are tens of
        # megabytes and cannot live beside the index on GitHub Pages.
        "file": os.path.basename(args.out),
        "sha256": digest,
        "bytes": size,
        "bounds": {"west": bbox[0], "south": bbox[1],
                   "east": bbox[2], "north": bbox[3]},
        "note": ATTRIBUTION,
        "maxZoom": args.max_zoom,
        # The app reads this to build its raster-dem source. Without it the
        # pixels are decoded by a guess, and a wrong guess draws hills in the
        # wrong places rather than failing.
        "encoding": ENCODING,
    }
    print("")
    print("catalogue entry:")
    print(json.dumps(entry, indent=2))
    if args.entry_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.entry_out)),
                    exist_ok=True)
        with open(args.entry_out, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, indent=2)
        print("entry written to %s" % args.entry_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

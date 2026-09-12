#!/usr/bin/env python3
"""Build a satellite imagery pack the app can carry offline.

A run at a time, resumable, packaged when the set is complete:

    # daily, politely
    python build_satellite.py --bbox -8.7 49.8 1.8 60.9 \
        --id gb-satellite --label "Satellite - Britain" \
        --max-zoom 13 --sharpen --staging staging/gb --budget 6000

    # monthly, when it reports everything staged
    python build_satellite.py ... --staging staging/gb \
        --package --out dist/packages/gb-satellite.pmtiles

Produces a PMTiles v3 archive of JPEG tiles, plus the catalogue entry to paste
into build_catalogue.py's pack list.

WHY SENTINEL-2 AND NOT SOMETHING SHARPER
----------------------------------------
Esri, Bing, Google and Mapbox imagery all forbid the bulk offline caching this
app is built on. None of them can go in a pack a rider carries up a moor.
Sentinel-2 can: the Copernicus data is open, and the EOX cloudless mosaic built
from it is CC BY 4.0, so it may be redistributed with attribution.

It is 10 m/pixel. At British latitudes that is exactly zoom 13, so:

  * z13 is the real resolution. Anything past it is interpolation.
  * z14 is 2x oversampled - four times the bytes for no new detail. It looks
    marginally crisper close up because an offline resampler beats the GPU's
    bilinear stretch, and that is the whole of the difference.
  * z15 is mush.

Measured tile counts and sizes (JPEG, ~18 KB/tile):

  40 km around Derby   z0-13     1,079 tiles     20 MB
                       z0-14     4,103 tiles     70 MB
  Midlands             z0-13     6,859 tiles    120 MB
                       z0-14    26,854 tiles    460 MB
  All of Britain       z0-13   143,637 tiles    2.5 GB
                       z0-14   572,403 tiles    9.8 GB

--sharpen applies an unsharp mask at build time. It costs nothing in size and
does more for how sharp the map LOOKS than upsampling to z14 does, which is why
z13 + sharpen is the default recommendation.

A NOTE ON PULLING THE TILES
---------------------------
The CC BY licence covers the DATA. It does not entitle anyone to hammer EOX's
public tile service, which is a free service run by a small company. This tool
is polite by default - few connections, retries with backoff, an honest
User-Agent - and is fine for building a sample area to look at.

So a country is built up a BUDGET AT A TIME, over as many days as it takes,
into a staging directory that survives between runs. Britain at z13 is 143,637
tiles: at 6,000 a day that is about twenty-four days, which is one release a
month with a week spare. Nothing is packaged until every tile is present, so a
pack never ships with holes in it, and a run that dies at tile 140,000 costs
one run rather than the lot.

That is a courtesy rather than a licence. If this becomes a standing job, talk
to EOX: they provide the mosaic for offline use, and the underlying Sentinel-2
L2A scenes are on AWS open data. Either is a better neighbour than a crawl
that never ends.
"""
import argparse
import gzip
import hashlib
import io
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image, ImageFilter
except ImportError:
    print("This needs Pillow:  pip install Pillow", file=sys.stderr)
    raise

# EOX Sentinel-2 cloudless. Attribution is REQUIRED by CC BY 4.0 and the app
# shows it; see ATTRIBUTION below and keep the two in step.
SOURCE = ("https://tiles.maps.eox.at/wmts/1.0.0/"
          "s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg")
ATTRIBUTION = ("Sentinel-2 cloudless 2024 by EOX IT Services GmbH, "
               "CC BY 4.0. Contains modified Copernicus Sentinel data 2024.")

USER_AGENT = "trailblazer-offline-maps dataset builder (contact: the repo owner)"


# --------------------------------------------------------------------------
# Slippy-map arithmetic. The same maths the app uses, so the counts agree.
# --------------------------------------------------------------------------
def x_of(lon, n):
    return int((lon + 180.0) / 360.0 * n)


def y_of(lat, n):
    r = math.radians(lat)
    return int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)


def tiles_in(bbox, min_zoom, max_zoom):
    west, south, east, north = bbox
    for z in range(min_zoom, max_zoom + 1):
        n = 1 << z
        for x in range(x_of(west, n), x_of(east, n) + 1):
            for y in range(y_of(north, n), y_of(south, n) + 1):
                if 0 <= x < n and 0 <= y < n:
                    yield z, x, y


def tile_id(z, x, y):
    """PMTiles v3 tile id: zoom-major, then Hilbert order within the level."""
    if z == 0:
        return 0
    acc = 0
    for lower in range(z):
        acc += (1 << lower) * (1 << lower)
    n = 1 << z
    rx = ry = 0
    d = 0
    tx, ty = x, y
    s = n // 2
    while s > 0:
        rx = 1 if (tx & s) > 0 else 0
        ry = 1 if (ty & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        # rotate
        if ry == 0:
            if rx == 1:
                tx = s - 1 - tx
                ty = s - 1 - ty
            tx, ty = ty, tx
        s //= 2
    return acc + d


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
class Fetcher:
    def __init__(self, sharpen, retries=4):
        self.sharpen = sharpen
        self.retries = retries
        self.lock = threading.Lock()
        self.done = 0
        self.failed = []

    def get(self, z, x, y):
        url = SOURCE.format(z=z, x=x, y=y)
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=40) as r:
                    body = r.read()
                if not body:
                    raise ValueError("empty body")
                return self._process(body)
            except Exception as e:  # noqa: BLE001 - any failure is worth a retry
                if attempt == self.retries - 1:
                    with self.lock:
                        self.failed.append((z, x, y, str(e)))
                    return None
                # Backoff, and do not stampede a free service.
                time.sleep(1.5 * (attempt + 1))
        return None

    def _process(self, body):
        if not self.sharpen:
            return body
        # Unsharp mask: the cheap way to make a 10 m mosaic read as crisp.
        # radius 1.0 / percent 60 is a light touch - enough to define field
        # boundaries and tree lines, not enough to put halos on everything.
        img = Image.open(io.BytesIO(body)).convert("RGB")
        img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=60, threshold=3))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=82, optimize=True, progressive=True)
        return out.getvalue()


# --------------------------------------------------------------------------
# PMTiles v3 writer
#
# Written here rather than shelling out to go-pmtiles so the build has no
# toolchain to install. The archive MUST be clustered: the reader in the app
# refuses an unclustered one outright.
# --------------------------------------------------------------------------
def varint(value, out):
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def serialise_directory(entries):
    """entries: list of (tile_id, offset, length, run_length), sorted by id."""
    out = bytearray()
    varint(len(entries), out)
    last = 0
    for tid, _, _, _ in entries:
        varint(tid - last, out)
        last = tid
    for _, _, _, run in entries:
        varint(run, out)
    for _, _, length, _ in entries:
        varint(length, out)
    # Offsets: 0 means "immediately after the previous entry", otherwise
    # offset + 1. Saves a lot of bytes on a clustered archive.
    prev_end = None
    for _, offset, length, _ in entries:
        if prev_end is not None and offset == prev_end:
            varint(0, out)
        else:
            varint(offset + 1, out)
        prev_end = offset + length
    return bytes(out)


def build_header(**f):
    h = bytearray(127)
    h[0:7] = b"PMTiles"
    h[7] = 3

    def u64(at, v):
        h[at:at + 8] = v.to_bytes(8, "little")

    def i32(at, v):
        h[at:at + 4] = int(v).to_bytes(4, "little", signed=True)

    u64(8, f["root_offset"])
    u64(16, f["root_length"])
    u64(24, f["metadata_offset"])
    u64(32, f["metadata_length"])
    u64(40, f["leaf_offset"])
    u64(48, f["leaf_length"])
    u64(56, f["data_offset"])
    u64(64, f["data_length"])
    u64(72, f["addressed"])
    u64(80, f["entries"])
    u64(88, f["contents"])
    h[96] = 1                 # clustered
    h[97] = 2                 # internal compression: gzip
    h[98] = 1                 # tile compression: none (JPEG is already that)
    h[99] = 3                 # tile type: jpeg
    h[100] = f["min_zoom"]
    h[101] = f["max_zoom"]
    i32(102, f["west"] * 1e7)
    i32(106, f["south"] * 1e7)
    i32(110, f["east"] * 1e7)
    i32(114, f["north"] * 1e7)
    h[118] = f["centre_zoom"]
    i32(119, f["centre_lon"] * 1e7)
    i32(123, f["centre_lat"] * 1e7)
    return bytes(h)


def write_pmtiles(path, tiles, bbox, min_zoom, max_zoom, metadata):
    """tiles: dict of tile_id -> jpeg bytes."""
    west, south, east, north = bbox

    # Identical tiles share one copy. Blank ocean and uniform cloud shadow
    # repeat a great deal, and a pack a rider downloads over hotel wifi should
    # not carry the same 3 KB of grey four hundred times.
    body = bytearray()
    offsets = {}
    entries = []
    addressed = 0
    for tid in sorted(tiles):
        blob = tiles[tid]
        addressed += 1
        digest = hashlib.sha256(blob).digest()
        if digest in offsets:
            offset, length = offsets[digest]
        else:
            offset, length = len(body), len(blob)
            body.extend(blob)
            offsets[digest] = (offset, length)
        # Run-length: consecutive ids pointing at the same blob collapse.
        if entries and entries[-1][1] == offset and entries[-1][2] == length \
                and entries[-1][0] + entries[-1][3] == tid:
            tid0, off0, len0, run0 = entries[-1]
            entries[-1] = (tid0, off0, len0, run0 + 1)
        else:
            entries.append((tid, offset, length, 1))

    root = gzip.compress(serialise_directory(entries), mtime=0)
    metadata_bytes = gzip.compress(
        json.dumps(metadata, separators=(",", ":")).encode("utf-8"), mtime=0)

    header_length = 127
    root_offset = header_length
    metadata_offset = root_offset + len(root)
    leaf_offset = metadata_offset + len(metadata_bytes)
    data_offset = leaf_offset

    header = build_header(
        root_offset=root_offset, root_length=len(root),
        metadata_offset=metadata_offset, metadata_length=len(metadata_bytes),
        leaf_offset=leaf_offset, leaf_length=0,
        data_offset=data_offset, data_length=len(body),
        addressed=addressed, entries=len(entries), contents=len(offsets),
        min_zoom=min_zoom, max_zoom=max_zoom,
        west=west, south=south, east=east, north=north,
        centre_zoom=min(max_zoom, 12),
        centre_lon=(west + east) / 2, centre_lat=(south + north) / 2,
    )

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(header)
        f.write(root)
        f.write(metadata_bytes)
        f.write(body)
    return len(entries), len(offsets)


# --------------------------------------------------------------------------
# Staging: the tiles fetched so far, on disk, resumable.
#
# A country at z13 is 143,637 tiles. Pulling that in one run would be both a
# rude thing to do to a free service and a single point of failure - one
# network blip at tile 140,000 and the whole thing starts again. So the fetch
# is spread over as many days as it takes, a budget at a time, and the pack is
# only written when every tile is present.
# --------------------------------------------------------------------------
def staged_path(staging, z, x, y):
    return os.path.join(staging, str(z), str(x), "%d.jpg" % y)


def already_staged(staging, z, x, y):
    path = staged_path(staging, z, x, y)
    # Zero length means a previous run left a stub. Treated as missing so it is
    # tried again rather than baked into the pack as a hole.
    return os.path.exists(path) and os.path.getsize(path) > 0


def stage(staging, z, x, y, blob):
    path = staged_path(staging, z, x, y)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)


# The detail levels one fetch is published at, coarsest first.
#
# TWO PACKS FROM ONE SET OF TILES. A z0-14 archive CONTAINS z0-13, so fetching
# once and packaging twice costs nothing but disk - and it is what makes the
# choice in the app real. `ImageryDetail` and the picker that orders tiers by
# `groundMetresPerPixel` have been in the app for a while with nothing to show,
# because no pack has ever carried a `detail` block.
#
# The DESCRIPTIONS have to stay honest, and the honest thing here is awkward:
# z14 adds no optical detail whatever. Sentinel-2 is 10 m/pixel and at British
# latitudes z13 already is that resolution. What z14 buys is RENDERED pixels -
# the phone stretching a 256px JPEG four times, against twice as many real
# pixels resampled offline - and zoomed in that is visibly better. Saying
# "sharper" without saying why would be selling a rider four times the bytes on
# a claim about detail that is not true.
#
# `groundMetresPerPixel` is the RENDERED figure, and it is never shown: the app
# uses it only to order the tiers, so a number that sorts correctly and is
# never read aloud is the right one to put there.
TIERS = [
    {
        "zoom": 13,
        "id": "standard",
        "label": "Standard",
        "description": "The whole area at the source's own resolution. "
                       "A quarter of the size.",
        "groundMetresPerPixel": 9.6,
    },
    {
        "zoom": 14,
        "id": "high",
        "label": "High detail",
        "description": "Twice the pixels when you zoom right in. The same "
                       "10 m satellite behind it - what improves is how it is "
                       "drawn, not what it can show.",
        "groundMetresPerPixel": 4.8,
    },
]


def tiers_up_to(max_zoom):
    """Every tier this fetch can be published at."""
    return [t for t in TIERS if t["zoom"] <= max_zoom]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    ap.add_argument("--id", required=True, help="pack id, and the filename stem")
    ap.add_argument("--label", required=True)
    ap.add_argument("--min-zoom", type=int, default=0)
    ap.add_argument("--max-zoom", type=int, default=13)
    ap.add_argument("--sharpen", action="store_true",
                    help="unsharp mask at build time; free, and worth more "
                         "than upsampling to z14")
    ap.add_argument("--staging", required=True,
                    help="where tiles accumulate between runs")
    ap.add_argument("--budget", type=int, default=6000,
                    help="most tiles to fetch in THIS run. Britain at z13 is "
                         "143,637 tiles, so 6000 a day is about 24 days.")
    ap.add_argument("--package", action="store_true",
                    help="write the pack, if every tile is staged")
    ap.add_argument("--out", help="required with --package")
    ap.add_argument("--entry-out",
                    help="write the catalogue entry here as well as printing "
                         "it, so automation does not have to scrape stdout")
    ap.add_argument("--workers", type=int, default=3,
                    help="keep this small; it is a free service")
    args = ap.parse_args()

    if args.package and not args.out:
        print("--package needs --out", file=sys.stderr)
        return 2

    bbox = tuple(args.bbox)
    wanted = list(tiles_in(bbox, args.min_zoom, args.max_zoom))
    missing = [t for t in wanted if not already_staged(args.staging, *t)]
    have = len(wanted) - len(missing)

    print("%s: %s tiles wanted, %s staged, %s to go" % (
        args.id, format(len(wanted), ","), format(have, ","),
        format(len(missing), ",")))

    if missing:
        batch = missing[:args.budget]
        print("fetching %s this run (budget %s, %d connections)" % (
            format(len(batch), ","), format(args.budget, ","), args.workers))
        fetcher = Fetcher(sharpen=args.sharpen)
        started = time.time()

        def work(t):
            z, x, y = t
            blob = fetcher.get(z, x, y)
            with fetcher.lock:
                fetcher.done += 1
                if fetcher.done % 200 == 0:
                    rate = fetcher.done / max(1e-9, time.time() - started)
                    print("  %s/%s  %.0f tiles/s" % (
                        format(fetcher.done, ","), format(len(batch), ","),
                        rate))
            if blob is not None:
                stage(args.staging, z, x, y, blob)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, batch))

        if fetcher.failed:
            print("%d failed this run; they stay on the list and are tried "
                  "next time. e.g. %s" % (len(fetcher.failed),
                                          fetcher.failed[:2]),
                  file=sys.stderr)

        still = len(missing) - (len(batch) - len(fetcher.failed))
        if still > 0:
            days = int(math.ceil(still / float(max(1, args.budget))))
            print("")
            print("%s left. About %d more run%s at this budget." % (
                format(still, ","), days, "" if days == 1 else "s"))
            if args.package:
                print("Not packaging: the set is not complete yet.")
            return 0

    if not args.package:
        print("")
        print("Everything is staged. Run again with --package --out ... to "
              "write the pack.")
        return 0

    print("")
    print("reading the staged tiles...")
    tiles = {}
    for z, x, y in wanted:
        with open(staged_path(args.staging, z, x, y), "rb") as f:
            tiles[tile_id(z, x, y)] = f.read()

    stem, ext = os.path.splitext(args.out)
    written = []
    for tier in tiers_up_to(args.max_zoom):
        # One tier per pack, each holding z0 up to its own ceiling. The
        # coarser one is a strict subset, so this is a filter rather than a
        # second fetch.
        subset = {
            tile_id(z, x, y): tiles[tile_id(z, x, y)]
            for (z, x, y) in wanted
            if z <= tier["zoom"]
        }
        # The only tier gets the plain filename, so an area published at one
        # level keeps the name every existing release asset already has.
        single = len(tiers_up_to(args.max_zoom)) == 1
        path = args.out if single else "%s-%s%s" % (stem, tier["id"], ext)

        entries, unique = write_pmtiles(
            path, subset, bbox, args.min_zoom, tier["zoom"],
            metadata={
                "name": "%s (%s)" % (args.label, tier["label"]),
                "format": "jpeg",
                "attribution": ATTRIBUTION,
                "type": "baselayer",
            },
        )

        size = os.path.getsize(path)
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        print("wrote %s" % path)
        print("  %s tiles, %s directory entries, %s unique blobs" % (
            format(len(subset), ","), format(entries, ","),
            format(unique, ",")))
        print("  %.1f MB   sha256 %s" % (size / 1024.0 / 1024.0, digest))

        written.append({
            "id": args.id if single else "%s-%s" % (args.id, tier["id"]),
            "kind": "basemap",
            "label": args.label,
            # Filled in by whoever uploads it. A satellite pack is hundreds of
            # megabytes and cannot live beside the index on GitHub Pages,
            # which caps a site at 1 GB - so these go to a release, addressed
            # absolutely, exactly as the routing tiles are.
            "file": os.path.basename(path),
            "sha256": digest,
            "bytes": size,
            "bounds": {"west": bbox[0], "south": bbox[1],
                       "east": bbox[2], "north": bbox[3]},
            "note": ATTRIBUTION,
            "maxZoom": tier["zoom"],
            "detail": {
                "id": tier["id"],
                "label": tier["label"],
                "description": tier["description"],
                "groundMetresPerPixel": tier["groundMetresPerPixel"],
            },
        })

    # One entry, or a list. Kept this way round so an area published at a
    # single level writes exactly what it always wrote, and nothing reading an
    # older entry file has to change.
    entry = written[0] if len(written) == 1 else written
    print("")
    print("catalogue entry:")
    print(json.dumps(entry, indent=2))
    if args.entry_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.entry_out)),
                    exist_ok=True)
        with open(args.entry_out, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2)
        print("entry written to %s" % args.entry_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

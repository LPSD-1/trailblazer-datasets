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
Sentinel-2 can, but only in the right YEAR. The Copernicus data is open, and
EOX publish a cloudless mosaic of it per year - and the years are not licensed
alike. EOX's licence page, https://cloudless.eox.at/license-non-commercial,
says "For the year 2016, EOxCloudless is licensed under the Creative Commons
Attribution 4.0 International License", and puts 2018-2025 under
CC BY-NC-SA 4.0, which NonCommercial rules out of a paid app. The 2016 layer's
abstract in EOX's WMTS capabilities
(https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml) agrees. The 2017
layer is CC BY in its abstract alone, not on the licence page, so it is not
relied on; the 2018 layer holds 2017 data and is NC all the same. It is the
LAYER that matters, not the data year.

So this builds from s2cloudless_3857, the 2016 mosaic, and redistributes it
with the attribution the licence page requires for 2016. Both sources are
quoted, dated, in docs/licences/eox-s2cloudless-2026-10-09.md. Until
9 Oct 2026 it built from the 2024 mosaic and called that CC BY 4.0, which it
is not. tools/test_imagery_licence.py holds the source to the 2016 layer.

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
The 2016 mosaic's CC BY licence covers redistributing the IMAGERY. It does
not entitle anyone to hammer EOX's public tile service, which is a free
service run by a small company. This tool is polite by default - few
connections, retries with backoff, an honest User-Agent - and is fine for
building a sample area to look at.

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
import collections
import datetime as dt
import email.utils
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

# EOxCloudless 2016: the one year EOX license CC BY 4.0 on both its licence
# page and its WMTS abstract (2018 onwards is NonCommercial; see the
# docstring). Attribution is REQUIRED by CC BY 4.0 s.3(a): EOX's own words
# for 2016, verbatim, then the licence with its link, and that we MODIFIED it
# (we resample, sharpen and recompress every tile). The app shows it. One
# constant: sample_imagery.py, make_detail_page.py and satellite.yml carry the
# same words, and tools/test_imagery_licence.py holds them to it.
LAYER = "s2cloudless_3857"
SOURCE = ("https://tiles.maps.eox.at/wmts/1.0.0/"
          + LAYER + "/default/g/{z}/{y}/{x}.jpg")
ATTRIBUTION = ("EOxCloudless https://cloudless.eox.at by EOX IT Services "
               "GmbH (Contains modified Copernicus Sentinel data 2016 & "
               "2017), CC BY 4.0 "
               "(https://creativecommons.org/licenses/by/4.0/). Resampled "
               "and sharpened by Trail Blazer.")

# EOX's tile service has no bulk-download terms, but it rate-limits: it
# answers a heavy user with HTTP errors and redirects to a "heavyload" page.
# So this tool says who it is (no email address), keeps to a stated rate, and
# treats a redirect, 401, 403, 429 or 5xx as EOX asking us to stop:
#
#   - a 401 or 403 stops the area at once, with no retry;
#   - a Retry-After over RETRY_AFTER_CAP stops it at once too: what EOX asked
#     for is recorded, never cut short to a wait we would rather make;
#   - otherwise it waits (Retry-After, else a growing backoff), and the wait
#     holds EVERY connection, not only the refused one; after REFUSED_TRIES
#     refusals for one tile the area stops.
#
# Failures below HTTP count as well: a reset, a timeout, a TLS fault, or a 200
# whose body is not a JPEG (a busy page served as success). Once a tile has
# used its tries it counts as failed, and FAILED_RUNNING failed tiles in a
# row, or more than FAILED_SHARE of the last FAILED_WINDOW, stop the area.
#
# A stopped area writes no pack and leaves the published one alone. With
# --block-out it also records the stop (time, status, Retry-After) where the
# planner reads it, so nothing asks again before it may. It never follows the
# redirect and never retries around a block.
USER_AGENT = "TrailBlazer-data/1.0 (+https://lpsd-1.github.io/trailblazer-help/)"
MAX_REQUESTS_PER_SECOND = 4.0   # across every worker; 55,000 tiles ~ 4 hours
REFUSED_TRIES = 4               # requests for one tile before the area stops
RETRY_AFTER_CAP = 600           # seconds; a longer Retry-After stops the area
STOP_AT_ONCE = (401, 403)       # "not you": no second request
FAILED_RUNNING = 20             # failed tiles in a row that stop the area
FAILED_WINDOW = 200             # the last this many tiles ...
FAILED_SHARE = 0.05             # ... of which more than this share stops it
BLOCKED_EXIT = 3                # main()'s exit code when EOX refused us


def _is_refusal(code):
    return 300 <= code < 400 or code in (401, 403, 429) or code >= 500


def _is_jpeg(body):
    return body[:3] == b"\xff\xd8\xff"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect from the tile service is a refusal, not a new address.
    Returning None makes urllib raise the 3xx as an HTTPError."""

    def redirect_request(self, *args, **kwargs):
        return None


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
    def __init__(self, sharpen, retries=REFUSED_TRIES, opener=None,
                 sleep=time.sleep, clock=time.monotonic,
                 rate=MAX_REQUESTS_PER_SECOND, source=SOURCE,
                 wall=time.time):
        self.sharpen = sharpen
        self.retries = retries
        self.opener = opener or urllib.request.build_opener(_NoRedirect)
        self.sleep, self.clock, self.wall = sleep, clock, wall
        self.interval = 1.0 / rate
        self.source = source
        self.lock = threading.Lock()
        self.done = 0
        self.failed = []
        # Why the run stopped, once EOX has refused us; None until then.
        self.stopped = None
        # What to remember about the stop: {"status", "retry_after",
        # "reason"}. status is the HTTP code, or "network".
        self.block = None
        self._next_start = 0.0
        self._recent = collections.deque(maxlen=FAILED_WINDOW)
        self._running = 0

    def _pace(self):
        """Hold this request until its slot: starts are `interval` apart
        across every worker, so the rate is the stated one however many
        connections there are."""
        with self.lock:
            now = self.clock()
            start = max(now, self._next_start)
            self._next_start = start + self.interval
        if start > now:
            self.sleep(start - now)

    def _hold(self, seconds):
        """No connection starts a request for `seconds`: a refusal is about
        us, not about the one connection that heard it."""
        with self.lock:
            self._next_start = max(self._next_start, self.clock() + seconds)

    def _retry_after(self, error):
        """Seconds EOX asked us to wait, in either of Retry-After's forms
        (seconds, or an HTTP date); None when it did not say."""
        try:
            value = error.headers.get("Retry-After")
        except AttributeError:
            return None
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return max(0.0, when.timestamp() - self.wall())

    def _stop(self, status, retry_after, reason):
        with self.lock:
            if self.stopped is None:
                self.stopped = reason
                self.block = {"status": status, "retry_after": retry_after,
                              "reason": reason}

    def _tile_done(self, failed):
        """Count a tile's outcome; too many failures stop the area."""
        with self.lock:
            self._recent.append(failed)
            self._running = self._running + 1 if failed else 0
            running, bad = self._running, sum(self._recent)
            full = len(self._recent) == FAILED_WINDOW
        if running >= FAILED_RUNNING:
            self._stop("network", None, "%d tiles failed running" % running)
        elif full and bad > FAILED_SHARE * FAILED_WINDOW:
            self._stop("network", None, "%d of the last %d tiles failed"
                       % (bad, FAILED_WINDOW))

    def get(self, z, x, y):
        url = self.source.format(z=z, x=x, y=y)
        for attempt in range(self.retries):
            if self.stopped:
                return None
            self._pace()
            # Another connection may have been refused while this one slept
            # for its slot: then nothing more leaves.
            if self.stopped:
                return None
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with self.opener.open(req, timeout=40) as r:
                    body = r.read()
                if not _is_jpeg(body):
                    raise ValueError("not a JPEG: %r" % body[:32])
                tile = self._process(body)
            except urllib.error.HTTPError as e:
                if not _is_refusal(e.code):
                    # A 404 or other 4xx is about this tile, not about us.
                    with self.lock:
                        self.failed.append((z, x, y, "HTTP %d" % e.code))
                    return None
                asked = self._retry_after(e)
                where = "HTTP %d for z%d/%d/%d" % (e.code, z, x, y)
                if e.code in STOP_AT_ONCE:
                    self._stop(e.code, asked, where + ", which is not retried")
                    return None
                if asked is not None and asked > RETRY_AFTER_CAP:
                    self._stop(e.code, asked, where + ", asking us to wait "
                               "%d s" % asked)
                    return None
                if attempt == self.retries - 1:
                    self._stop(e.code, asked, where + ", %d times running"
                               % self.retries)
                    return None
                wait = 15.0 * (2 ** attempt) if asked is None else asked
                self._hold(max(1.0, wait))
                continue
            except Exception as e:  # noqa: BLE001 - a network fault is retried
                if attempt == self.retries - 1:
                    with self.lock:
                        self.failed.append((z, x, y, str(e)))
                    self._tile_done(True)
                    return None
                # Backoff, and do not stampede a free service.
                self.sleep(1.5 * (attempt + 1))
                continue
            self._tile_done(False)
            return tile
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


#: The most a header and root directory may occupy, in bytes.
#:
#: NOT OUR NUMBER - it is the PMTiles v3 spec's, and it is the whole reason
#: leaf directories exist. A reader is entitled to fetch the first 16,384 bytes
#: in ONE range request and expect the header and the complete root directory
#: inside them. Everything else goes in leaves the root points at.
ROOT_LIMIT = 16384

#: What the header itself takes.
HEADER_LENGTH = 127


def build_directories(entries):
    """Root and leaf directories for `entries`, per the PMTiles v3 spec.

    WHAT THIS FIXES, MEASURED. Every entry used to go in the root and
    `leaf_length` was written as 0. That is legal only while the compressed
    root fits in 16,384 bytes, and SIX OF TEN published imagery packs did not:
    the root of `gb-north-satellite-high` ended at byte 102,706. Those six were
    refused outright by the app - "CorruptArchive: Root directory is out of
    bounds" - which is the reader enforcing the spec correctly.

    The failure is silent and it is shaped like a hardware fault: the packs
    download, they pass their sha256, they sit on the phone taking up 1.1 GB,
    and the imagery simply is not there. It is also biased towards exactly the
    packs a rider most wants, because the root grows with the tile count - so
    every high-detail pack and the whole of the North failed, while the four
    smallest worked.

    Returns (root_bytes, leaf_bytes, leaf_count). `leaf_bytes` is empty when
    everything fits in the root, which is the common case for a DEM pack.
    """
    root = gzip.compress(serialise_directory(entries), mtime=0)
    if HEADER_LENGTH + len(root) <= ROOT_LIMIT:
        return root, b"", 0

    # SMALLEST LEAVES THAT STILL FIT, found by doubling from small.
    #
    # Fitting the root is not the only thing worth optimising: a leaf is read
    # WHOLE to answer one tile, so leaf size is what a rider pays on every
    # lookup that misses the cache. Halving down from "everything in one leaf"
    # fits the root on the first try and leaves 300 KB to read per tile;
    # doubling up from small finds the smallest leaves whose pointers still fit
    # in 16 KB, which is a few KB per lookup instead.
    size = 512
    while size < len(entries) * 2:
        leaves = bytearray()
        pointers = []
        for i in range(0, len(entries), size):
            chunk = entries[i:i + size]
            blob = gzip.compress(serialise_directory(chunk), mtime=0)
            # A LEAF POINTER IS AN ENTRY WITH run_length 0. That is what tells
            # a reader "this is a directory, not a tile", and its offset is
            # relative to leaf_offset rather than to data_offset.
            pointers.append((chunk[0][0], len(leaves), len(blob), 0))
            leaves.extend(blob)
        root = gzip.compress(serialise_directory(pointers), mtime=0)
        if HEADER_LENGTH + len(root) <= ROOT_LIMIT:
            return root, bytes(leaves), len(pointers)
        size *= 2

    raise SystemExit(
        "cannot fit a root directory in %d bytes even with one entry per leaf"
        % ROOT_LIMIT)


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
    h[98] = 1                 # tile compression: none (both are already that)
    # 3 = jpeg, 2 = png. A parameter rather than a constant because the height
    # packs are PNG - Terrarium cannot survive JPEG, which is lossy in exactly
    # the low bits the height lives in - and they are written by this same
    # function. Defaulted to jpeg so the imagery builder is unchanged.
    h[99] = f.get("tile_type", 3)
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


def write_pmtiles(path, tiles, bbox, min_zoom, max_zoom, metadata,
                  tile_type=3):
    """tiles: dict of tile_id -> encoded bytes.

    `tile_type` is the PMTiles v3 code for what those bytes are: 3 for
    JPEG, which is the imagery and the default, and 2 for PNG, which is
    what a Terrarium height pack has to be.
    """
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

    root, leaves, leaf_count = build_directories(entries)
    metadata_bytes = gzip.compress(
        json.dumps(metadata, separators=(",", ":")).encode("utf-8"), mtime=0)

    root_offset = HEADER_LENGTH
    metadata_offset = root_offset + len(root)
    leaf_offset = metadata_offset + len(metadata_bytes)
    data_offset = leaf_offset + len(leaves)

    header = build_header(
        root_offset=root_offset, root_length=len(root),
        metadata_offset=metadata_offset, metadata_length=len(metadata_bytes),
        leaf_offset=leaf_offset, leaf_length=len(leaves),
        data_offset=data_offset, data_length=len(body),
        addressed=addressed, entries=len(entries), contents=len(offsets),
        min_zoom=min_zoom, max_zoom=max_zoom,
        west=west, south=south, east=east, north=north,
        centre_zoom=min(max_zoom, 12),
        centre_lon=(west + east) / 2, centre_lat=(south + north) / 2,
        tile_type=tile_type,
    )

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(header)
        f.write(root)
        f.write(metadata_bytes)
        # Leaves sit between the metadata and the tiles, which is where
        # `leaf_offset` says they are.
        f.write(leaves)
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
    # Under the LAYER, so tiles staged from one mosaic year are never resumed
    # into a pack labelled with another: the cache a run restores may have
    # been filled before SOURCE last changed.
    return os.path.join(staging, LAYER, str(z), str(x), "%d.jpg" % y)


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


def _planner():
    """satellite_plan, which owns how long a stop keeps an area idle."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "satellite_plan", os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "satellite_plan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def record_block(path, area_id, block):
    """Write the stop into `path` beside the other areas' records, and
    return it. Retry-After is rounded UP: what EOX asked for is never cut."""
    plan = _planner()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    asked = block.get("retry_after")
    rec = {"at": plan.stamp(now), "status": block.get("status"),
           "retry_after": None if asked is None else int(math.ceil(asked)),
           "reason": block.get("reason")}
    rec["until"] = plan.stamp(plan.blocked_until(rec))
    data = plan.load_blocks(path) if os.path.exists(path) else {"areas": {}}
    data.setdefault("areas", {})[area_id] = rec
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    return rec


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
    ap.add_argument("--block-out",
                    help="when EOX stops the area, record the stop in this "
                         "JSON file (satellite/blocks.json), which the "
                         "planner reads so nothing asks again before it may")
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

        if fetcher.stopped:
            # EOX asked us to stop. No pack: the published one stays, and
            # nothing here tries again around the refusal.
            print("STOPPED: EOX refused the tile service to us (%s). No pack "
                  "written; %s keeps its published imagery. Not retried."
                  % (fetcher.stopped, args.id), file=sys.stderr)
            if args.block_out:
                rec = record_block(args.block_out, args.id, fetcher.block)
                print("Recorded in %s: %s is not asked for again until %s."
                      % (args.block_out, args.id, rec["until"]),
                      file=sys.stderr)
            return BLOCKED_EXIT

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

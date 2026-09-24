#!/usr/bin/env python3
"""Sustained gradient and total climb for a way, from our own DEM.

    python tools/gradient.py --container containers/motor-midlands.tbmap \
        --height staging/gb-midlands-height.pmtiles --top 20

Step 1.4 of `greenroadmap-app/docs/PIVOT-PLAN.md`. Writes the two TERRAIN
columns of `docs/WAYS-SCHEMA.md`:

    climb_m        REAL   -- total ascent
    sustained_pct  REAL   -- steepest continuous 100-150 m

WHY NOT A MAXIMUM
-----------------
F1 measured OSM width on ~10% of byways, so gradient - which covers ~100% -
carries the motorbike/4x4 distinction instead. F4 then measured that the
OBVIOUS gradient measure does not survive contact with the data
(`greenroadmap-app/docs/measurements/gradient-discrimination.md`): of the five
steepest Midlands lanes by RAW MAXIMUM gradient, two showed 28% over **two
metres of total climb**. That is one spurious sample, not a hill.

The cause was checked rather than assumed. It is not vertical quantisation -
the packs are built at a 0.5 m step, which over a 40 m sample is 1.25% of
gradient. It is HORIZONTAL resolution: z12 is about 23 m per pixel at 53 N, so
a track running along a shelf, an embankment or a valley side has consecutive
samples landing on opposite sides of a feature the DEM cannot resolve.

Three things in here follow from that, and each is a deliberate cost:

1. **Bilinear sampling, not nearest pixel.** A nearest-pixel profile steps by a
   whole pixel at a time, which manufactures exactly the jump F4 caught.

2. **The profile is low-passed to the instrument's own resolution.** Two
   [1,2,1] passes over a 25 m sample step span ~100 m, which is about four DEM
   pixels. Detail finer than that is not in the DEM to begin with; keeping it
   is keeping noise. MEASURED on the synthetic case F4 describes - flat ground
   with one 28 m spurious sample - the answer falls **112% raw adjacent, 28%
   unsmoothed over a 100 m run, 14% after one pass, 10.5% after two**, while a
   genuine 12% ramp reads **12.00% after every one of them**. A straight line
   is invariant under a symmetric kernel, so this attenuates curvature and
   noise and leaves a sustained slope exactly alone. That is why two passes is
   the default and not one.

3. **The measure is a RUN, not a point.** `sustained_pct` is the steepest rise
   over a continuous stretch of [MIN_RUN_M, MAX_RUN_M] of track. One bad
   sample inside a 100 m window moves the answer by its share of the window,
   not by its whole height.

`climb_m` is accumulated with a hysteresis threshold, so a profile that
wanders by less than [CLIMB_THRESHOLD_M] does not book that wander as ascent.
Without it, total ascent on flat ground grows with the number of samples,
which is how a fen out-climbs a fell.

WHICH WAY IS UP - a correction to the schema's wording, MEASURED
----------------------------------------------------------------
`WAYS-SCHEMA.md` calls `climb_m` "total ascent". Taken literally - ascent in
the direction the authority happened to digitise the line - it is arbitrary,
because nobody rides a byway in the direction of its row. Measured over
`motor-midlands`, six of the twenty steepest lanes came back with a total
ascent under 20 m while descending 37 m to 133 m: DY-61/1 read `asc=0.0
desc=57.4`. The plan's own top-twenty clause would have failed on six genuine
hills, every one of them real.

So `climb_m` is the ascent a rider faces in the HARDER direction:
`max(ascent, descent)`. For a way that only goes one way this is the height
of the hill whichever end you start; for an undulating one the two are within
a few metres of each other. It is still total ascent - it is just not the
authority's arrow that decides which end is the bottom.

SHORT WAYS
----------
A way shorter than MIN_RUN_M has no 100 m run to be steepest over. Its
`sustained_pct` is measured over the whole way instead, and `sustained_run_m`
records the run actually used - so a consumer can see that a 60% figure came
off 30 m of track and treat it accordingly. Below [MIN_RUN_FLOOR_M] - about
one DEM pixel - nothing is reported at all, because there is no measurement
there to report. In `motor-midlands` that is 6.6% of ways; the plan's coverage
gate is met on the other 93.4%... which is why the floor is a parameter and
the report prints both counts rather than one number.

THE PMTILES READER
------------------
The directory parser is `repack_pmtiles.parse_directory`, imported rather than
copied: two readers of one format is how a builder and a reader come to
disagree about which tiles a pack holds. Terrarium decode is the published
`(R * 256 + G + B / 256) - 32768`.
"""
import argparse
import gzip
import importlib.util
import json
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _sibling(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# `repack_pmtiles` owns the v3 directory parser. Importing it also pulls
# `build_satellite`, which owns `tile_id` and `HEADER_LENGTH`.
_repack = _sibling("repack_pmtiles", "repack_pmtiles.py")
parse_directory = _repack.parse_directory
tile_id = _repack.bs.tile_id
HEADER_LENGTH = _repack.bs.HEADER_LENGTH

#: Degrees per stored unit in `build_map_container.pack_geometry`.
GEOMETRY_SCALE = 10 ** 7

TILE_SIZE = 256
EARTH_RADIUS_M = 6378137.0

#: Along-track spacing of the elevation profile, in metres.
#:
#: z12 is ~23 m/pixel at 53 N. Sampling at 25 m is a shade coarser than the
#: pixel, which is where a profile should sit: finer only interpolates the same
#: four pixels again and buys the illusion of detail.
SAMPLE_STEP_M = 25.0

#: The sustained window, in metres of track. F4's wording, unchanged.
MIN_RUN_M = 100.0
MAX_RUN_M = 150.0

#: Below this there is no run to measure - roughly one DEM pixel.
MIN_RUN_FLOOR_M = 25.0

#: Ascent smaller than this is not booked. Default when the pack does not
#: publish its own vertical step; otherwise four steps of whatever it says.
CLIMB_THRESHOLD_M = 2.0


# --------------------------------------------------------------------------
# Web Mercator
# --------------------------------------------------------------------------
def lonlat_to_pixel(lon, lat, zoom):
    """Global pixel coordinates at `zoom`, as floats."""
    n = float(TILE_SIZE << zoom)
    x = (lon + 180.0) / 360.0 * n
    lat = max(-85.05112878, min(85.05112878, lat))
    r = math.radians(lat)
    y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return x, y


def haversine_m(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2.0) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2)
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def decode_terrarium(r, g, b):
    """The published Terrarium decode, in metres."""
    return (r * 256.0 + g + b / 256.0) - 32768.0


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------
def _read_varint(buf, i):
    shift = 0
    value = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _unzigzag(n):
    return (n >> 1) if not n & 1 else -((n + 1) >> 1)


def unpack_geometry(blob):
    """The inverse of `build_map_container.pack_geometry`.

    Returns a list of lines, each a list of (lon, lat) in degrees.
    """
    i = 0
    count, i = _read_varint(blob, i)
    lines = []
    for _ in range(count):
        points, i = _read_varint(blob, i)
        last_lon = last_lat = 0
        line = []
        for _ in range(points):
            dlon, i = _read_varint(blob, i)
            dlat, i = _read_varint(blob, i)
            last_lon += _unzigzag(dlon)
            last_lat += _unzigzag(dlat)
            line.append((last_lon / GEOMETRY_SCALE, last_lat / GEOMETRY_SCALE))
        lines.append(line)
    return lines


def resample(line, step_m=SAMPLE_STEP_M):
    """Points along `line` at `step_m` spacing, with the end always kept.

    Returns (points, distances) where `distances[k]` is the along-track metres
    of `points[k]`. A line of one point returns that point at distance 0.
    """
    if not line:
        return [], []
    if len(line) == 1:
        return [line[0]], [0.0]
    out_pts = [line[0]]
    out_d = [0.0]
    travelled = 0.0        # along-track distance of the last emitted point
    walked = 0.0           # along-track distance of the cursor
    for k in range(1, len(line)):
        (x0, y0), (x1, y1) = line[k - 1], line[k]
        seg = haversine_m(x0, y0, x1, y1)
        if seg <= 0.0:
            continue
        target = travelled + step_m
        while target <= walked + seg:
            t = (target - walked) / seg
            out_pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
            out_d.append(target)
            travelled = target
            target += step_m
        walked += seg
    if out_d[-1] < walked - 1e-9:
        out_pts.append(line[-1])
        out_d.append(walked)
    return out_pts, out_d


# --------------------------------------------------------------------------
# The height pack
# --------------------------------------------------------------------------
class HeightSource:
    """Terrarium heights out of a PMTiles v3 archive, bilinearly sampled."""

    def __init__(self, path, cache_tiles=96):
        self.path = path
        self._f = open(path, "rb")
        header = self._f.read(HEADER_LENGTH)
        if len(header) < HEADER_LENGTH or bytes(header[:7]) != b"PMTiles":
            raise ValueError("%s is not a PMTiles archive" % path)
        if header[7] != 3:
            raise ValueError("%s is PMTiles v%d, not v3" % (path, header[7]))

        def u64(at):
            return int.from_bytes(header[at:at + 8], "little")

        self._root_off, self._root_len = u64(8), u64(16)
        meta_off, meta_len = u64(24), u64(32)
        self._leaf_off = u64(40)
        self._data_off = u64(56)
        self.internal_compression = header[97]
        self.tile_compression = header[98]
        self.tile_type = header[99]
        self.min_zoom, self.max_zoom = header[100], header[101]
        self.bounds = tuple(
            struct.unpack("<i", header[102 + 4 * k:106 + 4 * k])[0] / 1e7
            for k in range(4))                      # west, south, east, north
        if self.tile_type != 2:
            raise ValueError("%s holds tile type %d, not PNG - a Terrarium "
                             "pack is PNG because the low bits ARE the height"
                             % (path, self.tile_type))

        self.metadata = {}
        if meta_len:
            self._f.seek(meta_off)
            raw = self._inflate(self._f.read(meta_len))
            try:
                self.metadata = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.metadata = {}
        self.vertical_step_m = self.metadata.get("vertical_step_m")

        self._root = self._directory(self._root_off, self._root_len)
        self._leaves = {}
        self._tiles = {}
        self._order = []
        self._cache_tiles = cache_tiles
        self.tiles_read = 0
        self.tiles_missing = 0

    # -- plumbing ---------------------------------------------------------
    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _inflate(self, blob):
        if self.internal_compression == 2:
            return gzip.decompress(blob)
        return blob

    def _directory(self, off, length):
        if not length:
            return []
        self._f.seek(off)
        return parse_directory(self._inflate(self._f.read(length)))

    @staticmethod
    def _find(entries, tid):
        """The entry covering `tid`, honouring run lengths. Binary search."""
        lo, hi = 0, len(entries) - 1
        found = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if entries[mid][0] > tid:
                hi = mid - 1
            else:
                found = mid
                lo = mid + 1
        if found < 0:
            return None
        eid, off, length, run = entries[found]
        if run == 0:                       # pointer to a leaf directory
            return (eid, off, length, 0)
        if tid < eid + run:
            return (eid, off, length, run)
        return None

    def _tile_bytes(self, z, x, y):
        tid = tile_id(z, x, y)
        entry = self._find(self._root, tid)
        if entry is not None and entry[3] == 0:
            key = (entry[1], entry[2])
            leaf = self._leaves.get(key)
            if leaf is None:
                leaf = self._directory(self._leaf_off + entry[1], entry[2])
                self._leaves[key] = leaf
            entry = self._find(leaf, tid)
            if entry is not None and entry[3] == 0:
                raise ValueError("leaf directory points at another leaf")
        if entry is None:
            return None
        self._f.seek(self._data_off + entry[1])
        blob = self._f.read(entry[2])
        if self.tile_compression == 2:
            blob = gzip.decompress(blob)
        return blob

    def _tile(self, z, x, y):
        """(width, height, channels, pixel bytes) for a tile, or None."""
        key = (z, x, y)
        if key in self._tiles:
            return self._tiles[key]
        blob = self._tile_bytes(z, x, y)
        tile = None
        if blob is not None:
            tile = _decode_png(blob)
            self.tiles_read += 1
        else:
            self.tiles_missing += 1
        self._tiles[key] = tile
        self._order.append(key)
        if len(self._order) > self._cache_tiles:
            self._tiles.pop(self._order.pop(0), None)
        return tile

    # -- the one thing this class is for ----------------------------------
    def _pixel(self, z, px, py):
        n = 1 << z
        tx, ty = int(px) >> 8, int(py) >> 8
        if not (0 <= tx < n and 0 <= ty < n):
            return None
        tile = self._tile(z, tx, ty)
        if tile is None:
            return None
        w, h, ch, data = tile
        ix, iy = int(px) & 0xFF, int(py) & 0xFF
        if not (0 <= ix < w and 0 <= iy < h):
            return None
        at = (iy * w + ix) * ch
        return decode_terrarium(data[at], data[at + 1], data[at + 2])

    def height(self, lon, lat, zoom=None):
        """Metres above sea level, bilinear, or None outside the pack.

        Nearest-pixel sampling steps by a whole 23 m pixel at a time and
        manufactures the jump F4 caught; this interpolates the four pixel
        CENTRES around the point, which is what the DEM actually claims.
        """
        z = self.max_zoom if zoom is None else zoom
        px, py = lonlat_to_pixel(lon, lat, z)
        fx, fy = px - 0.5, py - 0.5     # pixel centres sit at integer + 0.5
        x0, y0 = math.floor(fx), math.floor(fy)
        dx, dy = fx - x0, fy - y0
        h00 = self._pixel(z, x0, y0)
        h10 = self._pixel(z, x0 + 1, y0)
        h01 = self._pixel(z, x0, y0 + 1)
        h11 = self._pixel(z, x0 + 1, y0 + 1)
        got = [v for v in (h00, h10, h01, h11) if v is not None]
        if not got:
            return None
        # At a pack edge some of the four are outside. Falling back to the
        # mean of what is there beats refusing a way for being near a border.
        mean = sum(got) / len(got)
        h00 = mean if h00 is None else h00
        h10 = mean if h10 is None else h10
        h01 = mean if h01 is None else h01
        h11 = mean if h11 is None else h11
        top = h00 + (h10 - h00) * dx
        bot = h01 + (h11 - h01) * dx
        return top + (bot - top) * dy


def _decode_png(blob):
    """(width, height, channels, pixel bytes) from a non-interlaced PNG.

    Written here rather than taken from Pillow so the decode is exact and a
    test can build a tile with the standard library alone. Terrarium tiles are
    8-bit RGB or
    RGBA, which is the only case this needs to handle - anything else raises
    rather than guessing, because a guess about the low bits is a guess about
    the height.
    """
    import zlib
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    i = 8
    width = height = None
    channels = None
    idat = bytearray()
    while i + 8 <= len(blob):
        length = int.from_bytes(blob[i:i + 4], "big")
        kind = blob[i + 4:i + 8]
        body = blob[i + 8:i + 8 + length]
        i += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            if depth != 8 or colour not in (2, 6):
                raise ValueError("PNG is depth %d colour type %d; a Terrarium "
                                 "tile is 8-bit RGB or RGBA" % (depth, colour))
            if body[12] != 0:
                raise ValueError("interlaced PNG")
            channels = 3 if colour == 2 else 4
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if width is None:
        raise ValueError("PNG has no IHDR")
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(height * stride)
    pos = 0
    for row in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        base = row * stride
        prior = out[base - stride:base] if row else bytes(stride)
        if ftype == 0:
            pass
        elif ftype == 1:
            for k in range(channels, stride):
                line[k] = (line[k] + line[k - channels]) & 0xFF
        elif ftype == 2:
            for k in range(stride):
                line[k] = (line[k] + prior[k]) & 0xFF
        elif ftype == 3:
            for k in range(stride):
                left = line[k - channels] if k >= channels else 0
                line[k] = (line[k] + ((left + prior[k]) >> 1)) & 0xFF
        elif ftype == 4:
            for k in range(stride):
                a = line[k - channels] if k >= channels else 0
                b = prior[k]
                c = prior[k - channels] if k >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[k] = (line[k] + pr) & 0xFF
        else:
            raise ValueError("unknown PNG filter %d" % ftype)
        out[base:base + stride] = line
    return width, height, channels, bytes(out)


# --------------------------------------------------------------------------
# The measures
# --------------------------------------------------------------------------
#: [1,2,1] passes over the profile. See point 2 of the module docstring.
SMOOTH_PASSES = 2


def smooth(heights, passes=1):
    """A [1, 2, 1] low pass, endpoints held.

    The DEM's own resolution is ~23 m; at a 25 m sample step each pass spans
    about two pixels. It removes detail the DEM never had. A straight line is
    invariant under it, so a sustained slope is untouched and only curvature
    and isolated samples are attenuated.
    """
    out = list(heights)
    for _ in range(passes):
        if len(out) < 3:
            return out
        nxt = [out[0]]
        for k in range(1, len(out) - 1):
            nxt.append((out[k - 1] + 2.0 * out[k] + out[k + 1]) / 4.0)
        nxt.append(out[-1])
        out = nxt
    return out


def total_ascent(heights, threshold=CLIMB_THRESHOLD_M):
    """Sum of rises, ignoring wander smaller than `threshold`.

    Without the threshold, total ascent on flat ground grows with the sample
    count: every pair of quantisation steps books a metre, and a fen
    out-climbs a fell.
    """
    if len(heights) < 2:
        return 0.0
    climb = 0.0
    low = high = heights[0]
    for x in heights[1:]:
        if x >= high:
            high = x
        elif x <= high - threshold:
            # A descent worth believing. Bank the rise from the bottom of this
            # climb to its top, and start a new one here.
            #
            # `low` is NOT lowered on the way down. An earlier draft did, and a
            # pure 58 m descent then booked 30 m of ASCENT - one step per pair
            # of samples - because it banked `high - low` against a low that
            # came AFTER the high. Caught by test 7 of test_gradient.py.
            climb += max(0.0, high - low)
            low = high = x
    return climb + max(0.0, high - low)


def harder_direction_climb(heights, threshold=CLIMB_THRESHOLD_M):
    """Ascent in whichever direction has more of it.

    See WHICH WAY IS UP above: the stored direction is the authority's
    digitising order, not a riding direction, and reading ascent off it made
    DY-61/1 - 57 m of hill - report a total climb of zero.
    """
    return max(total_ascent(heights, threshold),
               total_ascent(list(reversed(heights)), threshold))


def steepest_run(heights, distances,
                 min_run_m=MIN_RUN_M, max_run_m=MAX_RUN_M,
                 min_run_floor_m=MIN_RUN_FLOOR_M):
    """(percent, run_m) for the steepest continuous [min, max] metres.

    Both directions count: a 20% descent is a 20% climb ridden the other way.

    A line shorter than `min_run_m` has no such window, so it is measured over
    its whole length and the run is reported alongside - a caller that will not
    accept a 40 m basis can see that it got one. Shorter than
    `min_run_floor_m` - about one DEM pixel - returns (None, None), because
    there is nothing there to measure.
    """
    if len(heights) < 2:
        return None, None
    total = distances[-1] - distances[0]
    if total < min_run_floor_m:
        return None, None
    if total < min_run_m:
        run = total
        rise = abs(heights[-1] - heights[0])
        return 100.0 * rise / run, run
    best = None
    best_run = None
    j = 0
    for i in range(len(distances)):
        if j < i:
            j = i
        while j < len(distances) and distances[j] - distances[i] < min_run_m:
            j += 1
        k = j
        while k < len(distances) and distances[k] - distances[i] <= max_run_m:
            run = distances[k] - distances[i]
            pct = 100.0 * abs(heights[k] - heights[i]) / run
            if best is None or pct > best:
                best, best_run = pct, run
            k += 1
    if best is None:
        # Sample spacing straddled the window - fall back to the shortest
        # stretch that is at least min_run_m.
        for i in range(len(distances)):
            for k in range(i + 1, len(distances)):
                run = distances[k] - distances[i]
                if run >= min_run_m:
                    pct = 100.0 * abs(heights[k] - heights[i]) / run
                    if best is None or pct > best:
                        best, best_run = pct, run
                    break
    return best, best_run


def profile(line, source, step_m=SAMPLE_STEP_M, zoom=None,
            smooth_passes=SMOOTH_PASSES):
    """(heights, distances) along one line, or ([], []) with no coverage."""
    points, distances = resample(line, step_m)
    heights = []
    kept = []
    for (lon, lat), d in zip(points, distances):
        h = source.height(lon, lat, zoom)
        if h is None:
            continue
        heights.append(h)
        kept.append(d)
    if smooth_passes:
        heights = smooth(heights, smooth_passes)
    return heights, kept


def measure(lines, source, step_m=SAMPLE_STEP_M, zoom=None,
            smooth_passes=SMOOTH_PASSES,
            min_run_m=MIN_RUN_M, max_run_m=MAX_RUN_M,
            min_run_floor_m=MIN_RUN_FLOOR_M, climb_threshold_m=None):
    """`climb_m` and `sustained_pct` for one way's geometry.

    `lines` is what `unpack_geometry` returns: a list of lines of (lon, lat).
    Each line is measured on its own - a way published as two disconnected
    pieces has no elevation continuity across the gap, and pretending it does
    invents a climb between them. `climb_m` sums the lines; `sustained_pct` is
    the steepest of them.

    Returns a dict; `climb_m` and `sustained_pct` are the schema's two
    columns, the rest is there so a reader can see what the numbers rest on.
    """
    if climb_threshold_m is None:
        step = source.vertical_step_m if source is not None else None
        climb_threshold_m = max(CLIMB_THRESHOLD_M, 4.0 * step) if step \
            else CLIMB_THRESHOLD_M
    ascent = descent = 0.0
    best_pct = None
    best_run = None
    samples = 0
    measured_m = 0.0
    for line in lines:
        heights, distances = profile(line, source, step_m, zoom,
                                     smooth_passes)
        if len(heights) < 2:
            continue
        samples += len(heights)
        measured_m += distances[-1] - distances[0]
        # Both directions are accumulated over the WHOLE way before the
        # harder one is chosen, so a way published as several pieces is read
        # end to end rather than piece by piece.
        ascent += total_ascent(heights, climb_threshold_m)
        descent += total_ascent(list(reversed(heights)), climb_threshold_m)
        pct, run = steepest_run(heights, distances,
                                min_run_m, max_run_m, min_run_floor_m)
        if pct is not None and (best_pct is None or pct > best_pct):
            best_pct, best_run = pct, run
    if samples < 2:
        return {"climb_m": None, "sustained_pct": None,
                "sustained_run_m": None, "samples": samples,
                "ascent_m": None, "descent_m": None,
                "measured_m": measured_m,
                "climb_threshold_m": climb_threshold_m}
    climb = max(ascent, descent)
    return {"climb_m": round(climb, 1),
            "ascent_m": round(ascent, 1), "descent_m": round(descent, 1),
            "sustained_pct": None if best_pct is None else round(best_pct, 1),
            "sustained_run_m": None if best_run is None else round(best_run, 1),
            "samples": samples,
            "measured_m": round(measured_m, 1),
            "climb_threshold_m": climb_threshold_m}


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------
def run_container(container, height_path, top=20, limit=None, **kw):
    """Measure every way in a container. Returns (rows, summary)."""
    import sqlite3
    con = sqlite3.connect(container)
    con.row_factory = sqlite3.Row
    sql = ("SELECT lane_uid, lane_class, name, county, length_m, geometry "
           "FROM lanes ORDER BY lane_uid")
    if limit:
        sql += " LIMIT %d" % int(limit)
    rows = []
    with HeightSource(height_path) as source:
        for r in con.execute(sql):
            lines = unpack_geometry(r["geometry"])
            got = measure(lines, source, **kw)
            got.update(way_uid=r["lane_uid"], name=r["name"],
                       county=r["county"], length_m=r["length_m"])
            rows.append(got)
        tiles = source.tiles_read
        missing = source.tiles_missing
    con.close()
    total = len(rows)
    with_climb = sum(1 for r in rows if r["climb_m"] is not None)
    with_sust = sum(1 for r in rows if r["sustained_pct"] is not None)
    full_run = sum(1 for r in rows
                   if r["sustained_run_m"] is not None
                   and r["sustained_run_m"] >= MIN_RUN_M)
    ranked = sorted((r for r in rows if r["sustained_pct"] is not None),
                    key=lambda r: -r["sustained_pct"])
    summary = {"ways": total, "with_climb": with_climb,
               "with_sustained": with_sust, "full_run": full_run,
               "tiles_read": tiles, "tiles_missing": missing,
               "top": ranked[:top]}
    return rows, summary


def _report(summary, top):
    n = summary["ways"] or 1
    print("ways                     %6d" % summary["ways"])
    print("climb_m present          %6d  %5.1f%%"
          % (summary["with_climb"], 100.0 * summary["with_climb"] / n))
    print("sustained_pct present    %6d  %5.1f%%"
          % (summary["with_sustained"], 100.0 * summary["with_sustained"] / n))
    print("  of which on a full %d m run   %6d  %5.1f%%"
          % (MIN_RUN_M, summary["full_run"], 100.0 * summary["full_run"] / n))
    print("DEM tiles read %d, missing %d"
          % (summary["tiles_read"], summary["tiles_missing"]))
    print()
    # `measured_m`, not the container's `length_m`: on `motor-midlands` the
    # stored length_m disagrees with its own geometry by a median factor of
    # 1.609 on 2,149 of 2,151 ways. That is somebody else's file to fix; this
    # one prints the length it actually walked.
    print("%-22s %7s %8s %7s %9s  %s"
          % ("way_uid", "sust%", "climb_m", "run_m", "walked_m", "name"))
    under = 0
    for r in summary["top"]:
        if (r["climb_m"] or 0.0) < 20.0:
            under += 1
        print("%-22s %7.1f %8.1f %7.1f %9.0f  %s"
              % (r["way_uid"][:22], r["sustained_pct"], r["climb_m"],
                 r["sustained_run_m"], r["measured_m"] or 0.0,
                 (r["name"] or "")[:40]))
    print()
    print("top %d with climb_m under 20 m: %d   (the gate wants 0)"
          % (len(summary["top"]), under))
    return under


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--container", required=True,
                    help="a .tbmap holding a `lanes` table")
    ap.add_argument("--height", required=True, help="a Terrarium .pmtiles")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--step", type=float, default=SAMPLE_STEP_M)
    ap.add_argument("--smooth-passes", type=int, default=SMOOTH_PASSES,
                    help="0 reproduces the unsmoothed profile F4 measured")
    ap.add_argument("--json", help="write every row here")
    args = ap.parse_args(argv)

    rows, summary = run_container(args.container, args.height, top=args.top,
                                  limit=args.limit, step_m=args.step,
                                  smooth_passes=args.smooth_passes)
    under = _report(summary, args.top)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1)
    covered = summary["with_sustained"] / (summary["ways"] or 1)
    return 0 if covered >= 0.95 and under == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

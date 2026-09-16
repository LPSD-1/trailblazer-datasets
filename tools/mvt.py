"""Mapbox Vector Tile encoding, in the standard library only.

Written here rather than taking `mapbox-vector-tile` from PyPI for the same
reason `write_pmtiles` lives in build_satellite.py: this pipeline runs unattended
with `contents: write` and the dataset key in scope, and every dependency it does
not have is one fewer thing that can change underneath it.

The format is small. A tile is a protobuf message:

    Tile    { repeated Layer layers = 3 }
    Layer   { string name = 1; repeated Feature features = 2;
              repeated string keys = 3; repeated Value values = 4;
              uint32 extent = 5; uint32 version = 15 }
    Feature { uint64 id = 1; repeated uint32 tags = 2 [packed];
              GeomType type = 3; repeated uint32 geometry = 4 [packed] }

Geometry is a run of command integers and zigzag-encoded parameters, in tile
coordinates: x right, y DOWN, origin at the tile's top-left, `extent` units
across. A LINESTRING feature carries as many MoveTo/LineTo runs as it has lines,
which is how one feature holds a multilinestring - and is what makes the
coalescing in build_lane_tiles.py possible at all.

See docs/MAP_ARCHITECTURE.md in the app repository, section 5.
"""

import math
import struct

# Geometry types, as the spec numbers them.
POINT = 1
LINESTRING = 2
POLYGON = 3

# Command ids, packed into a command integer as (id & 7) | (count << 3).
_MOVE_TO = 1
_LINE_TO = 2

#: Tile-internal coordinate resolution. At z14 a UK tile is about 2.4 km across,
#: so 4096 units puts one unit at roughly 0.6 m - just under the one-metre
#: simplification tolerance the app already accepts, and therefore not the
#: limiting factor. Raising it costs tile size; see section 4 of the spec.
EXTENT = 4096


# --------------------------------------------------------------------------
# protobuf, only the three wire types this needs
# --------------------------------------------------------------------------

def _varint(value):
    """Base-128, low group first, high bit set on every group but the last."""
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _tag(field, wire):
    return _varint((field << 3) | wire)


def _len_delimited(field, payload):
    return _tag(field, 2) + _varint(len(payload)) + payload


def _varint_field(field, value):
    return _tag(field, 0) + _varint(value)


def _packed(field, values):
    body = b"".join(_varint(v) for v in values)
    return _len_delimited(field, body)


def _zigzag(n):
    """Signed to unsigned, so small negatives stay small."""
    return (n << 1) ^ (n >> 31) if n >= 0 else ((-n) << 1) - 1


# --------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------

def lon_to_tile_x(lon, zoom):
    return (lon + 180.0) / 360.0 * (1 << zoom)


def lat_to_tile_y(lat, zoom):
    # Clamped to the Web Mercator limit. A latitude past it is a data error, and
    # tan() at 90 degrees is an exception rather than a wrong answer.
    lat = max(-85.05112878, min(85.05112878, lat))
    rad = math.radians(lat)
    return (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * (1 << zoom)


def tile_bounds(z, x, y):
    """(west, south, east, north) of a tile, in degrees."""
    n = 1 << z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0

    def _lat(ty):
        t = math.pi * (1 - 2 * ty / n)
        return math.degrees(math.atan(math.sinh(t)))

    return (west, _lat(y + 1), east, _lat(y))


# --------------------------------------------------------------------------
# clipping
# --------------------------------------------------------------------------

def clip_line(points, minx, miny, maxx, maxy):
    """Clip a line in TILE coordinates to a box, keeping every piece.

    A line crossing a tile twice - a lane that leaves and comes back - must come
    out as two pieces, not one piece joined across the gap, or the tile draws a
    straight line through ground the lane never touches.

    Returns a list of point lists. Coordinates are floats; the caller rounds.
    """
    out = []
    run = []
    for i in range(len(points) - 1):
        ax, ay = points[i]
        bx, by = points[i + 1]
        seg = _clip_segment(ax, ay, bx, by, minx, miny, maxx, maxy)
        if seg is None:
            # Wholly outside: whatever run was building has ended.
            if len(run) > 1:
                out.append(run)
            run = []
            continue
        (cx, cy), (dx, dy) = seg
        if not run:
            run = [(cx, cy), (dx, dy)]
        elif run[-1] == (cx, cy):
            run.append((dx, dy))
        else:
            # The segment was clipped at its start, so it does not continue the
            # run - it begins a new one.
            if len(run) > 1:
                out.append(run)
            run = [(cx, cy), (dx, dy)]
    if len(run) > 1:
        out.append(run)
    return out


def _clip_segment(ax, ay, bx, by, minx, miny, maxx, maxy):
    """Liang-Barsky. Returns the clipped endpoints, or None if wholly outside."""
    dx = bx - ax
    dy = by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - minx), (dx, maxx - ax), (-dy, ay - miny), (dy, maxy - ay)):
        if p == 0:
            if q < 0:
                return None          # parallel to this edge and outside it
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            if t > t0:
                t0 = t
        else:
            if t < t0:
                return None
            if t < t1:
                t1 = t
    return ((ax + t0 * dx, ay + t0 * dy), (ax + t1 * dx, ay + t1 * dy))


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------

def encode_geometry(lines):
    """Command/parameter integers for a set of lines in tile coordinates.

    Points are integers. Repeated points are dropped: MVT allows them, every
    renderer ignores them, and they are pure tile size.
    """
    out = []
    cx = cy = 0
    for line in lines:
        pts = []
        for x, y in line:
            xi, yi = int(round(x)), int(round(y))
            if not pts or pts[-1] != (xi, yi):
                pts.append((xi, yi))
        if len(pts) < 2:
            continue
        x0, y0 = pts[0]
        out.append((_MOVE_TO & 0x7) | (1 << 3))
        out.append(_zigzag(x0 - cx))
        out.append(_zigzag(y0 - cy))
        cx, cy = x0, y0
        rest = pts[1:]
        out.append((_LINE_TO & 0x7) | (len(rest) << 3))
        for x, y in rest:
            out.append(_zigzag(x - cx))
            out.append(_zigzag(y - cy))
            cx, cy = x, y
    return out


def _encode_value(value):
    """One Value message. Types are chosen to keep tiles small."""
    if isinstance(value, bool):
        return _len_delimited(4, _varint_field(7, 1 if value else 0))
    if isinstance(value, int):
        if value >= 0:
            return _len_delimited(4, _varint_field(5, value))
        return _len_delimited(4, _tag(6, 0) + _varint(_zigzag(value)))
    if isinstance(value, float):
        return _len_delimited(4, _tag(3, 1) + struct.pack("<d", value))
    return _len_delimited(4, _len_delimited(1, str(value).encode("utf-8")))


class Layer(object):
    """Accumulates features, interning keys and values as the format wants."""

    def __init__(self, name, extent=EXTENT):
        self.name = name
        self.extent = extent
        self._keys = []
        self._key_index = {}
        self._values = []
        self._value_index = {}
        self._features = []

    def __len__(self):
        return len(self._features)

    def add(self, lines, properties, feature_id=None):
        geometry = encode_geometry(lines)
        if not geometry:
            return False
        tags = []
        for key in sorted(properties):
            value = properties[key]
            if value is None:
                continue
            tags.append(self._intern_key(key))
            tags.append(self._intern_value(value))
        body = b""
        if feature_id is not None:
            body += _varint_field(1, feature_id)
        if tags:
            body += _packed(2, tags)
        body += _varint_field(3, LINESTRING)
        body += _packed(4, geometry)
        self._features.append(_len_delimited(2, body))
        return True

    def _intern_key(self, key):
        got = self._key_index.get(key)
        if got is None:
            got = self._key_index[key] = len(self._keys)
            self._keys.append(_len_delimited(3, key.encode("utf-8")))
        return got

    def _intern_value(self, value):
        # Keyed on the type as well, so 1 and True and "1" stay distinct.
        marker = (type(value).__name__, value)
        got = self._value_index.get(marker)
        if got is None:
            got = self._value_index[marker] = len(self._values)
            self._values.append(_encode_value(value))
        return got

    def to_bytes(self):
        body = _len_delimited(1, self.name.encode("utf-8"))
        body += b"".join(self._features)
        body += b"".join(self._keys)
        body += b"".join(self._values)
        body += _varint_field(5, self.extent)
        body += _varint_field(15, 2)
        return body


def encode_tile(layers):
    """One tile from a list of Layer, skipping any that ended up empty."""
    return b"".join(_len_delimited(3, layer.to_bytes())
                    for layer in layers if len(layer))

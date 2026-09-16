"""Tests for the MVT encoder.

These decode what the encoder produced rather than comparing it to a recorded
blob. A golden-bytes test would pass for an encoder that is self-consistently
wrong, and MapLibre is the thing that has to read this, not us.

Run: python tools/test_mvt.py
"""

import math
import sys

import mvt


# --------------------------------------------------------------------------
# a minimal protobuf reader, so the tests can look inside a tile
# --------------------------------------------------------------------------

def _read_varint(buf, i):
    shift = 0
    out = 0
    while True:
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        if not b & 0x80:
            return out, i
        shift += 7


def _fields(buf):
    """Yield (field_number, wire_type, payload) in order."""
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(buf, i)
            yield field, wire, value
        elif wire == 2:
            length, i = _read_varint(buf, i)
            yield field, wire, buf[i:i + length]
            i += length
        elif wire == 1:
            yield field, wire, buf[i:i + 8]
            i += 8
        else:
            raise AssertionError("unexpected wire type %d" % wire)


def _packed_varints(buf):
    out = []
    i = 0
    while i < len(buf):
        v, i = _read_varint(buf, i)
        out.append(v)
    return out


def _unzig(n):
    return (n >> 1) ^ -(n & 1)


def decode_tile(blob):
    """Return [{name, extent, version, features:[{id, props, lines}]}]."""
    layers = []
    for field, _, payload in _fields(blob):
        if field != 3:
            continue
        name = None
        extent = 4096
        version = None
        keys = []
        values = []
        raw_features = []
        for f, w, p in _fields(payload):
            if f == 1 and w == 2:
                name = p.decode("utf-8")
            elif f == 2 and w == 2:
                raw_features.append(p)
            elif f == 3 and w == 2:
                keys.append(p.decode("utf-8"))
            elif f == 4 and w == 2:
                values.append(_decode_value(p))
            elif f == 5 and w == 0:
                extent = p
            elif f == 15 and w == 0:
                version = p
        features = []
        for raw in raw_features:
            fid = None
            tags = []
            gtype = None
            geom = []
            for f, w, p in _fields(raw):
                if f == 1 and w == 0:
                    fid = p
                elif f == 2 and w == 2:
                    tags = _packed_varints(p)
                elif f == 3 and w == 0:
                    gtype = p
                elif f == 4 and w == 2:
                    geom = _packed_varints(p)
            props = {}
            for i in range(0, len(tags), 2):
                props[keys[tags[i]]] = values[tags[i + 1]]
            features.append({"id": fid, "props": props, "type": gtype,
                             "lines": _decode_geometry(geom)})
        layers.append({"name": name, "extent": extent, "version": version,
                       "features": features})
    return layers


def _decode_value(payload):
    for f, w, p in _fields(payload):
        if f == 1:
            return p.decode("utf-8")
        if f == 3:
            import struct
            return struct.unpack("<d", p)[0]
        if f == 5:
            return p
        if f == 6:
            return _unzig(p)
        if f == 7:
            return bool(p)
    return None


def _decode_geometry(ints):
    lines = []
    cur = []
    x = y = 0
    i = 0
    while i < len(ints):
        cmd = ints[i]
        op, count = cmd & 7, cmd >> 3
        i += 1
        if op == 1:            # MoveTo
            for _ in range(count):
                x += _unzig(ints[i]); y += _unzig(ints[i + 1]); i += 2
                if len(cur) > 1:
                    lines.append(cur)
                cur = [(x, y)]
        elif op == 2:          # LineTo
            for _ in range(count):
                x += _unzig(ints[i]); y += _unzig(ints[i + 1]); i += 2
                cur.append((x, y))
        else:
            raise AssertionError("unexpected command %d" % op)
    if len(cur) > 1:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


def test_roundtrip():
    print("a tile says back what was put in it")
    layer = mvt.Layer("lanes")
    lines = [[(10, 20), (30, 40), (100, 200)]]
    layer.add(lines, {"class": "full-access", "county": "Derbyshire",
                      "v_moto": True, "colour": 4294901760}, feature_id=7)
    tile = mvt.encode_tile([layer])

    got = decode_tile(tile)
    check("one layer", len(got) == 1)
    check("version 2", got[0]["version"] == 2, got[0]["version"])
    check("extent 4096", got[0]["extent"] == 4096)
    f = got[0]["features"][0]
    check("id survives", f["id"] == 7, f["id"])
    check("type is LINESTRING", f["type"] == 2, f["type"])
    check("geometry survives", f["lines"] == lines, f["lines"])
    check("string property", f["props"]["class"] == "full-access")
    check("bool property", f["props"]["v_moto"] is True, f["props"]["v_moto"])
    check("large int property", f["props"]["colour"] == 4294901760)


def test_multiline_in_one_feature():
    print("one feature can hold several lines - what coalescing depends on")
    layer = mvt.Layer("lanes")
    lines = [[(0, 0), (10, 10)], [(500, 500), (600, 600)], [(50, 900), (60, 910)]]
    layer.add(lines, {"class": "restricted"})
    got = decode_tile(mvt.encode_tile([layer]))
    f = got[0]["features"][0]
    check("three lines, one feature", len(got[0]["features"]) == 1)
    check("all three survive", f["lines"] == lines, f["lines"])


def test_empty_things_are_dropped():
    print("nothing empty reaches a renderer")
    layer = mvt.Layer("lanes")
    check("a single point is refused", layer.add([[(1, 1)]], {}) is False)
    check("an empty line is refused", layer.add([[]], {}) is False)
    check("a degenerate line is refused",
          layer.add([[(5, 5), (5, 5), (5, 5)]], {}) is False)
    check("the layer stayed empty", len(layer) == 0)
    check("an empty layer is not written", mvt.encode_tile([layer]) == b"")


def test_values_are_interned():
    print("repeated values are stored once")
    layer = mvt.Layer("lanes")
    for i in range(50):
        layer.add([[(i, 0), (i, 10)]], {"class": "full-access", "county": "Derbyshire"})
    shared = mvt.encode_tile([layer])
    got = decode_tile(shared)
    check("all fifty features", len(got[0]["features"]) == 50)
    check("every one reads back right",
          all(f["props"]["class"] == "full-access" for f in got[0]["features"]))

    # Measured against the same tile with fifty DISTINCT values rather than
    # against a size this test's author guessed. If interning broke, these two
    # would come out the same.
    distinct = mvt.Layer("lanes")
    for i in range(50):
        distinct.add([[(i, 0), (i, 10)]],
                     {"class": "full-access-%02d" % i, "county": "Derbyshire-%02d" % i})
    apart = mvt.encode_tile([distinct])
    check("shared values cost far less than distinct ones",
          len(shared) < len(apart) * 0.65,
          "%d shared vs %d distinct bytes" % (len(shared), len(apart)))


def test_projection():
    print("the projection puts things where they belong")
    # Greenwich, at the equator-ish reference point of the tile grid.
    check("lon 0 at z1 is the tile seam", abs(mvt.lon_to_tile_x(0, 1) - 1.0) < 1e-9)
    check("lon -180 is the left edge", abs(mvt.lon_to_tile_x(-180, 1)) < 1e-9)
    check("lat 0 at z1 is the tile seam", abs(mvt.lat_to_tile_y(0, 1) - 1.0) < 1e-9)
    # y goes DOWN: further north is a smaller y.
    check("north is up", mvt.lat_to_tile_y(60, 8) < mvt.lat_to_tile_y(50, 8))
    # A latitude past the Mercator limit must not raise.
    check("the poles do not throw", isinstance(mvt.lat_to_tile_y(90, 5), float))

    w, s, e, n = mvt.tile_bounds(14, 8160, 5325)
    check("tile bounds are ordered", w < e and s < n, (w, s, e, n))
    check("a point inside maps inside the extent",
          0 <= (mvt.lon_to_tile_x((w + e) / 2, 14) - 8160) * 4096 <= 4096)


def test_clipping():
    print("clipping keeps every piece and invents none")
    # Straight across the middle: one piece, spanning the box.
    got = mvt.clip_line([(-100, 50), (200, 50)], 0, 0, 100, 100)
    check("one crossing gives one piece", len(got) == 1, got)
    check("and it spans the box",
          abs(got[0][0][0] - 0) < 1e-9 and abs(got[0][-1][0] - 100) < 1e-9, got)

    # Out, back in, out again: TWO pieces. Joining them would draw a line
    # through ground the lane never touches.
    got = mvt.clip_line(
        [(10, 50), (50, 50), (50, -50), (60, -50), (60, 50), (90, 50)],
        0, 0, 100, 100)
    check("a line that leaves and returns gives two pieces", len(got) == 2, got)

    # Wholly outside.
    check("a line outside the box gives nothing",
          mvt.clip_line([(200, 200), (300, 300)], 0, 0, 100, 100) == [])

    # Wholly inside, untouched.
    inside = [(10, 10), (20, 20), (30, 15)]
    got = mvt.clip_line(inside, 0, 0, 100, 100)
    check("a line inside is unchanged", got == [inside], got)

    # A segment exactly along an edge must not vanish.
    got = mvt.clip_line([(0, 0), (100, 0)], 0, 0, 100, 100)
    check("an edge-following line survives", len(got) == 1, got)


def test_geometry_is_relative():
    print("geometry is encoded as deltas, which is where the saving is")
    layer = mvt.Layer("lanes")
    # A long line of closely spaced points - the shape real lane data has.
    line = [(2000 + i, 2000 + (i % 3)) for i in range(400)]
    layer.add([line], {})
    blob = mvt.encode_tile([layer])
    check("400 points fit in well under 2 bytes each",
          len(blob) < 900, "%d bytes" % len(blob))
    check("and decode back exactly",
          decode_tile(blob)[0]["features"][0]["lines"] == [line])


def main():
    for fn in (test_roundtrip, test_multiline_in_one_feature,
               test_empty_things_are_dropped, test_values_are_interned,
               test_projection, test_clipping, test_geometry_is_relative):
        fn()
    print()
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all mvt tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

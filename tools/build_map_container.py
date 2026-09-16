"""Build a `.tbmap` - the SQLite container the app reads for one area.

One file holds both stores that docs/MAP_ARCHITECTURE.md (app repo) separates:

    tiles      MBTiles layout, gzipped MVT, for drawing
    lanes      one row per lane with exact geometry, for legal answers
    lanes_bbox an R-tree over those, for "what did I just tap"

Two kinds of container come out of here, and the difference is section 19.1:

    area      z11-z14, one per published area, features individual and
              identifiable
    overview  z6-z10, ONE nationally, features coalesced by the attributes the
              styles read, because a low-zoom tile spans twenty areas and
              merging twenty archives per request is not viable

Usage:
    python tools/build_map_container.py --area   OUT.tbmap PACK [PACK ...]
    python tools/build_map_container.py --overview OUT.tbmap PACK [PACK ...]
"""

import argparse
import base64
import collections
import gzip
import hashlib
import io
import json
import os
import sqlite3
import struct
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mvt  # noqa: E402

MAGIC = b"TBPK"
HDR_LEN = 6
NONCE_LEN = 12

AREA_ZOOMS = (11, 14)
OVERVIEW_ZOOMS = (6, 10)

#: How far past a tile's edge geometry is carried, in tile units.
#:
#: A line that stops dead at the boundary shows a seam where the renderer joins
#: and caps it. 64 of 4096 is the same fraction the app settled on for its
#: GeoJSON sources after measuring - enough to hide the join, small enough not
#: to pay for every feature twice.
BUFFER = 64

VEHICLES = ("motorcycle", "4x4", "bicycle", "horse", "foot")
VEHICLE_KEY = {"motorcycle": "v_moto", "4x4": "v_4x4", "bicycle": "v_cycle",
               "horse": "v_horse", "foot": "v_foot"}

#: Bit per vehicle, in [VEHICLES] order, for the record store's
#: `vehicle_access` column.
#:
#: MEASURED: as a JSON array this column was 48 bytes a row - 103 kB for one
#: region, on a par with the whole geometry table - to say one of thirty-two
#: things. It is also a better column: "which lanes may a motorcycle use" is
#: `vehicle_access & 1` rather than a LIKE over text.
#:
#: NULL still means "no finding", which is not the same as zero. The app leans
#: on that distinction: an empty vehicle list from a drifted upstream id means
#: the source said nothing, and is handled quite differently from the source
#: saying "nobody".
def vehicle_mask(vehicles):
    if vehicles is None:
        return None
    mask = 0
    for i, name in enumerate(VEHICLES):
        if name in vehicles:
            mask |= 1 << i
    return mask


# --------------------------------------------------------------------------
# reading what the pipeline publishes today
# --------------------------------------------------------------------------

def unpack(path, key):
    blob = open(path, "rb").read()
    if blob[:4] != MAGIC:
        raise SystemExit("%s is not a .tbpack" % path)
    prefix = blob[:HDR_LEN + NONCE_LEN]
    nonce = blob[HDR_LEN:HDR_LEN + NONCE_LEN]
    clear = AESGCM(key).decrypt(nonce, blob[HDR_LEN + NONCE_LEN:], prefix)
    return json.loads(gzip.decompress(clear))


def load_features(paths, key):
    """Every feature from every pack, deduped by lane_uid.

    A lane that straddles a boundary is published in BOTH neighbouring packs so
    it is never cut in half. Records dedupe on the uid, exactly as the app's
    loader does today; tiles need the same and get it here, because two copies
    would draw the lane twice with different simplification (section 19.2).

    The winner is deterministic - first pack in sorted order - so a rebuild
    produces the same container.
    """
    seen = {}
    order = []
    for path in sorted(paths):
        for f in unpack(path, key).get("features", []):
            uid = f["properties"]["lane_uid"]
            if uid in seen:
                continue
            seen[uid] = f
            order.append(uid)
    return [seen[uid] for uid in order]


def lines_of(feature):
    g = feature["geometry"]
    if g["type"] == "LineString":
        return [g["coordinates"]]
    if g["type"] == "MultiLineString":
        return g["coordinates"]
    return []


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def simplify(points, tolerance):
    """Ramer-Douglas-Peucker, iterative, endpoints fixed.

    Iterative because a council line can carry thousands of vertices and a
    recursive version on a pathological one is a stack overflow in the middle of
    a build.
    """
    if len(points) <= 2 or tolerance <= 0:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst, at = -1.0, -1
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        for i in range(first + 1, last):
            px, py = points[i]
            if length_sq == 0:
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
                cx, cy = ax + t * dx, ay + t * dy
                d = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
            if d > worst:
                worst, at = d, i
        if worst > tolerance and at > 0:
            keep[at] = True
            stack.append((first, at))
            stack.append((at, last))
    return [p for p, k in zip(points, keep) if k]


def project(lines, zoom):
    """Degrees to GLOBAL tile units at `zoom` - tile index times the extent."""
    scale = float(mvt.EXTENT)
    out = []
    for line in lines:
        out.append([(mvt.lon_to_tile_x(lon, zoom) * scale,
                     mvt.lat_to_tile_y(lat, zoom) * scale)
                    for lon, lat in line])
    return out


#: Degrees per stored unit in the record geometry.
#:
#: 1e-7 degrees is about 1.1 cm of latitude. The source data is a council's
#: digitised centreline, published to five decimal places - about a metre - so
#: this is two orders of magnitude finer than anything it can express, and four
#: orders finer than the GPS in a handlebar mount.
GEOMETRY_SCALE = 10 ** 7


def _varint(value):
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _zigzag(n):
    return (n << 1) if n >= 0 else ((-n) << 1) - 1


def pack_geometry(lines):
    """The exact geometry, for the record store.

    Varint line count, then per line a varint point count and zigzag varint
    DELTAS of fixed-point lon/lat at [GEOMETRY_SCALE].

    MEASURED, because the obvious encoding was four times too big. Float64
    pairs came to 691 KB for one region and compressed only to 332 KB - the
    mantissa bits of a double look like noise to a compressor, so the container
    lost to the gzipped GeoJSON it was replacing. Deltas between consecutive
    points of a lane are small integers, which varints store in one or two
    bytes and which a compressor then squeezes again.

    Nothing here is simplified: this is what every legal answer reads.
    """
    buf = bytearray()
    buf += _varint(len(lines))
    for line in lines:
        buf += _varint(len(line))
        last_lon = last_lat = 0
        for lon, lat in line:
            ilon = int(round(lon * GEOMETRY_SCALE))
            ilat = int(round(lat * GEOMETRY_SCALE))
            buf += _varint(_zigzag(ilon - last_lon))
            buf += _varint(_zigzag(ilat - last_lat))
            last_lon, last_lat = ilon, ilat
    return bytes(buf)


# --------------------------------------------------------------------------
# tiling
# --------------------------------------------------------------------------

def tile_properties(props, with_uid):
    out = {"class": props.get("class"), "county": props.get("county")}
    vehicles = props.get("vehicles") or []
    for name in VEHICLES:
        out[VEHICLE_KEY[name]] = name in vehicles
    if with_uid:
        out["lane_uid"] = props.get("lane_uid")
    return {k: v for k, v in out.items() if v is not None}


def coalesce_key(props):
    """Everything a style or a filter reads, and nothing else.

    Two lanes with the same answer to every question the map asks can share a
    feature at low zoom. What this must NOT include is anything per-lane - a
    uid, a name, a length - or the grouping achieves nothing.
    """
    return (props.get("class"), props.get("county"),
            tuple(props.get(VEHICLE_KEY[v], False) for v in VEHICLES))


def build_tiles(features, zoom, coalesced, on_tile):
    """Cut `features` into tiles at `zoom` and hand each to `on_tile`."""
    # Bucket every feature's clipped pieces by tile first, so each tile is
    # encoded once.
    buckets = collections.defaultdict(list)
    for index, feature in enumerate(features):
        lines = project(lines_of(feature), zoom)
        lines = [simplify(line, 1.0) for line in lines]
        lines = [line for line in lines if len(line) >= 2]
        if not lines:
            continue
        props = tile_properties(feature["properties"], with_uid=not coalesced)

        touched = set()
        for line in lines:
            xs = [p[0] for p in line]
            ys = [p[1] for p in line]
            for tx in range(int(min(xs)) // mvt.EXTENT, int(max(xs)) // mvt.EXTENT + 1):
                for ty in range(int(min(ys)) // mvt.EXTENT, int(max(ys)) // mvt.EXTENT + 1):
                    touched.add((tx, ty))

        limit = 1 << zoom
        for tx, ty in touched:
            if not (0 <= tx < limit and 0 <= ty < limit):
                continue
            ox, oy = tx * mvt.EXTENT, ty * mvt.EXTENT
            local = []
            for line in lines:
                shifted = [(x - ox, y - oy) for x, y in line]
                local.extend(mvt.clip_line(shifted, -BUFFER, -BUFFER,
                                           mvt.EXTENT + BUFFER, mvt.EXTENT + BUFFER))
            if local:
                buckets[(tx, ty)].append((index, props, local))

    for (tx, ty), items in sorted(buckets.items()):
        layer = mvt.Layer("lanes")
        if coalesced:
            groups = collections.OrderedDict()
            for index, props, local in items:
                key = coalesce_key(props)
                if key not in groups:
                    groups[key] = (props, [])
                groups[key][1].extend(local)
            for props, lines in groups.values():
                layer.add(lines, props)
        else:
            for index, props, local in items:
                # A numeric id is what setFeatureState needs; the uid stays a
                # property for the tap lookup.
                layer.add(local, props, feature_id=index + 1)
        blob = mvt.encode_tile([layer])
        if blob:
            on_tile(zoom, tx, ty, blob, len(layer))


# --------------------------------------------------------------------------
# the container
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE tiles (
  zoom_level  INTEGER NOT NULL,
  tile_column INTEGER NOT NULL,
  tile_row    INTEGER NOT NULL,
  tile_data   BLOB    NOT NULL,
  PRIMARY KEY (zoom_level, tile_column, tile_row)
) WITHOUT ROWID;

CREATE TABLE lanes (
  rowid             INTEGER PRIMARY KEY,
  lane_uid          TEXT UNIQUE NOT NULL,
  lane_class        TEXT NOT NULL,
  county            TEXT,
  name              TEXT,
  designation       TEXT,
  description       TEXT,
  authority         TEXT,
  vehicle_access    INTEGER,       -- bitmask; NULL means "no finding"
  length_m          REAL,
  geometry          BLOB NOT NULL
);

CREATE INDEX lanes_by_county ON lanes(county);
CREATE INDEX lanes_by_class  ON lanes(lane_class);

CREATE VIRTUAL TABLE lanes_bbox USING rtree(id, min_lon, max_lon, min_lat, max_lat);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def write_container(path, features, kind, zooms, source_date):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)

    # "both" builds the two bands into one file: coalesced below the split,
    # individual above, records present. It is NOT the shipping arrangement -
    # section 19.1 keeps the low zooms national and the high zooms per area -
    # but it is the whole design in one file, which is what a measurement on a
    # device needs.
    coalesced_below = 11 if kind == "both" else (99 if kind == "area" else 0)
    stats = collections.OrderedDict()

    def on_tile(z, x, y, blob, feature_count):
        # MBTiles counts rows from the south; MapLibre asks from the north.
        tms_y = (1 << z) - 1 - y
        # TILES ARE STORED RAW, not gzipped one by one.
        #
        # MEASURED. A lane tile is between 0.8 and 1.7 kB, which is too small
        # for deflate to build a useful dictionary: 965 kB of individually
        # gzipped tiles came to 638 kB when re-compressed together, so per-tile
        # gzip was leaving a third of the saving on the table AND costing a
        # decompress on every tile request.
        #
        # The container is compressed once for transport instead (section
        # 18.2), where the compressor sees every tile at once and the shared
        # structure of MVT actually pays. On the device the tiles are then
        # served straight out of SQLite with no decompress at all.
        db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, tms_y, blob))
        got = stats.setdefault(z, [0, 0, 0])
        got[0] += 1
        got[1] += len(blob)
        got[2] = max(got[2], len(blob))

    for zoom in range(zooms[0], zooms[1] + 1):
        build_tiles(features, zoom, zoom < coalesced_below, on_tile)

    # The overview carries no records: it exists to be looked at, and every
    # legal answer comes from an area container.
    if kind != "overview":
        for index, f in enumerate(features):
            props = f["properties"]
            lines = lines_of(f)
            lons = [p[0] for line in lines for p in line]
            lats = [p[1] for line in lines for p in line]
            if not lons:
                continue
            db.execute(
                "INSERT INTO lanes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (index + 1, props["lane_uid"], props.get("class"),
                 props.get("county"), props.get("name"),
                 props.get("designation"), props.get("description"),
                 props.get("authority"),
                 vehicle_mask(props.get("vehicles")),
                 (props.get("lengthKm") or 0) * 1000.0,
                 pack_geometry(lines)))
            db.execute("INSERT INTO lanes_bbox VALUES (?,?,?,?,?)",
                       (index + 1, min(lons), max(lons), min(lats), max(lats)))

    bounds = _bounds_of(features)
    for key, value in (("format_version", "1"), ("kind", kind),
                       ("built_at", source_date), ("bounds", bounds),
                       ("min_zoom", str(zooms[0])), ("max_zoom", str(zooms[1])),
                       ("lane_count",
                        str(0 if kind == "overview" else len(features)))):
        db.execute("INSERT INTO meta VALUES (?,?)", (key, value))

    db.commit()
    db.execute("VACUUM")
    db.close()
    return stats


def _bounds_of(features):
    """west,south,east,north over everything in the container.

    The app needs this to tell "there are no rights of way here" from "I have
    nothing downloaded for here", which is a distinction a rider's safety turns
    on - see section 16.3 of the spec.
    """
    lons, lats = [], []
    for f in features:
        for line in lines_of(f):
            for lon, lat in line:
                lons.append(lon)
                lats.append(lat)
    if not lons:
        return ""
    return "%.6f,%.6f,%.6f,%.6f" % (min(lons), min(lats), max(lons), max(lats))


def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--area", action="store_true")
    group.add_argument("--overview", action="store_true")
    group.add_argument("--both", action="store_true")
    ap.add_argument("out")
    ap.add_argument("packs", nargs="+")
    ap.add_argument("--key", default=os.environ.get(
        "DATASET_KEY_FILE",
        r"C:\Users\lucas\Desktop\greenroadmap-keys\dataset-encryption-key-256.b64"))
    args = ap.parse_args()

    key = base64.b64decode(open(args.key).read().strip())
    features = load_features(args.packs, key)
    kind = "overview" if args.overview else ("both" if args.both else "area")
    zooms = (OVERVIEW_ZOOMS if args.overview
             else ((OVERVIEW_ZOOMS[0], AREA_ZOOMS[1]) if args.both
                   else AREA_ZOOMS))

    # From the source, never the clock, so an unchanged area rebuilds
    # byte-identical and nobody downloads it again (section 18.1).
    source_date = max(os.path.basename(p).rsplit("-", 3)[-3:][0] for p in args.packs) \
        if args.packs else "unknown"
    try:
        source_date = json.loads('"%s"' % unpack(sorted(args.packs)[0], key).get("generated", ""))
    except Exception:
        pass

    stats = write_container(args.out, features, kind, zooms, str(source_date))

    size = os.path.getsize(args.out)
    print("%s  %s  %d lanes" % (args.out, kind, len(features)))
    print("  %-6s %8s %10s %10s" % ("zoom", "tiles", "total", "largest"))
    for zoom, (count, total, largest) in stats.items():
        print("  z%-5d %8d %9.1fK %9.1fK" % (zoom, count, total / 1024.0, largest / 1024.0))
    print("  container %.2f MB" % (size / 1048576.0))
    packed = gzip.compress(open(args.out, "rb").read(), mtime=0)
    print("  gzipped   %.2f MB" % (len(packed) / 1048576.0))
    print("  sha256    %s" % hashlib.sha256(open(args.out, "rb").read()).hexdigest()[:16])


if __name__ == "__main__":
    main()

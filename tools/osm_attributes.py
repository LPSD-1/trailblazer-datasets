#!/usr/bin/env python3
"""OSM physical attributes for English and Welsh byways (plan step 1.3).

Produces, for each way in our definitive-map data, the physical columns
`docs/WAYS-SCHEMA.md` names:

    surface, smoothness, tracktype, width_m, min_width_m, barriers

THE HONESTY RULE, AND IT IS THE WHOLE POINT OF THIS FILE.

    NULL means unknown, and unknown is never 'no'.

A way with no `width` tag is not narrow, it is unmeasured. A way we could not
match to anything in OSM gets NULL in every physical column, not a default. An
empty `barriers` array means "OSM maps no barrier here", which is not the same
claim as "there is no barrier here" - F1 measured that OSM carries a
4x4-blocking barrier on under 2% of byways in the best-mapped county in the
sample, so the absence of a barrier record is close to no information at all.
Nothing in this file writes `motorbike_ok`, `fourxfour_ok` or `access_reason`:
hiding a lane from a rider needs evidence, and this tool only reports what
evidence exists.

WHICH SOURCE. Two backends, and the choice is recorded here rather than left
to whoever runs it:

  * `--source extract` (DEFAULT, and the production path) reads a downloaded
    Geofabrik `.osm.pbf` with a PBF reader in this file - standard library
    only, zlib plus a protobuf varint decoder. One download, reproducible,
    offline, no rate limit, and the same bytes give the same answer next week.
    A national build reads `great-britain-latest.osm.pbf` once.

  * `--source overpass` queries the live API in tiles. Good for a county-sized
    measurement like this step's gate, and unfit for a national build: it is
    somebody else's server, the answer changes under you, and F1's own
    measurement lost three counties to HTTP 504 under load. Tiles here are
    cached, retried with backoff, rotated across mirrors, and split in four
    after repeated failure, so a 504 costs a retry rather than a county.

MATCHING. OSM ways are matched to ours by geometry, never by name - the
definitive map calls a way "DY|Great Hucklow-WD41|18/3" and OSM does not name
it at all. See `match_way` for the method and `--decoy-offset-m` for the
control that measures how often it matches something it should not.

    python osm_attributes.py --ways cache/DY/byway_open_to_all_traffic.json
        --extract derbyshire-latest.osm.pbf --county Derbyshire --out dy.json

    python osm_attributes.py --ways cache/SH/byway_open_to_all_traffic.json
        --source overpass --county Shropshire

    python osm_attributes.py ... --decoy-offset-m 500     # the control
"""
import argparse
import json
import math
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

UA = "TrailBlazer-data/1.0 (byway physical attributes; contact via github)"

# Highway values a definitive-map byway can plausibly be carried as in OSM.
# Wide on purpose: matching is done on geometry, and a value filtered out here
# is a way we can never match, which shows up as unknown - the safe direction.
HIGHWAY_KEEP = (
    "track", "unclassified", "residential", "service", "tertiary",
    "secondary", "primary", "road", "living_street", "path", "bridleway",
    "footway", "cycleway", "byway", "pedestrian", "busway",
)

# Barrier kinds worth carrying. `barriers` records what OSM maps; it is not a
# verdict. Which of these stops which vehicle is decided downstream, with the
# evidence attached, per the schema's access_evidence column.
BARRIER_KINDS = (
    "gate", "bollard", "block", "cycle_barrier", "stile", "kissing_gate",
    "lift_gate", "swing_gate", "cattle_grid", "chain", "height_restrictor",
    "jersey_barrier", "log", "motorcycle_barrier", "wicket_gate",
    "hampshire_gate", "sump_buster", "yes",
)

# Matching parameters. Printed in the run header, so any number this tool
# reports can be traced back to the tolerance that produced it.
SAMPLE_M = 25.0            # spacing of the sample points along our way
TOL_M = 20.0               # a sample is "on" an OSM way within this distance
BEARING_COS = 0.70         # |cos(delta bearing)|: about 45 degrees, unsigned
MIN_SAMPLES_PER_WAY = 2    # an OSM way contributing fewer is a junction touch
MIN_SHARE_PER_WAY = 0.10
MIN_UNION_COVER = 0.60     # below this, our way counts as unmatched

M_PER_DEG_LAT = 110574.0


# --------------------------------------------------------------------------
# protobuf, enough of it to read a .osm.pbf
# --------------------------------------------------------------------------

def _varint(buf, i):
    shift = 0
    val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def _zigzag(v):
    return (v >> 1) ^ -(v & 1)


def _fields(buf, start=0, end=None):
    """Yield (field_number, wire_type, value) over one protobuf message.

    value is bytes for wire type 2 and an int for wire types 0, 5 and 1.
    """
    if end is None:
        end = len(buf)
    i = start
    while i < end:
        key, i = _varint(buf, i)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            val, i = _varint(buf, i)
        elif wt == 2:
            ln, i = _varint(buf, i)
            val = buf[i:i + ln]
            i += ln
        elif wt == 5:
            val = struct.unpack_from("<I", buf, i)[0]
            i += 4
        elif wt == 1:
            val = struct.unpack_from("<Q", buf, i)[0]
            i += 8
        else:
            raise ValueError("unsupported wire type %d at offset %d" % (wt, i))
        yield fno, wt, val


def _packed(buf):
    """Decode a packed repeated varint field.

    One loop over the bytes, no per-value function call. Nearly every byte of
    a .osm.pbf passes through here, so this is the loop that decides whether
    reading a county extract takes seconds or minutes.
    """
    out = []
    append = out.append
    val = 0
    shift = 0
    for b in buf:
        if b < 0x80:
            append(val | (b << shift))
            val = 0
            shift = 0
        else:
            val |= (b & 0x7F) << shift
            shift += 7
    return out


def pbf_blobs(path):
    """Yield the decompressed body of every OSMData blob in a .osm.pbf."""
    with open(path, "rb") as fh:
        while True:
            head = fh.read(4)
            if len(head) < 4:
                return
            (hlen,) = struct.unpack(">I", head)
            header = fh.read(hlen)
            btype = None
            dsize = None
            for fno, _wt, val in _fields(header):
                if fno == 1:
                    btype = val.decode("ascii", "replace")
                elif fno == 3:
                    dsize = val
            if dsize is None:
                raise ValueError("blob header with no datasize")
            body = fh.read(dsize)
            if btype != "OSMData":
                continue
            raw = None
            for fno, _wt, val in _fields(body):
                if fno == 1:
                    raw = val                      # stored uncompressed
                elif fno == 3:
                    raw = zlib.decompress(val)     # the usual case
            if raw is not None:
                yield raw


def extract_replication_date(path):
    """The extract's own timestamp, ISO, or None.

    Geofabrik stamps `osmosis_replication_timestamp` into the OSMHeader. It is
    what dates the physical evidence: a rider reading "surface: gravel" is
    entitled to know whether that was surveyed last week or in 2016, and the
    run date is not the same claim as the data date.
    """
    with open(path, "rb") as fh:
        head = fh.read(4)
        if len(head) < 4:
            return None
        (hlen,) = struct.unpack(">I", head)
        header = fh.read(hlen)
        btype = None
        dsize = None
        for fno, _wt, val in _fields(header):
            if fno == 1:
                btype = val.decode("ascii", "replace")
            elif fno == 3:
                dsize = val
        if btype != "OSMHeader" or not dsize:
            return None
        body = fh.read(dsize)
        raw = None
        for fno, _wt, val in _fields(body):
            if fno == 1:
                raw = val
            elif fno == 3:
                raw = zlib.decompress(val)
        if not raw:
            return None
        for fno, _wt, val in _fields(raw):
            if fno == 32:
                return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(val))
    return None


def pbf_block(raw):
    """(stringtable, [primitive group bytes], granularity, lat_off, lon_off)"""
    strings = []
    groups = []
    gran, lat_off, lon_off = 100, 0, 0
    for fno, _wt, val in _fields(raw):
        if fno == 1:
            strings = [s for _f, _w, s in _fields(val)]
        elif fno == 2:
            groups.append(val)
        elif fno == 17:
            gran = val
        elif fno == 19:
            lat_off = val
        elif fno == 20:
            lon_off = val
    return strings, groups, gran, lat_off, lon_off


def pbf_dense_nodes(group_buf):
    """Yield (id, raw_lat, raw_lon, [(key_sid, val_sid)]) from DenseNodes."""
    for fno, _wt, val in _fields(group_buf):
        if fno != 2:
            continue
        ids = []
        lats = []
        lons = []
        kv = []
        for f2, _w2, v2 in _fields(val):
            if f2 == 1:
                ids = _packed(v2)
            elif f2 == 8:
                lats = _packed(v2)
            elif f2 == 9:
                lons = _packed(v2)
            elif f2 == 10:
                kv = _packed(v2)
        nid = lat = lon = 0
        k = 0
        for j in range(len(ids)):
            nid += _zigzag(ids[j])
            lat += _zigzag(lats[j])
            lon += _zigzag(lons[j])
            tags = []
            if kv:
                while k < len(kv) and kv[k] != 0:
                    tags.append((kv[k], kv[k + 1]))
                    k += 2
                k += 1
            yield nid, lat, lon, tags


def pbf_plain_nodes(group_buf):
    """Yield (id, raw_lat, raw_lon, [(key_sid, val_sid)]) from Node messages."""
    for fno, _wt, val in _fields(group_buf):
        if fno != 1:
            continue
        nid = lat = lon = 0
        keys = []
        vals = []
        for f2, _w2, v2 in _fields(val):
            if f2 == 1:
                nid = v2
            elif f2 == 2:
                keys = _packed(v2) if isinstance(v2, bytes) else [v2]
            elif f2 == 3:
                vals = _packed(v2) if isinstance(v2, bytes) else [v2]
            elif f2 == 8:
                lat = _zigzag(v2)
            elif f2 == 9:
                lon = _zigzag(v2)
        yield nid, lat, lon, list(zip(keys, vals))


def pbf_ways(group_buf):
    """Yield (id, [(key_sid, val_sid)], [node ids]) from Way messages."""
    for fno, _wt, val in _fields(group_buf):
        if fno != 3:
            continue
        wid = 0
        keys = []
        vals = []
        refs = []
        for f2, _w2, v2 in _fields(val):
            if f2 == 1:
                wid = v2
            elif f2 == 2:
                keys = _packed(v2)
            elif f2 == 3:
                vals = _packed(v2)
            elif f2 == 8:
                refs = _packed(v2)
        r = 0
        nodes = []
        for d in refs:
            r += _zigzag(d)
            nodes.append(r)
        yield wid, list(zip(keys, vals)), nodes


def _tags(strings, pairs):
    out = {}
    for k, v in pairs:
        try:
            out[strings[k].decode("utf8", "replace")] = \
                strings[v].decode("utf8", "replace")
        except IndexError:
            continue
    return out


def read_extract(path, bbox, progress=None):
    """Ways of interest and barrier nodes from a .osm.pbf, bbox-limited.

    Three streaming passes, so memory is bounded by the bbox rather than by
    the file: a GB extract holds ~250 million nodes and no laptop is going to
    hold them in a dict.

      1. node ids inside the bbox, and barrier nodes among them
      2. highway ways with at least one node in that set
      3. coordinates for the nodes those ways actually use

    Returns ([{id, tags, coords}], [{kind, lat, lon, tags}]).
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    inside = set()
    barrier_nodes = []

    def _say(msg):
        if progress:
            progress(msg)

    # pass 1 ------------------------------------------------------------
    seen = 0
    for raw in pbf_blobs(path):
        strings, groups, gran, lat_off, lon_off = pbf_block(raw)
        scale = gran * 1e-9
        for g in groups:
            for nid, rlat, rlon, tags in pbf_dense_nodes(g):
                lat = lat_off * 1e-9 + scale * rlat
                lon = lon_off * 1e-9 + scale * rlon
                seen += 1
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                    inside.add(nid)
                    if tags:
                        t = _tags(strings, tags)
                        if "barrier" in t:
                            barrier_nodes.append({
                                "kind": t["barrier"], "lat": lat, "lon": lon,
                                "tags": t})
            for nid, rlat, rlon, tags in pbf_plain_nodes(g):
                lat = lat_off * 1e-9 + scale * rlat
                lon = lon_off * 1e-9 + scale * rlon
                seen += 1
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                    inside.add(nid)
                    t = _tags(strings, tags)
                    if "barrier" in t:
                        barrier_nodes.append({
                            "kind": t["barrier"], "lat": lat, "lon": lon,
                            "tags": t})
    _say("pass 1: %d nodes read, %d inside the bbox, %d barriers"
         % (seen, len(inside), len(barrier_nodes)))

    # pass 2 ------------------------------------------------------------
    kept = []
    needed = set()
    ways_seen = 0
    for raw in pbf_blobs(path):
        strings, groups, _gran, _la, _lo = pbf_block(raw)
        for g in groups:
            for wid, pairs, refs in pbf_ways(g):
                ways_seen += 1
                if not refs:
                    continue
                t = _tags(strings, pairs)
                hw = t.get("highway")
                if hw not in HIGHWAY_KEEP:
                    continue
                if not any(r in inside for r in refs):
                    continue
                kept.append({"id": wid, "tags": t, "refs": refs})
                needed.update(refs)
    _say("pass 2: %d ways read, %d highway ways in the bbox, %d nodes needed"
         % (ways_seen, len(kept), len(needed)))

    # pass 3 ------------------------------------------------------------
    coords = {}
    for raw in pbf_blobs(path):
        _strings, groups, gran, lat_off, lon_off = pbf_block(raw)
        scale = gran * 1e-9
        for g in groups:
            for nid, rlat, rlon, _t in pbf_dense_nodes(g):
                if nid in needed:
                    coords[nid] = (lat_off * 1e-9 + scale * rlat,
                                   lon_off * 1e-9 + scale * rlon)
            for nid, rlat, rlon, _t in pbf_plain_nodes(g):
                if nid in needed:
                    coords[nid] = (lat_off * 1e-9 + scale * rlat,
                                   lon_off * 1e-9 + scale * rlon)
    _say("pass 3: %d of %d node positions resolved"
         % (len(coords), len(needed)))

    out = []
    for w in kept:
        pts = [coords[r] for r in w["refs"] if r in coords]
        if len(pts) >= 2:
            out.append({"id": w["id"], "tags": w["tags"], "coords": pts})
    return out, barrier_nodes


# --------------------------------------------------------------------------
# Overpass, for a county-sized run
# --------------------------------------------------------------------------

MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
)

TILE_QUERY = """[out:json][timeout:%(timeout)d];
(
  way["highway"~"^(%(highways)s)$"](%(s).5f,%(w).5f,%(n).5f,%(e).5f);
  node["barrier"](%(s).5f,%(w).5f,%(n).5f,%(e).5f);
);
out geom tags;"""


def overpass_tile(bbox, cache_dir, timeout=180, tries=4, gap_s=1.0,
                  progress=None, depth=0):
    """One tile, cached. Retries 429/504 with backoff, then splits in four.

    F1 lost Powys, North Yorkshire and Cornwall to HTTP 504 under load and did
    not retry them. This is that fix: a tile that keeps failing is cut into
    four smaller ones, which is what Overpass is actually complaining about.
    """
    s, w, n, e = bbox
    key = "osm_%.4f_%.4f_%.4f_%.4f.json" % (s, w, n, e)
    path = os.path.join(cache_dir, key)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, encoding="utf8") as fh:
                return json.load(fh)["elements"]
        except (ValueError, OSError, KeyError):
            os.remove(path)

    query = TILE_QUERY % {"timeout": timeout, "highways": "|".join(HIGHWAY_KEEP),
                          "s": s, "w": w, "n": n, "e": e}
    last = None
    for attempt in range(tries):
        url = MIRRORS[attempt % len(MIRRORS)]
        try:
            req = urllib.request.Request(
                url, data=query.encode("utf8"), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout + 60) as r:
                raw = r.read()
            parsed = json.loads(raw.decode("utf8", "replace"))
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(raw)
            time.sleep(gap_s)
            return parsed["elements"]
        except urllib.error.HTTPError as exc:
            last = "HTTP %d" % exc.code
            if exc.code not in (429, 504, 502, 503, 500):
                break
        except Exception as exc:                      # noqa: BLE001
            last = str(exc)
        wait = 5 * (2 ** attempt)
        if progress:
            progress("  tile %.3f,%.3f: %s - retry in %ds" % (s, w, last, wait))
        time.sleep(wait)

    if depth >= 3:
        raise RuntimeError("tile %s failed after splitting: %s" % (bbox, last))
    if progress:
        progress("  tile %.3f,%.3f: %s - splitting in four" % (s, w, last))
    mid_lat = (s + n) / 2.0
    mid_lon = (w + e) / 2.0
    out = []
    for sub in ((s, w, mid_lat, mid_lon), (s, mid_lon, mid_lat, e),
                (mid_lat, w, n, mid_lon), (mid_lat, mid_lon, n, e)):
        out.extend(overpass_tile(sub, cache_dir, timeout, tries, gap_s,
                                 progress, depth + 1))
    return out


def tiles_covering(ways, tile_deg):
    """Only the tiles our ways actually touch - a county is mostly not byway."""
    want = set()
    for way in ways:
        for lat, lon in way["coords"]:
            want.add((math.floor(lat / tile_deg), math.floor(lon / tile_deg)))
    out = []
    for ty, tx in sorted(want):
        out.append((ty * tile_deg, tx * tile_deg,
                    (ty + 1) * tile_deg, (tx + 1) * tile_deg))
    return out


def read_overpass(ways, cache_dir, tile_deg=0.05, progress=None):
    """Returns ([{id, tags, coords}], [{kind, lat, lon, tags}])."""
    tiles = tiles_covering(ways, tile_deg)
    if progress:
        progress("overpass: %d tiles of %.3f deg" % (len(tiles), tile_deg))
    osm_ways = {}
    barriers = {}
    for i, bbox in enumerate(tiles, 1):
        elements = overpass_tile(bbox, cache_dir, progress=progress)
        for el in elements:
            if el.get("type") == "way" and el.get("geometry"):
                osm_ways[el["id"]] = {
                    "id": el["id"], "tags": el.get("tags", {}),
                    "coords": [(p["lat"], p["lon"]) for p in el["geometry"]]}
            elif el.get("type") == "node" and el.get("tags", {}).get("barrier"):
                barriers[el["id"]] = {
                    "kind": el["tags"]["barrier"], "lat": el["lat"],
                    "lon": el["lon"], "tags": el["tags"]}
        if progress and (i % 20 == 0 or i == len(tiles)):
            progress("  %d/%d tiles, %d ways, %d barriers"
                     % (i, len(tiles), len(osm_ways), len(barriers)))
    return list(osm_ways.values()), list(barriers.values())


# --------------------------------------------------------------------------
# our ways
# --------------------------------------------------------------------------

def load_our_ways(path, county=None, authority=None):
    """rowmaps GeoJSON -> ([{uid, name, county, authority, coords}], skipped).

    `skipped` names every feature we could not use. Kent publishes one byway
    whose LineString holds a single coordinate; dropping it is right and
    dropping it silently is not, because a way that vanishes between the
    council's file and ours is exactly the fault nobody notices.

    rowmaps packs the record id into `Description`:
        BO|DY:16247|0.113|none|-1.73|53.29|-1.73|53.29|417846,378003|...
    Field 1 is the authority's own id, which is what makes `way_uid` stable
    across builds - a positional index would move the moment a council adds a
    row, and every rider's saved way would point somewhere else.
    """
    with open(path, encoding="utf8") as fh:
        doc = json.load(fh)
    out = []
    skipped = []
    for i, feat in enumerate(doc.get("features", [])):
        props = feat.get("properties") or {}
        label = props.get("Name") or "feature %d" % i
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            skipped.append("%s: geometry is %s" % (label, geom.get("type")))
            continue
        coords = [(float(lat), float(lon)) for lon, lat in geom["coordinates"]]
        if len(coords) < 2:
            skipped.append("%s: %d coordinate(s), not a line"
                           % (label, len(coords)))
            continue
        desc = (props.get("Description") or "").split("|")
        uid = desc[1] if len(desc) > 1 and ":" in desc[1] else None
        if not uid:
            uid = "%s:%d" % (authority or os.path.basename(
                os.path.dirname(path)), i)
        out.append({"uid": uid, "name": props.get("Name"), "county": county,
                    "authority": (authority or uid.split(":")[0]),
                    "coords": coords})
    return out, skipped


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def _mpd_lon(lat):
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


def to_metres(coords, lat0):
    k = _mpd_lon(lat0)
    return [(lon * k, lat * M_PER_DEG_LAT) for lat, lon in coords]


def polyline_length(pts):
    return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
               for i in range(len(pts) - 1))


def sample_polyline(pts, spacing=SAMPLE_M, minimum=3):
    """Points every `spacing` metres along a metric polyline, with bearings."""
    total = polyline_length(pts)
    n = max(minimum, int(total / spacing) + 1)
    step = total / (n - 1) if n > 1 else 0.0
    out = []
    seg = 0
    acc = 0.0
    for i in range(n):
        target = step * i
        while seg < len(pts) - 2:
            d = math.hypot(pts[seg + 1][0] - pts[seg][0],
                           pts[seg + 1][1] - pts[seg][1])
            if acc + d >= target or d == 0:
                break
            acc += d
            seg += 1
        ax, ay = pts[seg]
        bx, by = pts[seg + 1]
        d = math.hypot(bx - ax, by - ay)
        t = 0.0 if d == 0 else min(1.0, max(0.0, (target - acc) / d))
        ang = math.atan2(by - ay, bx - ax) if d else 0.0
        out.append((ax + (bx - ax) * t, ay + (by - ay) * t, ang))
    return out, total


def point_segment(px, py, ax, ay, bx, by):
    """(distance, segment bearing) from a point to a segment."""
    dx, dy = bx - ax, by - ay
    dd = dx * dx + dy * dy
    if dd == 0:
        return math.hypot(px - ax, py - ay), 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / dd
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    cx, cy = ax + dx * t, ay + dy * t
    return math.hypot(px - cx, py - cy), math.atan2(dy, dx)


class SegmentIndex(object):
    """A grid over OSM segments. Cells are expanded by the tolerance, so the
    single cell holding a query point holds every segment within TOL_M of it.

    `restrict`, when given, is the set of cells anywhere near one of our ways.
    Segments outside it are dropped unindexed: a county extract holds ~70,000
    highway ways and ~160 byways, and indexing the other 69,840 costs hundreds
    of megabytes to answer no question.
    """

    def __init__(self, cell=200.0, tol=TOL_M, restrict=None):
        self.cell = cell
        self.tol = tol
        self.restrict = restrict
        self.grid = {}

    def cells_for_point(self, px, py, ring=1):
        gx = int(math.floor(px / self.cell))
        gy = int(math.floor(py / self.cell))
        return [(gx + dx, gy + dy)
                for dx in range(-ring, ring + 1)
                for dy in range(-ring, ring + 1)]

    def add_way(self, way_id, pts):
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            lo_x = min(ax, bx) - self.tol
            hi_x = max(ax, bx) + self.tol
            lo_y = min(ay, by) - self.tol
            hi_y = max(ay, by) + self.tol
            for gx in range(int(math.floor(lo_x / self.cell)),
                            int(math.floor(hi_x / self.cell)) + 1):
                for gy in range(int(math.floor(lo_y / self.cell)),
                                int(math.floor(hi_y / self.cell)) + 1):
                    if self.restrict is not None and \
                            (gx, gy) not in self.restrict:
                        continue
                    self.grid.setdefault((gx, gy), []).append(
                        (way_id, ax, ay, bx, by))

    def near(self, px, py):
        return self.grid.get((int(math.floor(px / self.cell)),
                              int(math.floor(py / self.cell))), ())


def match_way(samples, index, tol=TOL_M, bearing_cos=BEARING_COS):
    """Which OSM ways lie under our way, and over how much of it.

    THE METHOD, stated so it can be argued with:

      * our way is sampled every 25 m (at least 3 points), each sample
        carrying the local bearing of our line;
      * a sample is "on" an OSM way when some segment of it passes within
        20 m AND runs within about 45 degrees of our bearing, unsigned - the
        bearing test is what stops a lane being matched to the road it
        crosses, which a distance-only test matches at every junction;
      * candidates are taken greedily, most new samples first, keeping only
        those covering at least 2 samples and 10% of the way, because a
        definitive-map way is routinely five or ten OSM ways end to end;
      * our way is MATCHED when the union covers at least 60% of it.

    Returns (list of (osm_way_id, samples_covered), union_cover_fraction).
    """
    total = len(samples)
    hits = {}
    for si, (px, py, ang) in enumerate(samples):
        for way_id, ax, ay, bx, by in index.near(px, py):
            dist, seg_ang = point_segment(px, py, ax, ay, bx, by)
            if dist > tol:
                continue
            if abs(math.cos(seg_ang - ang)) < bearing_cos:
                continue
            hits.setdefault(way_id, set()).add(si)

    chosen = []
    covered = set()
    while hits:
        best_id, best_new = None, set()
        for way_id, s in hits.items():
            new = s - covered
            if len(new) > len(best_new):
                best_id, best_new = way_id, new
        if best_id is None:
            break
        full = hits.pop(best_id)
        if len(full) < MIN_SAMPLES_PER_WAY or \
                len(full) < MIN_SHARE_PER_WAY * total:
            continue
        if not best_new:
            continue
        covered |= best_new
        chosen.append((best_id, len(full)))
    return chosen, (len(covered) / float(total) if total else 0.0)


# --------------------------------------------------------------------------
# attributes
# --------------------------------------------------------------------------

_WIDTH_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(m|metre|metres|meter|meters|ft|feet|'|\")?\s*$",
    re.I)
_FEET_INCHES_RE = re.compile(r"^\s*(\d+)\s*'\s*(\d+(?:\.\d+)?)\s*\"?\s*$")


def parse_width(value):
    """OSM width -> metres, or None. None is the answer far more often than
    anyone expects, and a guess here becomes a lane hidden from a rider."""
    if value is None:
        return None
    text = str(value).strip()
    m = _FEET_INCHES_RE.match(text)
    if m:
        return round((float(m.group(1)) * 12 + float(m.group(2))) * 0.0254, 3)
    m = _WIDTH_RE.match(text)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "m").lower()
    if unit in ("ft", "feet", "'"):
        val *= 0.3048
    elif unit == '"':
        val *= 0.0254
    if val <= 0 or val > 100:
        return None                       # 0 m and 999 m are both data entry
    return round(val, 3)


def way_width(tags):
    """The narrowest credible width this OSM way states, in metres, or None.

    `maxwidth` is a legal limit and `width` is the carriageway; where a way
    carries both, the smaller is the one a 4x4 has to fit through.
    """
    widths = []
    for key in ("width", "maxwidth", "est_width", "maxwidth:physical"):
        w = parse_width(tags.get(key))
        if w is not None:
            widths.append(w)
    return min(widths) if widths else None


def barrier_gap(tags):
    """A barrier node's stated gap in metres, or None. Never a default: an
    unmeasured bollard is not a 1.2 m bollard, whatever the usual bollard is."""
    for key in ("maxwidth", "width", "opening", "maxwidth:physical"):
        w = parse_width(tags.get(key))
        if w is not None:
            return w
    return None


def attributes_for(chosen, osm_by_id, barriers_for_way):
    """The schema's physical columns for one of our ways.

    Every value here is either something OSM states or None. Nothing is
    inferred from the absence of a tag, and `barriers` is [] only when we had
    OSM ways to look at - it means "none mapped", never "none there".
    """
    surface = {}
    smoothness = {}
    tracktype = {}
    widths = []
    for way_id, weight in chosen:
        tags = osm_by_id[way_id]["tags"]
        if tags.get("surface"):
            surface[tags["surface"]] = surface.get(tags["surface"], 0) + weight
        if tags.get("smoothness"):
            smoothness[tags["smoothness"]] = \
                smoothness.get(tags["smoothness"], 0) + weight
        if tags.get("tracktype"):
            tracktype[tags["tracktype"]] = \
                tracktype.get(tags["tracktype"], 0) + weight
        w = way_width(tags)
        if w is not None:
            widths.append((w, weight))

    def _dominant(counts):
        if not counts:
            return None
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    width_m = None
    if widths:
        total = sum(w for _v, w in widths)
        width_m = round(sum(v * w for v, w in widths) / total, 2)

    narrow = [v for v, _w in widths]
    for b in barriers_for_way:
        gap = barrier_gap(b.get("tags") or {})
        if gap is not None:
            narrow.append(gap)
    min_width_m = round(min(narrow), 2) if narrow else None

    return {
        "surface": _dominant(surface),
        "smoothness": _dominant(smoothness),
        "tracktype": _dominant(tracktype),
        "width_m": width_m,
        "min_width_m": min_width_m,
        "barriers": [{"kind": b["kind"], "lat": round(b["lat"], 6),
                      "lon": round(b["lon"], 6)} for b in barriers_for_way],
    }


NULL_ATTRS = {"surface": None, "smoothness": None, "tracktype": None,
              "width_m": None, "min_width_m": None, "barriers": None}


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def shift_ways(ways, metres):
    """Move every way north by `metres`. The decoy control: whatever this
    matches, the matcher would have matched wrongly."""
    out = []
    for way in ways:
        dlat = metres / M_PER_DEG_LAT
        out.append(dict(way, coords=[(lat + dlat, lon)
                                     for lat, lon in way["coords"]]))
    return out


def bbox_of(ways, pad_deg=0.01):
    lats = [lat for w in ways for lat, _lon in w["coords"]]
    lons = [lon for w in ways for _lat, lon in w["coords"]]
    return (min(lats) - pad_deg, min(lons) - pad_deg,
            max(lats) + pad_deg, max(lons) + pad_deg)


def run(our_ways, osm_ways, barrier_nodes, tol=TOL_M, barrier_tol_m=2.0):
    """Match, then read attributes off the matches. Returns (rows, stats)."""
    if not our_ways:
        return [], {}
    lat0 = sum(lat for w in our_ways for lat, _l in w["coords"]) / \
        sum(len(w["coords"]) for w in our_ways)

    # Sample our ways first: their samples decide which cells are worth
    # indexing, and they are needed again for matching.
    ours = []
    probe = SegmentIndex(tol=tol)
    wanted = set()
    for way in our_ways:
        pts = to_metres(way["coords"], lat0)
        samples, length_m = sample_polyline(pts)
        ours.append((way, pts, samples, length_m))
        for px, py, _ang in samples:
            wanted.update(probe.cells_for_point(px, py))

    osm_by_id = {}
    index = SegmentIndex(tol=tol, restrict=wanted)
    for way in osm_ways:
        pts = to_metres(way["coords"], lat0)
        if len(pts) < 2:
            continue
        osm_by_id[way["id"]] = {"tags": way["tags"], "pts": pts}
        index.add_way(way["id"], pts)

    # Barrier nodes are attached to the OSM way they sit on. On a way a
    # barrier IS one of its nodes, so the tolerance only absorbs rounding.
    # Through the same grid index: a county holds tens of thousands of each,
    # and the obvious nested loop over ways is quadratic and unusable.
    barrier_index = {}
    for b in barrier_nodes:
        if b["kind"] not in BARRIER_KINDS:
            continue
        bx, by = to_metres([(b["lat"], b["lon"])], lat0)[0]
        best = None
        for way_id, ax, ay, cx, cy in index.near(bx, by):
            d, _a = point_segment(bx, by, ax, ay, cx, cy)
            if d <= barrier_tol_m and (best is None or d < best[1]):
                best = (way_id, d)
        if best:
            barrier_index.setdefault(best[0], []).append(b)

    rows = []
    stats = {"ways": len(our_ways), "matched": 0, "osm_ways_used": set(),
             "cover_sum": 0.0, "length_m": 0.0, "matched_length_m": 0.0,
             "designation_corroborated": 0, "designation_present": 0,
             "designation_restricted": 0}
    for way, pts, samples, length_m in ours:
        chosen, cover = match_way(samples, index, tol=tol)
        stats["length_m"] += length_m
        matched = cover >= MIN_UNION_COVER and bool(chosen)
        row = {"way_uid": way["uid"], "name": way["name"],
               "county": way["county"], "authority": way["authority"],
               "length_m": round(length_m, 1),
               # Only ways we actually read attributes from. Half a lane found
               # is not a lane found, and a row that lists the OSM ways behind
               # a verdict it did not reach invites somebody to use them.
               "osm_way_ids": [i for i, _w in chosen] if matched else [],
               "candidate_osm_way_ids": [] if matched
               else [i for i, _w in chosen],
               "match_cover": round(cover, 3)}
        if matched:
            stats["matched"] += 1
            stats["cover_sum"] += cover
            stats["matched_length_m"] += length_m
            stats["osm_ways_used"].update(i for i, _w in chosen)
            # A matched OSM way usually runs past the end of ours, so a
            # barrier on it is only ours if it is also beside OUR line. The
            # bollard half a mile up the road is not on this lane.
            barriers = []
            for way_id, _w in chosen:
                for b in barrier_index.get(way_id, []):
                    bx, by = to_metres([(b["lat"], b["lon"])], lat0)[0]
                    near = min(
                        point_segment(bx, by, pts[i][0], pts[i][1],
                                      pts[i + 1][0], pts[i + 1][1])[0]
                        for i in range(len(pts) - 1))
                    if near <= tol:
                        barriers.append(b)
            row.update(attributes_for(chosen, osm_by_id, barriers))
            # Independent check: matching never looked at `designation`, so
            # OSM agreeing that this is a byway is evidence about the match.
            # `restricted_byway` does NOT corroborate a BOAT - it contradicts
            # it, and counting it as agreement is how a match rate flatters
            # itself. It is counted separately because the disagreement is
            # worth reading on its own.
            desig = [osm_by_id[i]["tags"].get("designation", "")
                     for i, _w in chosen]
            if any(d for d in desig):
                stats["designation_present"] += 1
                if any("byway" in d and "restricted" not in d for d in desig):
                    stats["designation_corroborated"] += 1
                elif any("restricted" in d for d in desig):
                    stats["designation_restricted"] += 1
        else:
            row.update(NULL_ATTRS)
        rows.append(row)
    stats["osm_ways_used"] = len(stats["osm_ways_used"])
    return rows, stats


def coverage(rows):
    """The percentages the gate is read from."""
    n = len(rows) or 1
    def pct(f):
        return 100.0 * sum(1 for r in rows if f(r)) / n
    any_physical = pct(lambda r: any(
        r.get(k) is not None for k in
        ("surface", "smoothness", "tracktype", "width_m")))
    return {
        "ways": len(rows),
        "matched_pct": pct(lambda r: r.get("match_cover", 0) >= MIN_UNION_COVER
                           and r.get("osm_way_ids")),
        "width_pct": pct(lambda r: r.get("width_m") is not None),
        "surface_pct": pct(lambda r: r.get("surface") is not None),
        "smoothness_pct": pct(lambda r: r.get("smoothness") is not None),
        "tracktype_pct": pct(lambda r: r.get("tracktype") is not None),
        "any_physical_pct": any_physical,
        "with_barrier_pct": pct(lambda r: r.get("barriers")),
    }


DESIGNATION_BYWAY = re.compile(
    r"byway_open_to_all_traffic|public_byway|byway", re.I)


def extract_designations(path):
    """Every way in a .osm.pbf whose designation says byway, tags only.

    No coordinates, so this is one cheap pass over the file and it covers the
    WHOLE extract rather than the bbox around our ways. That matters: the
    bbox around Derbyshire's BOATs holds 345 such ways and the county holds
    415, and comparing the smaller set against osm-coverage.md's county-wide
    figures moved every percentage by several points.
    """
    out = []
    for raw in pbf_blobs(path):
        strings, groups, _g, _la, _lo = pbf_block(raw)
        for grp in groups:
            for _wid, pairs, _refs in pbf_ways(grp):
                tags = _tags(strings, pairs)
                if DESIGNATION_BYWAY.search(tags.get("designation", "")):
                    out.append(tags)
    return out


def designation_basis(tag_sets):
    """Step 0.1's measurement, recomputed from whatever source we just read.

    0.1 counted tags over OSM ways SELECTED BY THEIR OWN designation tag; this
    tool counts them over OUR definitive-map ways, and one of ours is commonly
    five of theirs. The two denominators give different percentages from the
    same data, so the gate compares like with like by computing both. This is
    the one that is comparable to `docs/measurements/osm-coverage.md`.

    Note what 0.1's own regex does, because it is load-bearing and it is not
    obvious: `designation ~ ...|byway` is unanchored, so `restricted_byway`
    matches it. In Derbyshire that is 255 of the 415 ways counted - ways the
    schema greys and never draws green. The figures are reproduced here with
    that flaw intact, because the gate compares against them; `boat_only`
    reports the same file without it.
    """
    sel = [t for t in tag_sets
           if DESIGNATION_BYWAY.search(t.get("designation", ""))]
    n = len(sel) or 1

    def pct(keys, subset=None):
        s = sel if subset is None else subset
        m = len(s) or 1
        return 100.0 * sum(1 for t in s if any(t.get(k) for k in keys)) / m

    boat = [t for t in sel if "restricted" not in t.get("designation", "")]
    return {
        "osm_byways": len(sel),
        "restricted_byways_included": len(sel) - len(boat),
        "boat_only": {
            "osm_byways": len(boat),
            "width_pct": pct(("width", "maxwidth", "est_width"), boat),
            "surface_pct": pct(("surface",), boat),
            "smoothness_pct": pct(("smoothness",), boat),
            "tracktype_pct": pct(("tracktype",), boat),
            "any_physical_pct": pct(("width", "maxwidth", "est_width",
                                     "surface", "smoothness", "tracktype"),
                                    boat),
        },
        "width_pct": pct(("width", "maxwidth", "est_width")),
        "surface_pct": pct(("surface",)),
        "smoothness_pct": pct(("smoothness",)),
        "tracktype_pct": pct(("tracktype",)),
        "any_physical_pct": pct(("width", "maxwidth", "est_width", "surface",
                                 "smoothness", "tracktype")),
        "motor_vehicle_pct": pct(("motor_vehicle",)),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ways", required=True,
                    help="rowmaps GeoJSON of our ways (one authority, one type)")
    ap.add_argument("--source", choices=("extract", "overpass"),
                    default="extract")
    ap.add_argument("--extract", help="path to a Geofabrik .osm.pbf")
    ap.add_argument("--county")
    ap.add_argument("--authority")
    ap.add_argument("--out", help="write the rows here as JSON")
    ap.add_argument("--cache", help="tile cache directory (overpass)")
    ap.add_argument("--tile-deg", type=float, default=0.05)
    ap.add_argument("--limit", type=int, help="first N of our ways only")
    ap.add_argument("--decoy-offset-m", type=float, default=0.0,
                    help="move our ways north by N m before matching; the "
                         "control - anything matched is a false positive")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    def say(msg):
        if not args.quiet:
            print(msg)
            sys.stdout.flush()

    our, skipped = load_our_ways(args.ways, args.county, args.authority)
    if args.limit:
        our = our[:args.limit]
    if not our:
        sys.exit("no ways read from %s" % args.ways)
    say("our ways: %d from %s" % (len(our), args.ways))
    for line in skipped:
        say("  SKIPPED %s" % line)
    say("matching: sample %.0f m, tolerance %.0f m, bearing |cos|>=%.2f, "
        "union cover >= %.0f%%"
        % (SAMPLE_M, TOL_M, BEARING_COS, 100 * MIN_UNION_COVER))

    if args.decoy_offset_m:
        our = shift_ways(our, args.decoy_offset_m)
        say("DECOY CONTROL: ways moved %.0f m north. Every match below is a "
            "false positive." % args.decoy_offset_m)

    started = time.time()
    osm_date = None
    if args.source == "extract":
        if not args.extract:
            sys.exit("--source extract needs --extract <file.osm.pbf>")
        osm_date = extract_replication_date(args.extract)
        say("extract: %s, OSM as of %s" % (args.extract, osm_date or "unknown"))
        osm_ways, barriers = read_extract(args.extract, bbox_of(our),
                                          progress=say)
    else:
        cache = args.cache or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "cache", "osm")
        os.makedirs(cache, exist_ok=True)
        osm_ways, barriers = read_overpass(our, cache, args.tile_deg, say)
    say("osm: %d ways, %d barrier nodes (%.0fs)"
        % (len(osm_ways), len(barriers), time.time() - started))

    rows, stats = run(our, osm_ways, barriers)
    cov = coverage(rows)

    say("")
    say("--- %s: %d ways ---" % (args.county or args.ways, cov["ways"]))
    say("  matched to OSM      %6.1f%%  (%d ways, %d OSM ways used)"
        % (cov["matched_pct"], stats["matched"], stats["osm_ways_used"]))
    if stats["matched"]:
        say("  mean cover          %6.1f%%"
            % (100 * stats["cover_sum"] / stats["matched"]))
        say("  OSM agrees it is a byway %4.1f%% of the matched ways that carry"
            " any designation (%d of %d); %d say restricted byway, which is a"
            " disagreement, not a match error"
            % (100.0 * stats["designation_corroborated"] /
               max(1, stats["designation_present"]),
               stats["designation_corroborated"], stats["designation_present"],
               stats["designation_restricted"]))
    say("  width               %6.1f%%" % cov["width_pct"])
    say("  surface             %6.1f%%" % cov["surface_pct"])
    say("  smoothness          %6.1f%%" % cov["smoothness_pct"])
    say("  tracktype           %6.1f%%" % cov["tracktype_pct"])
    say("  any physical        %6.1f%%" % cov["any_physical_pct"])
    say("  carries a barrier   %6.1f%%" % cov["with_barrier_pct"])
    say("  NULL is unknown, not 'no'. An unmatched way carries NULL in every")
    say("  physical column and no barrier list at all.")

    if args.source == "extract":
        tag_sets = extract_designations(args.extract)     # the whole county
    else:
        tag_sets = [w["tags"] for w in osm_ways]          # the tiles we read
    basis = designation_basis(tag_sets)
    boat = basis["boat_only"]
    say("")
    say("--- the same source on step 0.1's denominator: %d OSM ways whose own"
        % basis["osm_byways"])
    say("    designation says byway (osm-coverage.md counts these, not ours);")
    say("    %d of them are restricted byways, which 0.1's regex also matched"
        % basis["restricted_byways_included"])
    say("                       0.1's basis   BOATs only (%d)"
        % boat["osm_byways"])
    for label, key in (("width", "width_pct"), ("surface", "surface_pct"),
                       ("smoothness", "smoothness_pct"),
                       ("tracktype", "tracktype_pct"),
                       ("any physical", "any_physical_pct")):
        say("  %-18s %8.1f%%    %8.1f%%" % (label, basis[key], boat[key]))
    say("  %-18s %8.1f%%" % ("motor_vehicle", basis["motor_vehicle_pct"]))

    if args.out:
        with open(args.out, "w", encoding="utf8") as fh:
            json.dump({"county": args.county, "source": args.source,
                       # The date of the evidence, not the date of the run.
                       "osm_source_date": osm_date,
                       "ways": args.ways, "skipped_features": skipped,
                       "coverage": cov,
                       "designation_basis": basis,
                       "match": {
                           "matched": stats["matched"],
                           "osm_ways_used": stats["osm_ways_used"],
                           "designation_corroborated":
                               stats["designation_corroborated"],
                           "designation_present":
                               stats["designation_present"],
                           "designation_restricted":
                               stats["designation_restricted"]},
                       "matching": {"sample_m": SAMPLE_M, "tol_m": TOL_M,
                                    "bearing_cos": BEARING_COS,
                                    "min_union_cover": MIN_UNION_COVER},
                       "rows": rows}, fh, indent=1)
        say("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

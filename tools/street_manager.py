#!/usr/bin/env python3
"""Street works and temporary closures on byways, from DfT Street Manager.

    python tools/street_manager.py usrn  --gpkg osopenusrn.gpkg
    python tools/street_manager.py fetch [--months 3]

Two steps, both read only:

  usrn   Give every published byway the Unique Street Reference Numbers of
         the streets it runs along, from OS Open USRN (a GB GeoPackage under
         the Open Government Licence). Highway authorities record most
         byways as streets in the national gazetteer, and Street Manager
         files every activity against one: "SOUTH MOLTON FOOTPATH 33" is
         USRN 27506247. Written to tro/streetworks/byway-usrn.json.

  fetch  Read the last few months of Street Manager's public activity
         archive (monthly zips on opendata.manage-roadworks.service.gov.uk,
         OGL; England only), keep the newest version of each activity, and
         publish the closures whose USRN is a byway's to
         tro/streetworks/orders/street-manager.json - the shape every council source
         uses, so the order build merges it with D-TRO and the councils' own
         feeds, below all of them in precedence (council_orders.PRECEDENCE).

WHAT COUNTS AS A CLOSURE. Street Manager's activity archive holds skips,
scaffolding, hoardings, events and temporary traffic orders and notices.
Only an activity that shuts the way is published: traffic management
`road_closure`, or a temporary traffic regulation order or notice, or an
activity named a closure. Skips and scaffolding on a byway are not a
closure. A cancelled activity, or one that has ended, is not published.

The permit archive (about 1 GB a month) is not read: the activity archive
is where temporary orders and closures are filed.
"""
import argparse
import datetime
import io
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import polite_http  # noqa: E402
from polite_http import FetchFailed, Refused  # noqa: E402
from osgb import grid_to_wgs84, wgs84_to_grid  # noqa: E402
from council_sources import read_json, write_json  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
USRN_FILE = os.path.join(ROOT, "tro", "streetworks", "byway-usrn.json")
OUT = os.path.join(ROOT, "tro", "streetworks", "orders", "street-manager.json")
ARCHIVE = "https://opendata.manage-roadworks.service.gov.uk/activity/%04d/%02d.zip"
USRN_DOWNLOAD = ("https://api.os.uk/downloads/v1/products/OpenUSRN/"
                 "downloads?area=GB&format=GeoPackage&redirect")

#: A street is a byway's if it runs along this share of the byway...
MIN_SHARE = 0.25
#: ...for at least this far (a crossing road shares ~2 x TOL_M of it).
MIN_ALONG_M = 50.0
#: Within this of the byway's line, the street is on it.
TOL_M = 12.0
STEP_M = 5.0

#: Keep-last-good: a USRN table under this share of the last one is refused.
FLOOR_SHARE = 2.0 / 3.0

SOURCE = {
    "id": "street-manager",
    "kind": "street-manager",
    "name": "Department for Transport - Street Manager open data",
    "licence": "Open Government Licence v3.0",
    "endpoint": "https://opendata.manage-roadworks.service.gov.uk/",
}


# ------------------------------------------------------------ geometry

def gpkg_lines(blob):
    """A GeoPackage geometry blob -> lines of (easting, northing)."""
    if not blob or blob[:2] != b"GP":
        return []
    flags = blob[3]
    env = (flags >> 1) & 7
    size = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(env, 0)
    return wkb_lines(blob[8 + size:])


def wkb_lines(data):
    lines = []

    def read(off):
        order = "<" if data[off] == 1 else ">"
        kind = struct.unpack_from(order + "I", data, off + 1)[0]
        off += 5
        base = kind % 1000
        dims = 2 + (1 if (kind // 1000) in (1, 2) else 0) + \
            (1 if (kind // 1000) == 3 else 0) * 2
        if kind & 0x80000000 or kind & 0x40000000:   # EWKB Z/M flags
            base = kind & 0xFFFF
            dims = 2 + bool(kind & 0x80000000) + bool(kind & 0x40000000)
        if base == 2:
            n = struct.unpack_from(order + "I", data, off)[0]
            off += 4
            pts = []
            for _ in range(n):
                x, y = struct.unpack_from(order + "dd", data, off)
                pts.append((x, y))
                off += 8 * dims
            lines.append(pts)
            return off
        if base in (5, 7):
            n = struct.unpack_from(order + "I", data, off)[0]
            off += 4
            for _ in range(n):
                off = read(off)
            return off
        raise ValueError("unexpected WKB type %d" % kind)

    read(0)
    return [l for l in lines if len(l) >= 2]


def _local(points, lat0, lon0):
    kx = 111320.0 * math.cos(math.radians(lat0))
    return [((lon - lon0) * kx, (lat - lat0) * 110574.0)
            for lon, lat in points]


def _densify(line, step=STEP_M):
    out = [line[0]]
    for (x0, y0), (x1, y1) in zip(line, line[1:]):
        d = math.hypot(x1 - x0, y1 - y0)
        for k in range(1, int(d // step) + 1):
            t = k * step / d
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
        out.append((x1, y1))
    return out


def _seg_dist(p, a, b):
    (x, y), (x0, y0), (x1, y1) = p, a, b
    dx, dy = x1 - x0, y1 - y0
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy)
                                         / L))
    return math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))


# ---------------------------------------------------------------- usrn

def streets_near(conn, e0, n0, e1, n1, margin=TOL_M + 5):
    """{row id: (usrn, blob)} whose bounding box meets this grid box."""
    rows = conn.execute(
        "SELECT o.id, o.usrn, o.geometry FROM rtree_openUSRN_geometry r "
        "JOIN openUSRN o ON o.id = r.id "
        "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ?",
        (e1 + margin, e0 - margin, n1 + margin, n0 - margin))
    return dict((rid, (usrn, blob)) for rid, usrn, blob in rows)


def to_grid(lines):
    """WGS84 lines -> National Grid, by an affine fit at their centre.

    One exact conversion (osgb.wgs84_to_grid) at the centre and the local
    derivative of its inverse; across the few kilometres of one byway that
    is good to a few centimetres, and it is plain arithmetic instead of a
    Newton solve per vertex.
    """
    pts = [p for l in lines for p in l]
    lon_c = sum(p[0] for p in pts) / len(pts)
    lat_c = sum(p[1] for p in pts) / len(pts)
    ec, nc = wgs84_to_grid(lon_c, lat_c)
    lon0, lat0 = grid_to_wgs84(ec, nc)
    lon_e, lat_e = grid_to_wgs84(ec + 100.0, nc)
    lon_n, lat_n = grid_to_wgs84(ec, nc + 100.0)
    a, b = (lon_e - lon0) / 100.0, (lon_n - lon0) / 100.0
    c, d = (lat_e - lat0) / 100.0, (lat_n - lat0) / 100.0
    det = a * d - b * c
    out = []
    for l in lines:
        line = []
        for lon, lat in l:
            dx, dy = lon - lon0, lat - lat0
            line.append((ec + (d * dx - b * dy) / det,
                         nc + (a * dy - c * dx) / det))
        out.append(line)
    return out


#: The byway is searched in pieces this many samples long (STEP_M each), so
#: a long diagonal byway does not pull in every street of every village in
#: its bounding box.
_CHUNK = 40


def usrns_of(way, conn):
    """The USRNs a byway runs along, longest first."""
    samples, length = [], 0.0
    for l in to_grid(way.lines):
        if len(l) < 2:
            continue
        samples.extend(_densify(l))
        length += sum(math.hypot(b[0] - a[0], b[1] - a[1])
                      for a, b in zip(l, l[1:]))
    if not samples:
        return []
    cell = TOL_M
    grid = {}
    for i, (x, y) in enumerate(samples):
        grid.setdefault((int(x // cell), int(y // cell)), []).append(i)
    found = {}
    for k in range(0, len(samples), _CHUNK):
        chunk = samples[k:k + _CHUNK + 1]
        found.update(streets_near(conn, min(p[0] for p in chunk),
                                  min(p[1] for p in chunk),
                                  max(p[0] for p in chunk),
                                  max(p[1] for p in chunk)))
    # Only the cells the byway occupies are worth visiting: a long street's
    # segments mostly lie nowhere near it.
    gx_lo = min(g[0] for g in grid)
    gx_hi = max(g[0] for g in grid)
    gy_lo = min(g[1] for g in grid)
    gy_hi = max(g[1] for g in grid)
    hits = {}
    for _rid, (usrn, blob) in sorted(found.items()):
        if usrn is None:
            continue
        mine = hits.setdefault(str(usrn), set())
        for pts in gpkg_lines(blob):
            for a, b in zip(pts, pts[1:]):
                x0, x1 = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
                y0, y1 = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])
                for gx in range(max(gx_lo, int((x0 - TOL_M) // cell)),
                                min(gx_hi, int((x1 + TOL_M) // cell)) + 1):
                    for gy in range(max(gy_lo, int((y0 - TOL_M) // cell)),
                                    min(gy_hi,
                                        int((y1 + TOL_M) // cell)) + 1):
                        for i in grid.get((gx, gy), ()):
                            if i not in mine and _seg_dist(
                                    samples[i], a, b) <= TOL_M:
                                mine.add(i)
    out = []
    for usrn, idx in hits.items():
        share = len(idx) / float(len(samples))
        if share >= MIN_SHARE and share * length >= MIN_ALONG_M:
            out.append((-share, usrn))
    return [u for _s, u in sorted(out)]


def download_usrn(client):
    """This month's OS Open USRN GeoPackage -> (path, temp dir to remove)."""
    body = client.get(USRN_DOWNLOAD)
    tmp = tempfile.mkdtemp(prefix="usrn-")
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = [n for n in z.namelist() if n.endswith(".gpkg")]
        if not names:
            raise FetchFailed("no GeoPackage in the OS Open USRN download")
        z.extract(names[0], tmp)
    return os.path.join(tmp, names[0]), tmp


def build_usrn_table(byways, gpkg, log=print):
    conn = sqlite3.connect("file:%s?mode=ro" % gpkg.replace("\\", "/"),
                           uri=True)
    by_usrn, matched = {}, 0
    try:
        for k, way in enumerate(sorted(byways.ways.values(),
                                       key=lambda w: w.uid)):
            usrns = usrns_of(way, conn)
            if usrns:
                matched += 1
            for u in usrns:
                by_usrn.setdefault(u, []).append(way.uid)
            if k and k % 1000 == 0:
                log("  %d byways, %d with a USRN" % (k, matched))
    finally:
        conn.close()
    return {"byways": len(byways.ways), "matched": matched,
            "usrns": dict((u, sorted(set(w))) for u, w in
                          sorted(by_usrn.items()))}


# ------------------------------------------------------------- activities

_CLOSURE = re.compile(r"(?i)temporary traffic regulation|\bTTR[ON]\b|"
                      r"road closure|\bclosure\b|\bclosed\b")


def is_closure(o):
    if (o.get("cancelled") or "").lower() == "yes":
        return False
    if o.get("traffic_management_type") == "road_closure":
        return True
    words = " ".join(str(o.get(k) or "") for k in (
        "activity_type_details", "activity_name"))
    return bool(_CLOSURE.search(words))


#: An activity's point must lie this close to the byway it is put on.
NEAR_M = 150.0

_POINT = re.compile(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)", re.I)


def activity_point(wkt):
    """'POINT(377934.1 159051.3)' (National Grid) -> (lon, lat), or None."""
    m = _POINT.search(wkt or "")
    if not m:
        return None
    e, n = float(m.group(1)), float(m.group(2))
    if not (0 < e < 800000 and 0 < n < 1400000):
        return None
    return grid_to_wgs84(e, n)


def distance_to(point, way):
    """Metres from a (lon, lat) point to the nearest part of a byway."""
    lat0 = point[1]
    p = _local([point], lat0, point[0])[0]
    return min(_seg_dist(p, a, b)
               for l in way.lines
               for a, b in zip(_local(l, lat0, point[0]),
                               _local(l, lat0, point[0])[1:]))


def newest_activities(zips):
    """{arn: object_data} keeping each activity's newest event."""
    best = {}
    for raw in zips:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for name in z.namelist():
                if not name.endswith(".json"):
                    continue
                try:
                    event = json.loads(z.read(name).decode("utf-8-sig"))
                except ValueError:
                    continue
                o = event.get("object_data") or {}
                arn = o.get("activity_reference_number") or \
                    event.get("object_reference")
                when = event.get("event_time") or ""
                if arn and (arn not in best or when >= best[arn][0]):
                    best[arn] = (when, o)
    return dict((arn, o) for arn, (_w, o) in best.items())


def items_for(activities, usrn_table, ways_by_uid, today):
    items = []
    for arn, o in sorted(activities.items()):
        usrn = str(o.get("usrn") or "").strip()
        uids = usrn_table.get(usrn)
        if not uids or not is_closure(o):
            continue
        end = (o.get("end_date") or "")[:10]
        start = (o.get("start_date") or "")[:10]
        if not start or (end and end < today):
            continue
        ways = [ways_by_uid[u] for u in uids if u in ways_by_uid]
        # A street can run on as tarmac for miles past the byway it
        # includes: the activity's own point must be on the byway's part.
        point = activity_point(o.get("activity_coordinates"))
        if point is not None:
            ways = [w for w in ways if distance_to(point, w) <= NEAR_M]
        if not ways:
            continue
        what = re.sub(r"\s+", " ", (o.get("activity_name") or
                                     o.get("activity_type_details") or
                                     "street works")).strip(" -")
        street = (o.get("street_name") or "").strip()
        lines = [[[round(x, 5), round(y, 5)] for x, y in l]
                 for w in ways for l in w.lines]
        items.append({
            "id": arn,
            "ref": arn,
            "authority": ways[0].authority,
            "title": "Temporary closure: %s" % what[:120],
            "where": street.title() if street.isupper() else street,
            "vehicles": "all_users",
            "form": "temporary",
            "start": start,
            "end": end or None,
            "url": "https://opendata.manage-roadworks.service.gov.uk/",
            "source_name": "%s, via Street Manager" % (
                (o.get("highway_authority") or "").title() or
                "the highway authority"),
            "ways": sorted(w.uid for w in ways),
            "match": "usrn",
            "usrn": usrn,
            "geometry": {"type": "MultiLineString", "coordinates": lines}
            if len(lines) > 1 else {"type": "LineString",
                                    "coordinates": lines[0]},
        })
    return items


def months_back(today, n):
    y, m = int(today[:4]), int(today[5:7])
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append((y, m))
    return out


def fetch(client, today, months=3, usrn_file=USRN_FILE, out=OUT,
          byways=None, log=print):
    table = read_json(usrn_file)
    if not table or not table.get("usrns"):
        raise SystemExit("no byway USRN table at %s: run `usrn` first"
                         % usrn_file)
    if byways is None:
        from byway_match import load_byways
        byways = load_byways()
    ways_by_uid = byways.ways
    raws, read = [], []
    for y, m in months_back(today, months):
        url = ARCHIVE % (y, m)
        try:
            raws.append(client.get(url))
            read.append("%04d-%02d" % (y, m))
        except FetchFailed as e:
            # The newest month is published on the 1st; until then it is
            # simply not there yet.
            log("  %04d-%02d: %s" % (y, m, e))
    if not raws:
        raise FetchFailed("no Street Manager archive month could be read")
    activities = newest_activities(raws)
    items = items_for(activities, table["usrns"], ways_by_uid, today)
    source = dict(SOURCE, authorities=sorted(set(i["authority"]
                                                 for i in items)))
    data = {"source": source, "months": read, "records": len(activities),
            "items": items}
    changed = write_json(out, data)
    log("Street Manager: %d activities in %s, %d closures on byways%s"
        % (len(activities), ", ".join(read), len(items),
           "" if changed else " (unchanged)"))
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    u = sub.add_parser("usrn")
    u.add_argument("--gpkg", help="an OS Open USRN GeoPackage already on "
                   "disk; without it, this month's is downloaded")
    u.add_argument("--out", default=USRN_FILE)
    f = sub.add_parser("fetch")
    f.add_argument("--months", type=int, default=3)
    f.add_argument("--today")
    f.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    if args.cmd == "usrn":
        from byway_match import load_byways
        byways = load_byways()
        if len(byways.ways) < 1000:
            print("::error::only %d byways in the published containers"
                  % len(byways.ways))
            return 1
        gpkg, tmp = args.gpkg, None
        if not gpkg:
            try:
                gpkg, tmp = download_usrn(
                    polite_http.PoliteClient(timeout=300))
            except (Refused, FetchFailed, zipfile.BadZipFile) as e:
                print("::error::OS Open USRN could not be read: %s; the "
                      "last byway USRN table is unchanged" % e)
                return 1
        try:
            table = build_usrn_table(byways, gpkg)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        before = (read_json(args.out) or {}).get("matched") or 0
        if before and table["matched"] < before * FLOOR_SHARE:
            print("::error::only %d byways matched a USRN against %d last "
                  "time; kept the last table" % (table["matched"], before))
            return 1
        table["source"] = ("OS Open USRN (Ordnance Survey, Open Government "
                           "Licence v3.0): contains OS data (c) Crown "
                           "copyright and database right")
        write_json(args.out, table)
        print("byways with a USRN: %d of %d; %d USRNs"
              % (table["matched"], table["byways"], len(table["usrns"])))
        return 0
    if args.cmd == "fetch":
        today = args.today or datetime.date.today().isoformat()
        try:
            fetch(polite_http.PoliteClient(), today, args.months,
                  out=args.out)
        except (Refused, FetchFailed) as e:
            print("::error::Street Manager could not be read: %s; the last "
                  "published file is unchanged" % e)
            return 1
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

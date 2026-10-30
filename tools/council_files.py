#!/usr/bin/env python3
"""Councils' rights of way FILES read without GDAL: shapefiles (in a zip),
GeoPackages (in a zip), KML (in a KMZ) and GeoJSON.

Some councils publish their network as a file to download rather than as a
live layer (council_ways.py reads those). Each reader here takes the bytes
of the download and returns [(attributes, lines)], the lines in WGS84 as
lists of (lon, lat). Only the formats and geometry types councils actually
publish are read: polylines (with or without Z or M), in British National
Grid or in WGS84. Anything else is a FetchFailed, never a guess: a file in a
projection we cannot name would put every byway in the wrong place.

The standard library only (zipfile, struct, sqlite3, xml.etree), because the
data jobs install nothing beyond cryptography, Pillow and pyyaml.
"""
import io
import json
import os
import re
import sqlite3
import struct
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from polite_http import FetchFailed
from council_sources import _wgs


# ------------------------------------------------------------ projections

#: A .prj naming the British National Grid.
_BNG = re.compile(r"British_National_Grid|OSGB[_ ]?1936|27700", re.I)


def _projection(prj_text):
    """'bng' or 'wgs84' from a .prj, or FetchFailed."""
    text = prj_text or ""
    if text.lstrip().upper().startswith("PROJCS"):
        if _BNG.search(text):
            return "bng"
        raise FetchFailed("a projection this reader does not know: %s"
                          % text[:120])
    if text.lstrip().upper().startswith("GEOGCS") and \
            re.search(r"WGS[_ ]?(19)?84", text, re.I):
        return "wgs84"
    raise FetchFailed("no projection this reader knows: %r" % text[:120])


def _lonlat(points):
    """WGS84 points as (lon, lat), rounded as _wgs rounds, repeats dropped."""
    out = []
    for p in points:
        if len(p) < 2:
            continue
        lon, lat = float(p[0]), float(p[1])
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            continue
        point = (round(lon, 6), round(lat, 6))
        if not out or out[-1] != point:
            out.append(point)
    return out


def _to_wgs(projection, parts):
    convert = _wgs if projection == "bng" else _lonlat
    return [l for l in (convert(p) for p in parts) if len(l) >= 2]


# -------------------------------------------------------------- shapefile

#: Shape types read: PolyLine, PolyLineZ, PolyLineM. Null shapes are skipped.
_POLYLINES = (3, 13, 23)


def shp_parts(data):
    """[parts] per record of a .shp, in record order; parts is a list of
    lists of (x, y). A null shape gives []."""
    if len(data) < 100 or struct.unpack(">i", data[:4])[0] != 9994:
        raise FetchFailed("not a shapefile")
    kind = struct.unpack("<i", data[32:36])[0]
    if kind not in _POLYLINES + (0,):
        raise FetchFailed("shapefile of shape type %d, not lines" % kind)
    out, pos = [], 100
    while pos + 8 <= len(data):
        length = struct.unpack(">i", data[pos + 4:pos + 8])[0] * 2
        rec = data[pos + 8:pos + 8 + length]
        pos += 8 + length
        if len(rec) < 4:
            break
        shape = struct.unpack("<i", rec[:4])[0]
        if shape == 0:
            out.append([])
            continue
        if shape not in _POLYLINES:
            raise FetchFailed("a shape of type %d among lines" % shape)
        nparts, npoints = struct.unpack("<ii", rec[36:44])
        starts = list(struct.unpack("<%di" % nparts,
                                    rec[44:44 + 4 * nparts]))
        base = 44 + 4 * nparts
        xy = struct.unpack("<%dd" % (2 * npoints),
                           rec[base:base + 16 * npoints])
        points = [(xy[2 * i], xy[2 * i + 1]) for i in range(npoints)]
        ends = starts[1:] + [npoints]
        out.append([points[a:b] for a, b in zip(starts, ends)])
    return out


def dbf_records(data, encoding="latin-1"):
    """[{field: text}] per record of a .dbf, deleted records as None (so
    the list stays aligned with the .shp)."""
    if len(data) < 32:
        raise FetchFailed("not a dbf file")
    count, header, size = struct.unpack("<IHH", data[4:12])
    fields, pos = [], 32
    while pos + 32 <= header and data[pos:pos + 1] != b"\r":
        name = data[pos:pos + 11].split(b"\0", 1)[0].decode("latin-1")
        fields.append((name.strip(), data[pos + 16]))
        pos += 32
    out = []
    for i in range(count):
        rec = data[header + i * size:header + (i + 1) * size]
        if len(rec) < size:
            break
        if rec[:1] == b"*":
            out.append(None)
            continue
        attrs, at = {}, 1
        for name, width in fields:
            raw = rec[at:at + width]
            at += width
            attrs[name] = raw.decode(encoding, "replace").strip()
        out.append(attrs)
    return out


def _members(z, stem, ext):
    names = [n for n in z.namelist()
             if n.lower().endswith(ext) and not n.startswith("__MACOSX")]
    if stem:
        names = [n for n in names
                 if os.path.basename(n)[:-len(ext)].lower() == stem.lower()]
    return names


def shapefile_records(blob, stem=None):
    """[(attributes, lines)] from a zipped shapefile. `stem` picks one
    shapefile where the zip holds several."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise FetchFailed("not a zip: %s" % e)
    shps = _members(z, stem, ".shp")
    if len(shps) != 1:
        raise FetchFailed("expected one shapefile%s, found %d"
                          % (" " + stem if stem else "", len(shps)))
    base = shps[0][:-4]

    def member(ext):
        for n in z.namelist():
            if n.lower() == (base + ext).lower():
                return z.read(n)
        return None

    prj, dbf = member(".prj"), member(".dbf")
    if prj is None or dbf is None:
        raise FetchFailed("the shapefile has no .prj or no .dbf")
    cpg = (member(".cpg") or b"").decode("ascii", "replace").strip()
    encoding = "utf-8" if cpg.upper().replace("-", "") == "UTF8" \
        else "latin-1"
    projection = _projection(prj.decode("latin-1"))
    parts = shp_parts(member(".shp"))
    attrs = dbf_records(dbf, encoding)
    if len(attrs) != len(parts):
        raise FetchFailed("the .dbf has %d records and the .shp %d"
                          % (len(attrs), len(parts)))
    return [(a, _to_wgs(projection, p))
            for a, p in zip(attrs, parts) if a is not None]


# --------------------------------------------------------------- GeoJSON

def geojson_records(blob, projection):
    """[(attributes, lines)] from a GeoJSON file. `projection` ('bng' or
    'wgs84') is the layer's own, stated where the layer is defined: a file's
    `crs` member is not trusted, because Calderdale's says EPSG:27700 over
    coordinates in degrees."""
    if projection not in ("bng", "wgs84"):
        raise FetchFailed("unknown projection %r" % projection)
    try:
        data = json.loads(blob.decode("utf-8-sig"))
    except ValueError as e:
        raise FetchFailed("not JSON: %s" % e)
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        raise FetchFailed("no features list")
    out = []
    for f in feats:
        g = (f or {}).get("geometry") or {}
        kind, coords = g.get("type"), g.get("coordinates") or []
        parts = ([coords] if kind == "LineString" else
                 coords if kind == "MultiLineString" else [])
        attrs = dict((k, "" if v is None else str(v))
                     for k, v in ((f or {}).get("properties") or {}).items())
        out.append((attrs, _to_wgs(projection, parts)))
    return out


# ------------------------------------------------------------ GeoPackage

def wkb_lines(blob):
    """The line parts of a WKB LineString or MultiLineString (2D, Z, M or
    ZM; ISO or EWKB-style flags) as lists of (x, y)."""
    def read(pos):
        order = "<" if blob[pos] == 1 else ">"
        kind = struct.unpack(order + "I", blob[pos + 1:pos + 5])[0]
        pos += 5
        dims = 2
        if kind & 0x80000000:
            dims += 1
        if kind & 0x40000000:
            dims += 1
        if kind & 0x20000000:   # EWKB SRID
            pos += 4
        kind &= 0x0FFFFFFF
        if kind >= 3000:
            dims, kind = 4, kind - 3000
        elif kind >= 2000:
            dims, kind = 3, kind - 2000
        elif kind >= 1000:
            dims, kind = 3, kind - 1000
        if kind == 2:
            n = struct.unpack(order + "I", blob[pos:pos + 4])[0]
            pos += 4
            vals = struct.unpack(order + "%dd" % (n * dims),
                                 blob[pos:pos + 8 * n * dims])
            pos += 8 * n * dims
            return [[(vals[i * dims], vals[i * dims + 1])
                     for i in range(n)]], pos
        if kind == 5:
            n = struct.unpack(order + "I", blob[pos:pos + 4])[0]
            pos += 4
            out = []
            for _ in range(n):
                lines, pos = read(pos)
                out.extend(lines)
            return out, pos
        raise FetchFailed("a WKB geometry of type %d, not lines" % kind)

    return read(0)[0]


def gpkg_geometry(blob):
    """The WKB inside a GeoPackage geometry blob, or None when empty."""
    if blob is None or bytes(blob[:2]) != b"GP":
        raise FetchFailed("not a GeoPackage geometry")
    flags = blob[3]
    if flags & 0x10:
        return None   # empty geometry
    envelope = (0, 32, 48, 48, 64)[(flags >> 1) & 0x07] \
        if (flags >> 1) & 0x07 <= 4 else None
    if envelope is None:
        raise FetchFailed("a GeoPackage envelope this reader does not know")
    return bytes(blob[8 + envelope:])


def gpkg_records(blob, table=None):
    """[(attributes, lines)] from a GeoPackage, or a zip holding one."""
    if blob[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile as e:
            raise FetchFailed("not a zip: %s" % e)
        names = [n for n in z.namelist() if n.lower().endswith(".gpkg")]
        if len(names) != 1:
            raise FetchFailed("expected one GeoPackage, found %d"
                              % len(names))
        blob = z.read(names[0])
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                               uri=True)
        try:
            try:
                rows = conn.execute(
                    "SELECT table_name, column_name, srs_id "
                    "FROM gpkg_geometry_columns").fetchall()
            except sqlite3.DatabaseError as e:
                raise FetchFailed("not a GeoPackage: %s" % e)
            if table:
                rows = [r for r in rows if r[0] == table]
            if len(rows) != 1:
                raise FetchFailed("expected one feature table%s, found %d"
                                  % (" " + table if table else "",
                                     len(rows)))
            name, column, srs = rows[0]
            if srs == 27700:
                projection = "bng"
            elif srs == 4326:
                projection = "wgs84"
            else:
                raise FetchFailed("a GeoPackage in SRS %s" % srs)
            cur = conn.execute('SELECT * FROM "%s"'
                               % name.replace('"', '""'))
            cols = [d[0] for d in cur.description]
            out = []
            for row in cur:
                attrs = {}
                geom = None
                for col, val in zip(cols, row):
                    if col == column:
                        geom = val
                    else:
                        attrs[col] = "" if val is None else str(val)
                wkb = gpkg_geometry(geom) if geom is not None else None
                parts = wkb_lines(wkb) if wkb else []
                out.append((attrs, _to_wgs(projection, parts)))
            return out
        finally:
            conn.close()
    finally:
        os.remove(path)


# ------------------------------------------------------------------- KML

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def kml_records(blob):
    """[(attributes, lines)] from a KML, or a KMZ holding one. Attributes
    are the placemark's `name`, the `folder` it sits in, and its
    ExtendedData values; KML is WGS84 by definition."""
    if blob[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile as e:
            raise FetchFailed("not a zip: %s" % e)
        names = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if len(names) != 1:
            raise FetchFailed("expected one KML, found %d" % len(names))
        blob = z.read(names[0])
    out, folders, stack = [], [], []
    try:
        for event, el in ET.iterparse(io.BytesIO(blob),
                                      events=("start", "end")):
            tag = _local(el.tag)
            if event == "start":
                stack.append(tag)
                if tag == "Folder":
                    folders.append("")
                continue
            stack.pop()
            parent = stack[-1] if stack else None
            if tag == "name" and parent == "Folder":
                folders[-1] = (el.text or "").strip()
            elif tag == "Folder":
                folders.pop()
                el.clear()
            elif tag == "Placemark":
                attrs = {"folder": folders[-1] if folders else ""}
                parts = []
                for sub in el.iter():
                    st = _local(sub.tag)
                    if st == "name" and "name" not in attrs:
                        attrs["name"] = (sub.text or "").strip()
                    elif st in ("SimpleData", "Data"):
                        value = sub.text
                        if st == "Data":
                            v = [c for c in sub if _local(c.tag) == "value"]
                            value = v[0].text if v else None
                        attrs.setdefault(sub.get("name") or "",
                                         (value or "").strip())
                    elif st == "LineString":
                        for c in sub:
                            if _local(c.tag) == "coordinates" and c.text:
                                parts.append([
                                    tuple(float(v) for v in
                                          p.split(",")[:2])
                                    for p in c.text.split()])
                out.append((attrs, _to_wgs("wgs84", parts)))
                el.clear()
    except ET.ParseError as e:
        raise FetchFailed("unreadable KML: %s" % e)
    return out

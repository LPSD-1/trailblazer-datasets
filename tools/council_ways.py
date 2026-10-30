#!/usr/bin/env python3
"""Byways read from the councils' own layers and files, kept beside rowmaps.

    python tools/council_ways.py fetch                 # every layer
    python tools/council_ways.py fetch --only CB,DN    # just these
    python tools/council_ways.py preview CB            # merge, report only

WHY. rowmaps.com is our source for every authority's definitive map, and a
good one, but it is a copy: 99 of its 149 byway files were over a year old on
7 October 2026 and 40 over two. A council that publishes its own rights of
way layer live knows today which byways exist. For those councils this reads
the byways (BOATs only - never restricted byways, never anything else) from
the council directly and writes them to council-ways/<CODE>.json, which
build_packages.py then uses in place of the rowmaps byway file.

THE MERGE KEEPS EVERY UNCHANGED WAY'S ID. A lane's id carries a hash of its
coordinates, and riders star lanes by id. So the council layer decides WHICH
byways exist, and the rowmaps record is kept, byte for byte, wherever the
council still has the same way (see `merge`): a rowmaps byway the council
layer still draws is kept as it was; one it no longer draws is dropped (it
was stopped up, downgraded or re-drawn); and a council byway rowmaps lacks is
added with the council's geometry. Only real changes move an id.

ROWMAPS STAYS THE FALLBACK, at three levels:
  * a council layer that cannot be read leaves its last good file in place
    (and the alarm says so: status.json, and an issue from the workflow);
  * a read that returns nothing, or under two thirds of what it held last
    time, is refused as a bad read and the last good file is kept;
  * at build time, a council file that disagrees with rowmaps too much to be
    the same network (`agrees`) is not used at all - the authority is built
    from rowmaps, with a warning;
  * at build time, a council file whose last good read (status.json
    `last_ok`) is more than MAX_AGE_DAYS old is not used either: the layer
    has been failing for a month, and rowmaps may since have added byways
    the old file would drop.

READ ONLY AND POLITE. Every request goes through tools/polite_http.py: an
honest User-Agent, robots.txt obeyed, paced, ArcGIS `/query` and WFS
`GetFeature` only (Wiltshire's FeatureServer and the Cheshire GeoServer
advertise editing to anonymous users; nothing here ever asks for it).
"""
import argparse
import datetime
import html
import json
import math
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_files  # noqa: E402
import polite_http  # noqa: E402
from polite_http import (FetchFailed, Refused, arcgis_query,  # noqa: E402
                         host_of)
from council_sources import (_wgs, esri_lines, geojson_lines,  # noqa: E402
                             read_json, write_json)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "council-ways")
CACHE = os.path.join(ROOT, "cache")
BOAT_FILE = "byway_open_to_all_traffic.json"

#: Keep-last-good: a read under this share of the last good one is refused.
FLOOR_SHARE = 2.0 / 3.0

#: A council file last read well this many days ago or more is not used:
#: the build falls back to rowmaps. A month: the layers are read daily or
#: weekly, so a few days' outage (a council server down over a weekend)
#: changes nothing,
#: while rowmaps refreshes its copies far less often than monthly, so a
#: council file a month old is about the age at which rowmaps' copy may be
#: the newer of the two.
MAX_AGE_DAYS = 30

#: Two lines are the same way where they lie within this of each other. The
#: two copies come from one council survey, so they normally coincide to a
#: metre; 15 m absorbs a re-projection or a light re-digitisation without
#: letting a parallel lane on the other side of a field count.
SAME_WAY_M = 15.0
#: A rowmaps byway is still there if this share of it is under the council's.
KEEP_SHARE = 0.9
#: A piece of council byway not under any kept rowmaps byway is new only if
#: it is at least this long: shorter is a junction overshoot, not a byway.
MIN_NEW_M = 30.0
#: Along-line sampling step for the coverage tests.
STEP_M = 5.0

#: When the council's layer and rowmaps disagree this much, they are not two
#: copies of one network and the council file is not used (`agrees`).
MIN_KEPT_SHARE = 0.75      # of rowmaps' byways, still drawn by the council
MAX_NEW_SHARE = 0.25       # of the council's byway length, new to rowmaps

OGL = ("Contains public sector information licensed under the Open "
       "Government Licence v3.0.")
OS_OPENDATA = "Published by the council under the OS OpenData Licence."


# ------------------------------------------------------------ references
#
# Only a NEW way (one rowmaps does not have) is named from these; a kept way
# keeps its rowmaps name. Each returns (parish, number) in rowmaps' style for
# that authority, so build_packages.council_reference reads both alike.

def _split_code(text):
    """'ARBO10' -> ('ARBO', '10'); 'LACO24A' -> ('LACO', '24A')."""
    m = re.match(r"\s*([A-Za-z]+)\s*(\d.*?)\s*$", text or "")
    return (m.group(1).upper(), m.group(2)) if m else ("", (text or "").strip())


def _last_word(text):
    """'Alciston 11a' -> ('Alciston', '11a')."""
    text = (text or "").strip()
    if " " not in text:
        return "", text
    head, tail = text.rsplit(" ", 1)
    return head.strip(), tail.strip()


def _ref_cambridgeshire(a):
    return (a.get("parish") or "").strip(), \
        str(a.get("name") or "").split("/")[-1].strip()


def _ref_central_beds(a):
    return (a.get("parish") or "").strip().upper(), \
        str(a.get("path_no") or "").strip()


def _ref_wokingham(a):
    return _split_code(a.get("prowuid"))


def _ref_dudley(a):
    """'SED0557' -> ('SED', '0557')."""
    return _split_code(a.get("prow_id"))


def _ref_devon(a):
    m = re.match(r"\s*(.*?)\s+Byway\s+(\S.*?)\s*$", a.get("Parish_Sta") or "",
                 re.I)
    return (m.group(1), m.group(2)) if m else _last_word(a.get("Parish_Sta"))


def _ref_cheshire_east(a):
    code = str(a.get("Routecode") or "")
    parish = _last_word(a.get("Alias"))[0]
    return parish, code.split("/", 1)[1] if "/" in code else code


def _ref_cheshire_west(a):
    code = str(a.get("Routecode") or "")
    return tuple(code.split("/", 1)) if "/" in code else ("", code)


def _ref_oxfordshire(a):
    code = str(a.get("RouteCode") or "")
    return tuple(code.split("/", 1)) if "/" in code else ("", code)


def _ref_northumberland(a):
    code = str(a.get("KEYID") or "")
    return tuple(code.split("/", 1)) if "/" in code else ("", code)


def _ref_lancashire(a):
    code = str(a.get("PathRefLong") or "")
    m = re.match(r"[A-Z]{2}(\d{4})(\d+\w*)$", code)
    return (m.group(1), m.group(2)) if m else ("", code)


def _ref_east_sussex(a):
    return _last_word(a.get("Path_Name"))


def _ref_hertfordshire(a):
    return _last_word(a.get("PATHNAME"))


def _ref_essex(a):
    central = str(a.get("Central_As") or "")
    return (a.get("Parish") or "").strip(), central.rsplit("_", 1)[-1]


def _ref_wiltshire(a):
    return _split_code(a.get("REF"))


def _ref_west_berkshire(a):
    """'Beed/22/2' -> ('BEED', '22'): parish code and route number."""
    code = str(a.get("RouteCode") or "")
    parts = code.split("/")
    return (parts[0].upper(), parts[1]) if len(parts) >= 2 else ("", code)


def _ref_isle_of_wight(a):
    return _split_code(a.get("P_NUMBER"))


def _ref_hampshire(a):
    return (a.get("PARISH") or "").strip(), \
        str(a.get("ROWCODE") or "").strip()


def _ref_bracknell(a):
    m = re.search(r"(\d+\w*)\s*$", str(a.get("UniqueID") or ""))
    return (a.get("Parish") or "").strip().upper(), m.group(1) if m else ""


def _slash(field):
    """'NMA/19/3' -> ('NMA', '19/3'): the first part, then the rest."""
    def ref(a):
        code = str(a.get(field) or "").strip()
        return tuple(code.split("/", 1)) if "/" in code else ("", code)
    return ref


def _fields(parish, number):
    """A reference read straight from two fields (or a fixed parish)."""
    return lambda a: (str(a.get(parish) or "").strip() if parish else "",
                      str(a.get(number) or "").strip())


def _ref_derbyshire(a):
    return str(a.get("PARISH") or "").strip(), _slash("ROUTECODE")(a)[1]


def _ref_lake_district(a):
    """'523004' -> ('523', '004'), as rowmaps splits the park's numbers."""
    code = str(a.get("NUMBER_") or "").strip()
    return (code[:3], code[3:]) if re.match(r"\d{6}$", code) else ("", code)


def _ref_stockport(a):
    """'138M' -> ('M', '138'); '55(b)CG' -> ('CG', '55(b)')."""
    row = str(a.get("row") or "").strip()
    m = re.match(r"(.+?)([A-Za-z]+)$", row)
    return (m.group(2).upper(), m.group(1)) if m else ("", row)


# ---------------------------------------------------------------- readers
#
# Each returns (records read, [way]); a way is {"id", "parish", "number",
# "lines": [[(lon, lat), ...]]}, the lines in WGS84.

def _way(oid, ref, lines):
    parish, number = ref
    return {"id": str(oid), "parish": (parish or "").strip(),
            "number": (number or "").strip(), "lines": lines}


def read_arcgis(client, layer):
    feats = arcgis_query(client, layer["url"], where=layer["where"],
                         out_fields=layer.get("fields", "*"))
    ways = []
    for f in feats:
        a = f.get("attributes") or {}
        lines = esri_lines(f.get("geometry"))
        if lines:
            oid = a.get(layer.get("oid", "OBJECTID"), a.get("FID", ""))
            ways.append(_way(oid, layer["ref"](a), lines))
    return len(feats), ways


def _ogc_filter(prop, value):
    return ('<Filter xmlns="http://www.opengis.net/ogc"><PropertyIsEqualTo>'
            '<PropertyName>%s</PropertyName><Literal>%s</Literal>'
            '</PropertyIsEqualTo></Filter>' % (prop, value))


def read_wfs_json(client, layer):
    """A WFS GetFeature answered as GeoJSON in British National Grid."""
    params = dict(layer["params"])
    if layer.get("filter"):
        params["FILTER"] = _ogc_filter(*layer["filter"])
    url = layer["url"] + "&" + urllib.parse.urlencode(params)
    data = client.get_json(url)
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        raise FetchFailed("no features list from %s" % url)
    ways = []
    for f in feats:
        a = f.get("properties") or {}
        lines = geojson_lines(f.get("geometry"))
        if lines:
            oid = f.get("id") or a.get(layer.get("oid", "ogc_fid"), "")
            ways.append(_way(oid, layer["ref"](a), lines))
    return len(feats), ways


def gml_ways(body, ref, match=None):
    """WFS 1.1 GML 3.1.1 (Central Bedfordshire answers in nothing else).
    With `match`, only the records whose attributes it accepts are ways."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise FetchFailed("unreadable GML: %s" % e)
    if root.tag.endswith("ExceptionReport"):
        raise FetchFailed("WFS exception: %s" % " ".join(
            t.strip() for t in root.itertext() if t.strip())[:300])
    ways, records = [], 0
    for member in root.iter():
        if not (member.tag.endswith("}featureMember")
                or member.tag.endswith("}featureMembers")):
            continue
        for feat in list(member):
            records += 1
            attrs, lines = {}, []
            for el in feat.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag == "posList" and el.text:
                    nums = [float(v) for v in el.text.split()]
                    dim = int(el.get("srsDimension") or 2)
                    lines.append(_wgs([nums[i:i + 2]
                                       for i in range(0, len(nums), dim)]))
                elif tag == "coordinates" and el.text:
                    lines.append(_wgs([[float(v) for v in p.split(",")[:2]]
                                       for p in el.text.split()]))
                elif len(el) == 0 and el.text and el is not feat:
                    attrs.setdefault(tag, el.text.strip())
            lines = [l for l in lines if len(l) >= 2]
            if lines and (match is None or match(attrs)):
                oid = feat.get("{http://www.opengis.net/gml}id") or \
                    attrs.get("ogc_fid", "")
                ways.append(_way(oid, ref(attrs), lines))
    return records, ways


def read_wfs_gml(client, layer):
    params = dict(layer["params"])
    if layer.get("filter"):
        params["FILTER"] = _ogc_filter(*layer["filter"])
    url = layer["url"] + "&" + urllib.parse.urlencode(params)
    return gml_ways(client.get(url), layer["ref"], layer.get("match"))


def _find(client, layer):
    """The one current file linked from the council's page (`url`), by the
    pattern `find`: a council that dates its file names (2026_10-...) moves
    the link each month. Only an https link on the page's own host is
    taken, and anything but exactly one such link is a failure."""
    page = layer["url"]
    body = client.get(page).decode("utf-8", "replace")
    links = sorted(set(urllib.parse.urljoin(page, html.unescape(m))
                       for m in re.findall(layer["find"], body)))
    if len(links) != 1:
        raise FetchFailed("%d links matching %s on %s"
                          % (len(links), layer["find"], page))
    link = links[0]
    if not link.startswith("https://") or host_of(link) != host_of(page):
        raise FetchFailed("the file linked from %s is not on its own host "
                          "over https: %s" % (page, link))
    return link


def read_file(client, layer):
    """A file the council publishes for download (zipped shapefile or
    GeoPackage, KMZ, GeoJSON), read whole by `format` (council_files), its
    byways picked out by `match`. Anything the file makes `format`, `match`
    or `ref` throw is FetchFailed: one council's odd file is a failed read
    of that council, never the end of the run.

    -> (records, ways), or (records, ways, {"credit": text}) for a layer
    whose `credit` is read from the file itself (Surrey's year)."""
    url = _find(client, layer) if layer.get("find") else layer["url"]
    blob = client.get(url)
    try:
        records = layer["format"](blob)
        ways = []
        for i, (a, lines) in enumerate(records):
            if lines and layer["match"](a):
                oid = a.get(layer["oid"]) if layer.get("oid") else None
                ways.append(_way(oid if oid not in (None, "") else i,
                                 layer["ref"](a), lines))
        credit = layer["credit"](blob) if callable(layer.get("credit")) \
            else None
    except (Refused, FetchFailed):
        raise
    except Exception as e:  # noqa: BLE001 - the file is the stranger
        raise FetchFailed("%s could not be read (%s): %s"
                          % (url, type(e).__name__, str(e)[:200]))
    if credit is None:
        return len(records), ways
    return len(records), ways, {"credit": credit}


def _is(field, *values):
    """`match`: the record's `field` is one of `values`, exactly."""
    return lambda a: a.get(field) in values


def _shapefile(blob):
    return council_files.shapefile_records(blob)


def _geojson_wgs84(blob):
    return council_files.geojson_records(blob, "wgs84")


def _geojson_bng(blob):
    return council_files.geojson_records(blob, "bng")


def _os_credit_dated(blob):
    """The Ordnance Survey's credit, in the year of the file's own date
    (gpkg_contents last_change) - never today's year, which would claim a
    survey the file does not hold. A file with no date is not used."""
    year = council_files.gpkg_year(blob)
    if not year:
        raise FetchFailed("the GeoPackage gives no date for its Ordnance "
                          "Survey credit")
    return ("Contains OS data \u00a9 Crown copyright and database right "
            "%s." % year)


# ----------------------------------------------------------------- layers

_ISHARE_11 = {"SERVICE": "WFS", "VERSION": "1.1.0", "REQUEST": "GetFeature"}
_GEOSERVER_11 = {"service": "WFS", "version": "1.1.0",
                 "request": "GetFeature", "outputFormat": "application/json",
                 "srsName": "EPSG:27700"}
_GEOSERVER_20 = {"service": "WFS", "version": "2.0.0",
                 "request": "GetFeature", "outputFormat": "application/json",
                 "srsName": "EPSG:27700"}

LAYERS = [
    {"code": "CB", "council": "Cambridgeshire County Council",
     "licence": "OGL-3.0", "read": read_wfs_json, "ref": _ref_cambridgeshire,
     "url": "https://maps.cambridgeshire.gov.uk/getows.ashx?"
            "mapsource=ccc/inspire",
     "params": dict(_ISHARE_11, typeName="ccc:public_rights_of_way",
                    outputFormat="application/json; subtype=geojson"),
     "filter": ("status", "Byway")},
    {"code": "BK", "council": "Central Bedfordshire Council",
     "licence": "OGL-3.0", "read": read_wfs_gml, "ref": _ref_central_beds,
     "url": "https://my.centralbedfordshire.gov.uk/GetOWS.ashx?"
            "MAPSOURCE=mapsources/AllMaps",
     "params": dict(_ISHARE_11, typeName="RoW_Legal_Network_1"),
     "filter": ("type", "BOAT")},
    {"code": "WJ", "council": "Wokingham Borough Council",
     "licence": "OGL-3.0", "read": read_arcgis, "ref": _ref_wokingham,
     "url": "https://services.arcgis.com/Uk58AEewwvNNTAX2/arcgis/rest/"
            "services/PRoW_in_Wokingham_(Public)/FeatureServer/0",
     "where": "status='Byway open to all traffic'"},
    {"code": "DN", "council": "Devon County Council",
     "licence": None, "read": read_arcgis, "ref": _ref_devon,
     "url": "https://map.devon.gov.uk/arcgis/rest/services/Environment/"
            "Public_Access/MapServer/0",
     "where": "Status='Byway'"},
    {"code": "CH", "council": "Cheshire East Council",
     "licence": None, "read": read_wfs_json, "ref": _ref_cheshire_east,
     "url": "https://maps.cheshireeast.gov.uk/geoserver/CEOpenData/wfs?",
     "params": dict(_GEOSERVER_20, typeNames="CEOpenData:"
                    "TN_S_ROWBywaysOpenToAllTraffic_LINE_CURRENT")},
    {"code": "CC", "council": "Cheshire West and Chester Council",
     "licence": "OGL-3.0", "read": read_wfs_json, "ref": _ref_cheshire_west,
     "url": "https://maps.cheshireeast.gov.uk/geoserver/CWaCOpenData/wfs?",
     "params": dict(_GEOSERVER_20, typeNames="CWaCOpenData:"
                    "TN_S_ROWBywaysOpenToAllTraffic_LINE_CURRENT")},
    {"code": "ON", "council": "Oxfordshire County Council",
     "licence": "OGL-3.0", "read": read_arcgis, "ref": _ref_oxfordshire,
     "url": "https://mymaps2.oxfordshire.gov.uk/server/rest/services/WFS/"
            "CAMS_PRoW/FeatureServer/0",
     "where": "StatusDescr='BOAT'"},
    {"code": "LA", "council": "Lancashire County Council",
     "licence": "OGL-3.0", "read": read_arcgis, "ref": _ref_lancashire,
     "url": "https://services-eu1.arcgis.com/9MmxkLJT84uEwsJx/arcgis/rest/"
            "services/Public_Rights_of_Way/FeatureServer/0",
     "where": "PathTypeShort='BT'"},
    {"code": "ND", "council": "Northumberland County Council",
     "licence": "OGL-3.0", "read": read_arcgis, "ref": _ref_northumberland,
     "url": "https://services2.arcgis.com/LrUbY6lLLgV3tEa5/arcgis/rest/"
            "services/PRoW_rowwork_ln_MASTER_view/FeatureServer/0",
     "where": "TYPE='2'"},
    {"code": "ES", "council": "East Sussex County Council",
     "licence": "OGL-3.0", "read": read_arcgis, "ref": _ref_east_sussex,
     "url": "https://services7.arcgis.com/US7sVCsS6jE1eTXo/arcgis/rest/"
            "services/Rights_of_Way_(non_definitive)_in_East_Sussex/"
            "FeatureServer/0",
     "where": "StatusDescr='BOAT'"},
    {"code": "HD", "council": "Hertfordshire County Council",
     "licence": "OGL-3.0", "read": read_arcgis, "ref": _ref_hertfordshire,
     "url": "https://gis.hertfordshire.gov.uk/webmaps/rest/services/public/"
            "row/MapServer/3",
     "where": "VALCHAR='BOAT'"},
    {"code": "EX", "council": "Essex County Council",
     "licence": None, "read": read_arcgis, "ref": _ref_essex,
     "url": "https://services-eu1.arcgis.com/48zF6lPtAjyLTDSU/arcgis/rest/"
            "services/PROW_view/FeatureServer/0",
     "where": "Classifica='Byway'"},
    {"code": "WT", "council": "Wiltshire Council",
     "licence": "OGL-3.0", "read": read_arcgis, "ref": _ref_wiltshire,
     "url": "https://maps.wiltshire.gov.uk/arcgis/rest/services/OpenData/"
            "PublicRightsofWay/FeatureServer/0",
     "where": "TYPE='1'"},
    # Licence unstated; read under the owner's decision that council data is
    # public information. Its FeatureServer advertises anonymous editing:
    # only /query is ever called (polite_http refuses anything else).
    {"code": "WB", "council": "West Berkshire Council",
     "licence": None, "read": read_arcgis, "ref": _ref_west_berkshire,
     "url": "https://gis.westberks.gov.uk/server/rest/services/Layers/"
            "PUBLIC_RIGHTS_OF_WAY/FeatureServer/1",
     "where": "StatusDescr='BOAT'",
     "fields": "OBJECTID,RouteCode,ParishDescr,RouteNo,StatusDescr"},
    # The layer holds byways only (its other layers are the other classes).
    # A CROSS-CHECK FOR NOW: on its first read (7 October 2026) it drew 95
    # of rowmaps' 116 Isle of Wight byway records, and the 21 it did not
    # included S26, which the same layer marks "closed" - a sign of drawing
    # differences, not 21 stopped-up byways. It sits in a folder called
    # "EsriTesting". Compared and reported; it replaces nothing until a
    # person has looked at those 21.
    {"code": "IW", "council": "Isle of Wight Council",
     "licence": None, "read": read_arcgis, "ref": _ref_isle_of_wight,
     "role": "cross-check",
     "url": "https://arcgis.iow.gov.uk/arcgis/rest/services/EsriTesting/"
            "PublicRightsOfWay/MapServer/0",
     "where": "1=1", "fields": "OBJECTID,P_NUMBER"},
    # HAMPSHIRE IS A CROSS-CHECK, NOT A SOURCE: the council's own layer was
    # last edited in June 2023 and rowmaps' copy (April 2026) is newer. It is
    # read and compared (status.json says how far the two agree) and used
    # only if rowmaps ever has no Hampshire file at all. EMAIL_ADDR and the
    # other office fields are never requested.
    {"code": "HP", "council": "Hampshire County Council",
     "licence": None, "read": read_arcgis, "ref": _ref_hampshire,
     "role": "cross-check",
     "url": "https://services-eu1.arcgis.com/JZryykSnmiY7YI6X/arcgis/rest/"
            "services/Hampshire_Rights_of_Way/FeatureServer/0",
     "where": "ROW_TYPE='BOAT'", "fields": "OBJECTID,ROWCODE,PARISH"},
    {"code": "BC", "council": "Bracknell Forest Council",
     "licence": "OGL-3.0", "read": read_arcgis, "ref": _ref_bracknell,
     "url": "https://services9.arcgis.com/5eO9hmsd8SoBl0Cj/arcgis/rest/"
            "services/GIS_PublicRightsOfWay/FeatureServer/7",
     "where": "ROWType='Byway Open to All Traffic'"},
    # FROM ROWMAPS TO THE COUNCILS' OWN PUBLICATIONS (the owner's decision,
    # 30 October 2026: rowmaps.com is a privately run copy, not a public
    # body). Each was compared on that day with the rowmaps lanes then
    # published - byway count, length, and how much of each lies within
    # 15 m of the other - and switched only where the council's own copy
    # was at least as complete. Downloads are whole files, read weekly
    # (`every_days`); the councils replace them monthly at most.
    {"code": "BM", "council": "Buckinghamshire Council",
     "licence": "OGL-3.0", "read": read_file, "format": _shapefile,
     "url": "https://www.buckinghamshire.gov.uk/documents/38315/ROW_2025.zip",
     "match": _is("StatusDesc", "BOAT"), "ref": _slash("RouteCode"),
     "oid": "MI_PRINX", "every_days": 7},
    # A CROSS-CHECK, NOT A SOURCE: Calderdale's file is an immutable
    # snapshot taken in January 2022 (its name says so, and it is never
    # replaced), older than rowmaps' copy. Compared and reported; used only
    # if rowmaps ever has no Calderdale file. The file says EPSG:27700 over
    # coordinates in degrees.
    {"code": "CA", "council": "Calderdale Metropolitan Borough Council",
     "licence": "OGL-3.0", "read": read_file, "format": _geojson_wgs84,
     "role": "cross-check",
     "url": "https://dataworks.calderdale.gov.uk/download/24qmx/p18/"
            "Public%20Rights%20of%20Way%20-%20Jan%202022%20-%20JSON.json",
     "match": _is("StatusDesc", "BOAT"), "ref": _slash("RouteCode"),
     "oid": "fid", "every_days": 7},
    {"code": "DY", "council": "Derbyshire County Council",
     "licence": "OGL-3.0", "read": read_wfs_json, "ref": _ref_derbyshire,
     "url": "https://wms.derbyshire.gov.uk/geoserver/DCC/ows?",
     "params": dict(_GEOSERVER_11, typeName="DCC:Rights_Of_Way_OpenData",
                    propertyName="ROUTECODE,PARISH,PROW_STATU,the_geom"),
     "filter": ("PROW_STATU", "BOAT")},
    # Dudley's "Byway" rows include a RUPP (now a restricted byway): only
    # the one it calls a byway open to all traffic is taken. Dudley and St
    # Helens answer a server-side filter with HTTP 500 and 404 (30 October
    # 2026), so the whole layer is read, weekly, and picked out here.
    {"code": "DZ", "council": "Dudley Metropolitan Borough Council",
     "licence": "OGL-3.0", "read": read_wfs_gml, "ref": _ref_dudley,
     "url": "https://maps.dudley.gov.uk/getows.ashx?"
            "mapsource=mapsources/inspire",
     "params": dict(_ISHARE_11, typeName="row_lines"),
     "match": _is("feature", "Byway Open to All Traffic"), "every_days": 7},
    # On Data Mill North, Leeds City Council's open data portal.
    {"code": "DU", "council": "Durham County Council",
     "licence": "OGL-3.0", "read": read_file, "format": _shapefile,
     "url": "https://datamillnorth.org/download/2lx03/112/prow.zip",
     "match": _is("STATUS", "BY"), "ref": _fields("PARISH", "PROW_NO__F"),
     "oid": "CRMID", "every_days": 7},
    {"code": "KL", "council": "Kirklees Council",
     "licence": "OGL-3.0", "read": read_file, "format": _shapefile,
     "url": "https://www.kirklees.gov.uk/beta/information-and-data/pdf/"
            "open-data/PROW_polyline.zip",
     "match": _is("Status", "BYWAY"), "ref": _slash("RouteCode"),
     "oid": "RouteCode", "every_days": 7},
    # The park authority's own file, not the supplier-hosted WFS.
    {"code": "L1", "council": "Lake District National Park Authority",
     "licence": "OGL-3.0", "read": read_file, "format": _shapefile,
     "url": "https://volunteering.lake-district.gov.uk/PublishedGIS/"
            "LDNPA_PROW.shz",
     "match": _is("Type_Text", "BOAT"), "ref": _ref_lake_district,
     "oid": "NUMBER_", "every_days": 7},
    {"code": "LD", "council": "Leeds City Council",
     "licence": "OGL-3.0", "read": read_file, "format": _shapefile,
     "url": "https://datamillnorth.org/download/e5l9w/"
            "46cccff6-dc09-4913-a266-0dbde0ff47a7/Def_Paths.zip",
     "match": _is("PROW_STATU", "BY"), "ref": _fields("LOCAL_AR_1", "PROW_ID"),
     "oid": "OBJECTID", "every_days": 7},
    # Norfolk stays on rowmaps, held back on its licence: its file says the
    # OS Open Data Licence where data.gov.uk says Creative Commons, and
    # which one binds is not settled.
    # A new file each month, linked from the dataset page; the page's own
    # host serves it.
    {"code": "NY", "council": "North Yorkshire Council",
     "licence": "OGL-3.0", "read": read_file, "format": _shapefile,
     "url": "https://hub.datanorthyorkshire.org/dataset/"
            "north-yorkshire-county-council-public-rights-of-way",
     "find": r'href="(https://hub\.datanorthyorkshire\.org/dataset/[^"]+/'
             r'download/[^"/]*-nyc-prow-shape\.zip)"',
     "match": _is("DESIGNATIO", "BOAT"), "ref": _slash("ROUTECODE"),
     "oid": "ROUTECODE", "every_days": 7},
    {"code": "SN", "council": "St Helens Council",
     "licence": "OGL-3.0", "read": read_wfs_gml,
     "ref": lambda a: ("St Helens", str(a.get("row_number") or "").strip()),
     "url": "https://maps.sthelens.gov.uk/getows.ashx?"
            "mapsource=mapsources/inspire",
     "params": dict(_ISHARE_11, typeName="Public_Rights_of_Way"),
     "match": _is("row_classification", "Byway Open to All Traffic"),
     "every_days": 7},
    # Stockport's own bucket (only its path is read: HOST_PATHS), linked
    # from its Data Mill North entry,
    # https://datamillnorth.org/dataset/stockport-rights-of-way-em7wl
    # (publisher Stockport MBC, OGL v3), which links the smbc-opendata files.
    {"code": "YY", "council": "Stockport Metropolitan Borough Council",
     "licence": "OGL-3.0", "read": read_file, "format": _geojson_bng,
     "url": "https://s3-eu-west-1.amazonaws.com/smbc-opendata/data/"
            "Transport/definitive_rights_of_way.json",
     "match": _is("status", "Byway Open to All Traffic (BOAT)"),
     "ref": _ref_stockport, "oid": "ogc_fid", "every_days": 7},
    # A dated file, linked from the council's open data page. "Byway" is
    # Surrey's word for a byway open to all traffic.
    {"code": "SU", "council": "Surrey County Council",
     "licence": "OGL-3.0", "read": read_file,
     "format": council_files.gpkg_records,
     "credit": _os_credit_dated,
     "url": "https://www.surreycc.gov.uk/culture-and-leisure/countryside/"
            "management/footpaths-byways-and-bridleways/rights-of-way/"
            "open-data",
     "find": r'href="(https://www\.surreycc\.gov\.uk/__data/assets/file/'
             r'[^"]+/RoW_OpenData_[^"/]*\.zip)"',
     "match": _is("Stat_Text", "Byway"), "ref": _fields("LocArea", "PROWNo"),
     "oid": "id", "every_days": 7},
    {"code": "WP", "council": "Telford & Wrekin Council",
     "licence": "OGL-3.0", "read": read_file, "format": _shapefile,
     "url": "https://map.telford.gov.uk/dataGovUk/BTW_PROW.zip",
     "match": _is("ALT_NAME", "Byway open to all traffic (BOAT)"),
     "ref": _fields(None, "PROW_NO"), "oid": "OBJECTID", "every_days": 7},
    # Powys and Dorset stay on rowmaps for now: Powys's GeoServer answered
    # 403 on 30 October 2026, and Dorset's refuses GitHub's runners
    # (HOME-COLLECTOR.md). A refusal is not asked again from here.
]

#: EVERY HOST A COUNCIL BYWAY LAYER IS READ FROM, and what its robots.txt
#: said when it was added. A layer on a host not listed is never fetched
#: (`unlisted`). polite_http still obeys robots.txt on every request; this
#: is the list a person checked.
HOSTS = {
    "maps.cambridgeshire.gov.uk": "robots.txt read by polite_http on each run",
    "my.centralbedfordshire.gov.uk": "robots.txt read by polite_http on each "
                                     "run",
    "services.arcgis.com": "robots.txt 403 (none): ArcGIS Online",
    "map.devon.gov.uk": "robots.txt read by polite_http on each run",
    "maps.cheshireeast.gov.uk": "robots.txt read by polite_http on each run",
    "mymaps2.oxfordshire.gov.uk": "robots.txt read by polite_http on each run",
    "services-eu1.arcgis.com": "robots.txt 403 (none): ArcGIS Online",
    "services2.arcgis.com": "robots.txt 403 (none): ArcGIS Online",
    "services7.arcgis.com": "robots.txt 403 (none): ArcGIS Online",
    "services9.arcgis.com": "robots.txt 403 (none): ArcGIS Online",
    "gis.hertfordshire.gov.uk": "robots.txt read by polite_http on each run",
    "maps.wiltshire.gov.uk": "robots.txt read by polite_http on each run",
    "gis.westberks.gov.uk": "no robots.txt (7 Oct 2026)",
    "arcgis.iow.gov.uk": "no robots.txt (7 Oct 2026)",
    # Added 30 October 2026, each robots.txt read that day.
    "www.buckinghamshire.gov.uk": "robots.txt allows all (Allow: /)",
    "dataworks.calderdale.gov.uk": "robots.txt allows /download/ (it shuts "
                                   "out named AI crawlers, not us)",
    "wms.derbyshire.gov.uk": "robots.txt redirects to the WMS capabilities: "
                             "no rules",
    "maps.dudley.gov.uk": "robots.txt shuts out two named AI crawlers only",
    "datamillnorth.org": "robots.txt allows /download/ (it shuts out named "
                         "AI crawlers, not us)",
    "www.kirklees.gov.uk": "robots.txt allows /beta/information-and-data/",
    "volunteering.lake-district.gov.uk": "no robots.txt (404)",
    "hub.datanorthyorkshire.org": "robots.txt allows /dataset/ and asks a "
                                  "10 s crawl delay; /api/ is disallowed and "
                                  "never used",
    "maps.sthelens.gov.uk": "robots.txt shuts out two named AI crawlers only",
    "s3-eu-west-1.amazonaws.com": "robots.txt 403 (none); only Stockport's "
                                  "smbc-opendata bucket is read",
    "www.surreycc.gov.uk": "robots.txt allows the open data page and "
                           "/__data/assets/file/",
    "map.telford.gov.uk": "no robots.txt (404)",
}


#: A host shared with others is read only under its council's own path.
#: polite_http enforces it on every request and every redirect.
HOST_PATHS = {
    "s3-eu-west-1.amazonaws.com": "/smbc-opendata/",
}


def client_allow():
    """{host: path prefix or None}: every host and path a fetch may ask for,
    for polite_http.PoliteClient(allow=...)."""
    return dict((host, HOST_PATHS.get(host)) for host in HOSTS)


def unlisted(layer):
    """Why `layer` may not be fetched (its host is not in HOSTS, or its path
    is outside the host's HOST_PATHS), or None."""
    host = host_of(layer["url"])
    if host not in HOSTS:
        return "%s is not in council_ways.HOSTS; not fetched" % host
    prefix = HOST_PATHS.get(host)
    if prefix and not urllib.parse.urlsplit(layer["url"]).path.startswith(
            prefix):
        return "only %s on %s is read (HOST_PATHS); not fetched" \
            % (prefix, host)
    return None


def by_code():
    return dict((l["code"], l) for l in LAYERS)


def attribution(layer, credit=None):
    """The credit a way from `layer` carries. `credit` is the one read from
    the file (read_file), where the layer's own is read from it."""
    said = ("Source: %s's public rights of way layer, as published by the "
            "council." % layer["council"])
    if credit is None and isinstance(layer.get("credit"), str):
        credit = layer["credit"]
    extra = (" " + credit) if credit else ""
    if layer.get("licence") == "OGL-3.0":
        return OGL + " " + said + extra
    if layer.get("licence") == "OS-OpenData":
        return said + " " + OS_OPENDATA + extra
    return said + " The council publishes it without stating a licence." \
        + extra


def public(layer, credit=None):
    if credit is None and isinstance(layer.get("credit"), str):
        credit = layer["credit"]
    out = {"code": layer["code"], "council": layer["council"],
           "url": layer["url"], "licence": layer.get("licence"),
           "attribution": attribution(layer, credit)}
    if credit:
        out["credit"] = credit
    if layer.get("role"):
        out["role"] = layer["role"]
    return out


# ---------------------------------------------------------------- geometry

def _xy(point):
    lon, lat = point
    return (lon * 111320.0 * math.cos(math.radians(lat)), lat * 110574.0)


def _samples(line, step=STEP_M):
    """Points every `step` metres along a line, with each original vertex.
    -> [(x, y, is_vertex, vertex_index)]"""
    pts = [_xy(p) for p in line]
    out = [(pts[0][0], pts[0][1], True, 0)]
    for i in range(1, len(pts)):
        (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        seg = math.hypot(x1 - x0, y1 - y0)
        n = int(seg // step)
        for k in range(1, n + 1):
            t = k * step / seg
            if t < 1.0:
                out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, False, i))
        out.append((x1, y1, True, i))
    return out


class _Index(object):
    """Segments in a grid, for "is this point within d of any of them"."""

    def __init__(self, lines, cell=50.0):
        self.cell = cell
        self.grid = {}
        for line in lines:
            pts = [_xy(p) for p in line]
            for a, b in zip(pts, pts[1:]):
                x0, x1 = sorted((a[0], b[0]))
                y0, y1 = sorted((a[1], b[1]))
                for gx in range(int(math.floor(x0 / cell)),
                                int(math.floor(x1 / cell)) + 1):
                    for gy in range(int(math.floor(y0 / cell)),
                                    int(math.floor(y1 / cell)) + 1):
                        self.grid.setdefault((gx, gy), []).append((a, b))

    def near(self, x, y, d):
        r = int(math.ceil(d / self.cell))
        gx, gy = int(math.floor(x / self.cell)), int(math.floor(y / self.cell))
        for i in range(gx - r, gx + r + 1):
            for j in range(gy - r, gy + r + 1):
                for a, b in self.grid.get((i, j), ()):
                    if _seg_dist(x, y, a, b) <= d:
                        return True
        return False


def _seg_dist(x, y, a, b):
    (x0, y0), (x1, y1) = a, b
    dx, dy = x1 - x0, y1 - y0
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy)
                                         / L))
    return math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))


def _length(line):
    pts = [_xy(p) for p in line]
    return sum(math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in zip(pts, pts[1:]))


def covered_share(line, index, d=SAME_WAY_M):
    s = _samples(line)
    return sum(1 for x, y, _v, _i in s if index.near(x, y, d)) / float(len(s))


def uncovered_runs(line, index, d=SAME_WAY_M, min_len=MIN_NEW_M):
    """The parts of `line` farther than d from everything in `index`, each
    at least min_len long, as lines of the original (lon, lat) vertices plus
    the run's own two ends."""
    s = _samples(line)
    flags = [not index.near(x, y, d) for x, y, _v, _i in s]
    runs, start = [], None
    for k, out in enumerate(flags + [False]):
        if out and start is None:
            start = k
        elif not out and start is not None:
            runs.append((start, k - 1))
            start = None
    pieces = []
    for a, b in runs:
        pts = []
        for k in range(a, b + 1):
            x, y, is_vertex, i = s[k]
            if is_vertex or k in (a, b):
                pts.append(line[i] if is_vertex else _lonlat(x, y))
        if len(pts) >= 2 and _length(pts) >= min_len:
            pieces.append(pts)
    return pieces


def _lonlat(x, y):
    lat = y / 110574.0
    return (round(x / (111320.0 * math.cos(math.radians(lat))), 5),
            round(lat, 5))


# ------------------------------------------------------------------ merge

def _feature_lines(feature):
    g = feature.get("geometry") or {}
    if g.get("type") == "LineString":
        return [g.get("coordinates") or []]
    if g.get("type") == "MultiLineString":
        return g.get("coordinates") or []
    return []


def merge(code, rowmaps_features, council):
    """-> (features, report). The council layer decides which byways exist;
    every rowmaps record it still draws is kept byte for byte (so its id is
    unchanged), and what it draws that rowmaps lacks is added.

    `features` are rowmaps-shaped (Name, Description) with TB_council and
    TB_attribution set, so build_packages.normalise credits the council.
    """
    source = council.get("source") or {}
    tag = {"TB_council": source.get("council") or code,
           "TB_attribution": source.get("attribution") or OGL}
    council_lines = [[tuple(p) for p in l]
                     for w in council.get("ways") or [] for l in w["lines"]]
    council_index = _Index(council_lines)

    kept, dropped = [], []
    for f in rowmaps_features:
        lines = [l for l in _feature_lines(f) if len(l) >= 2]
        if not lines:
            continue
        shares = [covered_share(l, council_index) for l in lines]
        total = sum(_length(l) for l in lines) or 1.0
        share = sum(s * _length(l) for s, l in zip(shares, lines)) / total
        if share >= KEEP_SHARE:
            kept.append(f)
        else:
            dropped.append(f)

    kept_index = _Index([tuple(p) for p in l] for f in kept
                        for l in _feature_lines(f))
    added = []
    council_len = new_len = 0.0
    for w in sorted(council.get("ways") or [],
                    key=lambda w: (w["parish"], w["number"], w["id"])):
        for line in w["lines"]:
            line = [tuple(p) for p in line]
            council_len += _length(line)
            for piece in uncovered_runs(line, kept_index):
                new_len += _length(piece)
                added.append(new_feature(code, w, piece, tag))

    out = []
    for f in kept:
        g = dict(f)
        g["properties"] = dict(f.get("properties") or {}, **tag)
        out.append(g)
    out.extend(added)
    report = {"rowmaps": len(kept) + len(dropped), "kept": len(kept),
              "dropped": sorted(_name(f) for f in dropped),
              "added": sorted(_name(f) for f in added),
              "council_ways": len(council.get("ways") or []),
              "new_share": round(new_len / council_len, 3)
              if council_len else 0.0}
    return out, report


def merge_features(old_features, new_features):
    """-> (features, report): a newer copy of the same rowmaps file, taken
    with every unchanged record's id kept.

    An old record the new file still draws (KEEP_SHARE of it within
    SAME_WAY_M) is kept byte for byte, so its lane id - which hashes its
    coordinates - does not move when only the file's precision or vertex
    order changed. A new record not already drawn by what was kept is added
    whole, and the kept records it covers give way to it: an extended or
    re-aligned byway becomes the new record, not the old one plus a copy.
    An old record the new file no longer draws is dropped.
    """
    new_index = _Index([tuple(p) for p in l] for f in new_features
                       for l in _feature_lines(f) if len(l) >= 2)
    kept, dropped = [], []
    for f in old_features:
        lines = [l for l in _feature_lines(f) if len(l) >= 2]
        if not lines:
            continue
        total = sum(_length(l) for l in lines) or 1.0
        share = sum(covered_share(l, new_index) * _length(l)
                    for l in lines) / total
        (kept if share >= KEEP_SHARE else dropped).append(f)

    kept_index = _Index([tuple(p) for p in l] for f in kept
                        for l in _feature_lines(f))
    added, new_len, all_len = [], 0.0, 0.0
    for f in new_features:
        lines = [l for l in _feature_lines(f) if len(l) >= 2]
        if not lines:
            continue
        total = sum(_length(l) for l in lines) or 1.0
        all_len += total
        share = sum(covered_share(l, kept_index) * _length(l)
                    for l in lines) / total
        if share < KEEP_SHARE:
            added.append(f)
            new_len += total * (1.0 - share)

    if added:
        # Kept records a new one now draws give way to it.
        added_index = _Index([tuple(p) for p in l] for f in added
                             for l in _feature_lines(f) if len(l) >= 2)
        still = []
        for f in kept:
            lines = [l for l in _feature_lines(f) if len(l) >= 2]
            total = sum(_length(l) for l in lines) or 1.0
            share = sum(covered_share(l, added_index) * _length(l)
                        for l in lines) / total
            (dropped if share >= KEEP_SHARE else still).append(f)
        kept = still

    report = {"rowmaps": len(old_features), "kept": len(kept),
              "dropped": sorted(_name(f) for f in dropped),
              "added": sorted(_name(f) for f in added),
              "council_ways": len(new_features),
              "new_share": round(new_len / all_len, 3) if all_len else 0.0}
    return kept + added, report


def agrees(report):
    """None if the council file may replace rowmaps' byways, else why not."""
    if not report["council_ways"]:
        return "the council file holds no byways"
    if report["rowmaps"] and report["kept"] < MIN_KEPT_SHARE * \
            report["rowmaps"]:
        return ("the council layer still draws only %d of rowmaps' %d "
                "byways" % (report["kept"], report["rowmaps"]))
    # With nothing from rowmaps to compare, everything is "new": the
    # council's layer is then the only record there is.
    if report["rowmaps"] and report["new_share"] > MAX_NEW_SHARE:
        return ("%.0f%% of the council's byway length is not in rowmaps"
                % (100 * report["new_share"]))
    return None


def _name(f):
    return (f.get("properties") or {}).get("Name") or ""


def new_feature(code, way, line, tag):
    miles = _length(line) / 1609.344
    coords = [[round(p[0], 5), round(p[1], 5)] for p in line]
    return {"type": "Feature",
            "properties": dict({
                "Name": "%s|%s|%s" % (code, way["parish"], way["number"]),
                "Description": "BO|%s:%s|%.3f|none" % (code, way["id"],
                                                       miles)}, **tag),
            "geometry": {"type": "LineString", "coordinates": coords}}


def load_rowmaps(code, cache=None):
    fc = read_json(os.path.join(cache or CACHE, code, BOAT_FILE))
    return None if fc is None else (fc.get("features") or [])


def not_used_because(role, rowmaps_features, report):
    """None if the build uses a council file, else why not. The one rule
    byways_for applies and status.json reports, so they cannot disagree."""
    if role == "cross-check" and rowmaps_features:
        return ("a cross-check layer: compared with rowmaps, and used only "
                "if rowmaps has no file for the authority")
    return agrees(report)


def too_old(code, out_dir, today, max_age_days=MAX_AGE_DAYS):
    """Why the council file is too old to use, or None. Its age is its
    last good read in status.json; a file with none recorded has no age
    anyone can vouch for and counts as too old."""
    status = read_json(os.path.join(out_dir, "status.json"), {}) or {}
    last = (status.get(code) or {}).get("last_ok")
    if not last:
        return "no good read of it is recorded in status.json"
    age = (datetime.date.fromisoformat(today)
           - datetime.date.fromisoformat(last)).days
    if age >= max_age_days:
        return ("its last good read was %s, %d days ago (the limit is %d)"
                % (last, age, max_age_days))
    return None


def council_source(code, out_dir=None):
    """The `source` (public()) the council file for `code` was written
    with, or None: what the build credits for byways it took from it."""
    council = read_json(os.path.join(out_dir or OUT, "%s.json" % code))
    return (council or {}).get("source")


def byways_for(code, rowmaps_features, out_dir=None, log=print, today=None):
    """What build_packages uses for an authority's byways: the merge when a
    council file exists, is recent and agrees with rowmaps, else rowmaps
    unchanged."""
    out_dir = out_dir or OUT
    council = read_json(os.path.join(out_dir, "%s.json" % code))
    if not council:
        return rowmaps_features, None
    role = (council.get("source") or {}).get("role")
    if role == "cross-check" and rowmaps_features:
        # A cross-check layer stands in only when rowmaps has nothing.
        return rowmaps_features, None
    old = too_old(code, out_dir,
                  today or datetime.date.today().isoformat())
    if old and rowmaps_features:
        # With nothing from rowmaps, an old council file is still the only
        # record there is, and better than no byways at all.
        log("::warning::%s: council byway layer NOT used, rowmaps kept: %s"
            % (code, old))
        return rowmaps_features, None
    features, report = merge(code, rowmaps_features or [], council)
    why = not_used_because(role, rowmaps_features, report)
    if why:
        log("::warning::%s: council byway layer NOT used, rowmaps kept: %s"
            % (code, why))
        return rowmaps_features, report
    log("  %s: council layer kept %d of %d rowmaps byways, dropped %d, "
        "added %d" % (code, report["kept"], report["rowmaps"],
                      len(report["dropped"]), len(report["added"])))
    return features, report


# ------------------------------------------------------------------ fetch

def fetch_one(layer, client, out_dir, today, cache=None):
    cache = cache or CACHE
    path = os.path.join(out_dir, "%s.json" % layer["code"])
    previous = read_json(path) or {}
    entry = {"council": layer["council"]}
    try:
        got = layer["read"](client, layer)
    except (Refused, FetchFailed) as e:
        entry.update({"ok": False, "error": str(e)[:300]})
        return entry
    records, ways = got[0], got[1]
    credit = (got[2] if len(got) > 2 else {}).get("credit")
    before = len(previous.get("ways") or [])
    if not ways:
        entry.update({"ok": False, "error": "read %d records and no byway "
                      "lines; kept the last good file" % records})
        return entry
    if before and len(ways) < before * FLOOR_SHARE:
        entry.update({"ok": False, "error": "%d byways against %d last time "
                      "- a bad read, not a quiet month; kept the last good "
                      "file" % (len(ways), before)})
        return entry
    ways = sorted(({"id": w["id"], "parish": w["parish"],
                    "number": w["number"],
                    "lines": [[[round(p[0], 5), round(p[1], 5)] for p in l]
                              for l in w["lines"]]} for w in ways),
                  key=lambda w: (w["parish"], w["number"], w["id"]))
    data = {"source": public(layer, credit), "records": records,
            "ways": ways}
    entry.update({"ok": True, "records": records, "ways": len(ways),
                  "changed": write_json(path, data), "last_ok": today})
    rowmaps = load_rowmaps(layer["code"], cache)
    if rowmaps is not None:
        _f, report = merge(layer["code"], rowmaps, data)
        why = not_used_because(layer.get("role"), rowmaps, report)
        entry["merge"] = {"kept": report["kept"],
                          "rowmaps": report["rowmaps"],
                          "dropped": report["dropped"][:40],
                          "added": report["added"][:40],
                          "new_share": report["new_share"],
                          "used": why is None,
                          "why_not": why}
    return entry


def fetch(codes=None, out_dir=None, today=None, client=None, report=None):
    out_dir = out_dir or OUT
    today = today or datetime.date.today().isoformat()
    client = client or polite_http.PoliteClient(allow=client_allow())
    status_path = os.path.join(out_dir, "status.json")
    status = read_json(status_path, {}) or {}
    failed = []
    import manual_inbox
    by_hand = set(b["code"] for b in manual_inbox.inboxes()
                  if not b["automated"])
    for layer in LAYERS:
        if codes and layer["code"] not in codes:
            continue
        old = status.get(layer["code"]) or {}
        if layer["code"] in by_hand:
            # The owner supplies this council's documents by hand
            # (manual/): nothing is fetched; the last file read stands.
            status[layer["code"]] = dict(old, skipped="documents come from "
                                         "manual/%s; not fetched"
                                         % layer["code"])
            continue
        every = layer.get("every_days")
        if every and old.get("last_ok") and (
                datetime.date.fromisoformat(today)
                - datetime.date.fromisoformat(old["last_ok"])).days < every:
            # Read well recently enough: a download is replaced monthly at
            # most. A failed read keeps the old last_ok, so it is tried
            # again the next day.
            if codes:
                # Asked for by name: say why nothing was read.
                due = datetime.date.fromisoformat(old["last_ok"]) + \
                    datetime.timedelta(days=every)
                print("%s %-34s not read: read well on %s, and read every "
                      "%d days, so next due %s" % (
                          layer["code"], layer["council"], old["last_ok"],
                          every, due.isoformat()))
            continue
        why = unlisted(layer)
        if why:
            entry = {"council": layer["council"], "ok": False, "error": why}
        else:
            entry = fetch_one(layer, client, out_dir, today)
        if entry["ok"]:
            entry["failing_since"] = None
        else:
            entry["last_ok"] = old.get("last_ok")
            entry["failing_since"] = old.get("failing_since") or today
            failed.append("%s (%s): %s" % (layer["code"], layer["council"],
                                           entry["error"]))
        merge_note = entry.get("merge") or {}
        # A cross-check is not used by design; that is not a problem to
        # raise every day.
        if merge_note and not merge_note.get("used") and \
                layer.get("role") != "cross-check":
            failed.append("%s (%s): read, but not used - %s"
                          % (layer["code"], layer["council"],
                             merge_note.get("why_not")))
        status[layer["code"]] = entry
        print("%s %-34s %s" % (layer["code"], layer["council"], json.dumps(
            dict((k, v) for k, v in entry.items()
                 if k not in ("council", "merge")))))
        if merge_note:
            print("     merge: kept %s/%s, dropped %d, added %d%s" % (
                merge_note["kept"], merge_note["rowmaps"],
                len(merge_note["dropped"]), len(merge_note["added"]),
                "" if merge_note["used"] else " - NOT USED: %s"
                % merge_note["why_not"]))
    write_json(status_path, status)
    if report:
        with open(report, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(line + "\n" for line in failed))
    for line in failed:
        print("::warning::council byway layer %s" % line)
    return status, failed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch")
    f.add_argument("--only", help="comma-separated rowmaps authority codes")
    f.add_argument("--out", default=OUT)
    f.add_argument("--today")
    f.add_argument("--report", help="write the problems here, one a line")
    p = sub.add_parser("preview")
    p.add_argument("code")
    p.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        codes = set(c.strip().upper() for c in (args.only or "").split(",")
                    if c.strip())
        status, _failed = fetch(codes or None, args.out, args.today,
                                report=args.report)
        # Exit 1 only when nothing at all could be read: one council being
        # down is reported, not a failed run.
        tried = [c for c in status if not codes or c in codes]
        if tried and not any(status[c].get("ok") for c in tried):
            print("::error::no council byway layer could be read")
            return 1
        return 0
    if args.cmd == "preview":
        rowmaps = load_rowmaps(args.code.upper())
        if rowmaps is None:
            print("no rowmaps file for %s in cache/" % args.code)
            return 1
        council = read_json(os.path.join(args.out, "%s.json"
                                         % args.code.upper()))
        _features, report = merge(args.code.upper(), rowmaps, council or {})
        print(json.dumps(report, indent=1))
        print("verdict:", agrees(report) or "used")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Read councils' own published closures and byway orders, match them to our
byways, and keep the last good copy of each.

    python tools/council_sources.py fetch                 # every source
    python tools/council_sources.py fetch --only dorset-closures
    python tools/council_sources.py list                  # what is read, and how

Writes, per source, `tro/council/<id>.json` - the source's own records that
fall on a byway riders have, already matched to its `way_uid`s - and
`tro/council/status.json`, when each source was last read successfully.
tools/build_tro.py lays them into the traffic-order pack (council_orders.py).

WHY THESE SOURCES
-----------------
D-TRO barely carries byway orders even for councils that publish to it (81 of
146,202 records name a byway; data-sources-report 2.2), and it is not
mandatory. These councils publish their own byway closures and orders in a
machine-readable form (report 2.3), and the owner decided on 7 October 2026
that council and government data is public information to be read from where
the council publishes it, crediting every source by name. They run beside
D-TRO indefinitely; the build drops anything D-TRO already holds.

THE RULES EVERY SOURCE HERE KEEPS (tools/polite_http.py enforces them):
  * an honest User-Agent, robots.txt obeyed, a gap between requests;
  * read only - ArcGIS layers are only ever asked for metadata and /query;
  * nothing behind a bot challenge, nothing robots.txt forbids: West
    Berkshire's GIS host disallows all robots, so its closures layer is
    listed here as not read, with the reason, rather than read anyway;
  * personal data is never stored: contact names, phone numbers, e-mail
    addresses, applicants and free-text reasons that can name a householder
    are dropped at the adapter, before anything is written.

A FAILED SOURCE NEVER BLANKS WHAT IS PUBLISHED. A source that cannot be read,
or that suddenly returns nothing (or under a third of what it held last time),
leaves its previous file exactly as it was and is reported as failing; the
workflow raises an issue. Only a successful read replaces a file, and a file
whose content did not change is not rewritten, so an unchanged day commits
nothing.
"""
import argparse
import calendar
import datetime
import html
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from osgb import grid_to_wgs84  # noqa: E402
import polite_http  # noqa: E402
from polite_http import FetchFailed, Refused, arcgis_query  # noqa: E402
from text_clean import clean_text  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "tro", "council")

# A source whose record count falls below this share of its last good read
# is treated as a bad read, not a quiet week (the same thinking as the D-TRO
# build's two-thirds floor, looser because council layers are small).
FLOOR_SHARE = 1.0 / 3.0
# ...but only once it held this many: a layer of four closures can genuinely
# fall to one.
FLOOR_MIN = 6

_MONTHS = dict((m.lower(), i) for i, m in enumerate(calendar.month_name)
               if m)
_MONTHS.update(dict((m.lower(), i) for i, m in enumerate(calendar.month_abbr)
                    if m))
_MONTHS["sept"] = 9
_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|"
        r"july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)")


# ----------------------------------------------------------------- parsing


def from_epoch_ms(value):
    """An ArcGIS date (milliseconds since 1970) as YYYY-MM-DD, or None.

    Anything past the year 2900 is a "never expires" sentinel (Northumberland
    stores 32472144000000), not a date.
    """
    if value in (None, ""):
        return None
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    if ms > 29000000000000 or ms < -2208988800000:
        return None
    day = datetime.datetime(1970, 1, 1) + datetime.timedelta(
        milliseconds=ms)
    return day.date().isoformat()


def parse_date(text):
    """A council's written date as YYYY-MM-DD, or None.

    "24th December 2007", "Thursday 24 September 2026", "14 May 2026",
    "01/12/2026", "20260413", "2026-04-06Z".
    """
    if not text:
        return None
    text = html.unescape(str(text)).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", text)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if m:
        return _iso(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"\.?,?\s+(\d{4})",
                  text, re.I)
    if m:
        return _iso(int(m.group(3)), _MONTHS[m.group(2).lower()[:3]],
                    int(m.group(1)))
    return None


def _iso(year, month, day):
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def end_of_month(text):
    """'(until December 2026)' -> 2026-12-31."""
    m = re.search(r"until\s+" + _MON + r"\s+(\d{4})", text or "", re.I)
    if not m:
        return None
    month = _MONTHS[m.group(1).lower()[:3]]
    year = int(m.group(2))
    return _iso(year, month, calendar.monthrange(year, month)[1])


def parse_season(text):
    """{"from": "MM-DD", "to": "MM-DD"} from "1 October to 30 April", or None.

    Also "(1 November - 31 March)" and "1st Oct until 30th Apr".
    """
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON +
                  r"\s*(?:to|-|–|until|and)\s*(\d{1,2})(?:st|nd|rd|th)?\s+"
                  + _MON, text or "", re.I)
    if not m:
        return None
    fm = _MONTHS[m.group(2).lower()[:3]]
    tm = _MONTHS[m.group(4).lower()[:3]]
    return {"from": "%02d-%02d" % (fm, int(m.group(1))),
            "to": "%02d-%02d" % (tm, int(m.group(3)))}


_EXCEPT_MC = re.compile(
    r"(except|excluding|other than|but not)\s+(for\s+)?(solo\s+)?"
    r"motor\s?-?cycles|"
    r"motor\s?-?c[yi]c?les?\s+(without\s+sidecars\s+)?(are\s+)?exempt",
    re.I)
_WIDTH = re.compile(r"width\s+(?:exceeding|over|of more than|greater than)"
                    r"\s+(\d+(?:\.\d+)?)\s*m", re.I)


def classify(text):
    """(vehicles, width_m) for an order's own words, or (None, None)."""
    t = (text or "").lower()
    if _EXCEPT_MC.search(t):
        return "motor_vehicles_except_motorcycles", None
    m = _WIDTH.search(t)
    if m:
        return "vehicles_over_width", float(m.group(1))
    if re.search(r"\b(all|any) (vehicles|traffic)\b|axled vehicles", t):
        return "all_vehicles", None
    if re.search(r"motor vehicles?|motori[sz]ed vehicles?|"
                 r"mechanically propelled|"
                 r"prohibition of driving|motor vehciles", t):
        return "motor_vehicles", None
    return None, None


_EMAIL = re.compile(r"\S+@\S+")


def strip_personal(text):
    """Drop e-mail addresses and named contacts from a council's summary."""
    text = _EMAIL.sub("", text or "")
    # "- Tom Partridge Pendle Bc Contact" / "Name Contact": everything from
    # a dash to the word Contact is a person, not the closure.
    text = re.sub(r"\s+-\s+[^-()]*?\bcontact\b", "", text, flags=re.I)
    text = re.sub(r"\b[\w.' ]*\bcontact\b", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -:")


# ---------------------------------------------------------------- geometry


def _wgs(points):
    out = []
    for p in points:
        if len(p) < 2:
            continue
        e, n = float(p[0]), float(p[1])
        if not (0 < e < 800000 and 0 < n < 1400000):
            continue
        lon, lat = grid_to_wgs84(e, n)
        point = (round(lon, 6), round(lat, 6))
        if not out or out[-1] != point:
            out.append(point)
    return out


def esri_lines(geometry):
    """An esri JSON polyline in BNG as lines of (lon, lat)."""
    if not isinstance(geometry, dict):
        return []
    paths = geometry.get("paths")
    if paths:
        return [l for l in (_wgs(p) for p in paths) if len(l) >= 2]
    if "x" in geometry and "y" in geometry:
        line = _wgs([(geometry["x"], geometry["y"])])
        return [line] if line else []
    return []


def geojson_lines(geometry):
    """A GeoJSON line geometry in BNG as lines of (lon, lat)."""
    if not isinstance(geometry, dict):
        return []
    kind, coords = geometry.get("type"), geometry.get("coordinates") or []
    if kind == "LineString":
        coords = [coords]
    elif kind != "MultiLineString":
        return []
    return [l for l in (_wgs(c) for c in coords) if len(l) >= 2]


def as_geometry(lines):
    lines = [[[round(x, 5), round(y, 5)] for x, y in l] for l in lines if l]
    if not lines:
        return None
    if len(lines) == 1:
        return {"type": "LineString", "coordinates": lines[0]}
    return {"type": "MultiLineString", "coordinates": lines}


# ----------------------------------------------------------------- sources
#
# Each adapter returns (records read, [candidate]). A candidate is a dict
# with the item fields council_orders.feature_of reads, plus either `lines`
# (its own geometry, WGS84) or `refs` ([(parish, number)]) to match by.


def _arc(layer):
    return layer.rstrip("/") + "/query"


DORSET_WFS = ("https://gi.dorsetcouncil.gov.uk/geoserver/countryside/wfs?"
              "service=WFS&version=2.0.0&request=GetFeature&"
              "typeNames=countryside:route_closed&"
              "outputFormat=application/json")


def read_dorset(client):
    data = client.get_json(DORSET_WFS)
    feats = data.get("features") or []
    out = []
    for f in feats:
        p = f.get("properties") or {}
        if (p.get("status") or "").strip().lower() != \
                "byway open to all traffic":
            continue
        code = (p.get("route_code") or "").strip()
        parish = re.sub(r"\s+CP$", "", (p.get("parish") or "").strip())
        partial = "part" in (p.get("operationalstatus") or "").lower()
        name = (p.get("name") or "").strip()
        where = "Byway %s, %s%s" % (code, parish,
                                    (" (%s)" % name) if name else "")
        start = parse_date(p.get("closure_start_date"))
        end = None if p.get("closure_is_indefinite") else \
            parse_date(p.get("closure_end_date"))
        prefix, _, number = code.partition("/")
        out.append({
            "id": "%s|%s" % (code, start or ""),
            "ref": code,
            "title": "Byway %s %s" % (code, "partly closed" if partial
                                      else "closed"),
            "where": where,
            "vehicles": "all_users", "partial": partial,
            "form": "temporary",
            "start": start, "end": end,
            "url": p.get("closure_notification_document"),
            "lines": geojson_lines(f.get("geometry")),
            "refs": [(prefix, number)] if number else [],
        })
    return len(feats), out


SUFFOLK = "https://services-eu1.arcgis.com/IJhjmLvg2gPzHqkX/arcgis/rest/services"


def read_suffolk_tros(client):
    feats = arcgis_query(client, SUFFOLK + "/ROWTRO_View/FeatureServer/0")
    out, review = [], []
    for f in feats:
        a = f.get("attributes") or {}
        if (a.get("Status") or "").strip().lower() != "byway":
            continue
        cond = clean_text(a.get("Conditions") or "").strip()
        vehicles, width = classify(cond)
        form, season = "permanent", None
        window = parse_season(cond)
        # "...prohibited all year. No person shall ... ride or lead a horse
        # between 1 October to 30 April": the season is about horses.
        if window and "all year" not in cond.lower():
            form, season = "seasonal", window
        number = str(a.get("Route_No") or "").strip()
        suffix = str(a.get("Suffix") or "").strip()
        if suffix and suffix != "0":
            number = "%s/%s" % (number, suffix)
        parish = (a.get("DM_Parish") or "").strip()
        item = {
            "id": "%s|%s|%s" % (a.get("TRO_Ref"), parish, number),
            "ref": (a.get("TRO_Ref") or "").strip(),
            "title": "Suffolk County Council %s: %s" % (
                (a.get("TRO_Ref") or "").strip(), cond),
            "where": "Byway %s, %s" % (number, parish),
            "vehicles": vehicles, "width_m": width,
            "form": form, "season": season,
            "url": a.get("Link"),
            "lines": esri_lines(f.get("geometry")),
            "refs": [],
        }
        if vehicles is None:
            item["vehicles"] = "other"
            item["label"] = cond[:80] or "Restriction"
        out.append(item)
    return len(feats), out


def read_suffolk_ttros(client):
    feats = arcgis_query(client, SUFFOLK + "/TTROS_LIVE_view/FeatureServer/0")
    out = []
    for f in feats:
        a = f.get("attributes") or {}
        status = (a.get("Status") or "").lower()
        if "byway" not in status or "restricted" in status:
            continue
        parish = (a.get("Parish") or "").strip()
        number = str(a.get("Path_No") or "").strip()
        start = from_epoch_ms(a.get("Date_Effective")) or parse_date(
            a.get("Date_Effective"))
        end = from_epoch_ms(a.get("End_Date")) or parse_date(
            a.get("End_Date"))
        out.append({
            "id": "%s|%s|%s|%s" % (a.get("ID"), parish, number, start),
            "ref": str(a.get("ID") or "").strip() or None,
            "title": "Temporary closure of Byway %s, %s" % (number, parish),
            "where": "Byway %s, %s" % (number, parish),
            "vehicles": "all_users", "form": "temporary",
            "start": start, "end": end,
            "url": "https://www.suffolk.gov.uk/roads-and-transport/"
                   "public-rights-of-way-in-suffolk",
            "lines": esri_lines(f.get("geometry")),
            "refs": [],
        })
    return len(feats), out


ESSEX = ("https://services-eu1.arcgis.com/48zF6lPtAjyLTDSU/arcgis/rest/"
         "services/Essex_PRoW_Traffic_Regulation_Orders_Public_View/"
         "FeatureServer/0")


def read_essex(client):
    feats = arcgis_query(client, ESSEX)
    out, review = [], []
    for f in feats:
        a = f.get("attributes") or {}
        ptype = (a.get("Path_Type") or "").lower()
        if "byway" not in ptype or "restricted" in ptype:
            continue
        # A diversion's line is where traffic is SENT, not what is shut.
        if (a.get("Item_Type") or "").strip().lower() == "diversion":
            continue
        desc = clean_text(a.get("Closure_Description") or "").strip()
        category = (a.get("Category") or "").strip().lower()
        parish = (a.get("Parish") or "").strip()
        number = str(a.get("Path_number") or "").strip()
        start = from_epoch_ms(a.get("Valid_From"))
        end = from_epoch_ms(a.get("Valid_to_"))
        vehicles, width = classify(desc)
        if category == "temporary":
            form, season = "temporary", None
            if vehicles is None:
                vehicles = "all_users"
        elif category == "seasonal":
            form, season = "seasonal", parse_season(desc)
        else:
            form, season = "permanent", None
        if vehicles is None:
            vehicles = "motor_vehicles" if "driving" in desc.lower() \
                else "other"
        item = {
            "id": a.get("GlobalID") or a.get("OBJECTID"),
            "ref": "%s Byway %s" % (parish, number),
            "title": "Essex County Council: %s" % (
                desc[:200] or "%s order" % category.title()),
            "where": "Byway %s, %s" % (number, parish),
            "vehicles": vehicles, "width_m": width,
            "form": form, "season": season,
            "start": start, "end": end,
            "url": a.get("File_Link"),
            "lines": esri_lines(f.get("geometry")),
            "refs": [(parish, number)],
        }
        if vehicles == "other":
            item["label"] = desc[:80] or "Restriction"
        out.append(item)
    return len(feats), out


NLAND = "https://services2.arcgis.com/LrUbY6lLLgV3tEa5/arcgis/rest/services"


def read_northumberland(client):
    """Permanent orders from the closures layer, temporary ones from the
    TTRO view. ALT features are alternative routes and are never read.
    CONTACT, APPLICANT, WhoBy and Reason are dropped: they name officers,
    companies and householders ("the owner of 1 Courtyard Gardens")."""
    perm = arcgis_query(client, NLAND +
                        "/PROW_RightsOfWayClosures/FeatureServer/0",
                        where="TYPE='PTR'")
    temp = arcgis_query(client, NLAND +
                        "/PRoW_TTRO_(rowclose_ln)_MASTER_VIEW/FeatureServer/0",
                        where="TYPE='TTR'")
    out = []
    for f in perm:
        a = f.get("attributes") or {}
        key = (a.get("KEYID") or "").strip()
        purpose = (a.get("PURPOSE_REASON") or "").strip()
        vehicles, width = classify(purpose or a.get("DESC_EXISTING"))
        parish, _, number = key.partition("/")
        out.append({
            "id": "PTR|%s|%s" % (key, a.get("OBJECTID")),
            "ref": key,
            "title": "Northumberland County Council permanent order on "
                     "path %s: %s" % (key, purpose or "prohibition of motor "
                                      "vehicles"),
            "where": clean_text(a.get("DESC_EXISTING") or "")[:300] or None,
            "vehicles": vehicles or "motor_vehicles", "width_m": width,
            "form": "permanent",
            "start": from_epoch_ms(a.get("START_ORDER_DATE")),
            "url": "https://www.northumberland.gov.uk/Highways/"
                   "Public-rights-of-way.aspx",
            "lines": esri_lines(f.get("geometry")),
            "refs": [(parish, number)] if number else [],
            # Neither layer says what kind of path it is: most temporary
            # closures are footpaths, so a miss is not worth reviewing.
            "claims_byway": False,
        })
    for f in temp:
        a = f.get("attributes") or {}
        key = (a.get("KEYID") or "").strip()
        start = parse_date(a.get("Start_date"))
        end = parse_date(a.get("End_date")) or from_epoch_ms(
            a.get("Expiry_Date"))
        parish, _, number = key.partition("/")
        out.append({
            "id": "TTR|%s|%s" % (key, a.get("GlobalID")),
            "ref": key,
            "title": "Northumberland County Council temporary closure of "
                     "path %s" % key,
            "where": "Path %s" % key,
            "vehicles": "all_users", "form": "temporary",
            "start": start, "end": end,
            "url": "https://www.northumberland.gov.uk/Highways/"
                   "Public-rights-of-way.aspx",
            "lines": esri_lines(f.get("geometry")),
            "refs": [(parish, number)] if number else [],
            # Neither layer says what kind of path it is: most temporary
            # closures are footpaths, so a miss is not worth reviewing.
            "claims_byway": False,
        })
    return len(perm) + len(temp), out


BRACKNELL = ("https://services9.arcgis.com/5eO9hmsd8SoBl0Cj/arcgis/rest/"
             "services/GIS_PROW_TROs/FeatureServer/9")


def read_bracknell(client):
    feats = arcgis_query(client, BRACKNELL)
    out = []
    for f in feats:
        a = f.get("attributes") or {}
        if "byway open to all traffic" not in (a.get("ROWType") or "").lower():
            continue
        limits = clean_text(a.get("TROLimitations") or "").strip()
        vehicles, width = classify(limits)
        parish = (a.get("Parish") or "").strip().title()
        number = str(a.get("ROWNo") or "").strip()
        name = (a.get("ROWName") or "").strip()
        item = {
            "id": "%s|%s" % ((a.get("UniqueID") or "").strip(),
                             a.get("OBJECTID")),
            "ref": (a.get("UniqueID") or "").strip(),
            "title": "Bracknell Forest Council order: %s" % (
                limits or "restriction"),
            "where": "Byway %s, %s%s" % (number, parish,
                                         (" (%s)" % name) if name else ""),
            "vehicles": vehicles, "width_m": width, "form": "permanent",
            "url": "https://www.bracknell-forest.gov.uk/parks-and-"
                   "countryside/public-rights-way",
            "lines": esri_lines(f.get("geometry")),
            "refs": [(parish, number)],
        }
        if vehicles is None:
            item["vehicles"] = "other"
            item["label"] = (limits[:1].upper() + limits[1:80]) or \
                "Restriction"
        out.append(item)
    return len(feats), out


LANCASHIRE = ("https://services-eu1.arcgis.com/9MmxkLJT84uEwsJx/arcgis/rest/"
              "services/Public_Rights_of_Way/FeatureServer/0")


def read_lancashire(client):
    feats = arcgis_query(
        client, LANCASHIRE,
        where="TempClosureSummary IS NOT NULL AND TempClosureSummary <> ''",
        out_fields="PATH_TYPE,PATH_NUMBE,SUFFIX,D_PARISH,PathRefLong,"
                   "PathTypeShort,TempClosureSummary,DIV_REF,GLOBALID,"
                   "DISTRICT,PARISH")
    out = []
    for f in feats:
        a = f.get("attributes") or {}
        ptype = (a.get("PATH_TYPE") or "").lower()
        short = (a.get("PathTypeShort") or "").upper()
        if not (("byway" in ptype and "restricted" not in ptype)
                or short in ("BOAT", "BY")):
            continue
        summary = strip_personal(clean_text(a.get("TempClosureSummary")))
        m = re.match(r"temporary closure\s*\((\d+)\)\s*:?\s*(.*)$", summary,
                     re.I)
        ref = m.group(1) if m else None
        reason = re.sub(r"\(until [^)]*\)", "", m.group(2) if m else summary)
        reason = reason.strip(" -:")
        # Our Lancashire byways are named "<district><parish> <number>",
        # e.g. "1404 348" - the council's own district and parish codes.
        refs = []
        try:
            refs = [("%d%02d" % (int(a.get("DISTRICT")), int(a.get("PARISH"))),
                     "%s%s" % (a.get("PATH_NUMBE"), a.get("SUFFIX") or ""))]
        except (TypeError, ValueError):
            pass
        out.append({
            "id": "%s|%s" % (a.get("PathRefLong"), ref),
            "ref": ("Lancashire closure %s" % ref) if ref else None,
            "title": "Lancashire County Council temporary closure%s%s" % (
                (" %s" % ref) if ref else "",
                (": %s" % reason) if reason else ""),
            "where": "Byway %s%s, %s" % (a.get("PATH_NUMBE") or "",
                                         a.get("SUFFIX") or "",
                                         a.get("D_PARISH") or ""),
            "vehicles": "all_users", "form": "temporary",
            "end": end_of_month(summary),
            "url": "https://www.lancashire.gov.uk/roads-parking-and-travel/"
                   "public-rights-of-way/",
            "lines": esri_lines(f.get("geometry")),
            "refs": refs,
        })
    return len(feats), out


DEVON = "https://www.devon.gov.uk/prow/wp-json/wp/v2/pages?parent=448&per_page=100"
_BOAT_WORDS = re.compile(r"(?<!restricted )\b(byway open to all traffic|"
                         r"b\.?o\.?a\.?t\.?|byway)\b", re.I)


def _plain(rendered):
    text = re.sub(r"<[^>]+>", " ", rendered or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def devon_refs(title, body):
    """[(parish, number)] for every byway a Devon notice names."""
    refs = []
    for m in re.finditer(
            r"([A-Z][A-Za-z.'’ -]+?)\s+(?:Byway Open to All Traffic|BOAT|"
            r"Byway)\s*(?:No\.?\s*)?(\d+[A-Za-z]?)", title or ""):
        parish = m.group(1)
        if parish.lower().endswith("restricted"):
            continue
        refs.append((re.sub(r"^(and|&)\s+", "", parish.strip()),
                     m.group(2)))
    for m in re.finditer(
            r"(?<!RESTRICTED )BYWAY OPEN TO ALL TRAFFIC\s*(?:No\.?\s*)?"
            r"(\d+[A-Z]?)\s*,\s*([A-Z][A-Z.'’ -]+)", body or ""):
        refs.append((m.group(2).strip().title(), m.group(1)))
    return sorted(set(refs))


def read_devon(client):
    pages = _wp_pages(client, DEVON)
    out = []
    for page in pages:
        title = _plain((page.get("title") or {}).get("rendered"))
        body = _plain((page.get("content") or {}).get("rendered"))
        if not _BOAT_WORDS.search(title + " " + body):
            continue
        refs = devon_refs(title, body)
        if not refs:
            continue
        start = parse_date((re.search(r"From:\s*(.{0,40}?\d{4})", body)
                            or [None, None])[1])
        end = parse_date((re.search(r"\bTo:\s*(.{0,40}?\d{4})", body)
                          or [None, None])[1])
        out.append({
            "id": page.get("id"),
            "ref": page.get("slug"),
            "title": "Devon County Council: %s" % title,
            "where": "; ".join("Byway %s, %s" % (n, p) for p, n in refs),
            "vehicles": "all_users", "form": "temporary",
            "start": start, "end": end,
            "url": page.get("link"),
            "lines": [], "refs": refs,
        })
    return len(pages), out


def _wp_pages(client, url, cap=20):
    out = []
    for page in range(1, cap + 1):
        sep = "&" if "?" in url else "?"
        try:
            batch = client.get_json("%s%spage=%d" % (url, sep, page))
        except FetchFailed as e:
            # WordPress answers 400 for a page past the end.
            if page > 1 and "400" in str(e):
                break
            raise
        if not isinstance(batch, list):
            raise FetchFailed("not a WordPress list from %s" % url)
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


SOMERSET_CLOSURES = ("https://www.somerset.gov.uk/wp-json/wp/v2/row_closures"
                     "?per_page=100")
SOMERSET_TROS = "https://www.somerset.gov.uk/wp-json/wp/v2/tro?per_page=100"
_SOM_CODE = re.compile(r"\b([A-Z]{1,3})\s?(\d{1,3}/\d{1,3}[A-Za-z]?"
                       r"(?:/\d+)?)\b")


def read_somerset(client):
    """Somerset's path closures and its traffic orders, by path code.

    Path codes ("SM 18/5") are unique to one path, and only codes that are
    one of OUR byways match, so a footpath closure never becomes a byway one.
    The TRO list is road orders; only those naming a byway are read, and a
    "Proposed" order is not an order.
    """
    closures = _wp_pages(client, SOMERSET_CLOSURES)
    tros = _wp_pages(client, SOMERSET_TROS)
    out = []
    for post in closures:
        acf = post.get("acf") or {}
        title = _plain((post.get("title") or {}).get("rendered"))
        refs = sorted(set(_SOM_CODE.findall(title + " " + (
            acf.get("type_of_work") or ""))))
        if not refs:
            continue
        docs = acf.get("supporting_documents") or []
        out.append({
            "id": "closure|%s" % post.get("id"),
            "ref": title,
            "title": "Somerset Council: %s" % title,
            "where": ", ".join("%s %s" % r for r in refs),
            "vehicles": "all_users", "form": "temporary",
            "end": parse_date(acf.get("expiry_date")),
            "claims_byway": False,
            "url": (docs[0].get("document_link") if docs and isinstance(
                docs[0], dict) else None) or post.get("link"),
            "lines": [], "refs": refs,
        })
    for post in tros:
        acf = post.get("acf") or {}
        if (acf.get("type_of_order") or "").strip().lower() == "proposed":
            continue
        title = _plain((post.get("title") or {}).get("rendered"))
        body = _plain((post.get("content") or {}).get("rendered"))
        text = "%s %s %s" % (title, body, acf.get("restrictions") or "")
        if not re.search(r"(?<!restricted )\bbyway\b", text, re.I):
            continue
        refs = sorted(set(_SOM_CODE.findall(text)))
        if not refs:
            continue
        vehicles, width = classify(acf.get("restrictions") or text)
        out.append({
            "id": "tro|%s" % post.get("id"),
            "ref": title,
            "title": "Somerset Council order: %s" % title,
            "where": ", ".join("%s %s" % r for r in refs),
            "vehicles": vehicles or "all_users", "width_m": width,
            "form": "temporary",
            "start": parse_date(acf.get("order_start_date")),
            "end": parse_date(acf.get("expiry")),
            "url": post.get("link"),
            "lines": [], "refs": refs,
        })
    return len(closures) + len(tros), out


HERTS = ("https://gis.hertfordshire.gov.uk/webmaps/rest/services/public/"
         "row/MapServer/3")
_HERTS_TYPES = {
    "prohibiting use of motor vehicles": ("motor_vehicles", None),
    "height restriction": ("height_limit", "Height restriction"),
    "weight restriction": ("weight_limit", "Weight restriction"),
    "prohibiting use of specified vehicles": (
        "other", "Specified vehicles prohibited - see the order"),
}


def read_hertfordshire(client):
    """Permanent orders recorded on Hertfordshire's own path layer.

    `PTROTYPE` names the kind of order and `PTROYEAR` the year; which
    vehicles a "specified vehicles" order names is only in the order
    itself, so it is published as a restriction to read, never as a ban.
    """
    feats = arcgis_query(
        client, HERTS, where="VALCHAR='BOAT'",
        out_fields="PATHNAME,PARISH,PATHNUMB,UNITID,PTROTYPE,PTROYEAR,"
                   "PTROURL,OBJECTID")
    out = []
    for f in feats:
        a = f.get("attributes") or {}
        kind = (a.get("PTROTYPE") or "").strip()
        if not kind:
            continue
        vehicles, label = _HERTS_TYPES.get(kind.lower(),
                                           ("other", kind[:80]))
        year = str(a.get("PTROYEAR") or "").strip()
        name = (a.get("PATHNAME") or "").strip()
        parish = (a.get("PARISH") or "").strip().title()
        item = {
            "id": "%s|%s" % (a.get("UNITID"), a.get("OBJECTID")),
            "ref": "%s %s" % (name, kind),
            "title": "Hertfordshire County Council permanent order%s: %s" % (
                (" (%s)" % year) if re.match(r"^\d{4}$", year) else "",
                kind),
            "where": "Byway %s, %s" % (str(a.get("PATHNUMB") or "")
                                       .lstrip("0"), parish),
            "vehicles": vehicles, "form": "permanent",
            "url": a.get("PTROURL") or "https://www.hertfordshire.gov.uk/"
                                       "ptros",
            "lines": esri_lines(f.get("geometry")),
            "refs": [(parish, a.get("PATHNUMB"))],
        }
        if label:
            item["label"] = label
        out.append(item)
    return len(feats), out


def read_blocked(_client):
    raise Refused("not read: robots.txt on gis2.westberks.gov.uk disallows "
                  "all automated access")


# id -> definition. `authority` is the container's authority name (what the
# byways are filed under); `publisher` is how the source is credited.
SOURCES = [
    {"id": "dorset-closures", "authority": "Dorset",
     "name": "Dorset Council - rights of way closures (WFS)",
     "kind": "council-layer", "licence": "Open Government Licence v3.0",
     "endpoint": DORSET_WFS, "read": read_dorset},
    {"id": "devon-closures", "authority": "Devon",
     "name": "Devon County Council - temporary path closures",
     "kind": "council-page", "licence": "Open Government Licence v3.0",
     "endpoint": DEVON, "read": read_devon},
    {"id": "somerset-closures", "authority": "Somerset",
     "name": "Somerset Council - rights of way closures and traffic orders",
     "kind": "council-page", "licence": "Open Government Licence v3.0",
     "endpoint": SOMERSET_CLOSURES, "read": read_somerset},
    {"id": "suffolk-prow-tros", "authority": "Suffolk",
     "name": "Suffolk County Council - PROW traffic regulation orders",
     "kind": "council-layer",
     "licence": "Terms equivalent to the OS OpenData Licence",
     "endpoint": SUFFOLK + "/ROWTRO_View/FeatureServer/0",
     "read": read_suffolk_tros},
    {"id": "suffolk-ttros", "authority": "Suffolk",
     "name": "Suffolk County Council - live temporary PROW closures",
     "kind": "council-layer", "licence": "Published by the council",
     "endpoint": SUFFOLK + "/TTROS_LIVE_view/FeatureServer/0",
     "read": read_suffolk_ttros},
    {"id": "lancashire-closures", "authority": "Lancashire",
     "name": "Lancashire County Council - public rights of way "
             "(temporary closures)",
     "kind": "council-layer", "licence": "Open Government Licence v3.0",
     "endpoint": LANCASHIRE, "read": read_lancashire},
    {"id": "essex-prow-tros", "authority": "Essex",
     "name": "Essex County Council - PRoW traffic regulation orders",
     "kind": "council-layer", "licence": "Published by the council",
     "endpoint": ESSEX, "read": read_essex},
    {"id": "northumberland-closures", "authority": "Northumberland",
     "name": "Northumberland County Council - rights of way closures "
             "and TTROs",
     "kind": "council-layer", "licence": "Published by the council",
     "endpoint": NLAND + "/PROW_RightsOfWayClosures/FeatureServer/0",
     "read": read_northumberland},
    {"id": "bracknell-prow-tros", "authority": "Bracknell Forest",
     "name": "Bracknell Forest Council - public rights of way TROs",
     "kind": "council-layer", "licence": "Published by the council",
     "endpoint": BRACKNELL, "read": read_bracknell},
    {"id": "hertfordshire-ptros", "authority": "Hertfordshire",
     "name": "Hertfordshire County Council - rights of way layer "
             "(permanent traffic regulation orders)",
     "kind": "council-layer", "licence": "Open Government Licence v3.0",
     "endpoint": HERTS, "read": read_hertfordshire},
    {"id": "west-berkshire-closures", "authority": "West Berkshire",
     "name": "West Berkshire Council - countryside closures layer",
     "kind": "council-layer", "licence": "Published by the council",
     "endpoint": "https://gis2.westberks.gov.uk/arcgis/rest/services/"
                 "Wbc_Countryside/MapServer/2",
     "read": read_blocked, "blocked": "robots.txt disallows the host"},
]


def by_id():
    return dict((s["id"], s) for s in SOURCES)


def public(source):
    """What is written about a source: everything but its reader."""
    return dict((k, v) for k, v in source.items() if k != "read")


# ---------------------------------------------------------------- matching


def match(candidates, byways, authority):
    """(items on our byways, records that claimed a byway and found none,
    records held back for a person to review)."""
    items, unmatched, review = [], [], []
    for c in candidates:
        lines = c.pop("lines", None) or []
        refs = c.pop("refs", None) or []
        claims = c.pop("claims_byway", True)
        ways, how = [], None
        if lines:
            got = byways.match_geometry(lines, authorities={authority})
            ways = [uid for uid, _share, _m in got]
            how = "geometry" if ways else None
        if not ways and refs:
            for parish, number in refs:
                ways.extend(byways.match_ref(authority, parish, number))
            ways = sorted(set(ways))
            how = "reference" if ways else None
        if not ways:
            if claims:
                unmatched.append({"id": c.get("id"), "ref": c.get("ref"),
                                  "where": c.get("where")})
            continue
        if c.get("form") == "seasonal" and not c.get("season"):
            # "Seasonal" with no season stated: there are no dates to draw
            # it by, and undated it would read as shut all year.
            review.append({"id": c.get("id"), "ref": c.get("ref"),
                           "where": c.get("where"), "ways": ways,
                           "why": "a seasonal order with no season stated"})
            continue
        geometry = as_geometry(lines) if (lines and how == "geometry") \
            else as_geometry(byways.geometry(ways))
        item = dict((k, v) for k, v in c.items() if v not in (None, ""))
        item["authority"] = authority
        item["ways"] = ways
        item["match"] = how
        item["geometry"] = geometry
        items.append(item)
    items = merge_twins(items)
    items.sort(key=lambda i: str(i.get("id")))
    unmatched.sort(key=lambda u: str(u.get("id")))
    review.sort(key=lambda u: str(u.get("id")))
    return items, unmatched, review


_TWIN_KEYS = ("ways", "vehicles", "width_m", "form", "season", "start",
              "end", "label", "partial")


def merge_twins(items):
    """One item per order per byway: a source that files one order as two
    records on the same byway (Bracknell's Pendrys Lane, in two pieces)
    would otherwise draw it twice. Their geometry is joined."""
    out, seen = [], {}
    for item in sorted(items, key=lambda i: str(i.get("id"))):
        key = json.dumps([item.get(k) for k in _TWIN_KEYS], sort_keys=True)
        first = seen.get(key)
        if first is None:
            seen[key] = item
            out.append(item)
            continue
        lines = _geometry_lines(first["geometry"]) +             _geometry_lines(item["geometry"])
        first["geometry"] = as_geometry(
            [[tuple(p) for p in l] for l in lines])
    return out


def _geometry_lines(geometry):
    if geometry["type"] == "LineString":
        return [geometry["coordinates"]]
    return list(geometry["coordinates"])


# ---------------------------------------------------------------- the files


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    """Write only when the content changed; True if it was written."""
    body = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False)
    body += "\n"
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == body:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return True


def guard(previous_records, records):
    """A reason to refuse this read, or None. The keep-last-good rule."""
    if not previous_records:
        return None
    if records == 0:
        return ("returned no records at all, against %d last time"
                % previous_records)
    if previous_records >= FLOOR_MIN and records < previous_records * \
            FLOOR_SHARE:
        return ("returned %d records against %d last time - a bad read, "
                "not a quiet week" % (records, previous_records))
    return None


def fetch_one(source, client, byways, out_dir, today):
    """Read one source. Returns its status entry. Never raises for a fault in
    the source; a fault in OUR code still raises."""
    path = os.path.join(out_dir, "%s.json" % source["id"])
    previous = read_json(path) or {}
    entry = {"name": source["name"], "authority": source["authority"]}
    if source.get("blocked"):
        entry.update({"ok": False, "blocked": source["blocked"]})
        return entry
    try:
        records, candidates = source["read"](client)
    except (Refused, FetchFailed) as e:
        entry.update({"ok": False, "error": str(e)[:300]})
        return entry
    refusal = guard(previous.get("records"), records)
    if refusal:
        entry.update({"ok": False, "error": "kept the last good read: %s"
                      % refusal})
        return entry
    items, unmatched, review = match(candidates, byways,
                                     source["authority"])
    data = {"source": public(source), "records": records,
            "items": items, "unmatched": unmatched, "review": review}
    changed = write_json(path, data)
    entry.update({"ok": True, "records": records, "items": len(items),
                  "unmatched": len(unmatched), "review": len(review),
                  "changed": changed,
                  "last_ok": today})
    return entry


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch")
    f.add_argument("--only", help="comma-separated source ids")
    f.add_argument("--out", default=OUT)
    f.add_argument("--today", default=datetime.date.today().isoformat())
    f.add_argument("--report", help="write the failures here (one per line)")
    sub.add_parser("list")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        for s in SOURCES:
            print("%-26s %-16s %s%s" % (s["id"], s["authority"], s["name"],
                                        ("  [NOT READ: %s]" % s["blocked"])
                                        if s.get("blocked") else ""))
        return 0
    if args.cmd != "fetch":
        ap.print_help()
        return 2

    from byway_match import load_byways
    byways = load_byways()
    if len(byways) < 1000:
        print("::error::only %d byways in the published containers; refusing "
              "to match against a broken checkout" % len(byways))
        return 1
    wanted = set((args.only or "").split(",")) - {""}
    client = polite_http.PoliteClient()
    status_path = os.path.join(args.out, "status.json")
    status = read_json(status_path, {}) or {}
    failures = []
    for source in SOURCES:
        if wanted and source["id"] not in wanted:
            continue
        print("--- %s" % source["id"])
        entry = fetch_one(source, client, byways, args.out, args.today)
        old = status.get(source["id"]) or {}
        if entry.get("ok"):
            print("  %d records, %d on our byways, %d naming a byway we do "
                  "not hold%s" % (entry["records"], entry["items"],
                                  entry["unmatched"],
                                  "" if entry["changed"] else " (unchanged)"))
            entry.pop("changed", None)
            entry.pop("ok", None)
            status[source["id"]] = entry
        else:
            why = entry.get("blocked") or entry.get("error")
            print("  NOT UPDATED: %s" % why)
            if entry.get("blocked"):
                status[source["id"]] = {"name": entry["name"],
                                        "authority": entry["authority"],
                                        "blocked": entry["blocked"]}
                continue
            kept = dict(old)
            kept.update({"name": entry["name"],
                         "authority": entry["authority"],
                         "error": entry.get("error")})
            kept.setdefault("failing_since", args.today)
            status[source["id"]] = kept
            failures.append("%s: %s" % (source["id"], why))
    write_json(status_path, status)
    print("requests made: %d" % client.requests)
    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="\n") as fh:
            for line in failures:
                fh.write(line + "\n")
    tried = [s for s in SOURCES if not s.get("blocked")
             and (not wanted or s["id"] in wanted)]
    if tried and len(failures) == len(tried):
        print("::error::every source failed; nothing was updated")
        return 1
    for line in failures:
        print("::warning::%s" % line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

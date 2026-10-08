#!/usr/bin/env python3
"""Unsurfaced unclassified roads (UCRs), read from the councils' own records.

    python tools/council_ucrs.py fetch               # every council layer
    python tools/council_ucrs.py fetch --only DN     # just these
    python tools/council_ucrs.py show DN             # what the build makes of it

WHAT A UCR IS, AND WHY IT IS ON THE MAP. Many green lanes are not byways at
all: they are ordinary public roads - unclassified county roads - that were
never given a hard surface. The council maintains them as highway and lists
them in its List of Streets (Highways Act 1980 s36(6)), and the Natural
Environment and Rural Communities Act 2006 s67(2)(b) kept the motor vehicle
rights over ways recorded in that list when it extinguished them elsewhere.
They are on no definitive map, so rowmaps (and every byway layer this
pipeline reads) does not have them. The owner decided on 8 October 2026 that
every green lane is to be shown, so the councils that publish their
unsurfaced roads are read here.

THEIR OWN OUTPUT, NEVER THE BYWAY MERGE. council_ways.py reads councils' byway
layers to REPLACE rowmaps' byways, keeping ids through `merge`. Rowmaps has
no UCRs, so a UCR layer has nothing to merge with; put through that merge
every UCR would read as "new" and `agrees` would refuse the whole council. So
UCRs have their own files (council-ucrs/<CODE>.json), their own status
(council-ucrs/status.json), their own keep-last-good floor, and their own
class (`ucr`) and table (`ucr_ways`) all the way to the phone.

PER-COUNCIL READING RULES, IN ONE PLACE. Every council words its records its
own way. `UCR_LAYERS` holds, per council, the layer and the field names that
say which record is an unsurfaced road, which parish and number it is, and
what the council calls it. Adding a council is a table entry and a test (the
`Layers` tests in test_council_ucrs.py check every entry has what the reader
needs and asks no blocked host).

KEEP LAST GOOD. A layer that cannot be read, reads nothing, reads fewer than
`min_records`, or under FLOOR_SHARE of the routes it held last time, leaves
the last good file exactly as it was and says why in status.json. There is
no second source for a UCR to fall back to, so an old file is still used by
the build (with a warning past MAX_AGE_DAYS): a road that was public last
month is public now far more often than not, and dropping every UCR from the
map would say less that is true.

READ ONLY AND POLITE. Every request goes through tools/polite_http.py:
honest User-Agent, robots.txt obeyed, paced, ArcGIS /query only.
"""
import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import polite_http  # noqa: E402
from polite_http import FetchFailed, Refused, arcgis_query  # noqa: E402
from council_sources import esri_lines, read_json, write_json  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "council-ucrs")

#: Keep-last-good: a read with fewer routes than this share of the last good
#: one is refused. The same two thirds as council_ways.FLOOR_SHARE.
FLOOR_SHARE = 2.0 / 3.0

#: Past this many days since the last good read the build still uses the
#: file (there is nothing else) but warns, and status.json says so.
MAX_AGE_DAYS = 30

#: Seconds between two requests to one host: the councils' servers are
#: asked gently (the owner's rule for every council read, 8 October 2026).
MIN_GAP_S = 4.5

#: What the owner decided about a layer published with no licence. Recorded
#: here because it travels with every file, pack and container that carries
#: the roads, and in README.md where the sources and licences are listed.
OWNER_DECISION = "2026-10-08"

#: The council layers this table holds and their notes all say the same
#: thing; one wording, so no council is credited differently. The owner's
#: decision of 8 October 2026 covers Devon and, by name, North Yorkshire,
#: Norfolk, Lincolnshire, Northumberland, East Riding of Yorkshire,
#: Oxfordshire, Surrey and Worcestershire. Suffolk and Herefordshire stay
#: out: their terms forbid copying.
def owner_decision_note(council):
    """What every road of `council`'s says about a layer with no licence."""
    return ("%s publishes this layer without stating a licence. "
            "Trail Blazer publishes it on the owner's decision of 8 October "
            "2026 that it is public highway information - highway records "
            "must be open to public inspection - credited to %s, and will "
            "take it down if the council objects." % (council, council))


DEVON_LICENCE_NOTE = owner_decision_note("Devon County Council")

#: Generic words a council's name field holds where the road has no name:
#: Devon's layer says "Unknown" on 28 routes, "Track" on 3 and "UNNAMED
#: Track" on one (8 Oct 2026). A name made only of these words is no name,
#: whatever the council (`no_name` in a layer's rules adds its own).
PLACEHOLDER_WORDS = frozenset((
    "unknown", "unnamed", "un-named", "unamed", "track", "lane", "road",
    "none", "n/a", "na", "tba", "tbc", "no", "name", "not", "known", "?",
    "-", "ucr", "uucr", "uuct"))

# --------------------------------------------------------------------- rules
#
# Each entry's `rules` names the fields; the reader below does the rest.
#
#   status     (field, [values]), or a list of them, ANY of which marks a
#              record as an unsurfaced road; compared without regard to case
#              or spaces (Oxfordshire pads its values with spaces)
#   require    optional [(field, [values])]: EVERY one must hold as well
#   exclude    optional [(field, [values])]: a record holding ANY is not one
#   oid        the object id field (default OBJECTID); the read is ordered
#              by it and a record seen twice is read once
#   parish     field holding the parish name, or None where the council
#              gives none
#   no_parish  the values of `parish` that mean "none" ("?" in Oxfordshire)
#   number     field holding the road's number
#   number_pattern
#              optional regex whose first group is the number within that
#              field: North Yorkshire's "U2686/9/60" is section 9/60 of U2686
#   letter     optional field holding a suffix letter ("301A")
#   name       optional field holding what the council calls the road
#   no_name    the values of `name` that mean "no name" (PLACEHOLDER_WORDS,
#              number-only and reference-like names are none everywhere)
#   per_parish True (the default) where a number is only unique within its
#              parish (Devon's "Abbotsham 301"): a route is a parish and
#              number. False where the number is the council's own county-
#              wide road number: a route is the number, named by the first
#              parish its sections give.
#
# A ROUTE IS ONE ROAD. Councils draw a road in sections; every section of one
# route becomes one lane, as a byway drawn in pieces does
# (build_packages.join_pieces), because the route is what the council and a
# rider both call it.
#
# NEVER ASK FOR A PERSON'S NAME. `fields` lists only what the rules read:
# Devon's `Comments` ("Unsurfaced - <officer>"), Northumberland's `Creator`,
# `Editor`, `AGENT_NAME` and `OWNER_NAME`, and any free-text description are
# never requested or stored (Layers.test_no_person_is_asked_for).

UCR_LAYERS = [
    # Devon's "PROW CAT 12" layer: its unsurfaced unclassified county roads,
    # maintenance category 12. The Highway Asset Management Plan's Annex 10
    # says category 12 roads "are simply vehicular highways that happen ... to
    # have remained or have become unsurfaced", and "often, they are 'green
    # lanes'". Read 8 October 2026: 1,143 sections, 958 routes, 591 km;
    # 'uUCR' on 1,142 (one spelt 'Uucr'), 'uUCT' on one. None lies on any of
    # Devon's 178 BOATs (a 20 m test). EXISTING_C (Cat 12, 11, 9...) is the
    # council's maintenance category today and is not a reading rule: every
    # row in the layer is a road the council lists as unsurfaced.
    {"code": "DN", "council": "Devon County Council", "authority": "Devon",
     "what": "Devon County Council's unsurfaced unclassified county roads "
             "(maintenance category 12)",
     "url": "https://map.devon.gov.uk/arcgis/rest/services/"
            "Environment_Intranet/Public_Access_Intranet/MapServer/5",
     "where": "Status IN ('uUCR','uUCT')",
     # Only what the rules read. `Comments` carries officers' names
     # ("Unsurfaced - <name>") and is never asked for.
     "fields": "OBJECTID,Parish,Status,Number,PathLetter,Section_Nu,"
               "Parish_Sta,Route_Name",
     "rules": {"status": ("Status", ("uUCR", "uUCT")),
               "parish": "Parish", "number": "Number",
               "letter": "PathLetter", "name": "Route_Name",
               "no_name": ("", "UNNAMED", "UN-NAMED", "NONE", "N/A", "?")},
     # A read of fewer than this is a broken layer, whatever last time said.
     "min_records": 500,
     "licence": None,
     "licence_note": DEVON_LICENCE_NOTE,
     "decided": OWNER_DECISION},

    # North Yorkshire Council's "U_Roads" layer ("A line layer showing NYC U
    # roads. Updated monthly."), reached through the ArcGIS utility proxy
    # the council's own public rights of way web map uses (item
    # eb4f42b26c404be49592e421249d235b). HIERARCHY '6' is the maintenance
    # hierarchy of the unsurfaced unclassified roads (UURs) the council's
    # traffic orders name: U586/1/90, U618/9, U8044/9/50, U8006/9/60 and
    # U7071/9/70 are all '6', and its sections read "FROM END OF SURFACE
    # ...". NAME_SECTION is road/section ("U2686/9/60"): a route is the
    # road. No parish field (DIS_NAME is a pre-2023 district code). Read 8
    # October 2026: 1,005 sections, 736 km. THE LAYER ADVERTISES ANONYMOUS
    # EDITING (Create, Update, Delete): only /query is ever sent.
    {"code": "NY", "council": "North Yorkshire Council",
     "authority": "North Yorkshire",
     "what": "North Yorkshire Council's unsurfaced unclassified roads "
             "(U roads of maintenance hierarchy 6)",
     "url": "https://utility.arcgis.com/usrsvcs/servers/"
            "2c059ce9bd2b43b99d8130ee23f3ddd5/rest/services/highways/"
            "Highways_Network/FeatureServer/3",
     "where": "HIERARCHY='6'",
     "fields": "OBJECTID,NAME_SECTION,HIERARCHY",
     "rules": {"status": ("HIERARCHY", ("6",)),
               "parish": None, "number": "NAME_SECTION",
               "number_pattern": r"^\s*([A-Za-z]*\d+[A-Za-z]?)\s*(?:/|$)",
               "per_parish": False},
     "min_records": 500,
     "licence": None,
     "licence_note": owner_decision_note("North Yorkshire Council"),
     "decided": OWNER_DECISION},

    # Norfolk County Council's layer "Norfolk County Council Maintained
    # Unsurfaced Roads" (layers_ext/crm, its external folder): every record
    # is CLASS 'U', ROAD_HIER '4D'. RNUM is the council's road number,
    # county-wide; PARNAME the parish. Read 8 October 2026: 711 sections,
    # 500 km. Norfolk's www site refuses data centres (Cloudflare); this map
    # server answered (IIS, no challenge) from an ordinary address.
    {"code": "NK", "council": "Norfolk County Council",
     "authority": "Norfolk",
     "what": "Norfolk County Council's maintained unsurfaced roads",
     "url": "https://maps.norfolk.gov.uk/arcgis/rest/services/layers_ext/"
            "crm/MapServer/16",
     "where": "CLASS='U' AND ROAD_HIER='4D'",
     "fields": "OBJECTID,SECTIONREF,RNUM,NAME,PARNAME,CLASS,ROAD_HIER",
     "rules": {"status": ("ROAD_HIER", ("4D",)),
               "require": [("CLASS", ("U",))],
               "parish": "PARNAME", "number": "RNUM", "name": "NAME",
               "per_parish": False},
     "min_records": 350,
     "licence": None,
     "licence_note": owner_decision_note("Norfolk County Council"),
     "decided": OWNER_DECISION},

    # Lincolnshire County Council's "Highways_Assets_Carriageway 2 view"
    # (used by its public web map "Lincolnshire Highway Assets (Public)"):
    # Road_Class 'Green Lane', every one LCC Hierarchy 8. Asset_Id is
    # road/section ("01G200/05", the G for green lane): a route is the road.
    # The council's rights of way page: "We apply a working presumption that
    # routes on the list of streets have public vehicular rights. Legislation
    # has extinguished the rights of motorists if these are also shown as a:
    # public footpath, public bridleway, restricted byway" - the NERC test
    # build_packages.ucr_lanes applies to every council. Read 8 October
    # 2026: 536 sections, 388 km.
    {"code": "LL", "council": "Lincolnshire County Council",
     "authority": "Lincolnshire",
     "what": "Lincolnshire County Council's green lanes (carriageway assets "
             "of road class 'Green Lane')",
     "url": "https://services-eu1.arcgis.com/WZPLjyOOFu4PQ3GJ/arcgis/rest/"
            "services/Highways_Assets_Carriageway_2_view/FeatureServer/3",
     "where": "Road_Class='Green Lane'",
     "fields": "fid,Asset_Id,Street,Road_Class,Parish",
     "rules": {"status": ("Road_Class", ("Green Lane",)), "oid": "fid",
               "parish": "Parish", "number": "Asset_Id",
               "number_pattern": r"^\s*([^/\s]+)",
               "name": "Street", "per_parish": False},
     "min_records": 270,
     "licence": None,
     "licence_note": owner_decision_note("Lincolnshire County Council"),
     "decided": OWNER_DECISION},

    # Northumberland County Council's "Adopted Highways Master View", its
    # List of Streets ("Highways Maintainable at Public Expense - List Of
    # Streets", the council's web map): maintenance category '8 - Unsurfaced
    # Roads'. Four of its 309 sections are DIVISION_N 'FP' (footpath) or
    # 'RH' (restricted highway), not roads a motor vehicle may use;
    # HIERARCHY_ '8' is NOT this category (7,877 local access roads).
    # ROAD_NAME is the road number ("U8050"). Read 8 October 2026: 305
    # sections, 284 km.
    {"code": "ND", "council": "Northumberland County Council",
     "authority": "Northumberland",
     "what": "Northumberland County Council's unsurfaced roads (adopted "
             "highway of maintenance category 8)",
     "url": "https://services2.arcgis.com/LrUbY6lLLgV3tEa5/arcgis/rest/"
            "services/adopted_highway_master_view/FeatureServer/4",
     "where": "MAINTANENC='8 - Unsurfaced Roads' AND "
              "DIVISION_N NOT IN ('FP','RH')",
     "fields": "OBJECTID,ROAD_NAME,MAINTANENC,DIVISION_N",
     "rules": {"status": ("MAINTANENC", ("8 - Unsurfaced Roads",)),
               "exclude": [("DIVISION_N", ("FP", "RH"))],
               "parish": None, "number": "ROAD_NAME", "per_parish": False},
     "min_records": 150,
     "licence": None,
     "licence_note": owner_decision_note("Northumberland County Council"),
     "decided": OWNER_DECISION},

    # East Riding of Yorkshire Council's "LSG_ESU_Dedications", its local
    # street gazetteer: network priority 'GREEN LANE' or maintenance
    # category '6 Unmetalled', dedicated to all vehicles and maintained by
    # the council ('HW: ERYC Highway Maintained' or 'HW: ERYC Part
    # Maintained' - never 'HW: Private' or 'CA: Public Rights Of Way'; the
    # three sections dedicated as BOATs are left to the definitive map).
    # SITE_CODE is the council's street code, area_name the parish. Last
    # edited 12 September 2022. Read 8 October 2026: 192 sections, 98.5 km.
    {"code": "EY", "council": "East Riding of Yorkshire Council",
     "authority": "East Riding of Yorkshire",
     "what": "East Riding of Yorkshire Council's green lanes and unmetalled "
             "roads (street gazetteer, dedicated to all vehicles)",
     "url": "https://services6.arcgis.com/Qptn479QktK11k72/arcgis/rest/"
            "services/LSG_ESU_Dedications/FeatureServer/9",
     "where": "(netwok_pri='GREEN LANE' OR category_n='6 Unmetalled') AND "
              "dedication='All Vehicles' AND customer_n LIKE 'HW: ERYC%'",
     "fields": "FID,SITE_CODE,site_name,area_name,customer_n,category_n,"
               "netwok_pri,dedication",
     "rules": {"status": [("netwok_pri", ("GREEN LANE",)),
                          ("category_n", ("6 Unmetalled",))],
               "require": [("dedication", ("All Vehicles",)),
                           ("customer_n", ("HW: ERYC Highway Maintained",
                                           "HW: ERYC Part Maintained"))],
               "oid": "FID", "parish": "area_name", "number": "SITE_CODE",
               "name": "site_name", "per_parish": False},
     "min_records": 100,
     "licence": None,
     "licence_note": owner_decision_note("East Riding of Yorkshire Council"),
     "decided": OWNER_DECISION},

    # Oxfordshire County Council's "Oxfordshire Maintained" streets (its List
    # of Streets: LIST_STREET 1, 'Maintainable at Public Expense' on every
    # one) with STREET_SURF 'Unmetalled'. NOT 'Mixed' (20 streets): two are
    # Oxford's cobbled Merton Street and Radcliffe Square, six the council
    # itself names as a public footpath or bridleway ("PRoW Hook Norton
    # Footpath 17"), and the rest are part-surfaced streets whose whole-
    # street line does not say which part is unmetalled. USRN is the street,
    # LOCALITY the place ("?" where none). Read 8 October 2026: 106 streets,
    # 93.5 km.
    {"code": "ON", "council": "Oxfordshire County Council",
     "authority": "Oxfordshire",
     "what": "Oxfordshire County Council's unmetalled streets maintainable "
             "at public expense",
     "url": "https://mymaps2.oxfordshire.gov.uk/server/rest/services/WMS/"
            "Highways_Centreline/MapServer/1",
     "where": "STREET_SURF LIKE 'Unmetalled%'",
     "fields": "OBJECTID,USRN,STREET,LOCALITY,STREET_SURF",
     "rules": {"status": ("STREET_SURF", ("Unmetalled",)),
               "parish": "LOCALITY", "no_parish": ("?",),
               "number": "USRN", "name": "STREET", "per_parish": False},
     "min_records": 50,
     "licence": None,
     "licence_note": owner_decision_note("Oxfordshire County Council"),
     "decided": OWNER_DECISION},

    # Surrey County Council's "Roads & Transport - Roads" (its publicly
    # maintained roads): surface 'UM' (unmetalled) and road_type
    # 'Unclassified', all class D. Not the 'UM' townpaths, footpaths and
    # licence roads. class_number is the road ("D262"), village the place
    # (on few). Read 8 October 2026: 167 sections, 85 km.
    {"code": "SU", "council": "Surrey County Council", "authority": "Surrey",
     "what": "Surrey County Council's unmetalled unclassified roads",
     "url": "https://sccmaps.surreycc.gov.uk/webmaps/rest/services/"
            "Surrey_Interactive_Map/RoadsTransport_Roads_Publicly_Maintained/"
            "MapServer/33",
     "where": "surface='UM' AND road_type='Unclassified'",
     "fields": "OBJECTID,class_number,roadname,road_type,surface,village",
     "rules": {"status": ("surface", ("UM",)),
               "require": [("road_type", ("Unclassified",))],
               "parish": "village", "number": "class_number",
               "name": "roadname", "per_parish": False},
     "min_records": 80,
     "licence": None,
     "licence_note": owner_decision_note("Surrey County Council"),
     "decided": OWNER_DECISION},

    # NOT READ: Worcestershire. Its "Highways and Footways" layer
    # (WccGISOnline_New/MapServer/11) has 193 sections of MAINT_CAT 'Keep
    # Safe Only', but no council publication ties that tier to unsurfaced
    # roads: the Asset Lifecycle Plan (June 2026) s1.5 lists "9 = Keep Safe
    # (KS)" as a Well-Managed-Highway hierarchy tier and says nothing of a
    # surface; the 2016 Transport Asset Management Plan, the Network
    # Management Plan and the LTP4 transport policies do not mention it; the
    # 2007 Rights of Way Improvement Plan says unsurfaced UCRs are the
    # Countryside Access Team's to maintain, and this layer is the Highways
    # Asset Management team's. Held out until the council says what the
    # tier is (8 October 2026). Suffolk and Herefordshire are not read:
    # their terms forbid copying (the owner, 8 October 2026).
]


def by_code():
    return dict((l["code"], l) for l in UCR_LAYERS)


def attribution(layer):
    said = "Source: %s, read from the council." % layer["what"]
    if layer.get("licence") == "OGL-3.0":
        return ("Contains public sector information licensed under the Open "
                "Government Licence v3.0. " + said)
    return said + " " + (layer.get("licence_note") or
                         "The council publishes it without stating a "
                         "licence.")


def public(layer):
    """What every file, pack and container says about the source."""
    out = {"code": layer["code"], "council": layer["council"],
           "authority": layer["authority"], "what": layer["what"],
           "url": layer["url"], "licence": layer.get("licence"),
           "attribution": attribution(layer)}
    if layer.get("licence_note"):
        out["licence_note"] = layer["licence_note"]
    if layer.get("decided"):
        out["decided"] = layer["decided"]
    return out


# ---------------------------------------------------------------- reading

def _norm(value):
    return re.sub(r"\s+", "", str(value if value is not None else "")).lower()


def _text(value):
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def _number(value):
    """301 -> '301'; 301.0 -> '301'; ' 12 ' -> '12'."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return _text(value)


def _title_word(w):
    """One word in title case, keeping an apostrophe's s small
    ("BITTAM'S" -> "Bittam's", where str.title gives "Bittam'S")."""
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize()
                  if m.start() == 0 or w[m.start() - 1] not in "'’"
                  else m.group(0).lower(), w.lower())


def _is_placeholder(text, no_name=()):
    """No name at all, said in words, digits or a reference.

    "Unknown", "Track", "UNNAMED Track" (only PLACEHOLDER_WORDS); "301"
    (digits and punctuation only); "204uUCR301", "U2686", "uUCR 306" (every
    word a placeholder or a token with a digit in it: a reference, which the
    lane's name carries already, not what anybody calls the road). "A30
    Link" is a name: "link" is a word."""
    if _norm(text) in set(_norm(n) for n in no_name):
        return True
    words = [w for w in re.split(r"\s+", text.lower()) if w]
    # Every word a placeholder or a reference ("uUCR 306", "Track 2").
    return all(w in PLACEHOLDER_WORDS or re.search(r"\d", w)
               for w in words) or not re.search(r"[A-Za-z]", text)


def road_name(raw, no_name=()):
    """What the council calls the road, as a rider should read it, or ''.

    Names in capitals ("ROCKY LANE") are put in title case a word at a time,
    keeping an apostrophe's s small. A name already in mixed case is the
    council's own and kept, except for the words in it that are in capitals
    and four letters or more ("Track to IVEDON HOUSE" -> "Track to Ivedon
    House"): three letters or fewer are kept as an abbreviation might be
    ("RAF", "MOD"). Placeholders, bare numbers and references are no name
    (_is_placeholder), so the lane falls back to its designation and
    reference.
    """
    text = _text(raw)
    if _is_placeholder(text, no_name):
        return ""
    if not re.search(r"[A-Z]", text):
        return text
    if text.upper() == text:
        return " ".join(_title_word(w) for w in text.split(" "))
    return re.sub(r"(?<![\w'’])[A-Z][A-Z'’]{3,}(?![\w])",
                  lambda m: _title_word(m.group(0)), text)


def _holds(a, test):
    field, values = test
    return _norm(a.get(field)) in set(_norm(v) for v in values)


def is_road(a, rules):
    """Whether a record is one of the council's unsurfaced roads, by the
    layer's own rule: ANY `status`, EVERY `require`, NO `exclude`."""
    status = rules["status"]
    if isinstance(status, tuple):
        status = [status]
    return (any(_holds(a, t) for t in status)
            and all(_holds(a, t) for t in rules.get("require") or ())
            and not any(_holds(a, t) for t in rules.get("exclude") or ()))


def _parish(a, rules):
    field = rules.get("parish")
    if not field:
        return ""
    text = _text(a.get(field))
    if not text or _norm(text) in set(_norm(v) for v in
                                      rules.get("no_parish") or ()):
        return ""
    # A parish in capitals ("SOUTH WALSHAM") reads as a road name does.
    return road_name(text) or text


def _road_number(a, rules):
    number = _number(a.get(rules["number"]))
    pattern = rules.get("number_pattern")
    if pattern:
        m = re.search(pattern, number)
        number = m.group(1) if m else ""
    letter = _text(a.get(rules["letter"])) if rules.get("letter") else ""
    return number + letter.upper()


def routes_of(features, rules):
    """esri JSON features -> [route], one per road.

    A route is {"parish", "number", "name", "objectids", "lines"}; its lines
    are every section's, in (lon, lat). A road is a parish and number where
    the council numbers within parishes (`per_parish`, Devon), and the
    number alone where its numbers are county-wide; then its parish is the
    first its sections give. Sections are taken in object id order, and a
    record whose id was already read (paging can repeat one) is read once.
    Sorted by parish then number.
    """
    oid = rules.get("oid") or "OBJECTID"
    per_parish = rules.get("per_parish", True)
    groups = {}
    seen = set()

    def order(f):
        v = (f.get("attributes") or {}).get(oid)
        return (v is None, v if isinstance(v, (int, float)) else 0)
    for f in sorted(features, key=order):
        a = f.get("attributes") or {}
        if not is_road(a, rules):
            continue
        ident = a.get(oid)
        if ident is not None:
            if ident in seen:
                continue
            seen.add(ident)
        lines = esri_lines(f.get("geometry"))
        if not lines:
            continue
        parish = _parish(a, rules)
        number = _road_number(a, rules)
        if not number or (per_parish and not parish):
            continue
        key = (parish, number) if per_parish else (number,)
        g = groups.setdefault(key, {"parish": parish, "number": number,
                                    "names": [], "objectids": [],
                                    "lines": []})
        if not g["parish"] and parish:
            g["parish"] = parish
        name = road_name(a.get(rules.get("name")), rules.get("no_name", ())) \
            if rules.get("name") else ""
        if name and name not in g["names"]:
            g["names"].append(name)
        g["objectids"].append(ident)
        g["lines"].extend([[round(p[0], 5), round(p[1], 5)] for p in l]
                          for l in lines)
    out = []
    for g in sorted(groups.values(),
                    key=lambda g: (g["parish"].lower(),
                                   _num_key(g["number"]))):
        # Sections named differently are one route still; the first name the
        # council gives (in object id order) is the one shown.
        out.append({"parish": g["parish"], "number": g["number"],
                    "name": g["names"][0] if g["names"] else "",
                    "objectids": sorted(o for o in g["objectids"]
                                        if o is not None),
                    "lines": sorted(g["lines"])})
    return out


def _num_key(number):
    m = re.match(r"(\d+)(.*)$", number)
    return (int(m.group(1)), m.group(2)) if m else (10 ** 9, number)


def read_layer(client, layer):
    oid = layer["rules"].get("oid") or "OBJECTID"
    feats = arcgis_query(client, layer["url"], where=layer["where"],
                         out_fields=layer.get("fields", "*"), order_by=oid)
    # Records, not rows: a record a page repeated is one record.
    ids = [(f.get("attributes") or {}).get(oid) for f in feats]
    records = len(set(i for i in ids if i is not None)) + \
        sum(1 for i in ids if i is None)
    return records, routes_of(feats, layer["rules"])


# ------------------------------------------------------------------ fetch

def fetch_one(layer, client, out_dir, today, read=None):
    """-> status entry. Writes <CODE>.json only on a good read."""
    path = os.path.join(out_dir, "%s.json" % layer["code"])
    previous = read_json(path) or {}
    entry = {"council": layer["council"]}
    try:
        records, routes = (read or read_layer)(client, layer)
    except (Refused, FetchFailed) as e:
        entry.update({"ok": False, "error": str(e)[:300]})
        return entry
    before = len(previous.get("routes") or [])
    floor = layer.get("min_records") or 1
    if not routes:
        entry.update({"ok": False, "error": "read %d records and no "
                      "unsurfaced road; kept the last good file" % records})
        return entry
    if records < floor:
        entry.update({"ok": False, "error": "%d records, under the %d this "
                      "layer has never been below - a broken read; kept the "
                      "last good file" % (records, floor)})
        return entry
    if before and len(routes) < before * FLOOR_SHARE:
        entry.update({"ok": False, "error": "%d roads against %d last time - "
                      "a bad read, not a quiet month; kept the last good "
                      "file" % (len(routes), before)})
        return entry
    same = previous.get("routes") == routes and \
        previous.get("source") == public(layer)
    data = {"source": public(layer), "records": records,
            # "Unchanged since": moves only when the roads do, so a daily
            # read of an unchanged layer rewrites nothing downstream.
            "since": previous.get("since") if same and
            previous.get("since") else today,
            "routes": routes}
    entry.update({"ok": True, "records": records, "routes": len(routes),
                  "changed": write_json(path, data), "last_ok": today,
                  "since": data["since"]})
    return entry


def fetch(codes=None, out_dir=None, today=None, client=None, report=None,
          read=None):
    out_dir = out_dir or OUT
    today = today or datetime.date.today().isoformat()
    client = client or polite_http.PoliteClient(min_gap=MIN_GAP_S)
    status_path = os.path.join(out_dir, "status.json")
    status = read_json(status_path, {}) or {}
    failed = []
    for layer in UCR_LAYERS:
        if codes and layer["code"] not in codes:
            continue
        old = status.get(layer["code"]) or {}
        entry = fetch_one(layer, client, out_dir, today, read=read)
        if entry["ok"]:
            entry["failing_since"] = None
        else:
            entry["last_ok"] = old.get("last_ok")
            entry["since"] = old.get("since")
            entry["failing_since"] = old.get("failing_since") or today
            failed.append("%s (%s, unsurfaced roads): %s"
                          % (layer["code"], layer["council"], entry["error"]))
        status[layer["code"]] = entry
        print("%s %-30s %s" % (layer["code"], layer["council"], json.dumps(
            dict((k, v) for k, v in entry.items() if k != "council"))))
    write_json(status_path, status)
    if report:
        with open(report, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(line + "\n" for line in failed))
    for line in failed:
        print("::warning::council UCR layer %s" % line)
    return status, failed


# ------------------------------------------------------------------ build

def too_old(code, out_dir, today, max_age_days=MAX_AGE_DAYS):
    """Why the file's last good read is old, or None (see the docstring:
    an old file is still used, there being no other record)."""
    status = read_json(os.path.join(out_dir, "status.json"), {}) or {}
    last = (status.get(code) or {}).get("last_ok")
    if not last:
        return "no good read of it is recorded in status.json"
    age = (datetime.date.fromisoformat(today)
           - datetime.date.fromisoformat(last)).days
    if age >= max_age_days:
        return ("its last good read was %s, %d days ago" % (last, age))
    return None


def held(out_dir=None, codes=None, today=None, log=print):
    """[(source, since, routes)] for every council file there is, in
    UCR_LAYERS order. A file whose source no longer matches its table entry
    is still used as it was read: the entry changing is not the roads
    changing."""
    out_dir = out_dir or OUT
    today = today or datetime.date.today().isoformat()
    out = []
    for layer in UCR_LAYERS:
        if codes is not None and layer["code"] not in codes:
            continue
        data = read_json(os.path.join(out_dir, "%s.json" % layer["code"]))
        if not data or not data.get("routes"):
            continue
        old = too_old(layer["code"], out_dir, today)
        if old:
            log("::warning::%s: unsurfaced roads used from an old file: %s"
                % (layer["code"], old))
        out.append((data.get("source") or public(layer),
                    data.get("since") or "", data["routes"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch")
    f.add_argument("--only", help="comma-separated authority codes")
    f.add_argument("--out", default=OUT)
    f.add_argument("--today")
    f.add_argument("--report", help="append the problems here, one a line")
    s = sub.add_parser("show")
    s.add_argument("code")
    s.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        codes = set(c.strip().upper() for c in (args.only or "").split(",")
                    if c.strip())
        status, _failed = fetch(codes or None, args.out, args.today,
                                report=args.report)
        tried = [c for c in status if not codes or c in codes]
        if tried and not any(status[c].get("ok") for c in tried):
            print("::error::no council UCR layer could be read")
            return 1
        return 0
    if args.cmd == "show":
        for source, since, routes in held(args.out, {args.code.upper()}):
            named = sum(1 for r in routes if r["name"])
            print("%s: %d routes (%d named), %d sections, unchanged since %s"
                  % (source["council"], len(routes), named,
                     sum(len(r["objectids"]) for r in routes), since))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

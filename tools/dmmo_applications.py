#!/usr/bin/env python3
"""Applications to change the definitive map that concern byways.

    python tools/dmmo_applications.py            # read every council layer
    python tools/dmmo_applications.py --offline  # re-match what is held

A definitive map modification order (DMMO) application asks a council to
add a right of way to the map, upgrade it, downgrade it or delete it. While
one is open, a route's status is in dispute: a lane may be on its way to
becoming a byway, or a byway may be on its way to losing its vehicular
rights. Several councils publish their registers of applications as map
layers. This reads the applications that concern byways open to all traffic,
matches each to the byways it runs along, and publishes:

  status/dmmo-applications.json     every byway application found, each
                                    with its council, reference, what it
                                    seeks, where it has got to, the byways
                                    it touches, and the layer it came from
  status/dmmo-applications.geojson  the ones still open, as lines

PERSONAL DATA NEVER ENTERS THIS REPOSITORY. Registers carry applicants'
names and home addresses, affected landowners' addresses and case officers'
names. Every request names its fields (`fields` below): the personal ones
are never asked for, so they never reach this machine, let alone a commit.
`PERSONAL` lists them so the test can prove no layer asks for one, and every
text field is passed through strip_personal for an e-mail address or a
named contact typed into a description. Scanned applications (Devon's
`WebScanned` PDFs) are never fetched or linked: they carry the applicant's
details.

Read only, through tools/polite_http.py. A layer that cannot be read keeps
what it published last time.
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from council_sources import (as_geometry, esri_lines, geojson_lines,  # noqa
                             read_json, strip_personal, write_json)
from polite_http import FetchFailed, Refused, arcgis_query  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "status")

#: Fields that hold a person's name or address, in any layer read here.
#: Never requested; dropped if a server sends one anyway.
PERSONAL = frozenset(f.lower() for f in (
    "Applicant", "ApplicantA", "ApplicantP", "applicant",
    "applicants_address", "property_address_or_addresses", "Creator",
    "Editor", "APPL_NAME", "APPL_ADDRESS_1", "APPL_ADDRESS_2",
    "APPL_ADDRESS_3", "APPL_ADDRESS_4", "APPL_POSTCODE", "OFFICER",
    "PROPERTY", "WebScanned", "ClaimLoc00"))

#: Words that say an application is about a byway open to all traffic, and
#: not merely a restricted byway (which no motor vehicle may use).
_BOAT = re.compile(r"(?i)byway\s+open|\bBOATs?\b|\bBOT\b")


def _clean(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = strip_personal(str(value))
    return text or None


def _date(value):
    """An ArcGIS epoch-ms or a text date -> ISO day, else the text."""
    if isinstance(value, (int, float)) and value > 0:
        return datetime.datetime.fromtimestamp(
            value / 1000.0, datetime.timezone.utc).date().isoformat()
    return _clean(value)


def _app(source, ref, parish, seeks, current, state, lines, extra=None):
    return {"source": source["id"], "council": source["council"],
            "ref": _clean(ref), "parish": _clean(parish),
            "seeks": _clean(seeks), "current": _clean(current),
            "state": state, "lines": lines, "details": dict(
                (k, v) for k, v in (extra or {}).items() if v not in
                (None, "", " "))}


# ---------------------------------------------------------------- readers
#
# Each takes (client, source) and returns (records read, [application]).

def _arcgis(client, source):
    fields = source["fields"]
    assert not PERSONAL & set(f.lower() for f in fields), source["id"]
    feats = arcgis_query(client, source["url"], where=source["where"],
                         out_fields=",".join(fields))
    out = []
    for f in feats:
        a = dict((k, v) for k, v in (f.get("attributes") or {}).items()
                 if k.lower() not in PERSONAL)
        lines = esri_lines(f.get("geometry"))
        app = source["shape"](source, a, lines)
        if app:
            out.append(app)
    return len(feats), out


def _devon(source, a, lines):
    if not (_BOAT.search(a.get("ProposedSt") or "")
            or _BOAT.search(a.get("InitialSta") or "")):
        return None
    progress = (a.get("Progress") or "").strip().lower()
    state = "open" if progress.startswith("undetermined") or \
        progress.startswith("direction") else \
        "determined" if progress.startswith("determined") else "unknown"
    return _app(source, a.get("Reference"), a.get("Parish"),
                a.get("ProposedSt"), a.get("InitialSta"), state, lines, {
                    "location": _clean(a.get("ClaimLocat")),
                    "received": _date(a.get("Applicatio")),
                    "determined": _date(a.get("Determinat")),
                    "decision": _clean(a.get("Committee_")),
                    "order_made": _date(a.get("OrderMadeD")),
                    "order_confirmed": _date(a.get("OrderCon00"))})


def _nland_current(source, a, lines):
    decided = _clean(a.get("decision")) or _clean(a.get("committee_decision"))
    return _app(source, a.get("ClaimNumber") or a.get("claim_reference"),
                a.get("Parish"), a.get("Intended_action") or
                "Byway open to all traffic", None,
                "determined" if decided else "open", lines, {
                    "path": _clean(a.get("path_name")),
                    "received": _date(a.get("application_date")),
                    "decision": decided,
                    "committee": _date(a.get("committee_lac_date"))})


def _nland_legacy(source, a, lines):
    decided = _clean(a.get("COMMITTEE_DEC")) or \
        _clean(a.get("CONFIRMATION_DETAILS"))
    return _app(source, a.get("KEYID"), a.get("PARISH"),
                a.get("APP_DESC") or "Byway open to all traffic", None,
                "determined" if decided else "unknown", lines, {
                    "received": _date(a.get("DATE_APP_RECD")),
                    "determined": _date(a.get("ACT_DET_DATE")),
                    "decision": decided,
                    "appeal": _clean(a.get("APPEAL_INFORMATION"))})


def _west_berks(source, a, lines):
    notes = a.get("Notes") or ""
    if not _BOAT.search(notes):
        return None
    status = (a.get("Status") or "").strip()
    return _app(source, a.get("s53B_Refer"), None, notes, None,
                "determined" if re.search(r"(?i)determin|confirm|refus|"
                                          r"withdr|reject|closed", status)
                else "open", lines, {"status": _clean(status)})


def _caerphilly(source, a, lines):
    return _app(source, a.get("routecode"), None,
                "Byway open to all traffic (claimed)",
                a.get("legaltd_en") or "Claimed", "open", lines)


def _bradford(source, a, lines):
    decided = any(_clean(a.get(k)) for k in ("DECISION", "CONFIRM"))
    return _app(source, a.get("REF_NO"), a.get("PARISH_C"),
                a.get("PROPOSAL"), a.get("PATH_NO"),
                "determined" if decided else "open", lines, {
                    "name": _clean(a.get("REF_NAME")),
                    "received": _date(a.get("APP_DATE")),
                    "direction": _clean(a.get("DIRECTION")),
                    "decision": _clean(a.get("DECISION")),
                    "appeal": _clean(a.get("APPEAL")),
                    "order_made": _clean(a.get("MADE")),
                    "order_confirmed": _clean(a.get("CONFIRM"))})


def _wfs_derbyshire(client, source):
    params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
              "typeNames": "DCC:ROW_Applications_Register",
              "outputFormat": "application/json", "srsName": "EPSG:27700",
              "propertyName": ",".join(source["fields"] + ["the_geom"]),
              "CQL_FILTER": "Effect ILIKE '%byway open%' OR Effect LIKE "
                            "'%BOAT%' OR PathStatus ILIKE '%byway open%' OR "
                            "PathStatus LIKE '%BOAT%'"}
    assert not PERSONAL & set(f.lower() for f in source["fields"])
    data = client.get_json(source["url"] + "?" + urllib.parse.urlencode(
        params))
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        raise FetchFailed("no features list from Derbyshire's register")
    # The register is drawn in segments: one application, many rows.
    grouped = {}
    for f in feats:
        a = dict((k, v) for k, v in (f.get("properties") or {}).items()
                 if k.lower() not in PERSONAL)
        if not (_BOAT.search(a.get("Effect") or "")
                or _BOAT.search(a.get("PathStatus") or "")):
            continue
        key = (a.get("LegalCode"), a.get("Parish"), a.get("PathNum"))
        entry = grouped.setdefault(key, {"a": a, "lines": []})
        entry["lines"].extend(geojson_lines(f.get("geometry")))
    out = []
    for (_code, _parish, _num), entry in sorted(
            grouped.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        a = entry["a"]
        stage = (a.get("Stage") or "").strip()
        out.append(_app(source, a.get("LegalCode"), a.get("Parish"),
                        a.get("Effect"), "%s %s" % (
                            a.get("PathStatus") or "",
                            a.get("PathNum") or ""),
                        derbyshire_state(stage), entry["lines"],
                        {"stage": _clean(stage)}))
    return len(feats), out


_DONE = re.compile(r"(?i)conclu|confirm|reject|refus|withdr|dismiss|"
                   r"not processed|not triggered|recommendation rejected")


def derbyshire_state(stage):
    """Derbyshire's `Stage` as open / determined / incomplete."""
    if _DONE.search(stage or ""):
        return "determined"
    if re.search(r"(?i)non-compliant", stage or ""):
        return "incomplete"
    return "open"


def group(apps):
    """One application per reference: registers draw one in segments."""
    out, by_ref = [], {}
    for app in apps:
        key = (app["source"], app["ref"]) if app["ref"] else None
        if key and key in by_ref:
            by_ref[key]["lines"].extend(app["lines"])
            continue
        if key:
            by_ref[key] = app
        out.append(app)
    return out


DORSET_DMMO = ("https://gi.dorsetcouncil.gov.uk/geoserver/countryside/wfs?"
               "service=WFS&version=2.0.0&request=GetFeature&"
               "typeNames=countryside:v_dmmo_public&"
               "outputFormat=application/json&"
               "propertyName=dmmo_ref_no,application_details,case_status,"
               "website_url,claimed_statuses,application_types,geom")

#: Dorset's claimed statuses: "Byway" there is a BOAT; never "Restricted".
_DORSET_BOAT = re.compile(r"(?i)(^|,\s*)byway\b")


def _wfs_dorset(client, source):
    data = client.get_json(source["url"])
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        raise FetchFailed("no features list from Dorset's register")
    out = []
    for f in feats:
        a = dict((k, v) for k, v in (f.get("properties") or {}).items()
                 if k.lower() not in PERSONAL)
        if not _DORSET_BOAT.search(a.get("claimed_statuses") or ""):
            continue
        status = (a.get("case_status") or "").strip()
        out.append(_app(source, a.get("dmmo_ref_no"), None,
                        "%s: %s" % (a.get("application_types") or
                                    "Application", a.get("claimed_statuses")),
                        None, "determined" if re.search(
                            r"(?i)closed|determined|confirmed|refused|"
                            r"withdrawn", status) else "open",
                        geojson_lines(f.get("geometry")),
                        {"details": _clean(a.get("application_details")),
                         "status": _clean(status),
                         "register": _clean(a.get("website_url"))}))
    return len(feats), out


SOURCES = [
    {"id": "devon-dmmo", "council": "Devon County Council",
     "authority": "Devon", "read": _arcgis, "shape": _devon,
     "url": "https://map.devon.gov.uk/arcgis/rest/services/Environment/"
            "Public_Access/MapServer/3",
     "where": "ProposedSt LIKE 'Byway Open%' OR InitialSta LIKE "
              "'Byway Open%'",
     "fields": ["OBJECTID", "Reference", "ClaimLocat", "Parish",
                "InitialSta", "ProposedSt", "Applicatio", "Determinat",
                "Progress", "Committee_", "OrderMadeD", "OrderCon00"]},
    {"id": "northumberland-claims", "council": "Northumberland County "
     "Council", "authority": "Northumberland", "read": _arcgis,
     "shape": _nland_current,
     "url": "https://services2.arcgis.com/LrUbY6lLLgV3tEa5/arcgis/rest/"
            "services/Join_Features_to_NEW_PRoW_Claims_Master_view/"
            "FeatureServer/0",
     "where": "TYPE='2'",
     "fields": ["ClaimNumber", "claim_reference", "KEYID", "Parish",
                "Intended_action", "path_name", "application_date",
                "decision", "committee_decision", "committee_lac_date"]},
    {"id": "northumberland-claims-2020", "council": "Northumberland County "
     "Council (2020 register)", "authority": "Northumberland",
     "read": _arcgis, "shape": _nland_legacy,
     "url": "https://services2.arcgis.com/LrUbY6lLLgV3tEa5/arcgis/rest/"
            "services/PROW_ClaimedRightsOfWay/FeatureServer/0",
     "where": "TYPE='2'",
     "fields": ["OBJECTID", "KEYID", "PARISH", "APP_DESC", "DATE_APP_RECD",
                "ACT_DET_DATE", "COMMITTEE_DEC", "CONFIRMATION_DETAILS",
                "APPEAL_INFORMATION"]},
    {"id": "caerphilly-claims", "council": "Caerphilly County Borough "
     "Council", "authority": "Caerphilly", "read": _arcgis,
     "shape": _caerphilly,
     "url": "https://services2.arcgis.com/joiLPYpH7xAxIkcx/arcgis/rest/"
            "services/prow_claimed_rights_of_way_view/FeatureServer/0",
     "where": "status='BOT'",
     "fields": ["FID", "routecode", "status", "legaltd_en"]},
    {"id": "bradford-dmmo", "council": "City of Bradford Metropolitan "
     "District Council", "authority": "Bradford", "read": _arcgis,
     "shape": _bradford,
     "url": "https://utility.arcgis.com/usrsvcs/servers/"
            "b9aaf7350f9746478448e39e0e2c844a/rest/services/AGOL/"
            "ROWIP_Consult_Pro/MapServer/2",
     "where": "PROPOSAL LIKE '%Byway%' OR PROPOSAL LIKE '%BOAT%'",
     "fields": ["OBJECTID", "REF_NO", "PROPOSAL", "PATH_NO", "REF_NAME",
                "PARISH_C", "APP_DATE", "DIRECTION", "DECISION", "APPEAL",
                "MADE", "CONFIRM"]},
    # Licence unstated (owner's decision: council data is public). The
    # FeatureServer advertises anonymous editing: only /query is called.
    {"id": "west-berkshire-s53b", "council": "West Berkshire Council",
     "authority": "West Berkshire", "read": _arcgis, "shape": _west_berks,
     "url": "https://gis.westberks.gov.uk/server/rest/services/Layers/"
            "PUBLIC_RIGHTS_OF_WAY_SECTION_53B_WCA81/FeatureServer/5",
     "where": "1=1",
     "fields": ["OBJECTID", "Notes", "s53B_Refer", "Status"]},
    {"id": "derbyshire-applications", "council": "Derbyshire County "
     "Council", "authority": "Derbyshire", "read": _wfs_derbyshire,
     "url": "https://wms.derbyshire.gov.uk/geoserver/DCC/wfs",
     "fields": ["LegalCode", "Stage", "Effect", "Parish", "PathStatus",
                "PathNum"]},
    # Dorset's GeoServer refuses GitHub's runners (403, 7 October 2026):
    # this is read from the home collector's snapshot (home-collected/),
    # never from the runner.
    {"id": "dorset-dmmo", "council": "Dorset Council",
     "authority": "Dorset", "read": _wfs_dorset, "url": DORSET_DMMO,
     "fields": ["dmmo_ref_no", "application_details", "case_status",
                "website_url", "claimed_statuses", "application_types"]},
]

#: Kept-last-good: a source under this share of what it held is refused.
FLOOR_SHARE = 2.0 / 3.0


def match(apps, byways, authority):
    for app in apps:
        hits = byways.match_geometry(app["lines"], {authority}) \
            if app["lines"] else []
        app["ways"] = [uid for uid, _s, _m in hits]
    return apps


def publish(sources_state, out_dir):
    apps = []
    for sid in sorted(sources_state):
        apps.extend(sources_state[sid].get("applications") or [])
    listed = []
    features = []
    for app in apps:
        geometry = as_geometry(app["lines"])
        entry = dict((k, v) for k, v in app.items() if k != "lines")
        listed.append(entry)
        if app["state"] == "open" and geometry:
            features.append({"type": "Feature", "geometry": geometry,
                             "properties": dict(entry, kind="dmmo-"
                                                "application")})
    write_json(os.path.join(out_dir, "dmmo-applications.geojson"),
               {"type": "FeatureCollection", "features": features,
                "attribution": attribution(sources_state)})
    return listed, features


def attribution(state):
    names = sorted(set(s["council"] for s in SOURCES
                       if (state.get(s["id"]) or {}).get("applications")))
    return ("Applications to modify the definitive map, from the registers "
            "published by %s. Personal details are not included." %
            (", ".join(names) or "the councils"))


def run(client, byways, out_dir=OUT, today=None, log=print):
    today = today or datetime.date.today().isoformat()
    path = os.path.join(out_dir, "dmmo-applications.json")
    held = read_json(path, {}) or {}
    state = held.get("sources") or {}
    failed = []
    import manual_inbox
    by_hand = manual_inbox.automated_off()
    for source in SOURCES:
        old = state.get(source["id"]) or {}
        if source.get("authority") in by_hand and not source.get("blocked"):
            source = dict(source, blocked="the council's documents come "
                          "from manual/ instead; not fetched")
        if source.get("blocked"):
            state[source["id"]] = dict(old, council=source["council"],
                                       ok=False, blocked=source["blocked"])
            continue
        try:
            records, apps = source["read"](client, source)
            served = client.take_served() if hasattr(
                client, "take_served") else {}
        except (Refused, FetchFailed) as e:
            failed.append("%s: %s" % (source["id"], e))
            state[source["id"]] = dict(old, council=source["council"],
                                       ok=False, error=str(e)[:300],
                                       failing_since=old.get(
                                           "failing_since") or today)
            continue
        apps = group(apps)
        before = len(old.get("applications") or [])
        if before >= 3 and len(apps) < before * FLOOR_SHARE:
            failed.append("%s: %d applications against %d - kept the last "
                          "read" % (source["id"], len(apps), before))
            state[source["id"]] = dict(old, ok=False, error="collapsed read")
            continue
        apps = match(apps, byways, source["authority"])
        for app in apps:
            app["lines"] = [[[round(x, 5), round(y, 5)] for x, y in l]
                            for l in app["lines"]]
        apps.sort(key=lambda a: (str(a["ref"]), json.dumps(a["lines"])))
        state[source["id"]] = {"council": source["council"], "ok": True,
                               "records": records, "last_ok": today,
                               "url": source["url"],
                               "applications": apps}
        if served:
            from home_collector import provenance_of
            state[source["id"]]["supplied"] = provenance_of(served)
        log("%-28s %4d records, %3d byway applications, %3d open, %3d on "
            "a published byway" % (source["id"], records, len(apps),
                                   sum(1 for a in apps if a["state"] ==
                                       "open"),
                                   sum(1 for a in apps if a["ways"])))
    listed, features = publish(state, out_dir)
    write_json(path, {"sources": state, "attribution": attribution(state),
                      "applications": len(listed), "open": len(features)})
    return state, failed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    from byway_match import load_byways
    byways = load_byways()
    if len(byways) < 1000:
        print("::error::only %d byways in the published containers"
              % len(byways))
        return 1
    import polite_http
    from home_collector import HomeClient
    state, failed = run(HomeClient(polite_http.PoliteClient(min_gap=2.0)),
                        byways, args.out)
    for line in failed:
        print("::warning::DMMO register %s" % line)
    readable = [s for s in SOURCES if not s.get("blocked")]
    if not any((state.get(s["id"]) or {}).get("ok") for s in readable):
        print("::error::no DMMO register could be read")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

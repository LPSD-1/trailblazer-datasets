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

#: What the owner decided about a layer published with no licence. Recorded
#: here because it travels with every file, pack and container that carries
#: the roads, and in README.md where the sources and licences are listed.
OWNER_DECISION = "2026-10-08"

DEVON_LICENCE_NOTE = (
    "Devon County Council publishes this layer without stating a licence. "
    "Trail Blazer publishes it on the owner's decision of 8 October 2026 "
    "that it is public highway information - highway records must be open "
    "to public inspection - credited to Devon County Council, and will take "
    "it down if the council objects.")

# --------------------------------------------------------------------- rules
#
# Each entry's `rules` names the fields; the reader below does the rest.
#
#   status     (field, [values]) - the records that are unsurfaced roads,
#              compared without regard to case or spaces
#   parish     field holding the parish name
#   number     field holding the road's number within the parish
#   letter     optional field holding a suffix letter ("301A")
#   name       optional field holding what the council calls the road
#   no_name    the values of `name` that mean "no name"
#
# A ROUTE IS ONE PARISH AND NUMBER (and letter). Councils draw a road in
# sections; every section of one route becomes one lane, as a byway drawn in
# pieces does (build_packages.join_pieces), because the route is what the
# council and a rider both call it.

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


def road_name(raw, no_name=()):
    """What the council calls the road, as a rider should read it, or ''.

    Devon's names are mostly in capitals ("ROCKY LANE"); those are put in
    title case a word at a time, keeping an apostrophe's s small
    ("BITTAM'S LANE" -> "Bittam's Lane", where str.title gives "Bittam'S").
    A name already in mixed case is the council's own and kept as it is.
    """
    text = _text(raw)
    if _norm(text) in set(_norm(n) for n in no_name):
        return ""
    if text.upper() != text or not re.search(r"[A-Z]", text):
        return text

    def word(w):
        return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize()
                      if m.start() == 0 or w[m.start() - 1] not in "'’"
                      else m.group(0).lower(), w)
    return " ".join(word(w.lower()) for w in text.split(" "))


def routes_of(features, rules):
    """esri JSON features -> [route], one per parish + number (+ letter).

    A route is {"parish", "number", "name", "objectids", "lines"}; its lines
    are every section's, in (lon, lat). Sorted by parish then number.
    """
    status_field, wanted = rules["status"]
    wanted = set(_norm(v) for v in wanted)
    groups = {}
    for f in features:
        a = f.get("attributes") or {}
        if _norm(a.get(status_field)) not in wanted:
            continue
        lines = esri_lines(f.get("geometry"))
        if not lines:
            continue
        parish = _text(a.get(rules["parish"]))
        number = _number(a.get(rules["number"]))
        letter = _text(a.get(rules["letter"])) if rules.get("letter") else ""
        number = number + letter.upper()
        if not parish or not number:
            continue
        key = (parish, number)
        g = groups.setdefault(key, {"parish": parish, "number": number,
                                    "names": [], "objectids": [],
                                    "lines": []})
        name = road_name(a.get(rules.get("name")), rules.get("no_name", ())) \
            if rules.get("name") else ""
        if name and name not in g["names"]:
            g["names"].append(name)
        g["objectids"].append(a.get("OBJECTID"))
        g["lines"].extend([[round(p[0], 5), round(p[1], 5)] for p in l]
                          for l in lines)
    out = []
    for key in sorted(groups, key=lambda k: (k[0].lower(), _num_key(k[1]))):
        g = groups[key]
        # Sections named differently are one route still; the first name the
        # council gives (in OBJECTID order) is the one shown.
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
    feats = arcgis_query(client, layer["url"], where=layer["where"],
                         out_fields=layer.get("fields", "*"))
    return len(feats), routes_of(feats, layer["rules"])


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
    client = client or polite_http.PoliteClient()
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

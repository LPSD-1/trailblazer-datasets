#!/usr/bin/env python3
"""Council documents a person saved by hand, or a council sent, as a source.

    python tools/manual_inbox.py          # list what the inbox holds

Some council documents cannot be read by this pipeline: pages behind a bot
challenge, and pages that refuse both GitHub's runners and the one fixed
collector server (HOME-COLLECTOR.md; the owner's policy of 7 October 2026
allows that server, and nothing else). Neither is ever got past. Instead
the owner saves them in a browser, or the council sends them
(an Environmental Information Regulations reply), and commits them to
manual/<CODE>/ - CODE being the authority's two-letter code in
manual/authorities.json. See manual/README.md.

Each file is then read exactly like an automated snapshot:

  * a document (PDF, HTML, text) goes to the order register's check
    (order_register.py check): flagged for review the first time it is
    seen and whenever it changes, never published until a person has
    transcribed and approved its orders in tro/register/orders.json;
  * a map layer (GeoJSON, a zipped shapefile, a GeoPackage, KML or KMZ,
    with a `layer` mapping in manifest.json) is a council source in
    council_sources.py: matched to our byways and published, credited, like
    a council's live layer.

Whatever the pipeline cannot take as it stands - a spreadsheet, a loose
shapefile part, an unreadable manifest.json, an unknown `how` or a date not
written YYYY-MM-DD - is a PROBLEM, listed by this tool, warned on every
council-orders run and in the order register's review issue; the file is
not read until it is put right, so nothing is credited on a guess.

Either way it is credited to the council, and the coverage table says how it
came: "supplied by the council" or "saved by hand", with the date.

NOTHING IS FETCHED for an authority with an inbox unless its manifest says
"automated": "on": the inbox stands in for the automated sources, so a
page the owner saved is not then requested from the council as well.
"""
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANUAL = os.path.join(ROOT, "manual")
PAGES_BASE = "https://lpsd-1.github.io/trailblazer-datasets/"

DOCUMENTS = (".pdf", ".html", ".htm", ".txt", ".csv", ".xml")
#: Read by council_files' readers: a .zip holds a shapefile (.shp, .shx,
#: .dbf, .prj) or a GeoPackage.
LAYERS = (".geojson", ".json", ".zip", ".gpkg", ".kml", ".kmz")
SKIP = ("manifest.json", "readme.md", "readme.txt", ".gitkeep")

#: Files the pipeline cannot read, and what to do instead.
CONVERT = ("convert a map layer to GeoJSON (longitude and latitude, or "
           "British National Grid), or save a list of orders as PDF or CSV")
REFUSED = dict(
    [(e, "a spreadsheet, which the pipeline does not read: " + CONVERT)
     for e in (".xlsx", ".xls", ".xlsm", ".ods")] +
    [(e, "one part of a shapefile: put the .shp, .shx, .dbf and .prj "
         "together in one .zip") for e in (".shp", ".shx", ".dbf", ".prj",
                                            ".cpg", ".sbn", ".sbx")])

#: `how` in manifest.json, trimmed and in any case. "eir" and "foi" are a
#: council's reply to an Environmental Information Regulations or Freedom of
#: Information request.
HOW = {"eir": "supplied by the council", "foi": "supplied by the council",
       "council": "supplied by the council",
       "hand": "saved by hand", "browser": "saved by hand"}

#: A layer's projection, where the manifest states it (`crs`).
CRS = ("bng", "wgs84")

_DATE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _real_date(text):
    """`text` if it is a real date written YYYY-MM-DD, else None."""
    if not isinstance(text, str) or not _ISO.match(text):
        return None
    try:
        datetime.date(int(text[:4]), int(text[5:7]), int(text[8:]))
    except ValueError:
        return None
    return text


def _manifest(folder):
    """(manifest, problem): {} when there is none; a manifest that cannot be
    read, or is not the shape README.md gives, is a problem, never {}."""
    path = os.path.join(folder, "manifest.json")
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return None, "manifest.json unreadable (%s)" % str(e)[:120]
    if not isinstance(data, dict):
        return None, "manifest.json unreadable (not an object)"
    if not isinstance(data.get("files", {}), dict):
        return None, "manifest.json unreadable (\"files\" is not an object)"
    return data, None


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def authority_table(root=MANUAL):
    """{CODE: {"authority": container name, "council": council name}}."""
    return _read(os.path.join(root, "authorities.json"), {}) or {}


def inboxes(root=MANUAL):
    """[{code, authority, council, automated, docs, problems}] for every
    manual/<CODE>/ holding at least one file."""
    table = authority_table(root)
    out = []
    if not os.path.isdir(root):
        return out
    for code in sorted(os.listdir(root)):
        folder = os.path.join(root, code)
        if not os.path.isdir(folder):
            continue
        names = sorted(n for n in os.listdir(folder)
                       if n.lower() not in SKIP and not n.startswith(".")
                       and os.path.isfile(os.path.join(folder, n)))
        if not names:
            continue
        info = table.get(code) or {}
        manifest, broken = _manifest(folder)
        manifest = manifest or {}
        box = {"code": code, "authority": info.get("authority"),
               "council": manifest.get("council") or info.get("council")
               or info.get("authority") or code,
               "automated": (manifest.get("automated") or "off") == "on",
               "docs": [], "problems": []}
        if not info:
            box["problems"].append("manual/%s: not a code in "
                                   "manual/authorities.json" % code)
        if broken:
            # Nothing in the folder is read: without its manifest, a reply
            # would be credited "saved by hand" and its layer go unread.
            box["problems"].append(
                "manual/%s/%s - no file in manual/%s is read until it is "
                "fixed (see manual/README.md)" % (code, broken, code))
            out.append(box)
            continue
        files = manifest.get("files") or {}
        for name in names:
            meta = files.get(name, {})
            wrong = _meta_problem(name, meta)
            if wrong:
                box["problems"].append("manual/%s/%s: %s" % (code, name,
                                                             wrong))
                continue
            doc = document(code, box, name, meta, root)
            if doc["kind"] is None:
                box["problems"].append("%s: not a kind of file the "
                                       "pipeline reads" % doc["id"])
                continue
            if not doc["date"]:
                box["problems"].append(
                    "%s: no date - put YYYY-MM-DD in the file name or a "
                    "\"date\" in manifest.json" % doc["id"])
            if doc["kind"] == "layer" and not doc["layer"]:
                box["problems"].append(
                    "%s: a map layer needs a \"layer\" mapping in "
                    "manifest.json (see manual/README.md)" % doc["id"])
                continue
            box["docs"].append(doc)
        out.append(box)
    return out


def _how(meta):
    value = meta.get("how", "hand")
    return HOW.get(value.strip().lower()) if isinstance(value, str) \
        else None


def _meta_problem(name, meta):
    """What is wrong with a file's manifest entry (or the file), or None."""
    ext = os.path.splitext(name)[1].lower()
    if ext in REFUSED:
        return REFUSED[ext]
    if not isinstance(meta, dict):
        return ("its entry in manifest.json is not an object "
                "(see manual/README.md)")
    if _how(meta) is None:
        return ("\"how\": %s is not one of %s (see manual/README.md)"
                % (json.dumps(meta.get("how")), ", ".join(sorted(HOW))))
    if "date" in meta and _real_date(meta["date"]) is None:
        return ("\"date\": %s is not a date written YYYY-MM-DD"
                % json.dumps(meta["date"]))
    if "layer" in meta and not isinstance(meta["layer"], dict):
        return "\"layer\" is not an object (see manual/README.md)"
    if "layer" in meta and ext in DOCUMENTS:
        return "a %s file cannot be a map layer: %s" % (ext, CONVERT)
    if "crs" in meta and (not isinstance(meta["crs"], str) or
                          meta["crs"].strip().lower() not in CRS):
        return ("\"crs\": %s is not one of %s"
                % (json.dumps(meta["crs"]), ", ".join(CRS)))
    return None


def document(code, box, name, meta, root=MANUAL):
    ext = os.path.splitext(name)[1].lower()
    kind = "document" if ext in DOCUMENTS else \
        "layer" if ext in LAYERS else None
    found = _DATE.search(name)
    date = _real_date(meta.get("date")) or \
        (_real_date(found.group(0)) if found else None)
    rel = "manual/%s/%s" % (code, name)
    return {"id": rel, "path": os.path.join(root, code, name),
            "name": name, "code": code, "kind": kind,
            "authority": box["authority"], "council": box["council"],
            "how": _how(meta),
            "date": date, "title": meta.get("title") or name,
            "url": meta.get("url"), "public_url": PAGES_BASE + rel,
            "layer": meta.get("layer"),
            "crs": meta["crs"].strip().lower() if meta.get("crs") else None}


def provenance(doc):
    """'saved by hand 2026-10-08' / 'supplied by the council 2026-09-30'."""
    return "%s %s" % (doc["how"], doc["date"] or "(date not given)")


def automated_off(root=MANUAL):
    """Container authority names whose automated sources are not fetched."""
    return set(b["authority"] for b in inboxes(root)
               if b["authority"] and not b["automated"])


def documents_by_id(root=MANUAL):
    return dict((d["id"], d) for b in inboxes(root) for d in b["docs"])


def main(argv=None):
    boxes = inboxes()
    if not boxes:
        print("manual/ holds no documents")
    for b in boxes:
        print("%s %s (%s): %d file(s), automated fetching %s"
              % (b["code"], b["authority"], b["council"], len(b["docs"]),
                 "ON" if b["automated"] else "off"))
        for d in b["docs"]:
            print("   %-9s %s - %s" % (d["kind"], d["id"], provenance(d)))
        for p in b["problems"]:
            print("   PROBLEM %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())

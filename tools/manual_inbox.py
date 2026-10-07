#!/usr/bin/env python3
"""Council documents a person saved by hand, or a council sent, as a source.

    python tools/manual_inbox.py          # list what the inbox holds

Some council documents cannot be read by this pipeline: pages behind a bot
challenge, and pages that refuse GitHub's runners. Neither is ever got
past. Instead the owner saves them in a browser, or the council sends them
(an Environmental Information Regulations reply), and commits them to
manual/<CODE>/ - CODE being the authority's two-letter code in
manual/authorities.json. See manual/README.md.

Each file is then read exactly like an automated snapshot:

  * a document (PDF, HTML, text) goes to the order register's check
    (order_register.py check): flagged for review the first time it is
    seen and whenever it changes, never published until a person has
    transcribed and approved its orders in tro/register/orders.json;
  * a map layer (GeoJSON, with a `layer` mapping in manifest.json) is a
    council source in council_sources.py: matched to our byways and
    published, credited, like a council's live layer.

Either way it is credited to the council, and the coverage table says how it
came: "supplied by the council" or "saved by hand", with the date.

NOTHING IS FETCHED for an authority with an inbox unless its manifest says
"automated": "on": the inbox stands in for the automated sources, so a
page the owner saved is not then requested from the council as well.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANUAL = os.path.join(ROOT, "manual")
PAGES_BASE = "https://lpsd-1.github.io/trailblazer-datasets/"

DOCUMENTS = (".pdf", ".html", ".htm", ".txt", ".csv", ".xml")
LAYERS = (".geojson", ".json")
SKIP = ("manifest.json", "readme.md", "readme.txt", ".gitkeep")

HOW = {"eir": "supplied by the council", "council": "supplied by the council",
       "hand": "saved by hand", "browser": "saved by hand"}

_DATE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")


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
        manifest = _read(os.path.join(folder, "manifest.json"), {}) or {}
        box = {"code": code, "authority": info.get("authority"),
               "council": manifest.get("council") or info.get("council")
               or info.get("authority") or code,
               "automated": (manifest.get("automated") or "off") == "on",
               "docs": [], "problems": []}
        if not info:
            box["problems"].append("manual/%s: not a code in "
                                   "manual/authorities.json" % code)
        files = manifest.get("files") or {}
        for name in names:
            doc = document(code, box, name, files.get(name) or {}, root)
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


def document(code, box, name, meta, root=MANUAL):
    ext = os.path.splitext(name)[1].lower()
    kind = "document" if ext in DOCUMENTS else \
        "layer" if ext in LAYERS else None
    found = _DATE.search(name)
    date = meta.get("date") or (found.group(0) if found else None)
    rel = "manual/%s/%s" % (code, name)
    return {"id": rel, "path": os.path.join(root, code, name),
            "name": name, "code": code, "kind": kind,
            "authority": box["authority"], "council": box["council"],
            "how": HOW.get((meta.get("how") or "hand").lower(),
                           "saved by hand"),
            "date": date, "title": meta.get("title") or name,
            "url": meta.get("url"), "public_url": PAGES_BASE + rel,
            "layer": meta.get("layer")}


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

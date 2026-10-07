#!/usr/bin/env python3
"""Planning Inspectorate rights-of-way decisions that change a byway's status.

    python tools/pins_decisions.py              # fetch, match, write status/
    python tools/pins_decisions.py --offline    # re-match what is held

A definitive map modification order (DMMO) adds, upgrades, downgrades or
deletes a public right of way. When anyone objects, the Planning Inspectorate
decides it, and the decision is the earliest official signal that a byway's
status has changed or is in dispute - rowmaps and the councils' own layers
catch up months later (data-sources-report 3.1). The Inspectorate publishes
its decisions on GOV.UK, read here through the GOV.UK content API (Open
Government Licence v3.0; robots.txt allows the API):

  * rights of way ORDER decisions, a page per period, one paragraph per
    order with its decision letter attached;
  * Schedule 14 decisions - appeals against a council refusing to make a
    DMMO.

Only decisions about byways open to all traffic are kept: those whose order
or appeal names one, and Modification Orders whose decision letter turns
on one. The decision letter (.docx) is read only for those, once each - its
"Summary of Decision" line says confirmed, not confirmed or confirmed with
modifications, and its text says what the order did to the BOAT. Inspectors'
and objectors' names are never stored; nothing but the reference, title,
council, date, outcome, effect and the GOV.UK links is.

Writes `status/pins-decisions.json` (every BOAT decision found, matched or
not) and `status/status-changes.geojson` (one feature per decision that
falls on a byway riders have, drawn on that byway), which the app can read
as a "status changed" flag beside the lane.
"""
import argparse
import datetime
import html
import io
import json
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from council_sources import read_json, write_json  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "status")
API = "https://www.gov.uk/api/content/guidance/"

PAGES = [
    ("orders", "rights-of-way-order-information-decisions-and-maps-"
               "published-in-2026"),
    ("orders", "rights-of-way-order-information-decisions-and-maps-"
               "published-in-2023"),
    ("orders", "2020-rights-of-way-order-information-decisions-and-maps"),
    ("schedule14", "schedule-14-decisions-in-2023"),
    ("schedule14", "schedule-14-decisions-2019"),
]

_BOAT = re.compile(r"byways?\s+open\s+to\s+all\s+traffic|\bB\.?O\.?A\.?T\b",
                   re.I)
_TOKEN = re.compile(
    r'<h2[^>]*>(?P<h2>.*?)</h2>|<p>(?P<p>.*?)</p>|'
    r'<a class="govuk-link" href="(?P<href>[^"]+)">(?P<a>.*?)</a>', re.S)


def plain(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_page(kind, slug, body):
    """Every decision on one page: council, title, ref, date, letter URL."""
    out, council, title = [], None, None
    for m in _TOKEN.finditer(body or ""):
        if m.group("h2") is not None:
            council, title = plain(m.group("h2")), None
        elif m.group("p") is not None:
            text = plain(m.group("p"))
            # The page's own preamble and its "15 September 2026 - ..."
            # index lines are paragraphs too; a title follows a council.
            if council and text and not re.match(r"^\d{1,2} \w+ \d{4}", text):
                title = text
        elif m.group("href") and council and title:
            label = plain(m.group("a"))
            ref = re.search(r"ROW/[A-Z0-9/]+", label)
            when = re.search(r"Decision date:\s*(\d{1,2} \w+ \d{4})", label)
            if not ref:
                continue
            out.append({"kind": kind, "page": slug, "council": council,
                        "title": title, "ref": ref.group(0),
                        "decided": _date(when.group(1)) if when else None,
                        "letter": m.group("href")})
    return out


def _date(text):
    try:
        return datetime.datetime.strptime(text, "%d %B %Y").date().isoformat()
    except ValueError:
        return None


def letter_text(blob):
    """The text of a .docx decision letter (PDFs are not read)."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def outcome_of(text):
    """confirmed | confirmed-with-modifications | proposed-modifications |
    not-confirmed | allowed | dismissed | unknown, from the summary line."""
    m = re.search(r"Summary of Decision:?\s*(.{0,300})", text, re.I)
    s = (m.group(1) if m else text[:3000]).lower()
    if "not confirmed" in s:
        return "not-confirmed"
    if re.search(r"propose to confirm|proposed? to be confirmed", s):
        return "proposed-modifications"
    if re.search(r"confirmed subject to|confirmed with modification", s):
        return "confirmed-with-modifications"
    if "confirmed" in s:
        return "confirmed"
    if re.search(r"appeal is allowed|allowed", s):
        return "allowed"
    if re.search(r"appeal is dismissed|dismissed", s):
        return "dismissed"
    return "unknown"


def _purpose(text):
    """The letter's own statement of what the order does: the sentences
    with "proposes to modify" or "effect of the Order", not the reasoning
    (which mentions every alternative it rejected)."""
    keep = [s for s in re.split(r"(?<=[.])\s+", text[:8000])
            if re.search(r"proposes to modify|effect of the order|"
                         r"order is made|should be modified", s, re.I)]
    return " ".join(keep[:3])


def effect_of(title, text):
    """What the order or appeal does to the BOAT, in a word."""
    t = (title + " " + _purpose(text)).lower()
    if re.search(r"(record|recorded|shown) as a restricted byway|"
                 r"downgrad|reclassif|instead of a byway open to all",
                 t):
        return "boat-to-restricted-byway"
    if re.search(r"(adding|add|upgrad\w*)\s+(a\s+)?(?:\w+\s+){0,4}"
                 r"byway open to all traffic", t):
        return "add-or-upgrade-to-boat"
    if re.search(r"delet\w*|extinguish", t):
        return "delete-or-extinguish"
    if "diversion" in t:
        return "diversion"
    return "status"


_TITLE_PATH = re.compile(
    r"\(([A-Z][A-Za-z.'& -]+?)\s*:\s*(?:part of\s+)?(?:the\s+)?"
    r"(?:Byway Open to All Traffic|BOAT)\s*(?:No\.?\s*)?(\d+[A-Za-z]?)",
    re.I)


def title_refs(title):
    """[(parish, number)] a title names: "(Odell: Byway Open to All Traffic
    No. 42)", "(Riseley: BOAT No.71)"."""
    return [(m.group(1).strip(), m.group(2)) for m in
            _TITLE_PATH.finditer(title or "")]


def grid_refs_in(text):
    """OS grid references written in a decision letter."""
    return re.findall(r"\b(?:SU|SP|ST|SX|SY|SZ|SO|SJ|SK|SE|SD|SH|SN|SM|SR|"
                      r"SS|TQ|TL|TF|TG|TM|TR|TA|NZ|NY|NU|NT)\s?\d{3,5}\s?"
                      r"\d{3,5}\b", text or "")


def council_lanes(council, table):
    """The container authority names for a council heading."""
    def norm(text):
        text = (text or "").lower().replace("&", "and")
        text = re.sub(r"\b(the|council|county|borough|district|city|of|"
                      r"metropolitan|royal|unitary|former)\b", " ", text)
        return re.sub(r"[^a-z]+", "", text)
    want = norm(council)
    for entry in table:
        if norm(entry["name"]) == want or any(norm(l) == want
                                              for l in entry["lanes"]):
            return entry["lanes"]
    return []


def match(decision, byways, table):
    """(way uids, how) for a decision on a byway we hold.

    By the title's parish and number first. Failing that, by the letter's
    grid references - but only a byway passing within 100 m of TWO of them
    (an order route's ends, not a byway that merely meets it at one), and
    never one whose number contradicts the number in the title: Odell's
    claimed BOAT 42, never added, would otherwise have been pinned on the
    byways 2 and 4 it joins.
    """
    from byway_match import norm_number
    from order_register import grid_ref, ways_near
    from osgb import grid_to_wgs84
    lanes = council_lanes(decision["council"], table)
    ways = set()
    for parish, number in title_refs(decision["title"]):
        for lane in lanes:
            ways.update(byways.match_ref(lane, parish, number))
    if ways:
        return sorted(ways), "reference"
    points = [grid_to_wgs84(*g) for g in
              (grid_ref(r) for r in decision.get("grid_refs") or []) if g]
    if len(points) < 2 or not lanes:
        return [], None
    numbers = set(norm_number(n) for n in re.findall(
        r"Byways? Open to All Traffic,?\s*Nos?\.?\s*(\d+[A-Za-z]?)",
        decision["title"] or "", re.I))
    hits = {}
    for point in points:
        for uid in ways_near(byways, set(lanes), [point], 100.0):
            hits[uid] = hits.get(uid, 0) + 1
    near = [uid for uid, n in hits.items() if n >= 2 and (
        not numbers or byways.ways[uid].number.split("/")[0] in numbers)]
    if near:
        return sorted(near), "grid"
    return [], None


def fetch(client, held):
    """Every BOAT decision on the pages, reading letters not yet held."""
    by_ref = dict((d["ref"] + "|" + (d.get("decided") or ""), d)
                  for d in held)
    found = []
    for kind, slug in PAGES:
        page = client.get_json(API + slug)
        body = (page.get("details") or {}).get("body") or ""
        decisions = parse_page(kind, slug, body)
        if not decisions:
            raise ValueError("no decisions parsed from %s - the page's "
                             "layout has changed" % slug)
        for d in decisions:
            key = d["ref"] + "|" + (d.get("decided") or "")
            old = by_ref.get(key)
            if old and old.get("letter_read"):
                d.update(dict((k, old[k]) for k in
                              ("outcome", "effect", "grid_refs",
                               "letter_read", "boat") if k in old))
                if d.get("boat"):
                    found.append(d)
                continue
            names_boat = bool(_BOAT.search(d["title"]))
            maybe = names_boat or (kind == "orders" and re.search(
                r"modification order", d["title"], re.I) and re.search(
                    r"restricted byway", d["title"], re.I))
            if not maybe:
                continue
            text = ""
            if d["letter"].lower().endswith(".docx"):
                text = letter_text(client.get(d["letter"]))
            boat = names_boat or bool(_BOAT.search(text[:8000]))
            d["boat"] = boat
            d["letter_read"] = bool(text)
            if not boat:
                continue
            d["outcome"] = outcome_of(text) if text else "unknown"
            d["effect"] = effect_of(d["title"], text)
            d["grid_refs"] = sorted(set(grid_refs_in(text)))[:12]
            found.append(d)
    found.sort(key=lambda d: (d.get("decided") or "", d["ref"]))
    return found


def features(decisions, byways):
    from council_sources import as_geometry
    out = []
    for d in decisions:
        if not d.get("ways"):
            continue
        props = dict((k, d[k]) for k in
                     ("ref", "council", "title", "decided", "outcome",
                      "effect", "kind", "ways", "match") if d.get(k))
        props["letter"] = d["letter"]
        props["page"] = "https://www.gov.uk/guidance/" + d["page"]
        props["source"] = "pins-decisions"
        out.append({"type": "Feature",
                    "geometry": as_geometry(byways.geometry(d["ways"])),
                    "properties": props})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--offline", action="store_true",
                    help="re-match the decisions already held; no network")
    args = ap.parse_args(argv)

    import build_tro
    from byway_match import load_byways
    byways = load_byways()
    if len(byways) < 1000:
        print("::error::only %d byways in the published containers"
              % len(byways))
        return 1
    table, _unmapped = build_tro.load_authority_table()
    held_path = os.path.join(args.out, "pins-decisions.json")
    held = (read_json(held_path, {}) or {}).get("decisions", [])
    if args.offline:
        decisions = held
    else:
        import polite_http
        client = polite_http.PoliteClient(min_gap=2.0)
        try:
            decisions = fetch(client, held)
        except (polite_http.Refused, polite_http.FetchFailed,
                ValueError) as e:
            # Keep what is published: the decisions already held stand.
            print("::error::GOV.UK decisions could not be read (%s); the "
                  "published status layer is unchanged" % e)
            return 1
        if held and len(decisions) < len(held) * 2 // 3:
            print("::error::%d BOAT decisions against %d held - a bad read; "
                  "the published status layer is unchanged"
                  % (len(decisions), len(held)))
            return 1
        print("requests made: %d" % client.requests)
    for d in decisions:
        d["ways"], d["match"] = match(d, byways, table)
        if not d["ways"]:
            d.pop("ways")
            d.pop("match")
    write_json(held_path, {
        "source": "Planning Inspectorate decisions published on GOV.UK "
                  "(Open Government Licence v3.0)",
        "pages": ["https://www.gov.uk/guidance/" + s for _k, s in PAGES],
        "decisions": decisions})
    feats = features(decisions, byways)
    write_json(os.path.join(args.out, "status-changes.geojson"), {
        "type": "FeatureCollection",
        "attribution": "Contains public sector information licensed under "
                       "the Open Government Licence v3.0. Source: Planning "
                       "Inspectorate decisions published on GOV.UK.",
        "note": "A decision on an order that adds, upgrades, downgrades or "
                "deletes a byway. The definitive map changes when the "
                "council records the order; until then the lane is drawn "
                "as the map stood.",
        "features": feats})
    print("decisions about byways: %d; on a byway riders have: %d"
          % (len(decisions), len(feats)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

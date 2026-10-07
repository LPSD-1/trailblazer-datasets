#!/usr/bin/env python3
"""The register of long-standing permanent and seasonal byway orders.

    python tools/order_register.py build     # register -> tro/council/order-register.json
    python tools/order_register.py check     # re-read the councils' pages, flag changes
    python tools/order_register.py accept PAGE_ID [...]   # after a person reviewed them

WHY A REGISTER, AND WHY A PERSON IN THE LOOP
-------------------------------------------
Most byway traffic orders were made between 1969 and 2010, and D-TRO only
requires orders made after it commences; older ones "can be sent
voluntarily" (data-sources-report 2.1). So the "no motor vehicles" and
"closed October to April" rules riders most need may never reach D-TRO.
About twenty councils list them on their own web pages and in PDFs, in prose
and tables no two councils lay out alike.

A parser that guessed at that prose on a schedule would publish its guesses
as law. So the orders are transcribed once into `tro/register/orders.json`,
each with its council, path, restriction, season, order name and date and
the URL it was read from, and REVIEWED: only entries with `status:
"approved"` are published. Entries a reviewer could not settle stay in the
file as `needs-review` (with why), and orders known only by their title as
`listed-only` - Cambridgeshire's 38 and Hertfordshire's 15 were, because
robots.txt kept us out of their PDFs. Since the owner's decision of
7 October 2026 those PDFs are read (tools/robots_override.json): `check`
follows them from their council's page (`follow` in pages.json) and flags
each for a person to transcribe. A council that refuses GitHub's runners
is read from the one fixed collector server instead (HOME-COLLECTOR.md) and
served here from home-collected/ - for an order PDF, by its digest alone.
Documents no request may reach (a bot challenge, a refusal of that server
too) are saved by hand or sent by the council into manual/<CODE>/ and are
checked from disk the same way.

`check` is the schedule (quarterly, order-register.yml). It re-reads each
council page listed in `tro/register/pages.json`, reduces it to its text,
and compares that with the snapshot in `tro/register/snapshots/`. A page
that changed is written to `tro/register/changes.json` with a diff and
raised as an issue; NOTHING in orders.json moves until a person has read
the change, updated the register, and run `accept` for that page.

`build` (no network) matches every approved order to our byways and writes
it as a council source the order build already reads: by path reference
first (parish and number, or the council's route code), then by the OS grid
references the council gave. Where both exist and disagree by more than
`GRID_AGREE_M`, the entry is not published and is listed for review.
"""
import argparse
import datetime
import difflib
import hashlib
import html
import html.parser
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from council_sources import read_json, write_json  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REGISTER = os.path.join(ROOT, "tro", "register")
COUNCIL_OUT = os.path.join(ROOT, "tro", "council", "order-register.json")

# A six-figure grid reference is good to 100 m, a council's path line to its
# own survey; a way this far from every reference the council gave is not
# the byway the order is about.
GRID_NEAR_M = 150.0
GRID_AGREE_M = 300.0

SOURCE = {
    "id": "order-register",
    "kind": "register",
    "licence": "Published by each council; transcribed and reviewed",
    "endpoint": "https://github.com/LPSD-1/trailblazer-datasets/blob/main/"
                "tro/register/orders.json",
}


# ---------------------------------------------------------------- grid refs

_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"


def grid_ref(text):
    """An OS grid reference to (easting, northing) metres, or None.

    "TQ 566 091", "SD842 921", "TL 002218" and full "511436 149706". A
    reference with no square letters and fewer than six digits a side
    (Surrey's "0692 4666") cannot be placed and is None.
    """
    t = (text or "").upper().strip()
    m = re.match(r"^(\d{6})\s*[ ,:]\s*(\d{6,7})$", t)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.match(r"^([A-HJ-Z]{2})\s*(\d+)\s*(\d*)$", t)
    if not m:
        return None
    letters, a, b = m.group(1), m.group(2), m.group(3)
    digits = a + b
    if not b:
        if len(digits) % 2:
            return None
        a, b = digits[:len(digits) // 2], digits[len(digits) // 2:]
    if len(a) != len(b) or not 2 <= len(a) <= 5:
        return None
    l1, l2 = _LETTERS.index(letters[0]), _LETTERS.index(letters[1])
    e100 = ((l1 - 2) % 5) * 5 + (l2 % 5)
    n100 = (19 - (l1 // 5) * 5) - (l2 // 5)
    scale = 10 ** (5 - len(a))
    # The centre of the square the reference names, not its corner.
    return (e100 * 100000 + int(a) * scale + scale / 2.0,
            n100 * 100000 + int(b) * scale + scale / 2.0)


def _metres(lon_lat, other):
    kx = 111320.0 * math.cos(math.radians(lon_lat[1]))
    return math.hypot((lon_lat[0] - other[0]) * kx,
                      (lon_lat[1] - other[1]) * 110574.0)


def _way_distance(way, lon_lat):
    best = float("inf")
    for line in way.lines:
        for p in line:
            d = _metres(p, lon_lat)
            if d < best:
                best = d
    return best


def ways_near(byways, authorities, points, within):
    """Ways of these authorities passing within `within` m of any point."""
    from byway_match import Byways  # noqa: F401 - type only
    out = set()
    for lon_lat in points:
        for way in byways.near([[lon_lat]], pad_m=within):
            if way.authority in authorities and \
                    _way_distance(way, lon_lat) <= within:
                out.add(way.uid)
    return out


# ------------------------------------------------------------------ matching


def parish_variants(parish):
    """"Albury / Shere" -> both; "Outwood (Burstow in the order)" -> both."""
    text = parish or ""
    parts = re.split(r"\s*/\s*|\s*\(\s*|\s*\)\s*", text)
    out = []
    for p in parts:
        p = re.sub(r"\bin the order\b", "", p, flags=re.I).strip()
        if p:
            out.append(p)
    return out or [""]


def match_order(byways, order):
    """(way uids, how, problem). `problem` is None for a clean match.

    A reviewer's `ways` on the order wins outright: it is how a person
    settles an order the references could not (and every uid in it must
    still be a byway riders have).
    """
    from byway_match import norm_parish, norm_number
    authorities = order.get("authorities") or []
    if order.get("ways"):
        missing = [u for u in order["ways"] if u not in byways.ways]
        if missing:
            return [], None, ("pinned to %s, no longer in the published "
                              "byways" % ", ".join(missing))
        return sorted(order["ways"]), "reviewed", None
    ways, how = set(), None
    for kind, code, number in order.get("paths") or []:
        for authority in authorities:
            if kind == "code":
                ways.update(byways.match_ref(authority, code, number))
                continue
            for parish in parish_variants(order.get("parish")):
                found = byways.match_ref(authority, parish, number)
                if not found:
                    # Sections: Derbyshire's "15/1", "15/2" are BOAT 15.
                    want = norm_parish(parish)
                    n = norm_number(number)
                    found = [w.uid for w in byways.ways.values()
                             if w.authority == authority and
                             w.parish == want and
                             w.number.startswith(n + "/")]
                ways.update(found)
    if ways:
        how = "reference"
    points = []
    for ref in order.get("grid_refs") or []:
        en = grid_ref(ref)
        if en:
            from osgb import grid_to_wgs84
            points.append(grid_to_wgs84(*en))
    if ways and points:
        # The council's references are the route's ends (and sometimes a
        # typo: East Sussex's "TQ 089 222" for Iden). The middle of a long
        # route is far from both, so what is asked is only that the byways
        # the reference named pass near at least one of them - which a
        # same-numbered path in the wrong parish does not.
        nearest = min(_way_distance(byways.ways[uid], p)
                      for uid in ways for p in points)
        if nearest > GRID_AGREE_M:
            return (sorted(ways), how,
                    "the path reference names %s, %d m from the nearest "
                    "grid reference the council gave" % (
                        ", ".join(sorted(ways)), nearest))
    if not ways and points:
        # Near a route's END is also near every byway that meets it there,
        # so a match by grid reference alone is a suggestion for the
        # reviewer, who pins the right ones with `ways`.
        near = ways_near(byways, authorities, points, GRID_NEAR_M)
        if near:
            return (sorted(near), "grid",
                    "matched by grid reference only (%s): confirm and pin "
                    "them with \"ways\"" % ", ".join(sorted(near)))
    if not ways:
        return [], None, "no byway of ours matches its path or grid refs"
    return sorted(ways), how, None


def public_url(url):
    """A manual/ document is linked where this site serves it."""
    import manual_inbox
    if url and url.startswith("manual/"):
        return manual_inbox.PAGES_BASE + url
    return url


def source_name(order):
    url = order.get("source_url") or ""
    if url.startswith("manual/"):
        import manual_inbox
        doc = manual_inbox.documents_by_id().get(url)
        how = manual_inbox.provenance(doc) if doc else "saved by hand"
        return "%s - order document (%s)" % (order.get("council"), how)
    return "%s - published list of byway orders" % order.get("council")


def item_of(order, byways, ways, how):
    """A register order as a council_orders item."""
    from council_sources import as_geometry
    where = "%s %s" % (order.get("parish") or "",
                       order.get("path_as_written") or "")
    if order.get("local_name"):
        where += " (%s)" % order["local_name"]
    title = order.get("order_name") or "%s: %s" % (
        order.get("council"), order.get("restriction") or "byway order")
    item = {
        "id": order["id"],
        "authority": byways.ways[ways[0]].authority,
        "ref": order.get("order_name") or order.get("path_as_written"),
        "title": title[:240],
        "where": where.strip(),
        "vehicles": order.get("vehicles") or "other",
        "form": order.get("form") or "permanent",
        "url": public_url(order.get("source_url")),
        "source_name": source_name(order),
        "ways": ways, "match": how,
        "geometry": as_geometry(byways.geometry(ways)),
    }
    for key in ("width_m", "season", "start", "end", "label"):
        if order.get(key) not in (None, ""):
            item[key] = order[key]
    return item


def provenance_of(items, overrides=None, manual=None, home_index=None):
    """{authority: [how its published orders were obtained]} for the
    coverage table: a document saved by hand or supplied by the council
    (manual/), or read under the owner's robots.txt decision."""
    import manual_inbox
    import polite_http
    if overrides is None:
        try:
            overrides = polite_http.load_overrides()
        except ValueError:
            overrides = []
    docs = manual_inbox.documents_by_id() if manual is None else manual
    if home_index is None:
        import home_collector
        home_index = home_collector.read_json(os.path.join(
            home_collector.HOME, home_collector.INDEX), {}) or {}
    out = {}
    for item in items:
        url = item.get("url") or ""
        notes = out.setdefault(item["authority"], set())
        doc = docs.get(url) or docs.get(
            url.replace(manual_inbox.PAGES_BASE, ""))
        if doc:
            notes.add(manual_inbox.provenance(doc))
            continue
        entry = polite_http.override_for(url, overrides)
        if entry:
            notes.add("robots.txt overridden by owner decision (%s)"
                      % entry.get("id"))
        home = (home_index or {}).get(url)
        if home and home.get("collected"):
            notes.add("collected directly from the council %s"
                      % home["collected"])
    return dict((a, sorted(n)) for a, n in sorted(out.items()) if n)


def build(byways, orders):
    """(source file content, problems) for every approved order."""
    items, problems = [], []
    councils = set()
    for order in orders:
        if order.get("status") != "approved":
            continue
        ways, how, problem = match_order(byways, order)
        if problem:
            problems.append({"id": order["id"], "why": problem})
            continue
        items.append(item_of(order, byways, ways, how))
        councils.add(order.get("council"))
    items.sort(key=lambda i: i["id"])
    problems.sort(key=lambda p: p["id"])
    source = dict(SOURCE)
    source["name"] = ("Published lists of permanent and seasonal byway "
                      "orders: " + "; ".join(sorted(councils)))
    source["authorities"] = sorted(set(i["authority"] for i in items))
    source["provenance"] = provenance_of(items)
    return {"source": source, "records": len(orders), "items": items,
            "unmatched": [], "review": problems}, problems


# ------------------------------------------------------------- page checks


class _Text(html.parser.HTMLParser):
    """The readable text of a page, inside <main> when it has one."""

    SKIP = {"script", "style", "noscript", "nav", "header", "footer",
            "form", "svg", "button", "iframe", "template"}

    def __init__(self):
        html.parser.HTMLParser.__init__(self, convert_charrefs=True)
        self.skip = 0
        self.main = 0
        self.saw_main = False
        self.all, self.inside = [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "main":
            self.main += 1
            self.saw_main = True
        if tag in ("p", "li", "tr", "br", "h1", "h2", "h3", "h4", "div",
                   "loc", "td", "dt", "dd"):
            self._text("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "main" and self.main:
            self.main -= 1

    def handle_data(self, data):
        if not self.skip:
            self._text(data)

    def _text(self, data):
        self.all.append(data)
        if self.main:
            self.inside.append(data)


def page_text(body, only=None):
    """Lines of readable text, whitespace collapsed; `only` filters lines."""
    parser = _Text()
    parser.feed(body.decode("utf-8", "replace"))
    parser.close()
    chunks = parser.inside if parser.saw_main else parser.all
    lines = []
    for line in "".join(chunks).split("\n"):
        line = re.sub(r"\s+", " ", html.unescape(line)).strip()
        if line and (not only or re.search(only, line)):
            lines.append(line)
    return lines


def fingerprint(page, body):
    """(sha256, text lines or None). A PDF is compared by its bytes."""
    if page["url"].lower().endswith(".pdf") or body[:4] == b"%PDF":
        return hashlib.sha256(body).hexdigest(), None
    lines = page_text(body, page.get("only"))
    joined = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(joined).hexdigest(), lines


def snapshot_paths(register, page_id):
    base = os.path.join(register, "snapshots",
                        re.sub(r"[^a-z0-9._-]+", "_", page_id))
    return base + ".sha256", base + ".txt"


#: The most documents followed from one page in one check.
FOLLOW_CAP = 80

_HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)""", re.I)


def linked_documents(page, body):
    """The documents a page links to that its `follow` pattern names, as
    pages of their own: [{id, url, council, authorities, parent}]."""
    if not page.get("follow"):
        return []
    import urllib.parse
    seen, out = set(), []
    for href in _HREF.findall(body.decode("utf-8", "replace")):
        url = urllib.parse.urljoin(page["url"], html.unescape(href.strip()))
        url = url.split("#", 1)[0]
        if url in seen or not re.search(page["follow"], url):
            continue
        seen.add(url)
        # Named by its path alone: Powys adds "?m=<version>" to each link,
        # and a new version is a change to one document, not a new one.
        path = urllib.parse.urlsplit(url).path
        name = re.sub(r"[^A-Za-z0-9._-]+", "-",
                      urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1]))
        out.append({"id": "%s/doc/%s" % (page["id"], name.lower()[:120]),
                    "url": url, "council": page.get("council"),
                    "authorities": page.get("authorities"),
                    "parent": page["id"], "new_is_review": True})
    return sorted(out, key=lambda d: d["id"])[:FOLLOW_CAP]


def manual_pages(root=None):
    """The inbox's documents (manual/<CODE>/) as pages read from disk."""
    import manual_inbox
    out = []
    for box in manual_inbox.inboxes(root or manual_inbox.MANUAL):
        for doc in box["docs"]:
            if doc["kind"] != "document":
                continue
            out.append({"id": doc["id"], "url": doc["public_url"],
                        "council": box["council"],
                        "authorities": [box["authority"]],
                        "local": doc["path"], "new_is_review": True,
                        "provenance": manual_inbox.provenance(doc)})
    return out


def _snapshot(register, page, digest, lines):
    hash_path, text_path = snapshot_paths(register, page["id"])
    os.makedirs(os.path.dirname(hash_path), exist_ok=True)
    with open(hash_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(digest + "\n")
    if lines is not None:
        with open(text_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")


def check(client, register=REGISTER, today=None, manual_root=None,
          automated_off=None):
    """Re-read every page. Returns (changed, unreachable, baselined).

    `automated_off`: authorities whose documents come from manual/ instead;
    none of their pages is requested. A followed or hand-saved document
    seen for the first time is flagged for review (its orders still have to
    be transcribed), not quietly baselined.
    """
    from polite_http import FetchFailed, NotDue, Refused
    if automated_off is None:
        import manual_inbox
        automated_off = manual_inbox.automated_off(
            manual_root or manual_inbox.MANUAL)
    pages = [p for p in read_json(os.path.join(register, "pages.json"), [])
             or [] if not set(p.get("authorities") or ()) & automated_off]
    queue = list(pages) + manual_pages(manual_root)
    changes_path = os.path.join(register, "changes.json")
    previous = dict((c["page"], c) for c in
                    (read_json(changes_path, []) or []))
    changed, unreachable, baselined = [], [], []
    while queue:
        page = queue.pop(0)
        if page.get("blocked"):
            continue
        hash_path, text_path = snapshot_paths(register, page["id"])
        # A council's order PDF the collector holds as a digest only
        # (home_collector: the bytes stay with the council). Its digest is
        # the SHA-256 of its bytes - what fingerprint() takes of any PDF.
        held = None
        if not page.get("local") and hasattr(client, "document_digest"):
            held = client.document_digest(page["url"])
        try:
            if held:
                body = None
            elif page.get("local"):
                with open(page["local"], "rb") as fh:
                    body = fh.read()
            else:
                body = client.get(page["url"])
        except NotDue:
            # Read under the owner's robots.txt decision within the week:
            # its snapshot and any pending change stand as they are.
            if page["id"] in previous:
                changed.append(previous[page["id"]])
            continue
        except (Refused, FetchFailed, OSError) as e:
            unreachable.append({"page": page["id"], "url": page["url"],
                                "why": str(e)[:300]})
            continue
        served = client.take_served() if hasattr(client, "take_served") \
            else {}
        if served and not page.get("provenance"):
            from home_collector import provenance_of
            page = dict(page, provenance=provenance_of(served))
        if held:
            digest, lines = held, None
        else:
            queue.extend(linked_documents(page, body))
            digest, lines = fingerprint(page, body)
        try:
            with open(hash_path, encoding="utf-8") as fh:
                old = fh.read().strip()
        except OSError:
            old = None
        overridden = getattr(client, "overridden", {}).get(page["url"])
        if old is None and not page.get("new_is_review"):
            _snapshot(register, page, digest, lines)
            baselined.append(page["id"])
            continue
        if old == digest:
            continue
        diff = []
        if old is None:
            diff = ["(newly readable - transcribe its orders into "
                    "tro/register/orders.json)"] + (lines or [])[:60]
        elif lines is not None and os.path.exists(text_path):
            with open(text_path, encoding="utf-8") as fh:
                before = fh.read().splitlines()
            diff = list(difflib.unified_diff(before, lines, "before",
                                             "now", n=1, lineterm=""))[:80]
        entry = {"page": page["id"], "url": page["url"],
                 "council": page.get("council"),
                 "new": old is None,
                 "digest": digest,
                 "detected": (previous.get(page["id"]) or {}).get(
                     "detected") or today,
                 "diff": diff or ["(the document's bytes changed; "
                                  "open it to compare)"]}
        if lines is not None:
            entry["text"] = lines[:2000]
        if overridden:
            entry["read_under"] = ("robots.txt overridden by owner "
                                   "decision (%s)" % overridden)
        if page.get("provenance"):
            entry["provenance"] = page["provenance"]
        changed.append(entry)
    write_json(changes_path, changed)
    return changed, unreachable, baselined


def accept(page_ids, client, register=REGISTER):
    """Take the page as it stands now as the reviewed snapshot.

    A flagged page is taken as it was when flagged (its digest is in
    changes.json), without reading it again.
    """
    pages = dict((p["id"], p) for p in
                 read_json(os.path.join(register, "pages.json"), []) or [])
    changes_path = os.path.join(register, "changes.json")
    pending = dict((c["page"], c) for c in
                   (read_json(changes_path, []) or []))
    for page_id in page_ids:
        flagged = pending.get(page_id)
        if flagged and flagged.get("digest"):
            _snapshot(register, {"id": page_id}, flagged["digest"],
                      flagged.get("text"))
            continue
        page = pages[page_id]
        body = client.get(page["url"])
        digest, lines = fingerprint(page, body)
        _snapshot(register, page, digest, lines)
    left = [c for c in pending.values() if c["page"] not in page_ids]
    left.sort(key=lambda c: c["page"])
    write_json(changes_path, left)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build")
    b.add_argument("--out", default=COUNCIL_OUT)
    c = sub.add_parser("check")
    c.add_argument("--report", help="write a summary for the issue here")
    a = sub.add_parser("accept")
    a.add_argument("pages", nargs="+")
    args = ap.parse_args(argv)
    today = datetime.date.today().isoformat()

    if args.cmd == "build":
        from byway_match import load_byways
        byways = load_byways()
        if len(byways) < 1000:
            print("::error::only %d byways in the published containers"
                  % len(byways))
            return 1
        orders = read_json(os.path.join(REGISTER, "orders.json"))
        if not isinstance(orders, list):
            print("::error::tro/register/orders.json is missing or unreadable")
            return 1
        data, problems = build(byways, orders)
        written = write_json(args.out, data)
        counts = {}
        for o in orders:
            counts[o.get("status")] = counts.get(o.get("status"), 0) + 1
        print("register: %s; %d published on %d byway(s); %d held back%s"
              % (", ".join("%d %s" % (n, s) for s, n in sorted(
                  counts.items())), len(data["items"]),
                 len(set(w for i in data["items"] for w in i["ways"])),
                 len(problems), "" if written else " (unchanged)"))
        for p in problems:
            print("  held back %s: %s" % (p["id"], p["why"]))
        return 0

    if args.cmd in ("check", "accept"):
        import polite_http
        # The register's check is the one reader of the paths the owner
        # chose to read despite robots.txt (tools/robots_override.json); it
        # logs every such read, which also holds each to once a week.
        from home_collector import HomeClient
        client = HomeClient(polite_http.PoliteClient(
            min_gap=4.0, today=today,
            override_log=os.path.join(REGISTER, "override-reads.json")))
        if args.cmd == "accept":
            accept(args.pages, client)
            return 0
        changed, unreachable, baselined = check(client, today=today)
        print("pages: %d changed since review, %d could not be read, %d "
              "read for the first time (snapshot taken)"
              % (len(changed), len(unreachable), len(baselined)))
        lines = []
        for c in changed:
            lines.append("%s %s %s%s" % (
                "NEW" if c.get("new") else "CHANGED", c["page"], c["url"],
                " (%s)" % (c.get("read_under") or c.get("provenance"))
                if c.get("read_under") or c.get("provenance") else ""))
        for u in unreachable:
            lines.append("UNREADABLE %s %s - %s" % (u["page"], u["url"],
                                                    u["why"]))
        # A file in manual/ the pipeline cannot use is said here, in the
        # review issue - never as a failed test that would stop a refresh.
        import manual_inbox
        for box in manual_inbox.inboxes():
            for problem in box["problems"]:
                lines.append("INBOX %s" % problem)
        for line in lines:
            print("  " + line)
        if args.report:
            with open(args.report, "w", encoding="utf-8",
                      newline="\n") as fh:
                for line in lines:
                    fh.write(line + "\n")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

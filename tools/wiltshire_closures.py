#!/usr/bin/env python3
"""Wiltshire Council's register of rights of way closures, read for byways.

    https://apps.wiltshire.gov.uk/RightsOfWay/Closure

The register lists "public rights of way permanent and voluntary closures"
(its temporary closures are on one.network, which this project does not
read). It is a search form, not a feed: a GET of the page with query
parameters answers "There are no closures that match your search criteria",
so it is read the way a person reads it - open the page, choose "Byway Open
To All Traffic", press Search. That is a POST, and the owner approved exactly
that one POST on 7 October 2026 (tools/polite_http.py, FORM_POSTS): the
form's own search fields, to its own Result address, nothing else.

The result page lists every closure on a BOAT (49 on 7 October 2026, all on
one page: "Showing all 49 items"), each with its reference, rights of way,
parishes, type and dates. WHO a closure binds and WHY - and whether a
"Permanent" one is in fact a permanent SEASONAL order ("Permanent seasonal
closure from 1st October to 30th April every year", the Ridgeway near
Avebury) - is only on each closure's detail page, a plain GET at a stable
address (`detail_url`). Without it a closure is not published: the list alone
would draw a winter-only order shut all year.

THE FIVE TYPES THE REGISTER USES, and what each becomes (council_orders.py):

    Permanent     a permanent traffic regulation order: form "permanent", no
                  oform, exactly as D-TRO's permanent orders are. Never a
                  temporary closure. If its own words state a season it is
                  "seasonal" with that season (dated, so it never hides a
                  lane outside it). Wording about stopping up or
                  extinguishment - a lane that would no longer be a byway at
                  all - is held for review, never drawn as a closure.
    Seasonal      "seasonal" with the season its words state; failing that,
                  dated by its own start and end; failing both, review.
    Experimental  "experimental", dated.
    Special       a special-event or similar closure, dated by its own start
                  and end; with no end date it is held for review rather than
                  drawn shut indefinitely.
    Voluntary     NOT AN ORDER. The council asks users to keep off (Wiltshire's
                  winter restraints, 1 October to 30 April); nothing makes it
                  unlawful to ride. The order pack has no type for advice that
                  shuts nothing - the app draws an unrecognised type in the
                  closure family and calls it a limit the order sets - so these
                  are HELD FOR REVIEW, with words that say what they are, until
                  the owner decides how the app should show one
                  (`PUBLISH_VOLUNTARY`).

Who it binds comes from the detail page's "Affecting" line (`vehicles_of`).

Pure functions apart from `read`; standard library only.
"""
import html
import re
import urllib.parse

HOST = "https://apps.wiltshire.gov.uk"
#: The page holding the search form (and its anti-forgery token).
FORM_PAGE = HOST + "/RightsOfWay/Closure"
#: Where the form posts. The one POST this repository makes.
RESULT = HOST + "/RightsOfWay/Closure/Result"
#: The search: Byway Open To All Traffic (RowType 1), any type of closure
#: (ClosureType 0), the Search button. Every other field is left as the form
#: has it, which for the hidden `Archived` field is "ActiveOnly".
SEARCH = {"RowType": "1", "ClosureType": "0", "Act": "Search"}
#: The public page riders are pointed to when a closure has no document.
PUBLIC = FORM_PAGE

COUNCIL = "Wiltshire Council"
AUTHORITY = "Wiltshire"

#: Voluntary closures are requests, not orders: held for review until the
#: owner decides how the app shows one. See the module docstring.
PUBLISH_VOLUNTARY = False

#: An end date this far out is "never" (the register writes 1 January 2099).
_NEVER_YEAR = 2090


class Changed(Exception):
    """The register's page is not the shape this reader knows."""


def detail_url(row):
    """The stable address of one closure's detail page.

    The result page links each one through the search's session
    ("/Closure/Detail/s83zj5dw?row=CHIP108$001"); the same page answers
    without the session part, measured 7 October 2026, and that is the
    address the collector keeps it under."""
    return "%s/RightsOfWay/Closure/Detail?row=%s" % (
        HOST, urllib.parse.quote(row, safe="$"))


def document_url(path):
    return urllib.parse.urljoin(HOST + "/", html.unescape(path))


# ----------------------------------------------------------------- reading

def _text(fragment):
    fragment = re.sub(r"<br\s*/?>", ", ", fragment or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    text = re.sub(r"\s*,\s*(,\s*)+", ", ", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


_CARD = re.compile(r'<div class="govuk-summary-card[ "]')
_ROW_LINK = re.compile(r'href="[^"]*/RightsOfWay/Closure/Detail/[^"?]*\?row='
                       r'([^"&]+)"[^>]*>([^<]+)</a>')
_PAIR = re.compile(r'<dt class="govuk-summary-list__key">(.*?)</dt>\s*'
                   r'<dd class="govuk-summary-list__value">(.*?)</dd>', re.S)
_SHOWING = re.compile(r"Showing\s+all\s+(\d+)\s+items?", re.I)
_NONE = re.compile(r"There are no closures that match", re.I)


def parse_results(body):
    """[{row, ref, rights_of_way, parishes, type, dates}] from a result page.

    Raises Changed when the page is not a result list this reader knows:
    no "Showing all N items" (a list split over pages says "Showing 1 to
    30 of N" instead), or a count that disagrees with the cards found. A
    changed page is never read as "no closures".
    """
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) \
        else body
    if _NONE.search(text):
        return []
    m = _SHOWING.search(text)
    if not m:
        raise Changed("no 'Showing all N items' on the result page (split "
                      "over pages, or a new layout)")
    cards = _CARD.split(text)[1:]
    out = []
    for card in cards:
        link = _ROW_LINK.search(card)
        if not link:
            continue
        fields = dict((_text(k).rstrip(":").lower(), _text(v))
                      for k, v in _PAIR.findall(card))
        out.append({
            "row": html.unescape(link.group(1)),
            "ref": _text(link.group(2)),
            "rights_of_way": fields.get("rights of way") or
            fields.get("right of way") or "",
            "parishes": fields.get("parishes") or fields.get("parish") or "",
            "type": fields.get("type of closure") or "",
            "dates": fields.get("dates") or "",
        })
    if len(out) != int(m.group(1)):
        raise Changed("the page says %s closures and %d were read"
                      % (m.group(1), len(out)))
    return out


_DETAIL = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)
_DOC = re.compile(r'href="(/RightsOfWay/Closure/Download/[^"]+)"[^>]*>'
                  r'([^<]+)</a>')
_MAP_LINK = re.compile(r"\(\s*(<a [^>]*>View on map</a>[^)]*)?\)", re.I)


def parse_detail(body):
    """{reference, parishes, type, rights_of_way, affecting, reason,
    row_type, start, end, documents: [(name, url)]} from a detail page."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) \
        else body
    start = text.find('<dl class="detail-list">')
    if start < 0:
        raise Changed("no detail list on the closure's page")
    block = text[start:text.find("</dl>", start)]
    out = {"documents": []}
    for key, value in _DETAIL.findall(block):
        key = _text(key).lower()
        if key == "documents":
            out["documents"] = [(_text(name), document_url(path))
                                for path, name in _DOC.findall(value)]
            continue
        if key == "contact":
            continue          # the council's office address; not needed
        value = _MAP_LINK.sub("", value)
        out[{"reference": "reference", "parish": "parishes",
             "parishes": "parishes", "application type": "type",
             "row id": "rights_of_way", "affecting": "affecting",
             "reason": "reason", "row type": "row_type",
             "start closure": "start",
             "end closure": "end"}.get(key, key)] = _text(value)
    if not out.get("reference"):
        raise Changed("the closure's page names no reference")
    return out


# ------------------------------------------------------------- meaning it

_PATH = re.compile(r"([A-Za-z]+)\s*(\d+[A-Za-z]?)\s*(\((?:part)\))?", re.I)


def path_refs(text):
    """[(parish code, number, part?)] from "CBIS13, ODST11" or
    "BSTO21, BSTO33 (PART)". The council's own codes - the same ones its
    rights of way layer, and so our Wiltshire byways, are named by
    (council_ways._ref_wiltshire)."""
    out = []
    for m in _PATH.finditer(text or ""):
        out.append((m.group(1).upper(), m.group(2).upper(),
                    bool(m.group(3))))
    return out


_EXCEPT_MOTORCYCLES = re.compile(
    r"\bexcept\b[^.;]*?\bmotor\s?-?(?:cycle|bike)s?\b", re.I)
_MOTOR = re.compile(r"\bmotor(?:i[sz]ed)?\b", re.I)
_ALL_VEHICLES = re.compile(r"\bvehicular\b|\ball vehicles\b", re.I)
_ALL_USERS = re.compile(r"\ball (?:users|traffic)\b|\beveryone\b", re.I)


def vehicles_of(affecting):
    """(council_orders vehicles key, label or None) for the "Affecting" line.

        "All motorised users except motorcycles."   -> motors except motorcycles
        "All vehicles except bycles and motorbikes"  -> motors except motorcycles
        "All motorised traffic" / "Motorised vehicles" -> motor vehicles
        "All vehicular users"                         -> all vehicles
        "All users"                                   -> everyone
        anything else                                 -> other, in its words
    """
    text = affecting or ""
    if _EXCEPT_MOTORCYCLES.search(text):
        return "motor_vehicles_except_motorcycles", None
    if _MOTOR.search(text):
        return "motor_vehicles", None
    if _ALL_VEHICLES.search(text):
        return "all_vehicles", None
    if _ALL_USERS.search(text):
        return "all_users", None
    label = text.strip().rstrip(".")
    return "other", (label[:1].upper() + label[1:80]) or "Restriction"


_GONE = re.compile(r"\b(stopp(?:ed|ing) up|extinguish\w*|diver(?:sion|ted))\b",
                   re.I)


def _date(text, parse_date):
    day = parse_date(text)
    if day and int(day[:4]) >= _NEVER_YEAR:
        return None
    return day


def candidate(entry, detail, parse_date, parse_season, clean):
    """One register closure as a council_sources candidate, or None for a
    closure that is not on a Byway Open To All Traffic.

    `entry` is a parse_results row, `detail` its parse_detail (None when the
    detail page could not be had). The parsing helpers are passed in from
    council_sources, so the dates and seasons are read exactly as every
    other council's are.
    """
    detail = detail or {}
    row_type = detail.get("row_type")
    if row_type and "byway open to all traffic" not in row_type.lower():
        return None
    kind = (detail.get("type") or entry.get("type") or "").strip()
    paths = path_refs(detail.get("rights_of_way") or entry["rights_of_way"])
    parishes = detail.get("parishes") or entry.get("parishes") or ""
    affecting = clean(detail.get("affecting") or "")
    reason = clean(detail.get("reason") or "")
    dates = entry.get("dates") or ""
    start = parse_date(detail.get("start")) or parse_date(
        dates.split("—")[0])
    end_text = detail.get("end") if detail else (
        dates.split("—")[1] if "—" in dates else "")
    end = _date(end_text, parse_date)
    docs = detail.get("documents") or []
    order_doc = next((u for n, u in docs if n.lower().startswith("order")),
                     None)
    names = ", ".join("%s%s%s" % (p, n, " (part)" if part else "")
                      for p, n, part in paths)
    item = {
        "id": entry["ref"],
        "ref": entry["ref"],
        "where": "Byway%s %s%s" % ("s" if len(paths) > 1 else "", names,
                                   (" (%s)" % parishes) if parishes else ""),
        "start": start, "end": end,
        "partial": any(part for _p, _n, part in paths),
        # The order itself where the register holds it; otherwise the
        # closure's own page, which shows every document it has.
        "url": order_doc or detail_url(entry["row"]),
        "refs": [(p, n) for p, n, _part in paths],
    }
    vehicles, label = vehicles_of(affecting)
    item["vehicles"] = vehicles
    if label:
        item["label"] = label
    said = []
    if affecting:
        said.append("affecting " + affecting[:1].lower() +
                    affecting[1:].rstrip("."))
    if reason:
        said.append("reason given: " + reason.rstrip("."))
    said = "; ".join(said)
    words = "%s %s" % (affecting, reason)
    season = parse_season(words)
    lowered = kind.lower()

    def titled(what):
        item["title"] = ("%s %s %s%s" % (COUNCIL, what, entry["ref"],
                                         (": " + said) if said else ""))[:300]

    if not detail:
        titled("%s closure" % lowered)
        item["form"] = "permanent" if lowered == "permanent" else "temporary"
        item["review_only"] = ("the closure's own page could not be read: "
                               "the register's list alone does not say who "
                               "it binds or whether it is seasonal")
        return item
    if lowered == "voluntary":
        item["form"] = "temporary"
        item["vehicles"] = "other"
        item["label"] = "Voluntary closure - a request, not a legal order"
        titled("voluntary closure (the council asks users to keep off; it "
               "is not an order and does not close the byway in law)")
        if not PUBLISH_VOLUNTARY:
            item["review_only"] = (
                "a voluntary closure is the council's request, not a legal "
                "order; the pack has no type for advice that shuts nothing, "
                "so it is held until the owner decides how the app shows one")
        return item
    if _GONE.search(words):
        titled("%s closure" % lowered)
        item["form"] = "permanent"
        item["review_only"] = ("its words speak of stopping up, "
                               "extinguishment or a diversion: confirm what "
                               "the byway is now before publishing anything")
        return item
    if lowered == "permanent":
        if season:
            item.update({"form": "seasonal", "season": season})
            titled("permanent seasonal order")
        else:
            item["form"] = "permanent"
            titled("permanent order")
        return item
    if lowered == "seasonal":
        titled("seasonal closure")
        if season:
            item.update({"form": "seasonal", "season": season})
        elif start and end:
            item["form"] = "temporary"
        else:
            item["form"] = "seasonal"      # no season: match() holds it
        return item
    if lowered == "experimental":
        item["form"] = "experimental"
        titled("experimental order")
        return item
    if lowered == "special":
        item["form"] = "temporary"
        titled("special closure")
        if not end:
            item["review_only"] = ("a special closure with no end date: "
                                   "not drawn shut indefinitely on the list's "
                                   "say-so")
        return item
    item["form"] = "temporary"
    titled("closure")
    item["review_only"] = "the register's type %r is not one this reader " \
        "knows" % kind
    return item


def read(client, parse_date, parse_season, clean, log=None):
    """(records, [candidate]) from the register, through `client`.

    `client.post_form` submits the search; each closure's detail page is a
    `client.get`. A detail page that cannot be had holds that one closure
    for review; the result page itself failing raises, and the caller keeps
    its last good read.
    """
    from polite_http import FetchFailed, Refused
    body = client.post_form(FORM_PAGE, RESULT, SEARCH)
    try:
        entries = parse_results(body)
    except Changed as e:
        raise FetchFailed("Wiltshire's closures register changed: %s" % e)
    out = []
    for entry in entries:
        try:
            detail = parse_detail(client.get(detail_url(entry["row"])))
        except (Refused, FetchFailed, Changed) as e:
            if log:
                log("  %s: detail not read (%s)" % (entry["ref"], e))
            detail = None
        item = candidate(entry, detail, parse_date, parse_season, clean)
        if item is not None:
            out.append(item)
    return len(entries), out


def detail_urls(body):
    """Every closure's detail page a result page lists, at its stable
    address: what the collector follows from the search. None when the page
    is not one this reader knows - which is not "no closures", so the
    collector keeps the detail pages it has."""
    try:
        entries = parse_results(body)
    except Changed:
        return None
    return [detail_url(e["row"]) for e in entries]

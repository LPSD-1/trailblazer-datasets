#!/usr/bin/env python3
"""Ceredigion County Council's live road closures, read for byways.

    https://wms.ceredigion.gov.uk/geoserver/CeredigionMaps/wfs
    typeNames=CeredigionMaps:road_closures_live_ctc

The council's own GeoServer layer behind its "Road Closures" page
(https://www.ceredigion.gov.uk/resident/travel-roads-parking/roadworks/
road-closures/): every temporary restriction it has made on its roads, one
feature per closed, access-only or diversion section. 127 features on
8 October 2026, every one `status` "Live", LineString or MultiLineString in
British National Grid (DefaultCRS EPSG:27700; asked for by name, and a read
in any other frame is refused - see `read`).

WHY IT IS READ. Ceredigion is in Wales, so Street Manager - England's
street works register, tools/street_manager.py - never covers it, and
D-TRO holds nothing for it. A road closure here is a temporary traffic order
on a county road, and a byway in Ceredigion can be one of those roads. Only a
closure that falls on one of OUR Ceredigion byways is published (the shared
geometry match, council_sources.match); on 8 October 2026 none did - the one
feature on a byway was a diversion route. It is in the pipeline for the day
one does.

ONE GET. The layer is asked for once per run, as GeoJSON, with `propertyName`
naming only the fields used here. The layer also has an `applicant` field,
which can hold a person's or a firm's name: it is NEVER requested, so it is
never fetched, kept or published (`FIELDS`). The geometry field is `geom`.
At least `MIN_GAP` seconds lie between any two requests to the host,
robots.txt included (`gentler`).

WHAT EACH CATEGORY BECOMES (council_orders.py):

    Closure             a temporary closure of the road: all users, dated by
    Incidental Closure  the record's own date_start and date_end (inclusive).
    Access Only         the road is shut to through traffic and open for
                        access: a restriction to read ("other", labelled),
                        never drawn as a full closure.
    Diversion           NOT A CLOSURE. Its line is where traffic is sent, not
                        what is shut: never read at all.
    Other               lane closures and two-way working ("Lane Closure
                        (Northbound)", "Two-way traffic flow"): not a road
                        closure, so never published; held for a person to
                        review if one lands on a byway.

Anything whose `status` is not "Live" is not read; anything in a category
this list does not know is held for review, never published on a guess.

`times` and `road_closed_all_day` are said in the title ("08:00-17:00", "24
awr/hours"): the pack has no time of day, and a closure for part of the day
is still drawn for its whole dates, as every temporary closure is.

LICENCE. The service's GetCapabilities states <ows:Fees>NONE</ows:Fees> and
<ows:AccessConstraints>NONE</ows:AccessConstraints> (read 8 October 2026),
and no licence. The council's mapping terms
(https://www.ceredigion.gov.uk/resident/maps/terms-and-conditions/) restrict
the Ordnance Survey data shown on its maps to viewing; that page does not
name this service. Credited as "Ceredigion County Council", read under the
owner's decision of 7 October 2026 that council data is public information.

Pure functions apart from `read`; standard library only.
"""
import contextlib
import re
import urllib.parse

from polite_http import FetchFailed

COUNCIL = "Ceredigion County Council"
AUTHORITY = "Ceredigion"

WFS = "https://wms.ceredigion.gov.uk/geoserver/CeredigionMaps/wfs"
TYPE_NAME = "CeredigionMaps:road_closures_live_ctc"
#: The fields asked for, and the only ones. `applicant` is not among them,
#: and never may be.
FIELDS = ("layer_type", "id", "reference", "status", "category", "times",
          "road_closed_all_day", "justification", "date_start", "date_end",
          "geom")
#: The frame the geometry is asked for in, and the only one read.
SRS = "EPSG:27700"
GET_FEATURE = WFS + "?" + urllib.parse.urlencode([
    ("service", "WFS"), ("version", "2.0.0"), ("request", "GetFeature"),
    ("typeNames", TYPE_NAME), ("outputFormat", "application/json"),
    ("srsName", SRS), ("propertyName", ",".join(FIELDS)),
], safe=":,/")
#: The council's own page for its road closures, where riders are sent.
PUBLIC = ("https://www.ceredigion.gov.uk/resident/travel-roads-parking/"
          "roadworks/road-closures/")

#: Seconds between two requests to the council's host, at the least.
MIN_GAP = 4.5

#: category (lower case) -> what it becomes. None: never read.
CLOSURE, ACCESS_ONLY, NOT_A_CLOSURE = "closure", "access-only", "review"
CATEGORIES = {
    "closure": CLOSURE,
    "incidental closure": CLOSURE,
    "access only": ACCESS_ONLY,
    "diversion": None,
    "other": NOT_A_CLOSURE,
}


@contextlib.contextmanager
def gentler(client, gap=MIN_GAP):
    """At least `gap` seconds between requests for the length of the block.

    Raises the gap of the PoliteClient doing the work (inside a HomeClient
    when the builds read) and puts it back after; never lowers it.
    """
    inner = client
    for _ in range(5):
        if "min_gap" in vars(inner) or not hasattr(inner, "client"):
            break
        inner = inner.client
    saved = vars(inner).get("min_gap")
    if saved is not None and saved < gap:
        inner.min_gap = gap
    try:
        yield client
    finally:
        if saved is not None:
            inner.min_gap = saved


def _frame_of(data):
    return str(((data.get("crs") or {}).get("properties") or {})
               .get("name") or "")


def check_response(data):
    """A FetchFailed for a read that must not replace the last good one.

    Not a FeatureCollection; not in British National Grid (every line would
    fall outside the grid and the file would be rewritten with nothing on
    any byway); or cut short by a server limit.
    """
    if not isinstance(data, dict) or not isinstance(data.get("features"),
                                                    list):
        raise FetchFailed("no features list from %s" % WFS)
    frame = _frame_of(data)
    if not re.search(r"(?:^|\D)27700$", frame):
        raise FetchFailed("%s answered in %r, not %s; nothing taken"
                          % (WFS, frame or "no stated frame", SRS))
    matched, returned = data.get("numberMatched"), data.get("numberReturned")
    if isinstance(matched, int) and isinstance(returned, int) and \
            returned < matched:
        raise FetchFailed("%s returned %d of %d features; a cut-short read "
                          "is not taken" % (WFS, returned, matched))


def candidate(feature, parse_date, geojson_lines, clean):
    """One council_sources candidate for a feature, or None.

    None for a diversion and for anything not "Live". `clean` strips
    personal details from free text (council_sources.strip_personal).
    """
    p = feature.get("properties") or {}
    if (p.get("status") or "").strip().lower() != "live":
        return None
    category = re.sub(r"\s+", " ", (p.get("category") or "").strip())
    kind = CATEGORIES.get(category.lower(), NOT_A_CLOSURE)
    if kind is None:
        return None
    ref = (p.get("reference") or "").strip()
    why = clean(p.get("justification"))[:160]
    times = clean(p.get("times"))[:40]
    all_day = (p.get("road_closed_all_day") or "").strip().upper() == "Y"
    when = times if times else ("all day" if all_day else "")
    what = {CLOSURE: "road closure", ACCESS_ONLY: "road closure (access "
            "only)"}.get(kind, (category or "restriction").lower())
    title = "%s %s%s%s%s" % (COUNCIL, what, (" %s" % ref) if ref else "",
                             (": %s" % why) if why else "",
                             (" (%s)" % when) if when else "")
    item = {
        "id": "%s|%s" % (ref, p.get("id")),
        "ref": ("Ceredigion %s" % ref) if ref else None,
        "title": title[:240],
        "where": "Ceredigion road closure %s" % (ref or p.get("id")),
        "vehicles": "all_users", "form": "temporary",
        "start": parse_date(p.get("date_start")),
        "end": parse_date(p.get("date_end")),
        "url": PUBLIC,
        "lines": geojson_lines(feature.get("geometry")),
        # A road closures layer: nearly every closure is on a road we do
        # not carry, so one that misses our byways is not "unmatched".
        "claims_byway": False,
    }
    if kind == ACCESS_ONLY:
        item["vehicles"] = "other"
        item["label"] = "Road closed except for access"
    elif kind == NOT_A_CLOSURE:
        item["review_only"] = ("Ceredigion's road closures layer files this "
                               "as \"%s\" (%s), not as a closure: check "
                               "before publishing" % (category or "no "
                                                      "category", why))
    return item


def read(client, parse_date, geojson_lines, clean):
    """(records read, [candidate]) from the live layer: one GET."""
    with gentler(client):
        data = client.get_json(GET_FEATURE)
    check_response(data)
    feats = data["features"]
    out = []
    for f in feats:
        item = candidate(f, parse_date, geojson_lines, clean)
        if item is not None:
            out.append(item)
    return len(feats), out

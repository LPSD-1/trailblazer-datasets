#!/usr/bin/env python3
"""Council records, as orders in the traffic-order pack the app already reads.

D-TRO is not mandatory, and even when it is, orders made before it will only
arrive if a council chooses to send them (data-sources-report section 2.1).
Measured on the 6 September 2026 extract: 81 of 146,202 records name a byway.
So the byway closures and the long-standing permanent and seasonal byway
orders riders most need are published, if at all, by councils themselves: a
closures layer, a page of notices, a list of orders. Those sources are read
by tools/council_sources.py into `tro/council/<source>.json`; this module
turns what they hold into features of EXACTLY the shape tools/build_tro.py
writes for D-TRO orders, so a phone shows them with no change to the app:

    code, label, otype, oform, name, where, start, end, ref, tra, tro_uid

and four more the app ignores today but the lane sheet can learn to cite:

    source       the source's id, e.g. "dorset-closures"
    source_name  who published it, by name ("Dorset Council - rights of way
                 closures layer")
    url          the council's own notice, order or record for this item
    ways         the way_uids of the byways it was matched to

PURE FUNCTIONS. No network, no key, no clock: the build passes the day.

WHAT A COUNCIL RECORD BECOMES
-----------------------------
`vehicles` (what the order stops) picks the D-TRO code and the spec 5.3 type,
so the app's existing table decides who it binds:

    all_users                  miscRoadClosure / prohibition  - a s.14 closure
    all_vehicles, motor_vehicles  movementOrderProhibitedAccess / prohibition
    motor_vehicles_except_motorcycles
                               councilProhibitionExceptMotorcycles /
                               motors_except_motorcycles (a new row; an app
                               built before it reads it as `other`, which
                               bites "sometimes" and never hides or shuts)
    vehicles_over_width        dimensionMaximumWidth / width
    voluntary                  councilVoluntaryClosure / voluntary - NOT AN
                               ORDER: the council asks riders to keep off,
                               and the type binds nobody. Carries `asked_by`,
                               the council doing the asking, for the app's
                               "X asks riders not to use this lane" line.
    other                      councilRestriction / other

`form` maps onto the pack's existing forms: a temporary closure is
"seasonal" (ORDER_FORMS: "Seasonal or temporary (TTRO)"), a seasonal order is
"seasonal" with the dates of its CURRENT or NEXT season, a permanent order
carries no oform at all, exactly as D-TRO's permanent orders do.

A SEASONAL ORDER IS NEVER WRITTEN WITHOUT DATES. "Closed 1 October to 30
April" becomes the season in force on the build's day, or the next one when
it starts inside the pack's 90-day horizon, and nothing otherwise - a dated
seasonal lane is open outside its dates, and an undated one would be drawn
shut all year. A source that says "seasonal" without saying when is not
published (council_sources.py lists it for review instead).
"""
import datetime
import hashlib
import json
import math

from tro import HORIZON_DAYS

# vehicles -> (D-TRO code, spec 5.3 type, label)
VEHICLES = {
    "all_users": ("miscRoadClosure", "prohibition", "Byway closed"),
    "all_vehicles": ("movementOrderProhibitedAccess", "prohibition",
                     "No vehicles"),
    "motor_vehicles": ("movementOrderProhibitedAccess", "prohibition",
                       "No motor vehicles"),
    "motor_vehicles_except_motorcycles": (
        "councilProhibitionExceptMotorcycles", "motors_except_motorcycles",
        "No motor vehicles except solo motorcycles"),
    "vehicles_over_width": ("dimensionMaximumWidth", "width", "Width limit"),
    "height_limit": ("dimensionMaximumHeightStructural", "height",
                     "Height limit"),
    "weight_limit": ("dimensionMaximumWeightStructural", "weight",
                     "Weight limit"),
    # A request, not an order (build_tro.ORDER_TYPES["voluntary"]). Its own
    # code, so a reader that only has the code - the orders container keeps
    # no otype - still knows it from a closure.
    "voluntary": ("councilVoluntaryClosure", "voluntary",
                  "Voluntary closure - a request, not a legal order"),
    "other": ("councilRestriction", "other", "Restriction"),
}

FORMS = {
    "temporary": "seasonal",
    "seasonal": "seasonal",
    "experimental": "experimental",
    "permanent": None,
}

# Which source wins when two describe the same order (report 2.5 rule 3):
# D-TRO, then a council's machine-readable feed, then its HTML/PDF list,
# then Street Manager, then anything else.
PRECEDENCE = {"dtro": 0, "council-layer": 1, "council-page": 2,
              "register": 3, "street-manager": 4}


def _day(value):
    if not value:
        return None
    text = str(value)[:10]
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def season_dates(season, day, horizon_days=HORIZON_DAYS):
    """(start, end) of the season in force on `day`, or the next one.

    `season` is {"from": "MM-DD", "to": "MM-DD"}, inclusive both ends and
    allowed to run over the new year. None when the next season starts
    beyond the horizon - the lane is open until then.
    """
    today = datetime.date.fromisoformat(day)

    def at(year, mmdd):
        month, dom = int(mmdd[:2]), int(mmdd[3:5])
        if month == 2 and dom == 29:
            dom = 28
        return datetime.date(year, month, dom)

    for year in (today.year - 1, today.year, today.year + 1):
        start = at(year, season["from"])
        end = at(year, season["to"])
        if end < start:
            end = at(year + 1, season["to"])
        if end < today:
            continue
        if start > today + datetime.timedelta(days=horizon_days):
            return None
        return start.isoformat(), end.isoformat()
    return None


def label_for(item):
    code, otype, label = VEHICLES.get(item.get("vehicles"),
                                      VEHICLES["other"])
    if item.get("vehicles") == "vehicles_over_width" and item.get("width_m"):
        label = "Width limit %s m" % _fmt(item["width_m"])
    elif item.get("vehicles") == "all_users" and item.get("partial"):
        label = "Byway partly closed"
    elif item.get("vehicles") in ("other", "height_limit",
                                  "weight_limit") and item.get("label"):
        label = item["label"]
    # On an unsurfaced unclassified road (council_sources.match sets `on`):
    # a public road, so not "Byway closed" over it.
    if item.get("on") == "ucr" and label.startswith("Byway "):
        label = "Road " + label[len("Byway "):]
    return code, otype, label


def _fmt(number):
    text = ("%.2f" % float(number)).rstrip("0").rstrip(".")
    return text


def _round_geometry(geometry):
    def walk(node):
        if isinstance(node, (list, tuple)) and node and isinstance(
                node[0], (int, float)):
            return [round(float(node[0]), 5), round(float(node[1]), 5)]
        return [walk(n) for n in node]
    return {"type": geometry["type"],
            "coordinates": walk(geometry["coordinates"])}


def feature_of(item, source, day, tra_of=None, horizon_days=HORIZON_DAYS):
    """One council item as a traffic-order feature for `day`, or None.

    None when it is over, when it starts beyond the horizon, or when it is a
    seasonal order with no season to date it by.
    """
    geometry = item.get("geometry")
    if not isinstance(geometry, dict) or not geometry.get("coordinates"):
        return None
    form = item.get("form") or "temporary"
    start, end = _day(item.get("start")), _day(item.get("end"))
    if form == "seasonal":
        season = item.get("season")
        if not season:
            return None
        window = season_dates(season, day, horizon_days)
        if window is None:
            return None
        s, e = window
        # An order with its own life inside which the season recurs: never
        # dated before it began or after it ended.
        if start and e < start:
            return None
        if end and s > end:
            return None
        start = max(s, start) if start else s
        end = min(e, end) if end else e
    if end is not None and end < day:
        return None
    if start is not None and horizon_days is not None:
        limit = (datetime.date.fromisoformat(day)
                 + datetime.timedelta(days=horizon_days)).isoformat()
        if start > limit:
            return None

    code, otype, label = label_for(item)
    props = {"code": code, "label": label, "otype": otype}
    oform = FORMS.get(form)
    if oform:
        props["oform"] = oform
    for key, value in (("name", item.get("title")),
                       ("where", item.get("where")),
                       ("start", start), ("end", end),
                       ("ref", item.get("ref"))):
        if value not in (None, ""):
            props[key] = value
    tra = tra_of(item.get("authority")) if tra_of else None
    if tra:
        props["tra"] = tra
    props["source"] = source["id"]
    # A register item names its own council; a layer's items share one.
    props["source_name"] = item.get("source_name") or source["name"]
    if item.get("url"):
        props["url"] = item["url"]
    if item.get("ways"):
        props["ways"] = sorted(item["ways"])
    # WHO IS ASKING, on a request: "Wiltshire Council asks riders not to use
    # this lane". Only on a voluntary closure - every other item is an order,
    # and an order is made, not asked.
    if otype == "voluntary" and item.get("asked_by"):
        props["asked_by"] = item["asked_by"]
    geometry = _round_geometry(geometry)
    seed = "|".join([source["id"], str(item.get("id")), str(code),
                     str(start), str(end), geometry["type"],
                     json.dumps(geometry["coordinates"],
                                separators=(",", ":"))])
    props["tro_uid"] = "c" + hashlib.sha256(seed.encode()).hexdigest()[:15]
    return {"type": "Feature", "geometry": geometry, "properties": props}


# ----------------------------------------------------------------- dedupe


def _lines(geometry):
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Point":
        return [[tuple(coords)]]
    if kind == "LineString":
        return [[tuple(p) for p in coords]]
    if kind in ("MultiLineString", "Polygon"):
        return [[tuple(p) for p in line] for line in coords]
    if kind == "MultiPolygon":
        return [[tuple(p) for p in ring] for poly in coords for ring in poly]
    return []


def _box(lines):
    xs = [p[0] for l in lines for p in l]
    ys = [p[1] for l in lines for p in l]
    return min(xs), min(ys), max(xs), max(ys)


def _days_apart(a, b):
    return abs((datetime.date.fromisoformat(a)
                - datetime.date.fromisoformat(b)).days)


def same_dates(a, b):
    """Report 2.5 rule 2: start within 3 days, end within 14.

    THE SAME KIND OF LIFE FIRST. Measured on the published pack: a one-day
    D-TRO road closure ("License (Other) on The Green", 30 November) sat on
    Suffolk's permanent motor-vehicle ban on Shop Drove, and with "an open
    end allowed" read loosely the permanent order was folded into it - and
    would have left the map on 1 December. So an order with an end only ever
    matches another with an end, an open-ended one only another open-ended
    one, and a start is compared only when both have one.
    """
    sa, sb = a.get("start"), b.get("start")
    ea, eb = a.get("end"), b.get("end")
    if bool(ea) != bool(eb):
        return False
    if (a.get("oform") or "permanent") != (b.get("oform") or "permanent"):
        return False
    if sa and sb and _days_apart(sa[:10], sb[:10]) > 3:
        return False
    if ea and eb and _days_apart(ea[:10], eb[:10]) > 14:
        return False
    return True


def _tra(value):
    """A traffic authority code as compared, as build_tro.normalise_tra."""
    if value is None:
        return None
    text = str(value).strip()
    return (text.lstrip("0") or "0") if text else None


def _family(props):
    """The kind two records must share to be one order: the spec 5.3 type,
    exactly. A width limit and a prohibition on one lane are two orders."""
    return props.get("otype") or "other"


def same_place(fa, fb, tol=25.0, min_share=0.5, min_length=20.0):
    """At least half of the shorter geometry within `tol` m of the other.

    NEVER FOR A POINT, OR A STUB. A D-TRO road closure published as one
    coordinate where its road crosses the byway lies "within 25 m" of the
    byway closure by every measure, and folding the council's closure into it
    would leave the rider with a dot on a road and no closure on the lane. So
    the shorter of the two must be a real stretch (`min_length` metres).
    """
    from byway_match import _length, _project, overlap
    la, lb = _lines(fa["geometry"]), _lines(fb["geometry"])
    if not la or not lb:
        return False
    pts = [p for l in la for p in l]
    lat0 = sum(p[1] for p in pts) / len(pts)
    lon0 = sum(p[0] for p in pts) / len(pts)
    pa = [_project(l, lat0, lon0) for l in la]
    pb = [_project(l, lat0, lon0) for l in lb]
    if min(_length(pa), _length(pb)) < min_length:
        return False
    share, _metres = overlap(pa, pb, tol)
    return share >= min_share


def merge_council(dtro_features, council, precedence_of):
    """Lay council features over D-TRO's without counting an order twice.

    `council` is a list of (feature, source_kind). A council feature that is
    the same order as one already kept - same place, compatible kind, dates
    agreeing within the report's tolerances - is dropped, and the kept
    feature records it under `also` so the sheet can say "also published by".
    D-TRO always wins; between councils' sources, `precedence_of(kind)`.

    Returns (features to add, how many were folded into another).
    """
    # Box index over D-TRO features, so 40,000 orders are not compared
    # against every council feature.
    cell = 0.01
    grid = {}

    def cells(box, pad=0.0005):
        w, s, e, n = box
        for i in range(int(math.floor((w - pad) / cell)),
                       int(math.floor((e + pad) / cell)) + 1):
            for j in range(int(math.floor((s - pad) / cell)),
                           int(math.floor((n + pad) / cell)) + 1):
                yield (i, j)

    def index(feature, rank):
        lines = _lines(feature.get("geometry") or {})
        if not lines:
            return
        entry = (feature, rank, _box(lines))
        for c in cells(entry[2]):
            grid.setdefault(c, []).append(entry)

    for f in dtro_features:
        index(f, PRECEDENCE["dtro"])

    ordered = sorted(council, key=lambda pair: (
        precedence_of(pair[1]), pair[0]["properties"]["tro_uid"]))
    added, folded = [], 0
    for feature, kind in ordered:
        props = feature["properties"]
        lines = _lines(feature["geometry"])
        box = _box(lines)
        twin = None
        seen = set()
        for c in cells(box):
            for other, _rank, obox in grid.get(c, ()):
                if id(other) in seen:
                    continue
                seen.add(id(other))
                if obox[2] < box[0] - 0.0005 or obox[0] > box[2] + 0.0005 \
                        or obox[3] < box[1] - 0.0005 \
                        or obox[1] > box[3] + 0.0005:
                    continue
                op = other["properties"]
                # One source's own records are never folded together: two
                # of its byways meeting end to end overlap "half the shorter"
                # and one would vanish. Its real duplicates were already
                # joined where it was read (council_sources.merge_twins).
                if op.get("source") and op.get("source") ==                         props.get("source"):
                    continue
                if _tra(op.get("tra")) != _tra(props.get("tra")):
                    continue
                if _family(op) != _family(props):
                    continue
                if not same_dates(op, props):
                    continue
                if not same_place(feature, other):
                    continue
                twin = other
                break
            if twin is not None:
                break
        if twin is not None:
            also = twin["properties"].setdefault("also", [])
            note = {"source": props.get("source"),
                    "source_name": props.get("source_name")}
            for key in ("ref", "url"):
                if props.get(key):
                    note[key] = props[key]
            if note not in also:
                also.append(note)
                also.sort(key=lambda n: (n.get("source") or "",
                                         n.get("ref") or ""))
            folded += 1
            continue
        added.append(feature)
        index(feature, precedence_of(kind))
    return added, folded

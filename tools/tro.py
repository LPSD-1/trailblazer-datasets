#!/usr/bin/env python3
"""Turn a D-TRO record into the handful of facts a rider needs.

A published D-TRO is a legal instrument expressed as deeply nested JSON: an
order, holding provisions, holding regulations, holding conditions, each with
its own validity and its own geometry. Most of that matters to the authority
that made it and to nobody on a bike.

What a rider needs, at a gate, in the rain, is four things:

    is there an order here, what does it stop me doing, is it in force now,
    and where exactly does it apply?

This module answers those and throws the rest away. Everything here is a pure
function of its input so it can be tested against real records without a
network, a key or a device.

MEASURED against the national corpus on 14 September 2026 (146,202 records):

  * 48,413 are live or undated. **Two thirds have already expired** — mostly
    roadworks closures that were never removed, because nothing removes them.
    Shipping the lot would treble the download to tell riders about road
    closures from last year.
  * 1,523,546 coordinate pairs across every live regulationLocation, which is
    about 12 MB of raw geometry for the whole country before any packing.

Both numbers are why a TRO pack can be small enough to fetch every day.
"""
import datetime

# The regulation kinds worth telling a rider about, and what to call them.
#
# NOT the whole enum. The corpus is dominated by kerbside parking rules —
# 15,129 "no waiting", 6,513 "no stopping", thousands of permit bays — and a
# rider heading for a byway does not need a bay-by-bay account of a town
# centre's parking. Carrying them would multiply the pack for information the
# app has no way to use.
#
# What survives is anything that can stop a vehicle, turn it round, or catch it
# by its size or weight.
INTERESTING = {
    # Closures. The big one, and the whole reason for doing this.
    "miscRoadClosure": "Road closed",
    "miscLaneClosure": "Lane closed",
    "miscFootwayClosure": "Footway closed",
    "miscCycleLaneClosure": "Cycle lane closed",
    "movementOrderProhibitedAccess": "No access",
    "miscPedestrianZone": "Pedestrian zone",
    # Turns and direction. A closed turn is a wrong turn taken at speed.
    "mandatoryDirectionOneWay": "One way",
    "miscSuspensionOfOneWay": "One way suspended",
    "bannedMovementNoRightTurn": "No right turn",
    "bannedMovementNoLeftTurn": "No left turn",
    "bannedMovementNoUTurn": "No U-turn",
    "miscContraflow": "Contraflow",
    # Size and weight. Few of these exist, and every one of them is exactly
    # the thing somebody wants to know BEFORE they are committed to a lane
    # with nowhere to turn round.
    "dimensionMaximumWeightStructural": "Weight limit",
    "dimensionMaximumHeightStructural": "Height limit",
    "dimensionMaximumWidth": "Width limit",
    "dimensionMaximumLength": "Length limit",
    "miscSuspensionOfWeightRestriction": "Weight limit suspended",
}

# Kinds that carry their own typed object rather than a generalRegulation.
SPEED_LIMIT = "speedLimitValueBased"


def _as_list(value):
    """D-TRO publishes single-element lists as bare objects, sometimes."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    return []


def _validity(regulation):
    """(start, end) as YYYY-MM-DD strings, either of which may be None.

    A regulation carries its validity on its conditions, and may have several.
    The widest window wins: an order in force from one date to another under
    one condition and open-ended under another is, in practice, open-ended,
    and telling a rider it had expired would be the worse mistake.
    """
    starts, ends = [], []
    open_ended = False
    for condition in _as_list(regulation.get("condition")):
        if not isinstance(condition, dict):
            continue
        validity = condition.get("timeValidity")
        if not isinstance(validity, dict):
            continue
        if validity.get("start"):
            starts.append(str(validity["start"])[:10])
        if validity.get("end"):
            ends.append(str(validity["end"])[:10])
        else:
            open_ended = True
    return (min(starts) if starts else None,
            None if open_ended or not ends else max(ends))


def is_placeholder(regulation):
    """A placeholder is a slot reserved for an order, not an order.

    The schema carries `isPlaceholderTro` and it means the authority has
    registered that something will be made here. Drawing it as a live
    restriction tells a rider a road is shut when it is not.
    """
    for condition in _as_list(regulation.get("condition")):
        if not isinstance(condition, dict):
            continue
        validity = condition.get("timeValidity")
        if isinstance(validity, dict) and validity.get("isPlaceholderTro"):
            return True
    return False


def kind_of(regulation):
    """(code, label) for a regulation, or (None, None) if not worth keeping."""
    general = regulation.get("generalRegulation")
    if isinstance(general, dict):
        code = general.get("regulationType")
        if code in INTERESTING:
            return code, INTERESTING[code]
        return None, None
    speed = regulation.get(SPEED_LIMIT)
    if isinstance(speed, dict):
        mph = speed.get("mphValue")
        if mph is None:
            return None, None
        return SPEED_LIMIT, "%s mph" % mph
    return None, None


# How far ahead an order is worth carrying.
#
# MEASURED on the national corpus: of 36,821 live restrictions, 17,511 have not
# started yet — and they run out to 2036. An order beginning next week is worth
# knowing about and one beginning in 2028 is not, in a pack that is rebuilt
# every day and will carry it long before it matters.
#
# Ninety days keeps a whole riding season's planned closures and drops the
# long tail. It is a horizon, not a filter on relevance: everything inside it
# still carries its own start date, so the app can say "from Monday" rather
# than drawing it as though the road were shut today.
HORIZON_DAYS = 90


def not_yet(start, today, horizon_days=HORIZON_DAYS):
    """Whether an order starts so far ahead it is not worth carrying today."""
    if start is None or horizon_days is None:
        return False
    limit = (datetime.date.fromisoformat(today)
             + datetime.timedelta(days=horizon_days)).isoformat()
    return start > limit


def expired(end, today):
    """Whether an order with this end date is over.

    `end` is the last day it applies. An order ending today is still on today,
    which is the boundary that decides whether a rider is told about the
    closure they are riding towards this afternoon.
    """
    return end is not None and end < today


def worth_hydrating(event, today, horizon_days=HORIZON_DAYS):
    """Whether a change event is worth fetching the full order for.

    THE CHEAPEST REQUEST IS THE ONE NOT MADE. An event carries the order's
    regulation types and end dates but NOT its geometry, so keeping up with
    the feed means one `GET /dtros/{id}` per changed order — and measured on
    14 September 2026, 1,118 orders changed in a single day. The service
    rate-limits and does not say where the limit is, so a filter that can
    reject an order from the event alone is worth more than any pacing.

    Rejects, without a request:
      * orders whose every regulation type is one we do not carry — the
        kerbside parking that dominates the corpus;
      * orders whose every end date has passed;
      * orders that do not start for longer than the horizon.

    Keeps anything it cannot rule out. An event with no type at all is a
    maybe, and a maybe is fetched: missing a real closure to save a request
    is the wrong way round.
    """
    if not isinstance(event, dict):
        return False
    if event.get("eventType") == "delete":
        return True  # nothing to fetch, but the caller must act on it

    types = event.get("regulationType")
    if isinstance(types, str):
        types = [types]
    if isinstance(types, list) and types:
        known = [t for t in types if isinstance(t, str)]
        if known and not any(t in INTERESTING or t == SPEED_LIMIT
                             for t in known):
            return False

    ends = event.get("regulationEnd")
    if isinstance(ends, str):
        ends = [ends]
    if isinstance(ends, list) and ends:
        dates = [str(e)[:10] for e in ends if e]
        if dates and all(expired(d, today) for d in dates):
            return False

    starts = event.get("regulationStart")
    if isinstance(starts, str):
        starts = [starts]
    if isinstance(starts, list) and starts:
        dates = [str(s)[:10] for s in starts if s]
        if dates and all(not_yet(d, today, horizon_days) for d in dates):
            return False

    return True


def features(record, today=None, keep_expired=False,
             horizon_days=HORIZON_DAYS):
    """Every restriction in one D-TRO record, as flat dictionaries.

    One feature per (provision, regulation, place). A single order routinely
    covers several stretches of road under one regulation, and each stretch is
    a separate thing to draw and a separate thing to be inside.

    Geometry is left in National Grid here, as `SRID=...;WKT` exactly as
    published. Converting is the caller's job — this module has no opinion
    about projections and no dependency on the one that does.
    """
    today = today or datetime.date.today().isoformat()
    out = []
    if not isinstance(record, dict):
        return out
    source = record.get("source")
    if not isinstance(source, dict):
        return out

    order_name = source.get("troName")
    tra = source.get("currentTraOwner") or source.get("traCreator")
    reference = source.get("reference")

    for provision in _as_list(source.get("provision")):
        if not isinstance(provision, dict):
            continue
        places = [p for p in _as_list(provision.get("regulatedPlace"))
                  if isinstance(p, dict)]
        for regulation in _as_list(provision.get("regulation")):
            if not isinstance(regulation, dict):
                continue
            if is_placeholder(regulation):
                continue
            code, label = kind_of(regulation)
            if code is None:
                continue
            start, end = _validity(regulation)
            if not keep_expired and expired(end, today):
                continue
            if not keep_expired and not_yet(start, today, horizon_days):
                continue

            for place in places:
                # The location the order applies to, NOT the diversion.
                #
                # A diversion route is where traffic is sent instead, and it
                # is often longer than the closure itself. Drawing it in the
                # same colour would put a "road closed" line along a road that
                # is open — the exact opposite of what it means.
                if place.get("type") != "regulationLocation":
                    continue
                geometry = None
                for key in ("linearGeometry", "pointGeometry",
                            "polygonGeometry"):
                    blob = place.get(key)
                    if isinstance(blob, dict):
                        geometry = (blob.get("linestring") or blob.get("point")
                                    or blob.get("polygon"))
                        if geometry:
                            break
                if not geometry:
                    continue

                out.append({
                    "ref": reference,
                    "name": order_name,
                    "tra": tra,
                    "code": code,
                    "label": label,
                    "start": start,
                    "end": end,
                    "where": place.get("description"),
                    "wkt": geometry,
                })
    return out

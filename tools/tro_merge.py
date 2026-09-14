#!/usr/bin/env python3
"""Apply a day's changes to the set of orders already published.

The published pack IS the state. There is no separate database to keep in step
with it, nothing to go stale in a CI cache, and nothing that can disagree with
what riders actually have — the build reads the pack it published last time,
applies what has changed since, and publishes the result.

WHY THERE HAS TO BE A MERGE AT ALL
----------------------------------
`GET /dtros/all` is a snapshot, and it is not cut daily: measured on 14
September 2026, it served an extract dated the 6th, morning and afternoon. A
pack built from the extract alone is as old as the extract, so "a closure made
this morning" would reach a rider a week late — which is the whole failure this
feature exists to prevent.

`/events` is live. Measured on that same day: 1,189 events over 1,124 orders in
twenty-four hours.

Everything here is a pure function of its arguments, so the awkward parts —
an order that shrinks from three stretches to one, an order deleted, an order
that quietly expires between runs — are tested without a network or a key.
"""


def index_by_order(features):
    """Published features grouped by the order they came from."""
    out = {}
    for feature in features:
        key = (feature.get("properties") or {}).get("dtro")
        if key is None:
            # From before ids were carried, or hand-made. Kept under its own
            # identity so a merge never silently drops it.
            key = "_" + (feature.get("properties") or {}).get("tro_uid", "?")
        out.setdefault(key, []).append(feature)
    return out


def merge(published, changed, deleted=(), still_valid=None):
    """The new feature set.

    published    features from the pack published last time
    changed      {dtro_id: [features]} freshly built for orders that changed.
                 An EMPTY list is meaningful: the order changed and now
                 contributes nothing — expired, or amended to something we do
                 not carry — so everything it used to contribute must go.
    deleted      order ids the feed reported as deleted
    still_valid  optional predicate; anything it rejects is dropped. This is
                 how orders that merely EXPIRED since the last run leave the
                 pack, which no event will ever announce.

    Returns features sorted by their stable id, so an unchanged day rebuilds
    to identical bytes.
    """
    by_order = index_by_order(published)

    for order_id, features in changed.items():
        # Replace WHOLESALE, never feature by feature. An amended order
        # routinely covers a different number of stretches than it did, and a
        # merge that only overwrites what it recognises leaves the stretches
        # that were removed on the map for ever.
        if features:
            by_order[order_id] = features
        else:
            by_order.pop(order_id, None)

    # DELETIONS LAST, so that an order reported both changed and deleted in
    # the same window ends up gone.
    #
    # The window can hold both, and nothing in the arguments says which came
    # first — a caller that knows the event times should resolve it before
    # calling. Where it cannot, this is the safe way round: continuing to
    # publish an order the authority has removed is a worse mistake than
    # dropping one that is about to come back, because the pack is rebuilt
    # within hours and a re-created order returns on its own.
    for order_id in deleted:
        by_order.pop(order_id, None)

    out = []
    for features in by_order.values():
        for feature in features:
            if still_valid is None or still_valid(feature):
                out.append(feature)

    out.sort(key=lambda f: ((f.get("properties") or {}).get("tro_uid") or ""))
    return out


def is_current(feature, today, horizon_days=90):
    """Whether a published feature still belongs in the pack today.

    Orders leave the pack in three ways. Two are announced — deleted, or
    amended into something we do not carry — and the third is not: an order
    simply reaches its end date and stops applying. Nothing in the feed says
    so, so every run has to re-check what it is already carrying. Without this
    the pack only ever grows, and last month's roadworks stay on the map.
    """
    import datetime

    properties = feature.get("properties") or {}
    end = properties.get("end")
    if end is not None and str(end)[:10] < today:
        return False
    start = properties.get("start")
    if start is not None and horizon_days is not None:
        limit = (datetime.date.fromisoformat(today)
                 + datetime.timedelta(days=horizon_days)).isoformat()
        if str(start)[:10] > limit:
            return False
    return True

#!/usr/bin/env python3
"""Move the order pack between extract cuts, from D-TRO's live /events feed.

WHY THIS EXISTS
---------------
`/dtros/all` is a snapshot that DfT re-cuts on its own schedule, and it can
stop. Measured on 2 October 2026: it was still serving
`dtros_20260906_010013.csv`, twenty-six days old, while `/events` reported
1,832 events over 1,650 orders between 1 October 00:00 and 2 October 05:46.
Every run of traffic-orders.yml was green and every one republished the 6
September picture, because build_tro.py read the extract and nothing else -
dtro_events.py and tro_merge.py, written for exactly this, were never called.
Riders were shown "orders as of 6 Sep" for four weeks while the service held
fresh orders the whole time.

HOW IT WORKS
------------
The extract is the baseline. On top of it sits a DELTA: for every order the
feed has reported since the extract was cut, what that order contributes now -
its features, freshly built from `GET /dtros/{id}`, or an empty list when it was
deleted or no longer carries anything we draw. The build overlays the delta on
the extract (tro_merge.merge replaces an order WHOLESALE), filters against the
day the delta reaches, and stamps the pack with that day.

The delta is kept between runs in dist/tro/_events (the workflow caches it),
so each run only fetches what changed since the last one. A new extract cut
discards it: the orders it held are in the new extract.

WHY THE DELTA HOLDS FEATURES UNFILTERED. An order amended to start in five
months is outside the 90-day horizon today and inside it in two months. If the
delta kept only what passed today's filter, that order would be lost the day
it was amended and the extract's OLD version would come back once it entered
the horizon. So the delta keeps everything an order contributes, and the
horizon and expiry are applied to the overlay on every build.

WHAT IT IS HONEST ABOUT. The pack's `generated` is the day the delta reaches -
the newest moment whose events have all been applied - not the day the build
ran. A catch-up that runs out of time stops at a window boundary and says so;
the pack is then dated by where it stopped, never by today.
"""
import datetime
import json
import os
import re
import time
import urllib.error

from tro import INTERESTING, SPEED_LIMIT

# One request window. The feed pages by ORDER (dtro_events.py), and a day is
# about 1,700 of them - thirty-odd pages - so a window is a minute or two of
# paging and a crash or a spent budget never loses more than one.
WINDOW = datetime.timedelta(days=1)

# How far behind "now" a run stops. An event published a second before the
# request may not be visible to it yet; stopping short and picking it up next
# run costs nothing, and claiming a moment we have not seen costs an order.
SETTLE = datetime.timedelta(minutes=5)

# How far back a fetch is dated. An order fetched at 10:00 is known as it
# stood at 10:00, so a later window's event at 09:00 needs no second fetch -
# but only if our clock and the service's agree, so the fetch is recorded as
# a little earlier than it was. An event inside the margin is fetched again,
# which costs one request and can never lose a change.
CLOCK_MARGIN = datetime.timedelta(minutes=10)

# How long one run may spend catching up, at most, before it publishes what it
# has. A full catch-up over a stalled extract is tens of minutes (measured: a
# day's window pages in ~50 s, and each order fetch is ~0.1 s); the job has an
# hour. Checked before every order fetch, and every fetch is kept, so even a
# run that stops mid-window has made progress the next one starts from.
BUDGET_SECONDS = 35 * 60

STATE_NAME = "delta.json"

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_CUT_NAME = re.compile(r"(\d{4})(\d{2})(\d{2})[_T-]?(\d{2})(\d{2})(\d{2})")
_CUT_DAY = re.compile(r"(\d{4})(\d{2})(\d{2})")


def parse_stamp(text):
    """An ISO UTC timestamp as an aware datetime."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc)


def stamp(moment):
    return moment.astimezone(datetime.timezone.utc).strftime(_STAMP)


def cut_moment(path):
    """When the extract was cut, from the service's own filename, or None.

    `dtros_20260906_010013.csv` -> 2026-09-06T01:00:13Z. A name carrying only
    a date is taken as that midnight, which re-applies a few hours of events
    the extract already holds - harmless, every event is idempotent here. A
    name with no date at all (a hand-made CSV) gives None: there is no moment
    to read the feed from.
    """
    name = os.path.basename(path)
    match = _CUT_NAME.search(name)
    if match:
        y, mo, d, h, mi, s = match.groups()
        return "%s-%s-%sT%s:%s:%sZ" % (y, mo, d, h, mi, s)
    match = _CUT_DAY.search(name)
    if match:
        return "%s-%s-%sT00:00:00Z" % match.groups()
    return None


# ---------------------------------------------------------------------------
# The delta, between runs
# ---------------------------------------------------------------------------

def fresh_state(extract, since):
    return {"extract": extract, "through": since, "orders": {}, "fetched": {}}


def load_state(path, extract):
    """The saved delta for THIS extract, or None.

    A delta saved against a different extract is discarded, not merged: it
    describes changes since a cut the build is no longer reading.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if (not isinstance(state, dict) or state.get("extract") != extract
            or not isinstance(state.get("through"), str)
            or not isinstance(state.get("orders"), dict)):
        return None
    try:
        parse_stamp(state["through"])
    except ValueError:
        return None
    if not isinstance(state.get("fetched"), dict):
        state["fetched"] = {}
    return state


def save_state(path, state):
    """Written beside and renamed, so a killed run never leaves half a file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    part = path + ".part"
    with open(part, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, separators=(",", ":"), sort_keys=True)
    os.replace(part, path)


# ---------------------------------------------------------------------------
# Applying the feed
# ---------------------------------------------------------------------------

def might_carry(event):
    """Whether an event's order could contribute anything we draw.

    Events name the order's regulation types, so most of the feed - parking
    bays, loading places, waiting restrictions - can be passed over without
    fetching the order. Anything the event does not describe is fetched.
    """
    types = event.get("regulationType")
    if not isinstance(types, list) or not types:
        return True
    for kind in types:
        if isinstance(kind, str) and (kind in INTERESTING
                                      or kind == SPEED_LIMIT
                                      or kind.startswith("speedLimit")):
            return True
    return False


def latest_by_order(events):
    """{order id: its last event}, by eventTime; a delete wins a tie.

    A tie goes to the delete for tro_merge's reason: publishing an order the
    authority has removed is worse than dropping one that comes straight back.
    """
    out = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        order = event.get("id")
        if not isinstance(order, str) or not order:
            continue
        key = (str(event.get("eventTime") or ""),
               event.get("eventType") == "delete")
        have = out.get(order)
        if have is None or key >= have[0]:
            out[order] = (key, event)
    return dict((order, pair[1]) for order, pair in out.items())


def catch_up(state, until, fetch, hydrate, build, carried=frozenset(),
             budget_seconds=BUDGET_SECONDS, clock=time.monotonic,
             on_window=None, now=None):
    """Apply the feed from state["through"] up to `until`, a window at a time.

    fetch(since, to)    the events between two timestamps (dtro_events.changes)
    hydrate(order)      the order's current record, or None when it has gone
    build(record, id)   every feature the record contributes, UNFILTERED
    carried             ids the extract already draws something for: always
                        fetched on any event, whatever the event says
    on_window(state)    called after each window, to save progress
    now()               the wall clock, for dating fetches (tests)

    EACH ORDER IS FETCHED ONCE PER CHANGE, NOT ONCE PER WINDOW. A fetch
    returns the order as it stands NOW, so an order fetched while catching up
    the 8th is already current for its events on the 20th. state["fetched"]
    records when each order was fetched, and an event no newer than that is
    already in hand. The service rate-limits at roughly a hundred requests a
    minute (measured 2 October 2026), so on a four-week catch-up this is the
    difference between hours and a fraction of them.

    Mutates `state` and returns statistics about the run. `through` only ever
    moves to the end of a window that was applied in full.

    A WINDOW THAT FAILS STOPS THE CATCH-UP, IT DOES NOT FAIL THE BUILD. The
    service rate-limits without saying how hard (dtro_events.py), and a fetch
    can die after its retries. What has been applied is still true, so the
    pack publishes as far as it got - dated by that, not by today - and
    `failed` says why it stopped. Orders already re-fetched inside the failed
    window keep their newer state; the window is read again next run.
    """
    orders = state["orders"]
    fetched = state.setdefault("fetched", {})
    if now is None:
        def now():
            return datetime.datetime.now(datetime.timezone.utc)
    stats = {"windows": 0, "events": 0, "orders": 0, "hydrated": 0,
             "passed_over": 0, "deleted": 0, "gone": 0, "newest": None,
             "already_current": 0,
             "stopped_early": False, "failed": None}
    started = clock()

    def out_of_time():
        return (budget_seconds is not None
                and clock() - started > budget_seconds)

    while True:
        since = parse_stamp(state["through"])
        if since >= until:
            break
        if out_of_time():
            stats["stopped_early"] = True
            break
        to = min(since + WINDOW, until)
        try:
            _apply_window(orders, fetched, fetch(stamp(since), stamp(to)),
                          hydrate, build, carried, stats, now, out_of_time)
        except _OutOfTime:
            # Mid-window: `through` stays where it was, but every order
            # fetched so far is kept and dated in `fetched`, so the next run
            # re-reads this window and fetches only what is left of it. A
            # window too big for one run - a council bulk-loading its orders
            # - therefore still finishes, over several, instead of running
            # every job into its timeout for ever.
            stats["stopped_early"] = True
            if on_window is not None:
                on_window(state)
            break
        except (OSError, ValueError, KeyError) as e:
            stats["failed"] = "%s .. %s: %s: %s" % (
                stamp(since), stamp(to), type(e).__name__, e)
            break
        state["through"] = stamp(to)
        stats["windows"] += 1
        if on_window is not None:
            on_window(state)
    return stats


def _moment(text):
    try:
        return parse_stamp(str(text))
    except ValueError:
        return None


class _OutOfTime(Exception):
    pass


def _apply_window(orders, fetched, events, hydrate, build, carried, stats,
                  now, out_of_time=lambda: False):
    """Lay one window's events into `orders`."""
    latest = latest_by_order(events)
    stats["events"] += len(events)
    stats["orders"] += len(latest)
    for order in sorted(latest):
        event = latest[order]
        when = event.get("eventTime")
        if isinstance(when, str) and when > (stats["newest"] or ""):
            stats["newest"] = when
        happened = _moment(when)
        known = _moment(fetched.get(order)) if order in fetched else None
        # Already in hand: fetched after this event happened. An event with
        # no readable time is never assumed to be.
        if happened is not None and known is not None and happened <= known:
            stats["already_current"] += 1
            continue
        if event.get("eventType") == "delete":
            orders[order] = []
            if happened is not None:
                fetched[order] = stamp(happened)
            stats["deleted"] += 1
            continue
        if (order not in orders and order not in carried
                and not might_carry(event)):
            stats["passed_over"] += 1
            continue
        if out_of_time():
            raise _OutOfTime()
        asked = now() - CLOCK_MARGIN
        record = hydrate(order)
        fetched[order] = stamp(asked)
        stats["hydrated"] += 1
        if record is None:
            orders[order] = []
            stats["gone"] += 1
        else:
            orders[order] = build(record, order)


# ---------------------------------------------------------------------------
# Where the events come from
# ---------------------------------------------------------------------------

class LiveFeed(object):
    """The production service, with a token that is renewed before it lapses.

    A catch-up can outlast one token, so it is renewed on age and, once, on a
    401. Nothing here prints a secret.
    """

    RENEW_AFTER = 20 * 60

    # A pause between order fetches, to stay UNDER the service's limit rather
    # than leaning on it. MEASURED on 2 October 2026: back to back (~10 a
    # second) and at a quarter-second apart alike, a day's window of about a
    # thousand fetches drew 36-59 429s and settled at roughly a hundred
    # requests a minute either way - each 429 a five-second wait. At this pace
    # the throughput is the same and the service is not hammered.
    PAUSE_BETWEEN_ORDERS = 0.6

    def __init__(self, env):
        import dtro_events
        from dtro_token import token
        self._events = dtro_events
        self._token = token
        self.base = env.get("DTRO_BASE_URL", "").strip()
        self._id = env["DTRO_CLIENT_ID"].strip()
        self._secret = env["DTRO_CLIENT_SECRET"].strip()
        self._access = None
        self._at = 0.0

    def _fresh(self, force=False):
        if force or self._access is None or \
                time.monotonic() - self._at > self.RENEW_AFTER:
            body = self._token(self.base, self._id, self._secret)
            self._access = body["access_token"]
            self._at = time.monotonic()
        return self._access

    def _call(self, fn):
        try:
            return fn(self._fresh())
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            return fn(self._fresh(force=True))

    def fetch(self, since, to):
        return self._call(
            lambda access: self._events.changes(self.base, access, since, to))

    def hydrate(self, order):
        time.sleep(self.PAUSE_BETWEEN_ORDERS)
        return self._call(
            lambda access: self._events.hydrate(self.base, access, order))


class ReplayFeed(object):
    """A recorded feed, for offline builds and tests.

    {"events": [...], "records": {order id: record or null}} - events are
    served by eventTime, `since <= eventTime < to`.
    """

    def __init__(self, path):
        with open(path, encoding="utf-8") as handle:
            body = json.load(handle)
        self.events = list(body.get("events") or [])
        self.records = dict(body.get("records") or {})

    def fetch(self, since, to):
        low, high = parse_stamp(since), parse_stamp(to)
        out = []
        for event in self.events:
            try:
                when = parse_stamp(str(event.get("eventTime")))
            except ValueError:
                continue
            if low <= when < high:
                out.append(event)
        return out

    def hydrate(self, order):
        return self.records.get(order)

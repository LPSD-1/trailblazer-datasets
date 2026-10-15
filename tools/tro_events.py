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
import sys
import time
import urllib.error

import dtro_events
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

# ONE BAD ORDER MUST NOT STALL THE FEED. From 8 to 13 October 2026, 17 runs in
# a row were green and applied nothing: one order's `GET /dtros/{id}` answered
# HTTP 400 every time, and a failed fetch ended the whole catch-up at its
# window's start. A REFUSAL - the service saying no to THIS data request
# (dtro_events.Refused: 400 or 422 from the order's or the window's own
# request, never the token's), or a 200 that is not an order or a page
# (dtro_events.Malformed) - is now recorded and stepped past once the service
# has answered something else (catch_up, _confirm): the order keeps what it
# had, the window applies, and the order is fetched again on later runs.
# Anything else (a token error, a 5xx, a timeout, a 429 past its back-off) is
# the service being unwell, and still stops the window so it is read again
# whole next run.

# How often a skipped order or window is tried in all, counting the first
# time, before it is given up on and reported. Four runs a day: two days.
MAX_TRIES = 8

# Refusals one window may skip. More than this is not a bad order, it is the
# service refusing everything, and skipping it all would date the pack past
# events it never applied.
MAX_REFUSED_PER_WINDOW = 10

# Refused /events windows the delta may hold at once. A window skipped is a
# day of changes not applied while the pack is dated past it, so few are
# allowed and every one is reported at once (report()).
MAX_SKIPPED_WINDOWS = 2

# Runs in a row that applied no window because the catch-up failed, before
# report() says the feed is stuck. Three is under a day.
STALL_RUNS = 3

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
    return {"extract": extract, "through": since, "orders": {}, "fetched": {},
            "skipped": {}, "skipped_windows": []}


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
    if not isinstance(state.get("skipped"), dict):
        state["skipped"] = {}
    if not isinstance(state.get("skipped_windows"), list):
        state["skipped_windows"] = []
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
    state.setdefault("fetched", {})
    state.setdefault("skipped", {})
    state.setdefault("skipped_windows", [])
    if now is None:
        def now():
            return datetime.datetime.now(datetime.timezone.utc)
    stats = {"windows": 0, "events": 0, "orders": 0, "hydrated": 0,
             "passed_over": 0, "deleted": 0, "gone": 0, "newest": None,
             "already_current": 0, "skipped": 0, "skipped_windows": 0,
             "recovered": 0, "recovered_windows": 0, "noted": [],
             "stopped_early": False, "failed": None}
    started = clock()
    # Whether the service answered a request of each kind this run. A
    # refusal is one bad order or window only beside an answer (_confirm).
    answered = {"fetch": False, "hydrate": False}
    # Only what EARLIER runs skipped is retried below; what this run refuses
    # has had its try.
    earlier_windows = list(state["skipped_windows"])
    earlier_orders = set(state["skipped"])

    def out_of_time():
        return (budget_seconds is not None
                and clock() - started > budget_seconds)

    def apply(events):
        _apply_window(state, events, hydrate, build, carried, stats, now,
                      answered, out_of_time)

    ahead = None  # (since, events) of a window read ahead, see below
    while True:
        since = parse_stamp(state["through"])
        if since >= until:
            break
        if out_of_time():
            stats["stopped_early"] = True
            break
        to = min(since + WINDOW, until)
        try:
            if ahead is not None and ahead[0] == stamp(since):
                events = ahead[1]
            else:
                events = fetch(stamp(since), stamp(to))
                answered["fetch"] = True
            ahead = None
        except (OSError, ValueError, KeyError) as e:
            # A REFUSED WINDOW IS SKIPPED ONLY WHEN THE NEXT ONE ANSWERS.
            # That is the one way `through` passes a window not applied in
            # full: recorded, retried, and reported at once. /events refusing
            # everything - or a refusal that is not from /events at all -
            # stops here as any failure does.
            nxt = None
            if (refused(e) and to < until and len(state["skipped_windows"])
                    < MAX_SKIPPED_WINDOWS):
                nxt = _read(fetch, to, min(to + WINDOW, until))
            if nxt is None:
                stats["failed"] = "%s .. %s: %s" % (stamp(since), stamp(to),
                                                    _why(e))
                break
            answered["fetch"] = True
            state["skipped_windows"].append(
                _note({"since": stamp(since), "to": stamp(to)}, e, now))
            stats["skipped_windows"] += 1
            state["through"] = stamp(to)
            ahead = (stamp(to), nxt)
            if on_window is not None:
                on_window(state)
            continue
        try:
            apply(events)
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
            stats["failed"] = "%s .. %s: %s" % (stamp(since), stamp(to),
                                                _why(e))
            break
        state["through"] = stamp(to)
        stats["windows"] += 1
        if on_window is not None:
            on_window(state)
    # WHAT EARLIER RUNS SKIPPED is tried again once this run has caught up,
    # MAX_TRIES times in all, and never on a run that failed: an outage must
    # not spend the tries of what it did not cause. Runs that stop at the
    # same place are counted in state["stall"] (_count_stall), which
    # report() turns into an alarm.
    if stats["failed"] is None and not stats["stopped_early"]:
        _retry_skipped(state, fetch, hydrate, build, stats, now, out_of_time,
                       apply, on_window, answered, earlier_windows,
                       earlier_orders - set(stats["noted"]))
    _count_stall(state, stats, now)
    return stats


def _read(fetch, since, to):
    """A window's events, or None when it cannot be read."""
    try:
        return fetch(stamp(since), stamp(to))
    except (OSError, ValueError, KeyError):
        return None


def refused(error):
    """Whether a failure is the service refusing ONE data request - which is
    skipped and recorded - rather than the service, or our credentials, being
    unwell, which stops the catch-up. Only dtro_events says so, and only for
    the request for that order or window (dtro_events.REFUSED_CODES); a 400
    from the token endpoint is a plain HTTPError and is never skipped."""
    return isinstance(error, (dtro_events.Refused, dtro_events.Malformed))


def _why(error):
    """One line naming a failure. Never a response body: str(HTTPError) is
    its code and reason only."""
    return " ".join(("%s: %s" % (type(error).__name__, error)).split())


def _note(entry, error, now):
    """Count one more refused try against a skipped order or window."""
    moment = stamp(now())
    entry["tries"] = int(entry.get("tries") or 0) + 1
    entry["error"] = _why(error)
    entry.setdefault("first", moment)
    entry["last"] = moment
    return entry


def _given_up(entry):
    return int(entry.get("tries") or 0) >= MAX_TRIES


def _retry_skipped(state, fetch, hydrate, build, stats, now, out_of_time,
                   apply, on_window, answered, earlier_windows,
                   earlier_orders):
    """Try once more what earlier runs skipped, within the budget.

    Anything that is not a refusal ends the pass without counting a try: the
    service is unwell, and the next run will try again.
    """
    windows = state["skipped_windows"]
    for entry in earlier_windows:
        if entry not in windows:
            continue
        if not isinstance(entry, dict) or "since" not in entry \
                or "to" not in entry:
            windows.remove(entry)
            continue
        if _given_up(entry):
            continue
        if out_of_time():
            stats["stopped_early"] = True
            return
        try:
            events = fetch(entry["since"], entry["to"])
        except (OSError, ValueError, KeyError) as e:
            if not (refused(e) and answered["fetch"]):
                return
            _note(entry, e, now)
            continue
        answered["fetch"] = True
        try:
            apply(events)
        except _OutOfTime:
            stats["stopped_early"] = True
            return
        except _TooManyRefused as e:
            _note(entry, e, now)  # a window that never applies runs out too
            continue
        except (OSError, ValueError, KeyError):
            return
        windows.remove(entry)
        stats["recovered_windows"] += 1
        if on_window is not None:
            on_window(state)
    skipped = state["skipped"]
    refusals = []
    for order in sorted(o for o in earlier_orders if o in skipped):
        entry = skipped[order]
        if isinstance(entry, dict) and _given_up(entry):
            continue
        if out_of_time():
            stats["stopped_early"] = True
            break
        try:
            error = _fetch_order(state, order, hydrate, build, stats, now,
                                 answered)
        except (OSError, ValueError, KeyError):
            return
        if error is None:
            stats["recovered"] += 1
        else:
            refusals.append((order, error))
    if refusals:
        try:
            _confirm(state, refusals, hydrate, build, stats, now, answered)
        except (OSError, ValueError, KeyError):
            return
        _skip(state, refusals, stats, now)


def _count_stall(state, stats, now):
    """Count runs that ended at the same place because something failed."""
    if not stats["failed"]:
        state.pop("stall", None)
        return
    stall = state.get("stall")
    if isinstance(stall, dict) and stall.get("at") == state["through"]:
        stall["runs"] = int(stall.get("runs") or 0) + 1
    else:
        stall = {"at": state["through"], "runs": 1, "first": stamp(now())}
    stall["error"] = stats["failed"]
    state["stall"] = stall


def _moment(text):
    try:
        return parse_stamp(str(text))
    except ValueError:
        return None


class _OutOfTime(Exception):
    pass


class _TooManyRefused(OSError):
    pass


class _ServiceRefuses(OSError):
    pass


def _fetch_order(state, order, hydrate, build, stats, now, answered):
    """Fetch one order and lay it into the delta.

    Returns None when it was applied, or the refusal when the service
    refused it - the order is then left exactly as it was, never emptied and
    never dated as fetched, and nothing is recorded yet (_skip). Any other
    failure is raised.
    """
    asked = now() - CLOCK_MARGIN
    try:
        record = hydrate(order)
    except (OSError, ValueError) as e:
        if not refused(e):
            raise
        return e
    answered["hydrate"] = True
    state["fetched"][order] = stamp(asked)
    state["skipped"].pop(order, None)
    stats["hydrated"] += 1
    if record is None:
        state["orders"][order] = []
        stats["gone"] += 1
    else:
        state["orders"][order] = build(record, order)
    return None


def _confirm(state, refusals, hydrate, build, stats, now, answered):
    """Raise _ServiceRefuses unless the service answers some other order.

    Nothing answered this run yet: ask for the order it answered most
    recently, which is a real fetch and applied like one. No such order, or
    that one refused too, means the service and not these orders.
    """
    if answered["hydrate"]:
        return
    refused_now = set(order for order, _ in refusals)
    known = sorted((when, order) for order, when in state["fetched"].items()
                   if isinstance(when, str) and order not in refused_now
                   and order not in state["skipped"])
    last = refusals[-1]
    if not known:
        raise _ServiceRefuses("%s refused (%s), and no other order to check "
                              "the service with" % (last[0], _why(last[1])))
    order = known[-1][1]
    error = _fetch_order(state, order, hydrate, build, stats, now, answered)
    if error is not None:
        raise _ServiceRefuses("%s refused, and %s too (%s)"
                              % (last[0], order, _why(error)))


def _skip(state, refusals, stats, now):
    """Record confirmed refusals in state["skipped"]."""
    for order, error in refusals:
        entry = state["skipped"].get(order)
        state["skipped"][order] = _note(
            entry if isinstance(entry, dict) else {}, error, now)
        stats["skipped"] += 1
        stats["noted"].append(order)


def _apply_window(state, events, hydrate, build, carried, stats, now,
                  answered, out_of_time=lambda: False):
    """Lay one window's events into the delta.

    More than MAX_REFUSED_PER_WINDOW refusals in one window, or refusals the
    service will not answer anything else beside (_confirm), are the service
    and not an order: the window fails and nothing is counted against them.
    """
    orders = state["orders"]
    fetched = state["fetched"]
    latest = latest_by_order(events)
    stats["events"] += len(events)
    stats["orders"] += len(latest)
    refusals = []
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
            state["skipped"].pop(order, None)
            stats["deleted"] += 1
            continue
        if (order not in orders and order not in carried
                and not might_carry(event)):
            stats["passed_over"] += 1
            continue
        if out_of_time():
            raise _OutOfTime()
        error = _fetch_order(state, order, hydrate, build, stats, now,
                             answered)
        if error is not None:
            refusals.append((order, error))
            if len(refusals) > MAX_REFUSED_PER_WINDOW:
                raise _TooManyRefused(
                    "%d orders refused in one window, the last %s"
                    % (len(refusals), _why(error)))
    if refusals:
        _confirm(state, refusals, hydrate, build, stats, now, answered)
        _skip(state, refusals, stats, now)


def report(state):
    """The delta's health, as key=value lines for traffic-orders.yml.

    stuck=true when the feed has failed at the same place for STALL_RUNS runs,
    when any /events window was skipped (a day of changes not applied), or
    when any order was given up on after MAX_TRIES. One line per key, always.
    """
    stall = state.get("stall")
    stall = stall if isinstance(stall, dict) else {}
    runs = int(stall.get("runs") or 0)
    skipped = state.get("skipped")
    skipped = dict((order, entry) for order, entry in
                   (skipped.items() if isinstance(skipped, dict) else [])
                   if isinstance(entry, dict))
    windows = state.get("skipped_windows")
    windows = [w for w in (windows if isinstance(windows, list) else [])
               if isinstance(w, dict)]
    lost = sorted(o for o, entry in skipped.items() if _given_up(entry))
    trying = sorted(o for o, entry in skipped.items() if not _given_up(entry))
    lost_windows = [w for w in windows if _given_up(w)]
    trying_windows = [w for w in windows if not _given_up(w)]
    stuck = runs >= STALL_RUNS or bool(windows) or bool(lost)

    def one_line(value):
        return " ".join(str(value).split())

    def spans(ws):
        return one_line(",".join("%s..%s" % (w.get("since"), w.get("to"))
                                 for w in ws))

    return [
        "stuck=%s" % ("true" if stuck else "false"),
        "through=%s" % one_line(state.get("through") or ""),
        "stalled_runs=%d" % runs,
        "error=%s" % one_line(stall.get("error") or ""),
        "skipped_orders=%d" % len(skipped),
        "gave_up_orders=%d" % len(lost),
        "gave_up=%s" % one_line(",".join(lost[:20])),
        "retrying_orders=%d" % len(trying),
        "retrying=%s" % one_line(",".join(trying[:20])),
        "skipped_windows=%d" % len(windows),
        "gave_up_windows=%d" % len(lost_windows),
        "windows_given_up=%s" % spans(lost_windows),
        "retrying_windows=%d" % len(trying_windows),
        "windows_retrying=%s" % spans(trying_windows),
    ]


def main(argv):
    """`--report DELTA`: print report() for the workflow's alarm step.

    Always exits 0. A delta that cannot be read prints stuck=unknown, which
    the step neither raises nor stands down on.
    """
    if len(argv) != 2 or argv[0] != "--report":
        print("usage: tro_events.py --report DELTA_JSON", file=sys.stderr)
        return 0
    try:
        with open(argv[1], encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = None
    if not isinstance(state, dict):
        print("stuck=unknown")
        return 0
    for line in report(state):
        print(line)
    return 0


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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

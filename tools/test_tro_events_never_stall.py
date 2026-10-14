#!/usr/bin/env python3
"""One bad order or window never stalls the D-TRO /events catch-up.

    python tools/test_tro_events_never_stall.py

Exit 0: pass. Exit 1: the defect.

THE LIVE STATE THIS WAS FOUND IN. From 8 to 13 October 2026, 17 traffic-orders
runs in a row were green and each one applied nothing: every run fetched the
window from 2026-10-08T00:11:02Z, hydrated a few orders, then one order's
`GET /dtros/{id}` answered HTTP 400 and catch_up ended the whole catch-up at
the window's start. `through` never moved, the pack stayed dated 8 Oct, and the
stale alarm only rings after 7 days. It recovered overnight on the 13th and
recurred on the 14th (runs 38575305268 and 38617592693: "0 window(s) ...
fetched 2 (0 gone) ... HTTP Error 400").

WHAT THIS PINS:

  * a refusal (HTTP 400 or 422, or a 200 whose body is not an order) for ONE
    order is recorded in state["skipped"], the order keeps what it had, and
    the rest of the window applies;
  * a refused /events window is recorded in state["skipped_windows"] and the
    catch-up moves past it - at most MAX_SKIPPED_WINDOWS of them;
  * both are retried on later runs, MAX_TRIES times in all, and applied when
    the service answers;
  * a service that refuses everything is the service, not an order: the
    window stops as before, so the pack's date is never bought by skipping;
  * a stall (no window applied, run after run) is counted in state["stall"];
    `tro_events.py --report` says stuck=true from STALL_RUNS runs, from any
    skipped window, or from anything given up on, and traffic-orders.yml
    opens one issue for it;
  * dtro_events.hydrate never reads a malformed 200 as "the order has gone".
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import dtro_events  # noqa: E402
import tro_events as te  # noqa: E402
from test_tro_cut_age_alarm import BASH, run_block  # noqa: E402

EXTRACT = "dtros_20261006_010013.csv"
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "traffic-orders.yml")
STEP = "Say so when the events feed is stuck"
LABEL = "traffic-orders-feed"
START = "2026-10-08T00:00:00Z"


def event(order, when, kind="update"):
    return {"id": order, "eventType": kind, "eventTime": when,
            "regulationType": ["miscRoadClosure"]}


def refusal(code=400, order="p"):
    """An HTTP error as urllib raises it - from ANY request, the token's
    included. Never a refusal on its own (see no())."""
    return urllib.error.HTTPError(
        "https://example.invalid/dtros/%s" % order, code,
        "Bad Request" if code == 400 else "Error", {}, None)


def no(what="GET /dtros/p", code=400):
    """The service refusing one data request, as dtro_events reports it."""
    return dtro_events.Refused(code, what)


def build(record, order):
    return [{"properties": {"dtro": order, "v": record.get("v")}}]


class Feed(object):
    """Events by time; records by order; `refuse` maps an order to the
    exception its fetch raises, and `refuse_windows` a window start to the
    exception /events raises for it."""

    def __init__(self, events=(), records=None, refuse=None,
                 refuse_windows=None):
        self.events = list(events)
        self.records = dict(records or {})
        self.refuse = dict(refuse or {})
        self.refuse_windows = dict(refuse_windows or {})
        self.fetches = []
        self.hydrates = []

    def fetch(self, since, to):
        self.fetches.append((since, to))
        if since in self.refuse_windows:
            raise self.refuse_windows[since]
        low, high = te.parse_stamp(since), te.parse_stamp(to)
        return [e for e in self.events
                if low <= te.parse_stamp(e["eventTime"]) < high]

    def hydrate(self, order):
        self.hydrates.append(order)
        if order in self.refuse:
            raise self.refuse[order]
        return self.records.get(order)


def run(state, feed, until, at="2026-10-14T18:00:00Z"):
    moment = te.parse_stamp(at)
    return te.catch_up(state, te.parse_stamp(until), feed.fetch,
                       feed.hydrate, build, now=lambda: moment)


def report(state):
    return dict(line.split("=", 1) for line in te.report(state))


class OneRefusedOrder(unittest.TestCase):
    """The 8-13 October stall: one order's GET answered 400."""

    def setUp(self):
        self.state = te.fresh_state(EXTRACT, START)
        self.events = [event("a", "2026-10-08T10:00:00Z"),
                       event("p", "2026-10-08T11:00:00Z"),
                       event("z", "2026-10-08T12:00:00Z"),
                       event("b", "2026-10-09T10:00:00Z")]
        self.records = {"a": {"v": 1}, "p": {"v": 1}, "z": {"v": 1},
                        "b": {"v": 1}}

    def test_a_refused_order_does_not_hold_up_its_window(self):
        feed = Feed(self.events, self.records, refuse={"p": no()})
        stats = run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertIsNone(stats["failed"])
        self.assertEqual(self.state["through"], "2026-10-10T00:00:00Z",
                         "one order's 400 held the whole catch-up back")
        self.assertEqual(stats["windows"], 2)
        for order in ("a", "z", "b"):
            self.assertEqual(self.state["orders"][order],
                             build({"v": 1}, order))
        self.assertNotIn("p", self.state["orders"])

    def test_the_refused_order_keeps_what_it_had_and_is_recorded(self):
        self.state["orders"]["p"] = [{"properties": {"dtro": "p", "v": 0}}]
        self.state["fetched"]["p"] = "2026-10-07T00:00:00Z"
        feed = Feed(self.events, self.records, refuse={"p": no()})
        stats = run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(self.state["orders"]["p"],
                         [{"properties": {"dtro": "p", "v": 0}}])
        # Not dated as fetched, or its next event would read as in hand.
        self.assertEqual(self.state["fetched"]["p"], "2026-10-07T00:00:00Z")
        skipped = self.state["skipped"]["p"]
        self.assertEqual(skipped["tries"], 1)
        self.assertIn("400", skipped["error"])
        self.assertEqual(stats["skipped"], 1)

    def test_a_422_is_a_refusal_too(self):
        feed = Feed(self.events, self.records, refuse={"p": no(code=422)})
        run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(self.state["through"], "2026-10-10T00:00:00Z")
        self.assertIn("p", self.state["skipped"])

    def test_it_is_retried_next_run_and_applied_when_it_answers(self):
        feed = Feed(self.events, self.records, refuse={"p": no()})
        run(self.state, feed, "2026-10-10T00:00:00Z")
        later = Feed(self.events, dict(self.records, p={"v": 2}))
        stats = run(self.state, later, "2026-10-10T06:00:00Z",
                    at="2026-10-14T20:00:00Z")
        self.assertEqual(later.hydrates, ["p"],
                         "the next run must retry the skipped order, and "
                         "only it")
        self.assertEqual(self.state["orders"]["p"], build({"v": 2}, "p"))
        self.assertEqual(self.state["skipped"], {})
        self.assertEqual(self.state["fetched"]["p"], "2026-10-14T19:50:00Z")
        self.assertEqual(stats["recovered"], 1)

    def test_an_order_that_has_gone_by_the_retry_is_emptied(self):
        self.state["orders"]["p"] = [{"properties": {"dtro": "p", "v": 0}}]
        run(self.state, Feed(self.events, self.records,
                             refuse={"p": no()}),
            "2026-10-10T00:00:00Z")
        later = Feed([], {})
        run(self.state, later, "2026-10-10T06:00:00Z")
        self.assertEqual(self.state["orders"]["p"], [])
        self.assertEqual(self.state["skipped"], {})

    def test_retries_are_bounded_and_then_reported(self):
        until = te.parse_stamp("2026-10-10T00:00:00Z")
        calls = 0
        for n in range(te.MAX_TRIES + 4):
            feed = Feed(self.events, self.records,
                        refuse={"p": no()})
            te.catch_up(self.state, until, feed.fetch, feed.hydrate, build)
            calls += feed.hydrates.count("p")
            until += te.datetime.timedelta(hours=6)
        self.assertEqual(calls, te.MAX_TRIES,
                         "a skipped order is retried a bounded number of "
                         "times, not for ever")
        self.assertEqual(self.state["skipped"]["p"]["tries"], te.MAX_TRIES)
        got = report(self.state)
        self.assertEqual(got["stuck"], "true")
        self.assertEqual(got["gave_up_orders"], "1")
        self.assertIn("p", got["gave_up"])

    def test_a_new_event_for_a_skipped_order_is_tried_and_clears_it(self):
        feed = Feed(self.events, self.records, refuse={"p": no()})
        run(self.state, feed, "2026-10-10T00:00:00Z")
        self.state["skipped"]["p"]["tries"] = te.MAX_TRIES  # given up on
        later = Feed([event("p", "2026-10-10T03:00:00Z")],
                     dict(self.records, p={"v": 3}))
        run(self.state, later, "2026-10-11T00:00:00Z")
        self.assertEqual(later.hydrates, ["p"])
        self.assertEqual(self.state["orders"]["p"], build({"v": 3}, "p"))
        self.assertEqual(self.state["skipped"], {})

    def test_a_delete_for_a_skipped_order_settles_it(self):
        feed = Feed(self.events, self.records, refuse={"p": no()})
        run(self.state, feed, "2026-10-10T00:00:00Z")
        self.state["skipped"]["p"]["tries"] = te.MAX_TRIES
        later = Feed([event("p", "2026-10-10T03:00:00Z", "delete")], {})
        run(self.state, later, "2026-10-11T00:00:00Z")
        self.assertEqual(self.state["orders"]["p"], [])
        self.assertEqual(self.state["skipped"], {})

    def test_a_server_error_still_stops_the_window(self):
        # A 500, a timeout, a 429 past its back-off: the service is unwell,
        # not the order. Skipping would date the pack past events it never
        # applied; stopping re-reads the window next run.
        feed = Feed(self.events, self.records, refuse={"p": refusal(500)})
        stats = run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(self.state["through"], START)
        self.assertIn("500", stats["failed"])
        self.assertEqual(self.state["skipped"], {})

    def test_a_timeout_still_stops_the_window(self):
        feed = Feed(self.events, self.records,
                    refuse={"p": OSError("timed out")})
        stats = run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(self.state["through"], START)
        self.assertIn("timed out", stats["failed"])

    def test_a_delta_saved_before_this_change_is_caught_up_too(self):
        # The cached delta.json of every run before this one has no
        # `skipped` or `skipped_windows`; catch_up is handed it as it is.
        state = {"extract": EXTRACT, "through": START, "orders": {},
                 "fetched": {}}
        feed = Feed(self.events, self.records, refuse={"p": no()})
        run(state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(state["through"], "2026-10-10T00:00:00Z")
        self.assertIn("p", state["skipped"])
        self.assertEqual(state["skipped_windows"], [])

    def test_a_failure_is_reported_on_one_line(self):
        # build_tro.py prints `failed` inside a ::warning:: workflow
        # command, which ends at the first newline.
        def broken(record, order):
            raise ValueError("first line\nsecond line")
        state = te.fresh_state(EXTRACT, START)
        feed = Feed(self.events, self.records)
        stats = te.catch_up(state, te.parse_stamp("2026-10-10T00:00:00Z"),
                            feed.fetch, feed.hydrate, broken)
        self.assertIn("first line second line", stats["failed"])
        self.assertNotIn("\n", stats["failed"])
        self.assertNotIn("\n", state["stall"]["error"])

    def test_the_order_refused_now_is_not_the_one_checked_against(self):
        # p was the order last answered, and p is the one refused now: the
        # service is asked about g instead, which answers.
        self.state["fetched"].update({"g": "2026-10-01T00:00:00Z",
                                      "p": "2026-10-07T00:00:00Z"})
        feed = Feed([event("p", "2026-10-08T11:00:00Z")], {"g": {"v": 1}},
                    refuse={"p": no()})
        stats = run(self.state, feed, "2026-10-09T00:00:00Z")
        self.assertIsNone(stats["failed"])
        self.assertEqual(feed.hydrates, ["p", "g"])
        self.assertIn("p", self.state["skipped"])
        self.assertNotIn("stall", self.state)

    def test_one_counted_try_per_run(self):
        self.state["skipped"]["p"] = {"tries": 1, "error": "x"}
        feed = Feed(self.events, self.records, refuse={"p": no()})
        run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(feed.hydrates.count("p"), 1)
        self.assertEqual(self.state["skipped"]["p"]["tries"], 2)

    def test_a_refusal_beside_an_answer_needs_no_check(self):
        feed = Feed(self.events, self.records, refuse={"p": no()})
        run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(feed.hydrates, ["a", "p", "z", "b"])

    def test_a_window_retry_with_nothing_else_read_counts_no_try(self):
        state = te.fresh_state(EXTRACT, "2026-10-10T00:00:00Z")
        state["skipped_windows"] = [
            {"since": START, "to": "2026-10-09T00:00:00Z", "tries": 1}]
        feed = Feed(refuse_windows={START: no("POST /events")})
        run(state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(feed.fetches, [(START, "2026-10-09T00:00:00Z")])
        self.assertEqual(state["skipped_windows"][0]["tries"], 1)

    def test_a_service_refusing_every_order_stops_the_window(self):
        self.state["fetched"]["good"] = "2026-10-07T00:00:00Z"
        orders = ["o%02d" % i for i in range(te.MAX_REFUSED_PER_WINDOW + 1)]
        events = [event(o, "2026-10-08T10:00:00Z") for o in orders]
        feed = Feed(events, {"good": {"v": 1}},
                    refuse=dict((o, no("GET /dtros/" + o)) for o in orders))
        stats = run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(self.state["through"], START,
                         "a refusal of everything is the service, and "
                         "skipping it would date the pack falsely")
        self.assertIn("400", stats["failed"])
        self.assertEqual(self.state["skipped"], {},
                         "nothing is counted against an order when the "
                         "window as a whole failed")

    def test_up_to_the_limit_of_refusals_the_window_still_applies(self):
        self.state["fetched"]["good"] = "2026-10-07T00:00:00Z"
        orders = ["o%02d" % i for i in range(te.MAX_REFUSED_PER_WINDOW)]
        events = [event(o, "2026-10-08T10:00:00Z") for o in orders]
        feed = Feed(events, {"good": {"v": 1}},
                    refuse=dict((o, no("GET /dtros/" + o)) for o in orders))
        stats = run(self.state, feed, "2026-10-09T00:00:00Z")
        self.assertIsNone(stats["failed"])
        self.assertEqual(self.state["through"], "2026-10-09T00:00:00Z")
        self.assertEqual(len(self.state["skipped"]),
                         te.MAX_REFUSED_PER_WINDOW)

    # S1: a refusal is one bad order only when the service, this run,
    # answered some other order. Otherwise it is the service.

    def test_a_lone_refused_order_with_nothing_to_check_against_stops(self):
        feed = Feed([event("p", "2026-10-08T11:00:00Z")], {},
                    refuse={"p": no()})
        stats = run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(self.state["through"], START)
        self.assertIn("p", stats["failed"])
        self.assertEqual(self.state["skipped"], {})

    def test_a_lone_refused_order_is_checked_against_a_known_one(self):
        # 14 Oct: every other order in the window was already current, so
        # the poison order was the only fetch. The service is asked for the
        # order it last answered; it answers, so p alone is refused.
        self.state["fetched"].update({"old": "2026-10-01T00:00:00Z",
                                      "new": "2026-10-07T00:00:00Z"})
        feed = Feed([event("p", "2026-10-08T11:00:00Z")],
                    {"new": {"v": 5}, "old": {"v": 4}}, refuse={"p": no()})
        stats = run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertIsNone(stats["failed"])
        self.assertEqual(feed.hydrates, ["p", "new"])
        self.assertEqual(self.state["orders"]["new"], build({"v": 5}, "new"))
        self.assertEqual(self.state["skipped"]["p"]["tries"], 1)
        self.assertEqual(self.state["through"], "2026-10-10T00:00:00Z")

    def test_when_the_known_order_is_refused_too_it_is_the_service(self):
        self.state["fetched"]["new"] = "2026-10-07T00:00:00Z"
        feed = Feed([event("p", "2026-10-08T11:00:00Z")], {},
                    refuse={"p": no(), "new": no("GET /dtros/new")})
        stats = run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(self.state["through"], START)
        self.assertIn("new", stats["failed"])
        self.assertEqual(self.state["skipped"], {})

    def test_a_skipped_order_is_never_the_one_checked_against(self):
        self.state["fetched"]["q"] = "2026-10-07T00:00:00Z"
        self.state["skipped"]["q"] = {"tries": te.MAX_TRIES, "error": "x"}
        feed = Feed([event("p", "2026-10-08T11:00:00Z")], {"q": {"v": 1}},
                    refuse={"p": no()})
        stats = run(self.state, feed, "2026-10-08T12:00:00Z")
        self.assertEqual(feed.hydrates, ["p"])
        self.assertEqual(self.state["through"], START)
        self.assertIsNotNone(stats["failed"])


class OneRefusedWindow(unittest.TestCase):
    def setUp(self):
        self.state = te.fresh_state(EXTRACT, START)
        self.events = [event("a", "2026-10-08T10:00:00Z"),
                       event("w", "2026-10-09T10:00:00Z"),
                       event("c", "2026-10-10T10:00:00Z")]
        self.records = {"a": {"v": 1}, "w": {"v": 1}, "c": {"v": 1}}

    def test_a_refused_window_is_skipped_and_the_rest_applied(self):
        feed = Feed(self.events, self.records,
                    refuse_windows={"2026-10-09T00:00:00Z": no("POST /events")})
        stats = run(self.state, feed, "2026-10-11T00:00:00Z")
        self.assertIsNone(stats["failed"])
        self.assertEqual(self.state["through"], "2026-10-11T00:00:00Z")
        self.assertEqual(sorted(self.state["orders"]), ["a", "c"])
        self.assertEqual(len(self.state["skipped_windows"]), 1)
        skipped = self.state["skipped_windows"][0]
        self.assertEqual((skipped["since"], skipped["to"], skipped["tries"]),
                         ("2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z", 1))
        self.assertIn("400", skipped["error"])
        self.assertEqual(stats["skipped_windows"], 1)

    def test_a_skipped_window_is_reported_at_once(self):
        feed = Feed(self.events, self.records,
                    refuse_windows={"2026-10-09T00:00:00Z": no("POST /events")})
        run(self.state, feed, "2026-10-11T00:00:00Z")
        got = report(self.state)
        self.assertEqual(got["stuck"], "true")
        self.assertEqual(got["skipped_windows"], "1")

    def test_the_skipped_window_is_read_again_next_run(self):
        run(self.state, Feed(self.events, self.records, refuse_windows={
            "2026-10-09T00:00:00Z": no("POST /events")}), "2026-10-11T00:00:00Z")
        later = Feed(self.events, self.records)
        stats = run(self.state, later, "2026-10-11T06:00:00Z")
        self.assertIn(("2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z"),
                      later.fetches)
        self.assertEqual(sorted(self.state["orders"]), ["a", "c", "w"])
        self.assertEqual(self.state["skipped_windows"], [])
        self.assertEqual(stats["recovered_windows"], 1)
        self.assertEqual(report(self.state)["stuck"], "false")

    def test_a_skipped_window_is_retried_a_bounded_number_of_times(self):
        until = te.parse_stamp("2026-10-11T00:00:00Z")
        tries = 0
        for _ in range(te.MAX_TRIES + 3):
            feed = Feed(self.events, self.records, refuse_windows={
                "2026-10-09T00:00:00Z": no("POST /events")})
            te.catch_up(self.state, until, feed.fetch, feed.hydrate, build)
            tries += sum(1 for w in feed.fetches
                         if w[0] == "2026-10-09T00:00:00Z")
            until += te.datetime.timedelta(hours=6)
        self.assertEqual(tries, te.MAX_TRIES)
        self.assertEqual(report(self.state)["gave_up_windows"], "1")

    def test_skipping_windows_is_capped(self):
        refuse = dict(("2026-10-%02dT00:00:00Z" % d, no("POST /events"))
                      for d in (9, 11, 13, 15))
        feed = Feed([], {}, refuse_windows=refuse)
        stats = run(self.state, feed, "2026-10-17T00:00:00Z")
        self.assertEqual(len(self.state["skipped_windows"]),
                         te.MAX_SKIPPED_WINDOWS)
        self.assertIn("400", stats["failed"])
        self.assertEqual(self.state["through"], "2026-10-13T00:00:00Z")

    def test_an_events_feed_refusing_everything_is_not_skipped(self):
        # S1: /events answering 400 to every window is the service. Nothing
        # is skipped and `through` does not move.
        refuse = dict(("2026-10-%02dT00:00:00Z" % d, no("POST /events"))
                      for d in range(8, 20))
        feed = Feed([], {}, refuse_windows=refuse)
        stats = run(self.state, feed, "2026-10-19T00:00:00Z")
        self.assertEqual(self.state["skipped_windows"], [])
        self.assertEqual(self.state["through"], START)
        self.assertIn("400", stats["failed"])

    def test_a_refused_first_window_is_skipped_when_the_next_answers(self):
        feed = Feed(self.events, self.records,
                    refuse_windows={START: no("POST /events")})
        stats = run(self.state, feed, "2026-10-11T00:00:00Z")
        self.assertIsNone(stats["failed"])
        self.assertEqual([w["since"] for w in self.state["skipped_windows"]],
                         [START])
        self.assertEqual(sorted(self.state["orders"]), ["c", "w"])
        self.assertEqual(self.state["through"], "2026-10-11T00:00:00Z")
        self.assertEqual(feed.fetches.count(("2026-10-09T00:00:00Z",
                                             "2026-10-10T00:00:00Z")), 1,
                         "the window read ahead is applied, not read twice")

    def test_a_refused_last_window_waits_for_one_after_it(self):
        feed = Feed(self.events, self.records, refuse_windows={
            "2026-10-10T00:00:00Z": no("POST /events")})
        stats = run(self.state, feed, "2026-10-11T00:00:00Z")
        self.assertEqual(self.state["through"], "2026-10-10T00:00:00Z")
        self.assertEqual(self.state["skipped_windows"], [])
        self.assertIn("400", stats["failed"])

    def test_a_retried_window_whose_orders_are_all_refused_counts_a_try(self):
        orders = ["o%02d" % i for i in range(te.MAX_REFUSED_PER_WINDOW + 1)]
        events = [event(o, "2026-10-09T10:00:00Z") for o in orders]
        run(self.state, Feed(events, {}, refuse_windows={
            "2026-10-09T00:00:00Z": no("POST /events")}),
            "2026-10-11T00:00:00Z")
        self.assertEqual(self.state["skipped_windows"][0]["tries"], 1)
        feed = Feed(events, {}, refuse=dict(
            (o, no("GET /dtros/" + o)) for o in orders))
        run(self.state, feed, "2026-10-11T06:00:00Z")
        self.assertEqual(self.state["skipped_windows"][0]["tries"], 2,
                         "a window that can never apply must still run "
                         "out of tries")

    def test_a_window_that_times_out_still_stops(self):
        feed = Feed(self.events, self.records, refuse_windows={
            "2026-10-09T00:00:00Z": OSError("timed out")})
        stats = run(self.state, feed, "2026-10-11T00:00:00Z")
        self.assertEqual(self.state["through"], "2026-10-09T00:00:00Z")
        self.assertEqual(self.state["skipped_windows"], [])
        self.assertIn("timed out", stats["failed"])


class AStallIsCounted(unittest.TestCase):
    def stalled_runs(self, runs):
        state = te.fresh_state(EXTRACT, START)
        for _ in range(runs):
            feed = Feed(refuse_windows={START: OSError("timed out")})
            run(state, feed, "2026-10-10T00:00:00Z")
        return state

    def test_one_failed_run_is_not_yet_a_stall(self):
        state = self.stalled_runs(te.STALL_RUNS - 1)
        got = report(state)
        self.assertEqual(got["stuck"], "false")
        self.assertEqual(got["stalled_runs"], str(te.STALL_RUNS - 1))

    def test_a_stall_that_persists_across_runs_is_reported(self):
        state = self.stalled_runs(te.STALL_RUNS)
        got = report(state)
        self.assertEqual(got["stuck"], "true",
                         "17 green runs applied nothing and nobody was told")
        self.assertEqual(got["stalled_runs"], str(te.STALL_RUNS))
        self.assertEqual(got["through"], START)
        self.assertIn("timed out", got["error"])
        self.assertEqual(state["stall"]["at"], START)

    def test_a_run_that_moves_clears_it(self):
        state = self.stalled_runs(te.STALL_RUNS)
        run(state, Feed(), "2026-10-10T00:00:00Z")
        self.assertNotIn("stall", state)
        self.assertEqual(report(state)["stuck"], "false")

    def test_a_failure_further_on_starts_the_count_again(self):
        state = self.stalled_runs(te.STALL_RUNS)
        feed = Feed(refuse_windows={"2026-10-09T00:00:00Z":
                                    OSError("timed out")})
        run(state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(state["stall"]["runs"], 1)
        self.assertEqual(state["stall"]["at"], "2026-10-09T00:00:00Z")

    def test_the_delta_keeps_the_count_between_runs(self):
        tmp = tempfile.mkdtemp(prefix="tro-stall-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "delta.json")
        state = self.stalled_runs(2)
        state["skipped"]["p"] = {"tries": 1, "error": "x"}
        te.save_state(path, state)
        self.assertEqual(te.load_state(path, EXTRACT), state)

    def test_an_old_delta_without_the_new_fields_still_loads(self):
        tmp = tempfile.mkdtemp(prefix="tro-stall-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "delta.json")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"extract": EXTRACT, "through": START, "orders": {},
                       "fetched": {}, "skipped": "junk",
                       "skipped_windows": {"x": 1}}, fh)
        state = te.load_state(path, EXTRACT)
        self.assertEqual(state["skipped"], {})
        self.assertEqual(state["skipped_windows"], [])
        self.assertEqual(report(state)["stuck"], "false")


class TheReportCommand(unittest.TestCase):
    def cli(self, path):
        done = subprocess.run(
            [sys.executable, os.path.join(HERE, "tro_events.py"), "--report",
             path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True)
        return done.returncode, done.stdout.splitlines()

    def test_it_prints_the_report_and_never_fails(self):
        tmp = tempfile.mkdtemp(prefix="tro-report-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "delta.json")
        state = te.fresh_state(EXTRACT, START)
        state["stall"] = {"at": START, "runs": te.STALL_RUNS,
                          "error": "a\nb"}
        te.save_state(path, state)
        code, lines = self.cli(path)
        self.assertEqual(code, 0, lines)
        self.assertIn("stuck=true", lines)
        self.assertIn("through=%s" % START, lines)
        self.assertIn("error=a b", lines, "one line per key, always")

    def test_no_delta_is_unknown_not_fine(self):
        code, lines = self.cli(os.path.join(HERE, "no-such-delta.json"))
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines, ["stuck=unknown"])


class FakeResponse(object):
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class AMalformed200IsNotADeletion(unittest.TestCase):
    """dtro_events.hydrate: only 404 and 410 mean the order has gone."""

    def answer(self, body=None, error=None):
        def urlopen(request, timeout=None):
            if error is not None:
                raise error
            return FakeResponse(body)
        real = dtro_events.urllib.request.urlopen
        dtro_events.urllib.request.urlopen = urlopen
        self.addCleanup(setattr, dtro_events.urllib.request, "urlopen", real)
        backoff = dtro_events.BACKOFF_SECONDS
        dtro_events.BACKOFF_SECONDS = []  # a 429 is past its back-off
        self.addCleanup(setattr, dtro_events, "BACKOFF_SECONDS", backoff)

    def hydrate(self):
        return dtro_events.hydrate("https://example.invalid", "t", "p")

    def test_a_list_body_raises(self):
        self.answer(b"[]")
        with self.assertRaises(ValueError):
            self.hydrate()

    def test_a_string_body_raises(self):
        self.answer(b'"ok"')
        with self.assertRaises(ValueError):
            self.hydrate()

    def test_a_dict_that_is_not_an_order_raises(self):
        self.answer(b'{"message": "try later"}')
        with self.assertRaises(ValueError):
            self.hydrate()

    def test_a_body_that_is_not_json_raises_the_same(self):
        self.answer(b"<html>")
        with self.assertRaises(dtro_events.Malformed):
            self.hydrate()

    def test_the_wrapped_record_is_unwrapped(self):
        self.answer(b'{"data": {"source": {"x": 1}}}')
        self.assertEqual(self.hydrate(), {"source": {"x": 1}})

    def test_an_unwrapped_record_is_taken_whole(self):
        self.answer(b'{"source": {"x": 1}}')
        self.assertEqual(self.hydrate(), {"source": {"x": 1}})

    def test_404_and_410_are_gone(self):
        for code in (404, 410):
            self.answer(error=refusal(code))
            self.assertIsNone(self.hydrate())

    def test_a_400_or_422_is_a_refusal_not_gone(self):
        for code in (400, 422):
            self.answer(error=refusal(code))
            with self.assertRaises(dtro_events.Refused) as raised:
                self.hydrate()
            self.assertEqual(raised.exception.code, code)

    def test_no_other_status_is_a_refusal(self):
        for code in (401, 403, 405, 408, 409, 429, 500, 502, 503):
            self.answer(error=refusal(code))
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.hydrate()
            self.assertEqual(raised.exception.code, code)
            self.assertFalse(te.refused(raised.exception), code)

    def test_shapes_that_are_not_an_order_raise(self):
        # {"data": {}} used to come back as {} - an order with nothing in
        # it, which emptied the order from the pack: a closure removed.
        for body in (b'{"data": null}', b'{"data": []}', b'{"data": {}}',
                     b'{"data": {"x": 1}}', b'{"data": {"source": null}}',
                     b'{"source": []}', b"null", b"\xff\xfe\x00"):
            self.answer(body)
            with self.assertRaises(dtro_events.Malformed, msg=body):
                self.hydrate()

    def test_in_the_catch_up_the_order_keeps_what_it_had(self):
        def urlopen(request, timeout=None):
            if request.full_url.endswith("/g"):
                return FakeResponse(b'{"data": {"source": {}}}')
            return FakeResponse(b"[]")
        real = dtro_events.urllib.request.urlopen
        dtro_events.urllib.request.urlopen = urlopen
        self.addCleanup(setattr, dtro_events.urllib.request, "urlopen", real)
        state = te.fresh_state(EXTRACT, START)
        state["fetched"]["g"] = "2026-10-07T00:00:00Z"
        old = [{"properties": {"dtro": "p", "v": 0}}]
        state["orders"]["p"] = list(old)
        feed = Feed([event("p", "2026-10-08T10:00:00Z")])
        stats = te.catch_up(
            state, te.parse_stamp("2026-10-09T00:00:00Z"), feed.fetch,
            lambda order: dtro_events.hydrate("https://example.invalid", "t",
                                              order), build)
        self.assertEqual(state["orders"]["p"], old,
                         "a malformed 200 deleted the order from the pack")
        self.assertEqual(stats["gone"], 0)
        self.assertIn("p", state["skipped"])
        self.assertEqual(state["through"], "2026-10-09T00:00:00Z")


BASE = "https://example.invalid"


class Service(object):
    """A stand-in for D-TRO behind the real dtro_events: urlopen is patched,
    so dtro_events.changes and dtro_events.hydrate run for real.

    window_errors   {window start: exception} for POST /events
    order_errors    {order id: exception} for GET /dtros/{id}
    """

    def __init__(self, events=(), window_errors=None, order_errors=None):
        self.events = list(events)
        self.window_errors = dict(window_errors or {})
        self.order_errors = dict(order_errors or {})
        self.calls = []

    def urlopen(self, request, timeout=None):
        url = request.full_url
        if url.endswith("/events"):
            asked = json.loads(request.data.decode())
            self.calls.append(("events", asked["since"]))
            if asked["since"] in self.window_errors:
                raise self.window_errors[asked["since"]]
            found = [e for e in self.events
                     if asked["since"] <= e["eventTime"] < asked["to"]]
            return FakeResponse(json.dumps({"events": found}).encode())
        order = url.rsplit("/", 1)[1]
        self.calls.append(("order", order))
        if order in self.order_errors:
            raise self.order_errors[order]
        return FakeResponse(json.dumps(
            {"data": {"source": {"o": order}}}).encode())

    def install(self, case):
        real = dtro_events.urllib.request.urlopen
        dtro_events.urllib.request.urlopen = self.urlopen
        case.addCleanup(setattr, dtro_events.urllib.request, "urlopen", real)
        backoff = dtro_events.BACKOFF_SECONDS
        dtro_events.BACKOFF_SECONDS = []  # a 429 is past its back-off
        case.addCleanup(setattr, dtro_events, "BACKOFF_SECONDS", backoff)

    def catch_up(self, state, until):
        return te.catch_up(
            state, te.parse_stamp(until),
            lambda since, to: dtro_events.changes(BASE, "t", since, to),
            lambda order: dtro_events.hydrate(BASE, "t", order), build,
            now=lambda: te.parse_stamp("2026-10-14T18:00:00Z"))


NOT_REFUSALS = (401, 403, 405, 408, 409, 429, 500, 502, 503, 504)


class OnlyTheDataRequestsRefusalIsSkipped(unittest.TestCase):
    """T2 and S1, through the real dtro_events: only 400 or 422 from the
    request for that order or that window is skipped. Every other status, and
    anything from the token endpoint, stops the catch-up as before."""

    def setUp(self):
        self.events = [event("a", "2026-10-08T10:00:00Z"),
                       event("p", "2026-10-08T11:00:00Z"),
                       event("c", "2026-10-09T10:00:00Z")]

    def state(self):
        return te.fresh_state(EXTRACT, START)

    def test_an_order_answering_400_or_422_is_skipped(self):
        for code in (400, 422):
            service = Service(self.events, order_errors={"p": refusal(code)})
            service.install(self)
            state = self.state()
            stats = service.catch_up(state, "2026-10-10T00:00:00Z")
            self.assertIsNone(stats["failed"], code)
            self.assertIn("p", state["skipped"])
            self.assertEqual(state["through"], "2026-10-10T00:00:00Z")

    def test_no_other_status_on_an_order_is_skipped(self):
        for code in NOT_REFUSALS:
            service = Service(self.events, order_errors={"p": refusal(code)})
            service.install(self)
            state = self.state()
            stats = service.catch_up(state, "2026-10-10T00:00:00Z")
            self.assertEqual(state["through"], START, code)
            self.assertEqual(state["skipped"], {}, code)
            self.assertIn(str(code), stats["failed"])

    def test_gone_is_applied_not_skipped(self):
        for code in (404, 410):
            service = Service(self.events, order_errors={"p": refusal(code)})
            service.install(self)
            state = self.state()
            stats = service.catch_up(state, "2026-10-10T00:00:00Z")
            self.assertEqual(state["orders"]["p"], [], code)
            self.assertEqual(state["skipped"], {})
            self.assertEqual(stats["gone"], 1)

    def test_a_window_answering_400_or_422_is_skipped(self):
        for code in (400, 422):
            service = Service(self.events, window_errors={
                "2026-10-08T00:00:00Z": refusal(code)})
            service.install(self)
            state = self.state()
            stats = service.catch_up(state, "2026-10-10T00:00:00Z")
            self.assertIsNone(stats["failed"], code)
            self.assertEqual(len(state["skipped_windows"]), 1)
            self.assertIn("HTTP %d" % code,
                          state["skipped_windows"][0]["error"])

    def test_no_other_status_on_a_window_is_skipped(self):
        for code in NOT_REFUSALS:
            service = Service(self.events, window_errors={
                "2026-10-08T00:00:00Z": refusal(code)})
            service.install(self)
            state = self.state()
            stats = service.catch_up(state, "2026-10-10T00:00:00Z")
            self.assertEqual(state["through"], START, code)
            self.assertEqual(state["skipped_windows"], [], code)
            self.assertIn(str(code), stats["failed"])

    def test_404_on_events_is_the_documented_empty_window(self):
        service = Service(self.events, window_errors={
            "2026-10-08T00:00:00Z": refusal(404)})
        service.install(self)
        state = self.state()
        stats = service.catch_up(state, "2026-10-10T00:00:00Z")
        self.assertIsNone(stats["failed"])
        self.assertEqual(stats["windows"], 2)
        self.assertEqual(state["skipped_windows"], [])
        self.assertEqual(sorted(state["orders"]), ["c"])

    def live(self, token):
        feed = te.LiveFeed({"DTRO_BASE_URL": BASE, "DTRO_CLIENT_ID": "id",
                            "DTRO_CLIENT_SECRET": "secret"})
        feed._token = token
        feed.PAUSE_BETWEEN_ORDERS = 0
        return feed

    @staticmethod
    def token_refusal():
        return urllib.error.HTTPError(BASE + "/oauth-generator", 400,
                                      "Bad Request", {}, None)

    def test_a_token_refusal_on_the_events_request_stops(self):
        # S1: the token endpoint answering 400 is not /events refusing a
        # window; skipping would move `through` past changes never applied.
        service = Service(self.events)
        service.install(self)

        def token(base, client, secret):
            raise self.token_refusal()

        feed = self.live(token)
        state = self.state()
        stats = te.catch_up(state, te.parse_stamp("2026-10-12T00:00:00Z"),
                            feed.fetch, feed.hydrate, build)
        self.assertEqual(state["through"], START)
        self.assertEqual(state["skipped_windows"], [])
        self.assertIn("400", stats["failed"])
        self.assertEqual(service.calls, [])

    def test_a_token_refusal_that_clears_still_stops_the_run(self):
        # The token endpoint answers 400 once and then recovers. Read as a
        # refused window, the window after would answer and the first be
        # skipped: changes never applied, `through` moved past them.
        service = Service(self.events)
        service.install(self)
        issued = []

        def token(base, client, secret):
            issued.append(1)
            if len(issued) == 1:
                raise self.token_refusal()
            return {"access_token": "t"}

        feed = self.live(token)
        state = self.state()
        stats = te.catch_up(state, te.parse_stamp("2026-10-11T00:00:00Z"),
                            feed.fetch, feed.hydrate, build)
        self.assertEqual(state["through"], START)
        self.assertEqual(state["skipped_windows"], [])
        self.assertIsNotNone(stats["failed"])

    def test_a_token_error_is_never_a_refusal(self):
        def token(base, client, secret):
            raise self.token_refusal()

        feed = self.live(token)
        for call in (lambda: feed.fetch(START, "2026-10-09T00:00:00Z"),
                     lambda: feed.hydrate("p")):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                call()
            self.assertNotIsInstance(raised.exception, dtro_events.Refused)
            self.assertFalse(te.refused(raised.exception))

    def test_a_token_refusal_on_an_order_fetch_stops(self):
        service = Service(self.events)
        service.install(self)
        issued = []

        def token(base, client, secret):
            issued.append(1)
            if len(issued) > 1:
                raise self.token_refusal()
            return {"access_token": "t"}

        feed = self.live(token)
        feed.RENEW_AFTER = -1  # renewed before every request
        state = self.state()
        state["fetched"]["g"] = "2026-10-07T00:00:00Z"
        stats = te.catch_up(state, te.parse_stamp("2026-10-12T00:00:00Z"),
                            feed.fetch, feed.hydrate, build)
        self.assertEqual(state["through"], START)
        self.assertEqual(state["skipped"], {})
        self.assertIn("400", stats["failed"])


class TheEventsFeedIsReadStrictly(unittest.TestCase):
    """A 200 from /events that is not a page of events is not 'nothing
    changed': the window would be applied empty and never read again."""

    def answer(self, body):
        def urlopen(request, timeout=None):
            return FakeResponse(body)
        real = dtro_events.urllib.request.urlopen
        dtro_events.urllib.request.urlopen = urlopen
        self.addCleanup(setattr, dtro_events.urllib.request, "urlopen", real)

    def changes(self):
        return dtro_events.changes(BASE, "t", START, "2026-10-09T00:00:00Z")

    def test_shapes_that_are_not_a_page_raise(self):
        for body in (b"[]", b"null", b'"ok"',
                     b'{"events": "none"}', b'{"events": {}}', b"<html>",
                     b"\xff\xfe"):
            self.answer(body)
            with self.assertRaises(dtro_events.Malformed, msg=body):
                self.changes()

    def test_a_dict_without_events_raises(self):
        # An error envelope or a re-cased key is not "nothing changed": read
        # as empty, `through` would pass the window and its changes be lost.
        for body in (b"{}", b'{"message": "later"}',
                     b'{"Events": [{"id": "a"}]}', b'{"EVENTS": []}'):
            with self.subTest(body=body):
                self.answer(body)
                with self.assertRaises(dtro_events.Malformed):
                    self.changes()

    def test_explicit_null_events_is_empty(self):
        # 404 is the documented empty answer; an explicit null `events` in a
        # 200 is the same answer in another shape.
        self.answer(b'{"events": null}')
        self.assertEqual(self.changes(), [])

    def test_an_empty_page_is_nothing_changed(self):
        self.answer(b'{"events": []}')
        self.assertEqual(self.changes(), [])

    def test_a_page_is_read(self):
        self.answer(b'{"events": [{"id": "a"}]}')
        self.assertEqual(self.changes(), [{"id": "a"}])


class RetriesKeepToTheBudget(unittest.TestCase):
    """T5: retries are checked against the run's time budget, one by one."""

    def setUp(self):
        self.state = te.fresh_state(EXTRACT, "2026-10-11T00:00:00Z")
        self.state["fetched"]["g"] = "2026-10-07T00:00:00Z"

    def go(self, feed, clock):
        return te.catch_up(self.state,
                           te.parse_stamp("2026-10-11T06:00:00Z"),
                           feed.fetch, feed.hydrate, build,
                           budget_seconds=10, clock=clock)

    def test_a_spent_budget_retries_no_window(self):
        self.state["skipped_windows"] = [
            {"since": "2026-10-08T00:00:00Z", "to": "2026-10-09T00:00:00Z",
             "tries": 1}]
        feed = Feed([], {})
        stats = self.go(feed, lambda: 1000 if feed.fetches else 0)
        self.assertEqual(feed.fetches,
                         [("2026-10-11T00:00:00Z", "2026-10-11T06:00:00Z")])
        self.assertTrue(stats["stopped_early"])
        self.assertEqual(self.state["skipped_windows"][0]["tries"], 1)

    def test_the_budget_is_checked_before_each_window(self):
        self.state["skipped_windows"] = [
            {"since": "2026-10-08T00:00:00Z", "to": "2026-10-09T00:00:00Z",
             "tries": 1},
            {"since": "2026-10-09T00:00:00Z", "to": "2026-10-10T00:00:00Z",
             "tries": 1}]
        feed = Feed([], {})
        stats = self.go(feed, lambda: 1000 if len(feed.fetches) > 1 else 0)
        self.assertEqual(len(feed.fetches), 2)
        self.assertEqual([w["since"] for w in self.state["skipped_windows"]],
                         ["2026-10-09T00:00:00Z"])
        self.assertTrue(stats["stopped_early"])

    def test_a_spent_budget_retries_no_order(self):
        self.state["skipped"]["p"] = {"tries": 1, "error": "x"}
        feed = Feed([], {"p": {"v": 1}})
        stats = self.go(feed, lambda: 1000 if feed.fetches else 0)
        self.assertEqual(feed.hydrates, [])
        self.assertTrue(stats["stopped_early"])
        self.assertEqual(self.state["skipped"]["p"]["tries"], 1)

    def test_the_budget_is_checked_before_each_order(self):
        self.state["skipped"]["p"] = {"tries": 1, "error": "x"}
        self.state["skipped"]["q"] = {"tries": 1, "error": "x"}
        feed = Feed([], {"p": {"v": 1}, "q": {"v": 1}})
        stats = self.go(feed, lambda: 1000 if feed.hydrates else 0)
        self.assertEqual(feed.hydrates, ["p"])
        self.assertEqual(sorted(self.state["skipped"]), ["q"])
        self.assertTrue(stats["stopped_early"])

    def test_a_failed_run_does_not_spend_tries(self):
        # The service unwell this run: nothing earlier runs skipped is tried
        # against it, so a long outage cannot exhaust their tries.
        self.state["skipped"]["p"] = {"tries": 1, "error": "x"}
        self.state["skipped_windows"] = [
            {"since": "2026-10-08T00:00:00Z", "to": "2026-10-09T00:00:00Z",
             "tries": 1}]
        feed = Feed(refuse_windows={"2026-10-11T00:00:00Z":
                                    OSError("timed out")})
        te.catch_up(self.state, te.parse_stamp("2026-10-11T06:00:00Z"),
                    feed.fetch, feed.hydrate, build)
        self.assertEqual(feed.hydrates, [])
        self.assertEqual(len(feed.fetches), 1)
        self.assertEqual(self.state["skipped"]["p"]["tries"], 1)
        self.assertEqual(self.state["skipped_windows"][0]["tries"], 1)


class TheBuildKeepsTheStall(unittest.TestCase):
    """T1: build_tro.apply_feed saves the delta after a run that applied
    nothing, so the stall count survives to the next run and the report."""

    def test_three_stalled_builds_are_reported_stuck(self):
        try:
            import build_tro
        except ImportError as e:  # pragma: no cover - CI installs its deps
            self.skipTest("BLIND: build_tro.py cannot be imported: %s" % e)
        tmp = tempfile.mkdtemp(prefix="tro-stall-build-")
        self.addCleanup(shutil.rmtree, tmp, True)
        extract = os.path.join(tmp, EXTRACT)
        with open(extract, "w", encoding="utf-8", newline="") as fh:
            fh.write("Id,SchemaVersion,Data\nx,3.4.0,{}\n")
        delta = os.path.join(tmp, "_events", te.STATE_NAME)
        feed = Feed(refuse_windows={
            "2026-10-06T01:00:13Z": OSError("timed out")})
        until = te.parse_stamp("2026-10-08T00:00:00Z")
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            for _ in range(te.STALL_RUNS):
                build_tro.apply_feed(extract, feed, delta, until, None)
        finally:
            sys.stdout = real
        done = subprocess.run(
            [sys.executable, os.path.join(HERE, "tro_events.py"), "--report",
             delta], stdout=subprocess.PIPE, universal_newlines=True)
        lines = done.stdout.splitlines()
        self.assertIn("stuck=true", lines, out.getvalue())
        self.assertIn("stalled_runs=%d" % te.STALL_RUNS, lines)
        self.assertIn("through=2026-10-06T01:00:13Z", lines)


class TheLimitsKeepTheirPromises(unittest.TestCase):
    """The numbers the workflow comment and the issue promise. traffic-orders
    runs four times a day."""

    def test_a_stall_is_said_within_a_day(self):
        self.assertGreaterEqual(te.STALL_RUNS, 2,
                                "one failed run is not yet a stall")
        self.assertLessEqual(te.STALL_RUNS, 4, "within a day: 4 runs")

    def test_retries_are_few_and_finish_within_three_days(self):
        self.assertGreaterEqual(te.MAX_TRIES, 2, "a skip is retried at all")
        self.assertLessEqual(te.MAX_TRIES, 12)

    def test_skipping_is_a_small_allowance(self):
        self.assertGreaterEqual(te.MAX_REFUSED_PER_WINDOW, 1)
        self.assertLessEqual(te.MAX_REFUSED_PER_WINDOW, 20)
        self.assertGreaterEqual(te.MAX_SKIPPED_WINDOWS, 1)
        self.assertLessEqual(te.MAX_SKIPPED_WINDOWS, 2)


FAKE_GH_STRICT = r'''#!/usr/bin/env bash
# One line per call; arguments split by \x1f, newlines inside one as \n.
line=""
for a in "$@"; do line="$line${a//$'\n'/\\n}"$'\x1f'; done
printf '%s\n' "$line" >> "$FAKE_GH_LOG"
if [ "$FAKE_GH_FAIL" = "1" ]; then
  echo "gh: HTTP 502" >&2
  exit 1
fi
if [ "$1 $2" = "issue list" ]; then
  if [ "$FAKE_GH_LIST_FAIL" = "1" ]; then
    echo "gh: HTTP 502" >&2
    exit 1
  fi
  [ -n "$FAKE_GH_OPEN" ] && printf '%s\n' $FAKE_GH_OPEN
  exit 0
fi
if [ "$FAKE_GH_VERB_FAIL" = "1" ]; then
  echo "gh: HTTP 502" >&2
  exit 1
fi
exit 0
'''


class TheWorkflowStep(unittest.TestCase):
    """The real step, run under bash -eo pipefail with a stand-in gh that
    records every argument (T3)."""

    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.text = fh.read()

    def setUp(self):
        if not BASH:
            self.skipTest("BLIND: bash is needed to run the workflow step")
        self.tmp = tempfile.mkdtemp(prefix="tro-feed-step-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.write(os.path.join(self.bin, "gh"), FAKE_GH_STRICT)
        self.write(os.path.join(self.bin, "python"),
                   '#!/usr/bin/env bash\nexec "%s" "$@"\n'
                   % sys.executable.replace("\\", "/"))
        for name in ("gh", "python"):
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.work = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.work, "tools"))
        for name in ("tro_events.py", "tro.py", "dtro_events.py"):
            shutil.copy(os.path.join(HERE, name),
                        os.path.join(self.work, "tools", name))
        self.log = os.path.join(self.tmp, "gh.log")

    @staticmethod
    def write(path, text):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def run_step(self, state, open_issues="", fail=False, list_fail=False,
                 verb_fail=False):
        if os.path.exists(self.log):
            os.remove(self.log)
        if state is not None:
            te.save_state(os.path.join(self.work, "dist", "tro", "_events",
                                       te.STATE_NAME), state)
        script = os.path.join(self.tmp, "step.sh")
        self.write(script, run_block(self.text, STEP))
        env = dict(os.environ)
        sep = ";" if os.name == "nt" else ":"
        env.update({
            "PATH": self.bin + sep + env.get("PATH", ""),
            "FAKE_GH_LOG": self.log.replace("\\", "/"),
            "FAKE_GH_OPEN": open_issues,
            "FAKE_GH_FAIL": "1" if fail else "0",
            "FAKE_GH_LIST_FAIL": "1" if list_fail else "0",
            "FAKE_GH_VERB_FAIL": "1" if verb_fail else "0",
            "GH_TOKEN": "x", "RUN_URL": "https://example.invalid/runs/1",
        })
        done = subprocess.run([BASH, "--noprofile", "--norc", "-eo",
                               "pipefail", script], cwd=self.work, env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              universal_newlines=True)
        calls = []
        if os.path.exists(self.log):
            with open(self.log, encoding="utf-8") as fh:
                calls = [line.rstrip("\x1f").split("\x1f")
                         for line in fh.read().splitlines() if line]
        return done.returncode, done.stdout, calls

    @staticmethod
    def calls_of(calls, *verb):
        return [c for c in calls if tuple(c[:len(verb)]) == verb]

    @staticmethod
    def arg(call, flag):
        return call[call.index(flag) + 1]

    def stuck(self):
        state = te.fresh_state(EXTRACT, START)
        state["stall"] = {"at": START, "runs": te.STALL_RUNS,
                          "error": "HTTPError: HTTP Error 503: Unavailable"}
        state["skipped"] = {"p": {"tries": te.MAX_TRIES, "error": "x"},
                            "q": {"tries": 1, "error": "x"}}
        state["skipped_windows"] = [
            {"since": "2026-10-01T00:00:00Z", "to": "2026-10-02T00:00:00Z",
             "tries": te.MAX_TRIES},
            {"since": "2026-10-03T00:00:00Z", "to": "2026-10-04T00:00:00Z",
             "tries": 1}]
        return state

    def test_the_open_issues_are_listed_under_the_feed_label(self):
        code, out, calls = self.run_step(self.stuck())
        self.assertEqual(code, 0, out)
        listed = self.calls_of(calls, "issue", "list")
        self.assertEqual(len(listed), 1, calls)
        self.assertEqual(self.arg(listed[0], "--label"), LABEL)
        self.assertEqual(self.arg(listed[0], "--state"), "open")

    def test_a_stuck_feed_opens_one_issue_under_its_label(self):
        code, out, calls = self.run_step(self.stuck())
        self.assertEqual(code, 0, out)
        labels = self.calls_of(calls, "label", "create")
        self.assertEqual(len(labels), 1, calls)
        self.assertEqual(labels[0][2], LABEL)
        created = self.calls_of(calls, "issue", "create")
        self.assertEqual(len(created), 1, calls)
        self.assertEqual(self.arg(created[0], "--label"), LABEL)
        self.assertIn(START, self.arg(created[0], "--title"))
        self.assertIn("::warning::", out)

    def test_the_issue_tells_given_up_from_still_retrying(self):
        code, out, calls = self.run_step(self.stuck())
        body = self.arg(self.calls_of(calls, "issue", "create")[0], "--body")
        self.assertIn("Windows given up on: 1 (2026-10-01T00:00:00Z.."
                      "2026-10-02T00:00:00Z)", body)
        self.assertIn("Windows still being retried: 1 (2026-10-03T00:00:00Z.."
                      "2026-10-04T00:00:00Z)", body)
        self.assertIn("Orders given up on: 1 (p)", body)
        self.assertIn("Orders still being retried: 1 (q)", body)
        self.assertIn("Say so when the events feed is stuck", body,
                      "the body must say where the full report is printed")
        self.assertNotIn("'Build the pack'", body)
        self.assertIn("first 20 of each list", body)
        self.assertNotIn("full report", body,
                         "the report is cut to 20 as well")
        self.assertIn("a 200 from /events that is not a page of events",
                      body)
        self.assertIn("gave_up=p", out.splitlines(),
                      "the report itself is printed in this step")

    def test_an_open_issue_is_updated_not_duplicated(self):
        code, out, calls = self.run_step(self.stuck(), open_issues="7")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "issue", "create"), [])
        edits = self.calls_of(calls, "issue", "edit")
        self.assertEqual(len(edits), 1, calls)
        self.assertEqual(edits[0][2], "7")
        self.assertIn(START, self.arg(edits[0], "--title"))
        self.assertIn("--body", edits[0])

    def test_a_moving_feed_closes_it(self):
        code, out, calls = self.run_step(te.fresh_state(EXTRACT, START),
                                         open_issues="7 9")
        self.assertEqual(code, 0, out)
        closed = self.calls_of(calls, "issue", "close")
        self.assertEqual([c[2] for c in closed], ["7", "9"])
        self.assertEqual(self.calls_of(calls, "issue", "create"), [])

    def test_no_delta_neither_opens_nor_closes(self):
        code, out, calls = self.run_step(None, open_issues="7")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "issue", "close"), [])
        self.assertEqual(self.calls_of(calls, "issue", "create"), [])
        self.assertIn("::warning::", out)

    def test_a_failing_report_neither_opens_nor_closes(self):
        self.write(os.path.join(self.work, "tools", "tro_events.py"),
                   "import sys\nprint('Traceback: boom')\nsys.exit(1)\n")
        code, out, calls = self.run_step(self.stuck(), open_issues="7")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "issue", "close"), [])
        self.assertEqual(self.calls_of(calls, "issue", "create"), [])
        self.assertIn("::warning::", out)

    def test_gh_failing_throughout_never_fails_the_step(self):
        for state in (self.stuck(), te.fresh_state(EXTRACT, START)):
            code, out, calls = self.run_step(state, fail=True)
            self.assertEqual(code, 0, out)
            self.assertTrue(calls, "PREMISE: the stand-in gh was called")

    def test_gh_failing_after_the_listing_never_fails_the_step(self):
        cases = ((self.stuck(), "7", [("issue", "edit")]),
                 (self.stuck(), "", [("label", "create"),
                                     ("issue", "create")]),
                 (te.fresh_state(EXTRACT, START), "7", [("issue", "close")]))
        for state, open_issues, verbs in cases:
            code, out, calls = self.run_step(state, open_issues=open_issues,
                                             verb_fail=True)
            self.assertEqual(code, 0, out)
            for verb in verbs:
                self.assertTrue(self.calls_of(calls, *verb), (verb, calls))

    def test_a_failed_listing_raises_no_duplicate(self):
        # The trade-off: a run that cannot list cannot tell an open issue
        # from none, so it says so and raises nothing; the next run will.
        code, out, calls = self.run_step(self.stuck(), list_fail=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "issue", "create"), [])
        self.assertEqual(self.calls_of(calls, "issue", "edit"), [])
        self.assertIn("::warning::", out)
        self.assertIn("could not list", out.lower())

    def test_a_failed_listing_closes_nothing(self):
        code, out, calls = self.run_step(te.fresh_state(EXTRACT, START),
                                         list_fail=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "issue", "close"), [])


class TheWorkflowWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.text = fh.read()

    def block(self, name):
        lines = self.text.splitlines()
        at = lines.index("      - name: %s" % name)
        out = []
        for line in lines[at + 1:]:
            if line.startswith("      - "):
                break
            out.append(line)
        return "\n".join(out)

    def test_it_runs_after_the_build_and_before_publishing(self):
        build = self.text.index("      - name: Build the pack")
        step = self.text.index("      - name: %s" % STEP)
        changed = self.text.index("      - name: Has anything changed?")
        self.assertLess(build, step)
        self.assertLess(step, changed,
                        "a stuck feed publishes nothing; this must not "
                        "depend on publishing")

    def test_it_is_not_skipped_and_cannot_stop_the_publish(self):
        body = self.block(STEP)
        self.assertNotIn("\n        if:", body)
        self.assertNotIn("continue-on-error", body)
        self.assertIn("|| true", body)

    def test_this_suite_runs_before_the_build(self):
        self.assertIn("python tools/test_tro_events_never_stall.py",
                      self.block("Run the tests before trusting the build"))


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)

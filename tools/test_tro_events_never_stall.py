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
from test_tro_cut_age_alarm import BASH, FAKE_GH, run_block  # noqa: E402

EXTRACT = "dtros_20261006_010013.csv"
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "traffic-orders.yml")
STEP = "Say so when the events feed is stuck"
LABEL = "traffic-orders-feed"
START = "2026-10-08T00:00:00Z"


def event(order, when, kind="update"):
    return {"id": order, "eventType": kind, "eventTime": when,
            "regulationType": ["miscRoadClosure"]}


def refusal(code=400, order="p"):
    return urllib.error.HTTPError(
        "https://example.invalid/dtros/%s" % order, code,
        "Bad Request" if code == 400 else "Error", {}, None)


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
        feed = Feed(self.events, self.records, refuse={"p": refusal(400)})
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
        feed = Feed(self.events, self.records, refuse={"p": refusal(400)})
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
        feed = Feed(self.events, self.records, refuse={"p": refusal(422)})
        run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(self.state["through"], "2026-10-10T00:00:00Z")
        self.assertIn("p", self.state["skipped"])

    def test_it_is_retried_next_run_and_applied_when_it_answers(self):
        feed = Feed(self.events, self.records, refuse={"p": refusal(400)})
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
                             refuse={"p": refusal(400)}),
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
                        refuse={"p": refusal(400)})
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
        feed = Feed(self.events, self.records, refuse={"p": refusal(400)})
        run(self.state, feed, "2026-10-10T00:00:00Z")
        self.state["skipped"]["p"]["tries"] = te.MAX_TRIES  # given up on
        later = Feed([event("p", "2026-10-10T03:00:00Z")],
                     dict(self.records, p={"v": 3}))
        run(self.state, later, "2026-10-11T00:00:00Z")
        self.assertEqual(later.hydrates, ["p"])
        self.assertEqual(self.state["orders"]["p"], build({"v": 3}, "p"))
        self.assertEqual(self.state["skipped"], {})

    def test_a_delete_for_a_skipped_order_settles_it(self):
        feed = Feed(self.events, self.records, refuse={"p": refusal(400)})
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
        feed = Feed(self.events, self.records, refuse={"p": refusal(400)})
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

    def test_a_service_refusing_every_order_stops_the_window(self):
        orders = ["o%02d" % i for i in range(te.MAX_REFUSED_PER_WINDOW + 1)]
        events = [event(o, "2026-10-08T10:00:00Z") for o in orders]
        feed = Feed(events, {}, refuse=dict((o, refusal(400))
                                            for o in orders))
        stats = run(self.state, feed, "2026-10-10T00:00:00Z")
        self.assertEqual(self.state["through"], START,
                         "a refusal of everything is the service, and "
                         "skipping it would date the pack falsely")
        self.assertIn("400", stats["failed"])

    def test_up_to_the_limit_of_refusals_the_window_still_applies(self):
        orders = ["o%02d" % i for i in range(te.MAX_REFUSED_PER_WINDOW)]
        events = [event(o, "2026-10-08T10:00:00Z") for o in orders]
        feed = Feed(events, {}, refuse=dict((o, refusal(400))
                                            for o in orders))
        stats = run(self.state, feed, "2026-10-09T00:00:00Z")
        self.assertIsNone(stats["failed"])
        self.assertEqual(self.state["through"], "2026-10-09T00:00:00Z")
        self.assertEqual(len(self.state["skipped"]),
                         te.MAX_REFUSED_PER_WINDOW)


class OneRefusedWindow(unittest.TestCase):
    def setUp(self):
        self.state = te.fresh_state(EXTRACT, START)
        self.events = [event("a", "2026-10-08T10:00:00Z"),
                       event("w", "2026-10-09T10:00:00Z"),
                       event("c", "2026-10-10T10:00:00Z")]
        self.records = {"a": {"v": 1}, "w": {"v": 1}, "c": {"v": 1}}

    def test_a_refused_window_is_skipped_and_the_rest_applied(self):
        feed = Feed(self.events, self.records,
                    refuse_windows={"2026-10-09T00:00:00Z": refusal(400)})
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
                    refuse_windows={"2026-10-09T00:00:00Z": refusal(400)})
        run(self.state, feed, "2026-10-11T00:00:00Z")
        got = report(self.state)
        self.assertEqual(got["stuck"], "true")
        self.assertEqual(got["skipped_windows"], "1")

    def test_the_skipped_window_is_read_again_next_run(self):
        run(self.state, Feed(self.events, self.records, refuse_windows={
            "2026-10-09T00:00:00Z": refusal(400)}), "2026-10-11T00:00:00Z")
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
                "2026-10-09T00:00:00Z": refusal(400)})
            te.catch_up(self.state, until, feed.fetch, feed.hydrate, build)
            tries += sum(1 for w in feed.fetches
                         if w[0] == "2026-10-09T00:00:00Z")
            until += te.datetime.timedelta(hours=6)
        self.assertEqual(tries, te.MAX_TRIES)
        self.assertEqual(report(self.state)["gave_up_windows"], "1")

    def test_skipping_windows_is_capped(self):
        # /events refusing every window is the service, not a bad slice.
        refuse = dict(("2026-10-%02dT00:00:00Z" % d, refusal(400))
                      for d in range(8, 20))
        feed = Feed([], {}, refuse_windows=refuse)
        stats = run(self.state, feed, "2026-10-19T00:00:00Z")
        self.assertEqual(len(self.state["skipped_windows"]),
                         te.MAX_SKIPPED_WINDOWS)
        self.assertIn("400", stats["failed"])
        self.assertEqual(
            self.state["through"],
            "2026-10-%02dT00:00:00Z" % (8 + te.MAX_SKIPPED_WINDOWS))

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

    def test_a_400_is_raised_not_gone(self):
        self.answer(error=refusal(400))
        with self.assertRaises(urllib.error.HTTPError):
            self.hydrate()

    def test_in_the_catch_up_the_order_keeps_what_it_had(self):
        self.answer(b"[]")
        state = te.fresh_state(EXTRACT, START)
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


class TheWorkflowStep(unittest.TestCase):
    """The real step, run under bash -eo pipefail with a stand-in gh."""

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
        self.write(os.path.join(self.bin, "gh"), FAKE_GH)
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

    def run_step(self, state, open_issues=""):
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
            "FAKE_GH_OPEN": open_issues, "FAKE_GH_FAIL": "0",
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
                calls = [c for c in fh.read().splitlines() if c]
        return done.returncode, done.stdout, calls

    @staticmethod
    def calls_of(calls, verb):
        return [c for c in calls if c.startswith("issue %s" % verb)]

    def stuck(self):
        state = te.fresh_state(EXTRACT, START)
        state["stall"] = {"at": START, "runs": te.STALL_RUNS,
                          "error": "HTTPError: HTTP Error 400: Bad Request"}
        return state

    def test_a_stuck_feed_opens_one_issue(self):
        code, out, calls = self.run_step(self.stuck())
        self.assertEqual(code, 0, out)
        created = self.calls_of(calls, "create")
        self.assertEqual(len(created), 1, calls)
        self.assertIn("--label %s" % LABEL, created[0])
        self.assertIn(START, created[0])
        self.assertIn("::warning::", out)

    def test_an_open_issue_is_updated_not_duplicated(self):
        code, out, calls = self.run_step(self.stuck(), open_issues="7")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "create"), [])
        edits = self.calls_of(calls, "edit")
        self.assertEqual(len(edits), 1, calls)
        self.assertTrue(edits[0].startswith("issue edit 7 "), edits)

    def test_a_moving_feed_closes_it(self):
        code, out, calls = self.run_step(te.fresh_state(EXTRACT, START),
                                         open_issues="7")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls_of(calls, "close")), 1, calls)
        self.assertEqual(self.calls_of(calls, "create"), [])

    def test_no_delta_neither_opens_nor_closes(self):
        code, out, calls = self.run_step(None, open_issues="7")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "close"), [])
        self.assertEqual(self.calls_of(calls, "create"), [])
        self.assertIn("::warning::", out)


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

#!/usr/bin/env python3
"""The order pack moves with D-TRO's live feed, not only with the extract.

    python tools/test_tro_events_move_the_pack.py

Exit 0: the build applies /events on top of the extract and dates the pack by
how far the feed reached. Exit 1: it does not (the defect). Exit 2: something
this needs and CI always has is missing (the `cryptography` package).

THE LIVE STATE THIS WAS FOUND IN. On 2 October 2026 tro/index.json said
`"generated": "2026-09-06"`, and every run of traffic-orders.yml was green.
`/dtros/all` was still handing back `dtros_20260906_010013.csv` - DfT had not
re-cut the national extract for 26 days - while `/events` reported 1,832
events over 1,650 orders in the previous thirty hours. build_tro.py read the
extract and nothing else, so the pack was rebuilt to identical bytes four
times a day and riders were told "orders as of 6 Sep" while the service held
that morning's closures. tools/dtro_events.py and tools/tro_merge.py, written
for exactly this, were imported by no build.

HOW IT IS SHOWN. The real build_tro.py is run as the workflow runs it, over a
three-order extract named the way the service names its cuts, with a recorded
feed (--events-replay) in which, after the cut:

  * order B is amended - its closure moves to a different road;
  * order C is deleted;
  * order D is created, by a council the extract holds nothing from;
  * order E (not in the extract) is updated as a parking bay - passed over
    without being fetched, and its record would fail the build if it were.

The sealed pack is opened and must carry A as it was, B's NEW geometry and
not its old one, no C, D, and a `generated` date that is the feed's, not the
extract's. The catch-up's own rules - windowing, the budget, delete-wins, the
delta surviving between runs and being dropped for a new extract - are tested
directly against tools/tro_events.py.
"""
import base64
import csv
import datetime
import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - CI installs it
    print("SKIP: the cryptography package is not installed "
          "(pip install cryptography)")
    sys.exit(2)

import build_tro  # noqa: E402

EXTRACT = "dtros_20260906_010013.csv"
LINE_A = "SRID=27700;LINESTRING(464946.33 293262.84, 464918.98 293239.27)"
LINE_B_OLD = "SRID=27700;LINESTRING(420000 380000, 420100 380100)"
LINE_B_NEW = "SRID=27700;LINESTRING(430000 390000, 430200 390200)"
LINE_C = "SRID=27700;LINESTRING(440000 300000, 440100 300100)"
LINE_D = "SRID=27700;LINESTRING(450000 310000, 450100 310100)"


def record(name, line, code="miscRoadClosure", start="2026-08-01T00:00:00",
           end=None, tra=2460):
    validity = {"start": start}
    if end:
        validity["end"] = end
    return {"source": {
        "troName": name,
        "reference": name,
        "traCreator": tra,
        "currentTraOwner": tra,
        "provision": [{
            "reference": name + "/1",
            "regulation": [{
                "timeZone": "Europe/London",
                "generalRegulation": {"regulationType": code},
                "condition": [{"timeValidity": validity}],
            }],
            "regulatedPlace": [{
                "type": "regulationLocation",
                "description": name + " Lane",
                "linearGeometry": {"linestring": line},
            }],
        }],
    }}


def event(order, kind, when, types=("miscRoadClosure",)):
    return {"id": order, "eventType": kind, "eventTime": when,
            "publicationTime": when, "regulationType": list(types)}


def unseal(path, key):
    with open(path, "rb") as fh:
        blob = fh.read()
    # MAGIC, version, algorithm, then the nonce: build_packages.pack.
    header, nonce_len = 6, 12
    if blob[:4] != b"TBPK":
        raise AssertionError("%s is not a .tbpack" % path)
    prefix = blob[:header + nonce_len]
    nonce = blob[header:header + nonce_len]
    clear = AESGCM(key).decrypt(nonce, blob[header + nonce_len:], prefix)
    return json.loads(gzip.decompress(clear))


class TheBuildAppliesTheFeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tro-events-")
        cls.key = os.urandom(32)
        cls.key_path = os.path.join(cls.tmp, "throwaway.key")
        with open(cls.key_path, "w") as fh:
            fh.write(base64.b64encode(cls.key).decode())
        cls.csv = os.path.join(cls.tmp, EXTRACT)
        with open(cls.csv, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Id", "SchemaVersion", "Data"])
            for order, line in (("order-a", LINE_A), ("order-b", LINE_B_OLD),
                                ("order-c", LINE_C)):
                writer.writerow([order, "3.4.0",
                                 json.dumps(record(order, line))])
        cls.replay = os.path.join(cls.tmp, "feed.json")
        with open(cls.replay, "w", encoding="utf-8") as fh:
            json.dump({
                "events": [
                    event("order-b", "update", "2026-09-20T09:00:00Z"),
                    event("order-c", "delete", "2026-09-25T10:00:00Z"),
                    event("order-d", "create", "2026-10-01T08:00:00Z"),
                    event("order-e", "update", "2026-10-01T09:00:00Z",
                          types=("kerbsidePermitParkingPlace",)),
                ],
                "records": {
                    "order-b": record("order-b", LINE_B_NEW),
                    # Derbyshire's first order: the extract holds none.
                    "order-d": record("order-d", LINE_D, tra=1050),
                    # Fetching E would be a waste of a request; if the build
                    # does, this unreadable record makes it obvious.
                    "order-e": record("order-e", "SRID=27700;NOT WKT"),
                },
            }, fh)
        cls.out = os.path.join(cls.tmp, "out")
        cls.index = os.path.join(cls.tmp, "index.json")
        cls.proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "build_tro.py"),
             "--key", cls.key_path, "--csv", cls.csv, "--out", cls.out,
             "--index", cls.index, "--events-replay", cls.replay,
             "--now", "2026-10-02T06:00:00Z"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, cwd=ROOT)
        cls.pack = None
        if cls.proc.returncode == 0:
            cls.pack = unseal(os.path.join(cls.out, "gb-tro.tbpack"), cls.key)

    def built(self):
        if self.proc.returncode != 0:
            self.fail("build_tro.py cannot apply a feed at all (exit %d):\n%s"
                      % (self.proc.returncode, self.proc.stdout[-2000:]))
        return self.pack

    def orders(self):
        return sorted(set(f["properties"].get("dtro")
                          for f in self.built()["features"]))

    def coords_of(self, order):
        return [f["geometry"]["coordinates"] for f in self.built()["features"]
                if f["properties"].get("dtro") == order]

    def test_the_pack_is_dated_by_the_feed_not_the_extract(self):
        self.assertEqual(self.built()["generated"], "2026-10-02",
                         "the pack still carries the extract's date")
        with open(self.index, encoding="utf-8") as fh:
            index = json.load(fh)
        self.assertEqual(index["generated"], "2026-10-02")
        self.assertEqual(index["packs"][0]["generated"], "2026-10-02")
        # The extract's own date is still written down, beside it.
        self.assertEqual(index.get("cut"), "2026-09-06")

    def test_an_amended_order_is_replaced_wholesale(self):
        new = build_tro.geojson(
            {"wkt": LINE_B_NEW, "code": "miscRoadClosure", "label": "x"})
        old = build_tro.geojson(
            {"wkt": LINE_B_OLD, "code": "miscRoadClosure", "label": "x"})
        got = self.coords_of("order-b")
        self.assertIn(new["geometry"]["coordinates"], got)
        self.assertNotIn(old["geometry"]["coordinates"], got,
                         "the amended order's old road is still shown shut")

    def test_a_deleted_order_leaves_and_a_new_one_arrives(self):
        self.assertEqual(self.orders(), ["order-a", "order-b", "order-d"])

    def test_a_council_whose_first_order_came_by_the_feed_publishes(self):
        # Counted from the extract alone, Derbyshire would be written into the
        # pack as "does not publish" beside the order it just published.
        rows = dict((a["swa"], a) for a in self.built()["authorities"])
        self.assertEqual(rows["1050"]["records"], 1)
        self.assertEqual(rows["2460"]["records"], 3)

    def test_an_order_the_feed_did_not_touch_is_kept(self):
        self.assertEqual(len(self.coords_of("order-a")), 1)

    def test_a_parking_event_is_passed_over_unfetched(self):
        self.built()
        self.assertIn("passed over 1", self.proc.stdout)
        self.assertNotIn("order-e", self.orders())

    def test_the_build_without_a_feed_still_matches_the_extract(self):
        # --csv with no feed is the offline build: nothing to apply, and the
        # pack keeps the extract's date exactly as before.
        out = os.path.join(self.tmp, "offline")
        index = os.path.join(self.tmp, "offline.json")
        run = subprocess.run(
            [sys.executable, os.path.join(HERE, "build_tro.py"),
             "--key", self.key_path, "--csv", self.csv, "--out", out,
             "--index", index],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, cwd=ROOT)
        self.assertEqual(run.returncode, 0, run.stdout[-2000:])
        pack = unseal(os.path.join(out, "gb-tro.tbpack"), self.key)
        self.assertEqual(pack["generated"], "2026-09-06")
        self.assertEqual(len(pack["features"]), 3)


class CatchUp(unittest.TestCase):
    """tools/tro_events.py's rules, without a build around them."""

    def setUp(self):
        try:
            import tro_events
        except ImportError:
            self.fail("tools/tro_events.py does not exist: nothing applies "
                      "the feed")
        self.te = tro_events

    def feed(self, events, records):
        calls = {"fetch": [], "hydrate": []}

        def fetch(since, to):
            calls["fetch"].append((since, to))
            low, high = self.te.parse_stamp(since), self.te.parse_stamp(to)
            return [e for e in events
                    if low <= self.te.parse_stamp(e["eventTime"]) < high]

        def hydrate(order):
            calls["hydrate"].append(order)
            return records.get(order)

        return fetch, hydrate, calls

    def build(self, rec, order):
        return [{"properties": {"dtro": order, "tro_uid": order}}]

    def test_windows_are_contiguous_and_stop_at_until(self):
        state = self.te.fresh_state(EXTRACT, "2026-09-06T01:00:13Z")
        fetch, hydrate, calls = self.feed([], {})
        until = self.te.parse_stamp("2026-09-08T12:00:00Z")
        stats = self.te.catch_up(state, until, fetch, hydrate, self.build)
        self.assertEqual(calls["fetch"], [
            ("2026-09-06T01:00:13Z", "2026-09-07T01:00:13Z"),
            ("2026-09-07T01:00:13Z", "2026-09-08T01:00:13Z"),
            ("2026-09-08T01:00:13Z", "2026-09-08T12:00:00Z")])
        self.assertEqual(state["through"], "2026-09-08T12:00:00Z")
        self.assertEqual(stats["windows"], 3)

    def test_a_spent_budget_stops_at_a_window_boundary(self):
        state = self.te.fresh_state(EXTRACT, "2026-09-06T00:00:00Z")
        fetch, hydrate, calls = self.feed([], {})
        ticks = iter(range(0, 10000, 100))
        stats = self.te.catch_up(
            state, self.te.parse_stamp("2026-09-30T00:00:00Z"), fetch,
            hydrate, self.build, budget_seconds=150,
            clock=lambda: next(ticks))
        self.assertTrue(stats["stopped_early"])
        self.assertEqual(state["through"], "2026-09-07T00:00:00Z")

    def test_a_window_too_big_for_one_run_still_finishes_over_several(self):
        # A council bulk-loading its orders can put more into one day than a
        # run has time to fetch. Stopping only between windows would run
        # every job into its timeout on that day, for ever; stopping inside
        # it, keeping what was fetched, lets the next run carry on.
        orders = ["o%02d" % i for i in range(6)]
        events = [event(o, "create", "2026-09-06T10:00:00Z") for o in orders]
        records = dict((o, {"source": {}}) for o in orders)
        state = self.te.fresh_state(EXTRACT, "2026-09-06T00:00:00Z")
        until = self.te.parse_stamp("2026-09-07T00:00:00Z")
        at = self.te.parse_stamp("2026-10-02T06:00:00Z")
        fetched_runs = []
        for _ in range(5):
            fetch, hydrate, calls = self.feed(events, records)
            ticks = iter(range(0, 10000, 100))
            stats = self.te.catch_up(
                state, until, fetch, hydrate, self.build,
                budget_seconds=350, clock=lambda: next(ticks),
                now=lambda: at)
            fetched_runs.append(len(calls["hydrate"]))
            if not stats["stopped_early"]:
                break
        self.assertEqual(fetched_runs, [2, 2, 2])
        self.assertEqual(sorted(state["orders"]), orders)
        self.assertEqual(state["through"], "2026-09-07T00:00:00Z")

    def test_a_failed_window_stops_at_the_last_whole_one(self):
        state = self.te.fresh_state(EXTRACT, "2026-09-06T00:00:00Z")
        seen = []

        def fetch(since, to):
            seen.append(since)
            if len(seen) == 3:
                raise OSError("HTTP Error 429: Too Many Requests")
            return []

        stats = self.te.catch_up(
            state, self.te.parse_stamp("2026-09-30T00:00:00Z"), fetch,
            lambda order: None, self.build)
        self.assertEqual(state["through"], "2026-09-08T00:00:00Z")
        self.assertIn("429", stats["failed"])
        self.assertEqual(stats["windows"], 2)

    def test_an_order_is_fetched_once_per_change_not_once_per_window(self):
        # Fetched on the 2nd of October while catching up the 6th of
        # September, the order is already current for its event on the 20th.
        state = self.te.fresh_state(EXTRACT, "2026-09-06T00:00:00Z")
        events = [event("x", "update", "2026-09-06T10:00:00Z"),
                  event("x", "update", "2026-09-20T10:00:00Z")]
        fetch, hydrate, calls = self.feed(events, {"x": {"source": {}}})
        at = self.te.parse_stamp("2026-10-02T06:00:00Z")
        stats = self.te.catch_up(
            state, self.te.parse_stamp("2026-09-30T00:00:00Z"), fetch,
            hydrate, self.build, now=lambda: at)
        self.assertEqual(calls["hydrate"], ["x"])
        self.assertEqual(stats["already_current"], 1)

    def test_an_older_event_is_skipped_and_a_newer_delete_applied(self):
        state = self.te.fresh_state(EXTRACT, "2026-09-06T00:00:00Z")
        state["fetched"]["x"] = "2026-09-10T00:00:00Z"
        events = [event("x", "update", "2026-09-06T10:00:00Z"),
                  event("x", "delete", "2026-09-12T10:00:00Z")]
        fetch, hydrate, calls = self.feed(events, {"x": {"source": {}}})
        self.te.catch_up(state, self.te.parse_stamp("2026-09-14T00:00:00Z"),
                         fetch, hydrate, self.build)
        self.assertEqual(calls["hydrate"], [])
        self.assertEqual(state["orders"], {"x": []})

    def test_an_update_after_the_fetch_is_fetched_again(self):
        state = self.te.fresh_state(EXTRACT, "2026-09-06T00:00:00Z")
        state["fetched"]["x"] = "2026-09-10T00:00:00Z"
        fetch, hydrate, calls = self.feed(
            [event("x", "update", "2026-09-12T10:00:00Z")],
            {"x": {"source": {}}})
        self.te.catch_up(state, self.te.parse_stamp("2026-09-14T00:00:00Z"),
                         fetch, hydrate, self.build)
        self.assertEqual(calls["hydrate"], ["x"])

    def test_a_delete_beats_an_update_at_the_same_moment(self):
        state = self.te.fresh_state(EXTRACT, "2026-09-06T00:00:00Z")
        events = [event("x", "update", "2026-09-06T10:00:00Z"),
                  event("x", "delete", "2026-09-06T10:00:00Z")]
        fetch, hydrate, calls = self.feed(events, {"x": {"source": {}}})
        self.te.catch_up(state, self.te.parse_stamp("2026-09-07T00:00:00Z"),
                         fetch, hydrate, self.build)
        self.assertEqual(state["orders"], {"x": []})
        self.assertEqual(calls["hydrate"], [])

    def test_a_later_update_brings_a_deleted_order_back(self):
        state = self.te.fresh_state(EXTRACT, "2026-09-06T00:00:00Z")
        events = [event("x", "delete", "2026-09-06T10:00:00Z"),
                  event("x", "create", "2026-09-06T11:00:00Z")]
        fetch, hydrate, _ = self.feed(events, {"x": {"source": {}}})
        self.te.catch_up(state, self.te.parse_stamp("2026-09-07T00:00:00Z"),
                         fetch, hydrate, self.build)
        self.assertEqual(len(state["orders"]["x"]), 1)

    def test_an_order_that_has_gone_is_emptied(self):
        state = self.te.fresh_state(EXTRACT, "2026-09-06T00:00:00Z")
        fetch, hydrate, _ = self.feed(
            [event("x", "update", "2026-09-06T10:00:00Z")], {})
        self.te.catch_up(state, self.te.parse_stamp("2026-09-07T00:00:00Z"),
                         fetch, hydrate, self.build)
        self.assertEqual(state["orders"], {"x": []})

    def test_a_carried_order_is_fetched_whatever_its_event_says(self):
        # The extract draws a closure for it; the event says parking. Passing
        # it over would leave the closure on the map for ever.
        state = self.te.fresh_state(EXTRACT, "2026-09-06T00:00:00Z")
        fetch, hydrate, calls = self.feed(
            [event("x", "update", "2026-09-06T10:00:00Z",
                   types=("kerbsideNoWaiting",))], {"x": {"source": {}}})
        self.te.catch_up(state, self.te.parse_stamp("2026-09-07T00:00:00Z"),
                         fetch, hydrate, lambda r, o: [],
                         carried=frozenset(["x"]))
        self.assertEqual(calls["hydrate"], ["x"])
        self.assertEqual(state["orders"], {"x": []})

    def test_the_delta_is_kept_for_its_extract_and_dropped_for_a_new_one(self):
        tmp = tempfile.mkdtemp(prefix="tro-delta-")
        path = os.path.join(tmp, "delta.json")
        state = self.te.fresh_state(EXTRACT, "2026-09-06T01:00:13Z")
        state["orders"]["x"] = []
        state["through"] = "2026-10-01T00:00:00Z"
        self.te.save_state(path, state)
        self.assertEqual(self.te.load_state(path, EXTRACT), state)
        self.assertIsNone(
            self.te.load_state(path, "dtros_20261005_010000.csv"))
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(self.te.load_state(path, EXTRACT))

    def test_the_cut_moment_comes_from_the_service_filename(self):
        self.assertEqual(self.te.cut_moment("x/dtros_20260906_010013.csv"),
                         "2026-09-06T01:00:13Z")
        self.assertEqual(self.te.cut_moment("dtros_20260906.csv"),
                         "2026-09-06T00:00:00Z")
        self.assertIsNone(self.te.cut_moment("dtros_all.csv"))


class TheWorkflowKeepsTheDelta(unittest.TestCase):
    def test_the_delta_directory_is_cached_between_runs(self):
        with open(os.path.join(ROOT, ".github", "workflows",
                               "traffic-orders.yml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("path: dist/tro/_events", text,
                      "every run would catch up from the extract's cut again")
        # Keyed per run, so every run saves: actions/cache never re-saves a
        # key it restored exactly, and a per-day key would throw away three of
        # every four runs' progress.
        self.assertIn("key: dtro-events-${{ github.run_id }}", text)
        self.assertIn("restore-keys: dtro-events-", text)
        self.assertNotIn("--no-events", text)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)

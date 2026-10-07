#!/usr/bin/env python3
"""A newer rowmaps file is taken without moving any unchanged lane's id, and
a bad or unreadable one never replaces the cached file.

    python tools/test_rowmaps_refresh.py

No network: a stand-in client and a temporary cache. The lane-id half runs
the real build_packages.normalise, so it needs `cryptography` (exit 2
without it, as the other pack tests do).
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rowmaps_refresh as rr  # noqa: E402
from polite_http import FetchFailed, Refused  # noqa: E402

try:
    import build_packages  # noqa: E402
except (ImportError, SystemExit):
    print("BLIND: build_packages needs cryptography (pip install "
          "cryptography)")
    sys.exit(2)

DLON, DLAT = 1 / 68700.0, 1 / 110574.0
BOAT = "byway_open_to_all_traffic"


def line(x0, y0, x1, y1, n=5, places=5):
    return [[round(-1.0 + (x0 + (x1 - x0) * k / float(n)) * DLON, places),
             round(52.0 + (y0 + (y1 - y0) * k / float(n)) * DLAT, places)]
            for k in range(n + 1)]


def rec(number, coords):
    return {"type": "Feature",
            "properties": {"Name": "ZZ|Ash|%s" % number,
                           "Description": "BO|ZZ:%s|0.300|none|" % number},
            "geometry": {"type": "LineString", "coordinates": coords}}


def fc(features):
    return {"type": "FeatureCollection", "name": "x", "features": features}


#: Eight byways 100 m apart, 600 m long.
OLD = [rec(i, line(0, 100 * i, 600, 100 * i)) for i in range(8)]


def uid(feature):
    return build_packages.normalise(copy.deepcopy(feature), "ZZ", "Zedshire",
                                    BOAT)["properties"]["lane_uid"]


class Take(unittest.TestCase):
    def test_a_republish_at_other_precision_keeps_every_id(self):
        new = [rec(i, line(0, 100 * i + 1, 600, 100 * i + 1, n=9, places=6))
               for i in range(8)]
        kept, report, refusal = rr.take(fc(OLD), fc(new))
        self.assertIsNone(refusal)
        self.assertEqual([uid(f) for f in kept["features"]],
                         [uid(f) for f in OLD])
        self.assertEqual((report["dropped"], report["added"]), ([], []))

    def test_a_stopped_up_byway_goes_and_a_new_one_comes(self):
        new = OLD[:7] + [rec(9, line(2000, 0, 2000, 500))]
        kept, report, refusal = rr.take(fc(OLD), fc(new))
        self.assertIsNone(refusal)
        self.assertEqual(report["dropped"], ["ZZ|Ash|7"])
        self.assertEqual(report["added"], ["ZZ|Ash|9"])
        self.assertEqual(sorted(uid(f) for f in kept["features"]),
                         sorted([uid(f) for f in OLD[:7]] + [uid(new[-1])]))

    def test_an_extended_byway_is_the_new_record_not_old_plus_copy(self):
        longer = rec(0, line(0, 0, 900, 0))
        kept, report, _r = rr.take(fc(OLD), fc([longer] + OLD[1:]))
        names = [f["properties"]["Name"] for f in kept["features"]]
        self.assertEqual(names.count("ZZ|Ash|0"), 1)
        self.assertIn(uid(longer), [uid(f) for f in kept["features"]])

    def test_an_empty_or_collapsed_file_is_refused(self):
        for new in ([], OLD[:2]):
            kept, _rep, refusal = rr.take(fc(OLD), fc(new))
            self.assertIsNotNone(refusal, len(new))
            self.assertEqual(kept["features"], OLD)

    def test_a_different_network_is_refused(self):
        moved = [rec(i, line(0, 100 * i + 50, 600, 100 * i + 50))
                 for i in range(8)]
        kept, _rep, refusal = rr.take(fc(OLD), fc(moved))
        self.assertIn("same network", refusal)
        self.assertEqual(kept["features"], OLD)


class Client(object):
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def get_if_changed(self, url, last_modified=None, etag=None):
        self.asked.append((url, last_modified))
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return False, None, {}
        return True, json.dumps(answer).encode(), {
            "Last-Modified": "Wed, 07 Oct 2026 09:00:00 GMT"}


URL = rr.BASE % ("ZZ", 4)


class Refresh(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.cache, "ZZ"))
        self.path = os.path.join(self.cache, "ZZ", BOAT + ".json")
        with open(self.path, "w") as fh:
            json.dump(fc(OLD), fh)
        # Footpaths are cached too, and never re-read.
        with open(os.path.join(self.cache, "ZZ", "footpath.json"), "w") as fh:
            json.dump(fc([]), fh)

    def tearDown(self):
        shutil.rmtree(self.cache)

    def run_with(self, answer, today="2026-10-07"):
        client = Client({URL: answer})
        problems, done = rr.refresh(client, self.cache, today,
                                    log=lambda *_: None)
        return client, problems, done

    def cached(self):
        with open(self.path) as fh:
            return json.load(fh)["features"]

    def checks(self):
        with open(os.path.join(self.cache, rr.CHECKS)) as fh:
            return json.load(fh)

    def test_a_changed_file_is_taken_and_its_validator_kept(self):
        new = OLD[:7] + [rec(9, line(2000, 0, 2000, 500))]
        client, problems, _d = self.run_with(fc(new))
        self.assertEqual(problems, [])
        self.assertEqual([u for u, _lm in client.asked], [URL],
                         "a footpath file was re-read")
        self.assertEqual(len(self.cached()), 8)
        entry = self.checks()["ZZ/" + BOAT]
        self.assertEqual(entry["last_modified"],
                         "Wed, 07 Oct 2026 09:00:00 GMT")

    def test_the_next_check_is_conditional_and_a_week_later(self):
        self.run_with(fc(OLD))
        client, _p, done = self.run_with(None, today="2026-10-10")
        self.assertEqual(client.asked, [], "re-checked inside the week")
        client, _p, done = self.run_with(None, today="2026-10-14")
        self.assertEqual(client.asked,
                         [(URL, "Wed, 07 Oct 2026 09:00:00 GMT")])
        self.assertEqual(done["ZZ/" + BOAT], "unchanged")

    def test_a_refused_or_failed_read_leaves_the_cache_alone(self):
        for month, answer in enumerate((Refused("403"),
                                        FetchFailed("HTTP 404"), fc([]),
                                        fc(OLD[:1])), 1):
            _c, problems, _d = self.run_with(answer,
                                             today="2027-%02d-01" % month)
            self.assertTrue(problems, answer)
            self.assertEqual(self.cached(), OLD, answer)

    def test_a_refused_file_is_not_called_unchanged_next_time(self):
        self.run_with(fc(OLD[:1]))
        self.assertIsNone(self.checks()["ZZ/" + BOAT].get("last_modified"))

    def test_at_most_max_files_a_run_oldest_first(self):
        for code in ("AA", "BB"):
            os.makedirs(os.path.join(self.cache, code))
            with open(os.path.join(self.cache, code, BOAT + ".json"),
                      "w") as fh:
                json.dump(fc(OLD), fh)
        checks = {"AA/" + BOAT: {"checked": "2026-09-01"},
                  "ZZ/" + BOAT: {"checked": "2026-09-20"}}
        got = rr.due(self.cache, checks, "2026-10-07", max_files=2)
        self.assertEqual([k for k, _c, _t in got],
                         ["BB/" + BOAT, "AA/" + BOAT])


if __name__ == "__main__":
    unittest.main(verbosity=1)

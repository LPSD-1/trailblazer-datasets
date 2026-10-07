#!/usr/bin/env python3
"""Range firing notices are read for the right month and their timings kept
as published.

    python tools/test_mod_ranges.py

No network. The bodies are the shape of GOV.UK's HTML publications for
Salisbury Plain and Otterburn (October 2026), trimmed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mod_ranges as mr  # noqa: E402
from polite_http import FetchFailed  # noqa: E402

OTTERBURN = """<div class="govspeak"><p>Firing times for October 2026:</p>
<h3>Otterburn Ranges</h3><ul><li><p>Monday:</p><p>9am to 5pm</p></li>
<li><p>Tuesday:</p><p>9am to 11:59pm</p></li></ul>
<h3>Ponteland Ranges</h3><ul><li><p>Monday:</p><p>9am to 4:30pm</p></li></ul>
<p>Firing takes place when roads are marked as &#8216;Closed&#8217;</p>
<p>Email address: someone@mod.example</p></div>"""


class Titles(unittest.TestCase):
    def test_the_month_a_notice_covers(self):
        self.assertEqual(mr.month_of("Castlemartin firing notice October "
                                     "2026"), (2026, 10))
        self.assertEqual(mr.month_of("Dartmoor six week firing times: 28 "
                                     "September to 8 November 2026"),
                         (2026, 11))
        self.assertIsNone(mr.month_of("Range notice"))

    def test_one_label_per_range_whatever_the_wording(self):
        self.assertEqual(
            mr.label_of("Firing times for Kingsbury, Whittington and Leek "
                        "Ranges October 2026"),
            mr.label_of("Kingsbury, Whittington and Leek Ranges September "
                        "2026 firing times"))
        self.assertEqual(mr.label_of("Salisbury Plain Training Area (SPTA) "
                                     "firing times: October 2026"),
                         "Salisbury Plain Training Area (SPTA)")

    def test_this_months_notice_is_chosen_not_the_newest_listed(self):
        got = mr.choose([
            {"title": "Castlemartin firing notice September 2026", "url": "s"},
            {"title": "Castlemartin firing notice October 2026", "url": "o"},
            {"title": "Castlemartin firing notice November 2026", "url": "n"},
        ], "2026-10-07")
        self.assertEqual(got["Castlemartin"][0]["url"], "o")

    def test_without_one_for_this_month_the_last_begun_is_kept(self):
        got = mr.choose([
            {"title": "Holcombe Moor firing times June 2025", "url": "j"},
            {"title": "Holcombe Moor firing times May 2025", "url": "m"},
        ], "2026-10-07")
        self.assertEqual(got["Holcombe Moor"][0]["url"], "j")


class Body(unittest.TestCase):
    def test_each_days_times_under_their_range(self):
        times = mr.timings(mr.text_lines(OTTERBURN))
        self.assertIn({"heading": "Otterburn Ranges",
                       "line": "Tuesday: 9am to 11:59pm"}, times)
        self.assertIn({"heading": "Ponteland Ranges",
                       "line": "Monday: 9am to 4:30pm"}, times)

    def test_contact_lines_are_not_timings(self):
        times = mr.timings(mr.text_lines(OTTERBURN))
        self.assertFalse(any("@" in t["line"] for t in times))


class Fetch(unittest.TestCase):
    def test_scotland_is_skipped_and_the_notice_is_read(self):
        asked = []

        class Client(object):
            def get_json(self, url):
                asked.append(url)
                if url.endswith("/firing-notice"):
                    return {"links": {"documents": [
                        {"title": "Scotland firing times",
                         "base_path": "/government/publications/scotland"},
                        {"title": "Otterburn firing times",
                         "base_path": "/government/publications/otb"}]}}
                if url.endswith("/otb"):
                    return {"details": {"attachments": [
                        {"title": "Otterburn and Ponteland firing times "
                                  "October 2026",
                         "url": "/government/publications/otb/oct"}]}}
                return {"details": {"body": OTTERBURN},
                        "public_updated_at": "2026-09-24T17:43:24+01:00"}

        ranges = mr.fetch(Client(), "2026-10-07", log=lambda *_: None)
        self.assertFalse(any("scotland" in u for u in asked))
        self.assertEqual(len(ranges), 1)
        r = ranges[0]
        self.assertEqual((r["label"], r["month"], r["updated"]),
                         ("Otterburn and Ponteland", "2026-10",
                          "2026-09-24"))
        self.assertTrue(r["byway_notes"])

    def test_an_empty_collection_is_a_failure(self):
        class Client(object):
            def get_json(self, url):
                return {"links": {"documents": []}}

        with self.assertRaises(FetchFailed):
            mr.fetch(Client(), "2026-10-07", log=lambda *_: None)


if __name__ == "__main__":
    unittest.main(verbosity=1)

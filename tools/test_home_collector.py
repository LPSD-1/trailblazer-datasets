#!/usr/bin/env python3
"""The home collector stores only real changes, never leaves a partial file,
keeps the last good snapshot, and the CI builds read its snapshots instead
of asking councils that refuse GitHub's runners.

    python tools/test_home_collector.py

No network and no git: a stand-in client and a temporary home-collected/.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import home_collector as hc  # noqa: E402
from polite_http import FetchFailed, NotDue, Refused  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

WFS = ("https://gi.dorset.example/geoserver/countryside/wfs?service=WFS&"
       "request=GetFeature&typeNames=x&outputFormat=application/json")
PAGE = "https://en.powys.example/article/2446"
PDF = "https://en.powys.example/media/10190/Gap-Road/pdf/gap.pdf?m=1"


def layer(stamp, ids=("a", "b")):
    return json.dumps({"type": "FeatureCollection", "timeStamp": stamp,
                       "features": [{"id": i, "properties": {}} for i in
                                    reversed(ids)]}).encode()


def page(footer):
    return ("<html><body><main><h1>Traffic orders</h1><ul><li>"
            "<a href='/media/10190/Gap-Road/pdf/gap.pdf?m=1'>Gap Road</a>"
            "</li></ul></main><footer>%s</footer></body></html>"
            % footer).encode()


class Client(object):
    """Answers by URL: bytes, an exception, or None for a 304."""

    def __init__(self, answers):
        self.answers, self.asked, self.overridden = answers, [], {}

    def get_if_changed(self, url, last_modified=None, etag=None):
        self.asked.append((url, last_modified))
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return False, None, {}
        return True, answer, {"Last-Modified": "Wed, 07 Oct 2026"}


CONFIG = {"sources": [
    {"id": "dorset-closures", "url": WFS, "kind": "json",
     "council": "Dorset Council", "authority": "Dorset"},
    {"id": "powys-2446", "url": PAGE, "kind": "page",
     "council": "Powys County Council", "authority": "Powys",
     "follow": r"^https://en\.powys\.example/media/\d+/"}]}


class Collect(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        with open(os.path.join(self.home, "collector.json"), "w") as fh:
            json.dump(CONFIG, fh)

    def tearDown(self):
        shutil.rmtree(self.home)

    def run_with(self, answers, today="2026-10-08"):
        client = Client(answers)
        changed, failures = hc.collect(client, self.home, today=today)
        return client, changed, failures

    def index(self):
        with open(os.path.join(self.home, hc.INDEX)) as fh:
            return json.load(fh)

    def snapshot(self, url):
        with open(os.path.join(self.home, "snapshots",
                               self.index()[url]["file"]), "rb") as fh:
            return fh.read()

    def test_first_run_stores_each_source_and_the_documents_it_links(self):
        client, changed, failures = self.run_with(
            {WFS: layer(1), PAGE: page("a"), PDF: b"%PDF-1.4 order"})
        self.assertEqual(failures, [])
        self.assertEqual([u for u, _lm in client.asked], [WFS, PAGE, PDF])
        self.assertIn("dorset-closures", changed)
        self.assertEqual(self.snapshot(PDF), b"%PDF-1.4 order")
        self.assertFalse(any(n.endswith(".tmp") for n in os.listdir(
            os.path.join(self.home, "snapshots"))))

    def test_a_clock_or_a_footer_is_not_a_change(self):
        self.run_with({WFS: layer(1), PAGE: page("a"), PDF: b"%PDF"})
        _c, changed, _f = self.run_with(
            {WFS: layer(2), PAGE: page("rendered 10:02"), PDF: None},
            today="2026-10-08")
        self.assertEqual(changed, [])

    def test_a_real_change_is_stored_and_a_304_asks_with_the_validator(self):
        self.run_with({WFS: layer(1), PAGE: page("a"), PDF: b"%PDF"})
        client, changed, _f = self.run_with(
            {WFS: layer(1, ids=("a", "b", "c")), PAGE: None, PDF: None})
        self.assertEqual(changed, ["dorset-closures", "index"])
        self.assertIn((PAGE, "Wed, 07 Oct 2026"), client.asked)
        self.assertIn(b'"c"', self.snapshot(WFS))

    def test_a_failure_keeps_the_last_good_snapshot_and_says_so(self):
        self.run_with({WFS: layer(1), PAGE: page("a"), PDF: b"%PDF"})
        before = self.snapshot(WFS)
        _c, _changed, failures = self.run_with(
            {WFS: Refused("HTTP 403"), PAGE: FetchFailed("HTTP 500"),
             PDF: NotDue("read 2 days ago")}, today="2026-10-09")
        self.assertEqual(len(failures), 2)
        self.assertEqual(self.snapshot(WFS), before)
        self.assertEqual(self.index()[WFS]["failing_since"], "2026-10-09")

    def test_the_heartbeat_moves_once_a_day(self):
        self.run_with({WFS: layer(1), PAGE: page("a"), PDF: b"%PDF"})
        _c, changed, _f = self.run_with({WFS: None, PAGE: None, PDF: None})
        self.assertEqual(changed, [])
        _c, changed, _f = self.run_with({WFS: None, PAGE: None, PDF: None},
                                        today="2026-10-09")
        self.assertEqual(changed, ["heartbeat"])

    def test_stale_after_three_days(self):
        self.run_with({WFS: layer(1), PAGE: page("a"), PDF: b"%PDF"})
        self.assertIsNone(hc.stale(self.home, 3, today="2026-10-11"))
        self.assertIn("4 days ago", hc.stale(self.home, 3,
                                             today="2026-10-12"))
        shutil.rmtree(self.home)
        os.makedirs(self.home)
        self.assertIn("never", hc.stale(self.home, 3, today="2026-10-12"))


class Real(object):
    def __init__(self):
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        return b"from the network"

    def get_json(self, url):
        self.asked.append(url)
        return {"network": True}


class HomeClientInCI(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        with open(os.path.join(self.home, "collector.json"), "w") as fh:
            json.dump(CONFIG, fh)
        hc.collect(Client({WFS: layer(1), PAGE: page("a"),
                           PDF: b"%PDF-1.4"}), self.home,
                   today="2026-10-08")

    def tearDown(self):
        shutil.rmtree(self.home)

    def test_a_home_source_is_answered_from_its_snapshot_and_credited(self):
        real = Real()
        client = hc.HomeClient(real, self.home)
        self.assertEqual(len(client.get_json(WFS)["features"]), 2)
        self.assertEqual(real.asked, [])
        self.assertEqual(hc.provenance_of(client.take_served()),
                         "collected from a home connection 2026-10-08")
        self.assertEqual(client.take_served(), {})

    def test_a_home_host_is_never_asked_from_ci(self):
        real = Real()
        client = hc.HomeClient(real, self.home)
        with self.assertRaises(Refused):
            client.get("https://en.powys.example/article/9999")
        self.assertEqual(real.asked, [])

    def test_every_other_host_goes_to_the_network(self):
        real = Real()
        client = hc.HomeClient(real, self.home)
        self.assertEqual(client.get("https://other.example/x"),
                         b"from the network")


class TheRealConfig(unittest.TestCase):
    def test_every_url_is_one_the_pipeline_asks_for(self):
        import council_sources
        import dmmo_applications
        with open(os.path.join(ROOT, "home-collected",
                               "collector.json")) as fh:
            config = json.load(fh)
        urls = dict((s["id"], s["url"]) for s in config["sources"])
        self.assertEqual(urls["dorset-closures"], council_sources.DORSET_WFS)
        self.assertEqual(urls["dorset-dmmo"], dmmo_applications.DORSET_DMMO)
        with open(os.path.join(ROOT, "tro", "register",
                               "pages.json")) as fh:
            page_urls = set(p["url"] for p in json.load(fh))
        for sid, url in urls.items():
            if not sid.startswith("dorset"):
                self.assertIn(url, page_urls, sid)

    def test_the_collector_reads_only_councils_that_refuse_the_runners(self):
        with open(os.path.join(ROOT, "home-collected",
                               "collector.json")) as fh:
            hosts = set(hc.polite_http.host_of(s["url"])
                        for s in json.load(fh)["sources"])
        self.assertEqual(hosts, {"gi.dorsetcouncil.gov.uk",
                                 "www.norfolk.gov.uk", "www.wiltshire.gov.uk",
                                 "en.powys.gov.uk"})


if __name__ == "__main__":
    unittest.main(verbosity=1)

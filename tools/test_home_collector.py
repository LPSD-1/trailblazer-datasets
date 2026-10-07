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
    """A GeoServer view: its feature ids are renumbered on every request."""
    return json.dumps({"type": "FeatureCollection", "timeStamp": stamp,
                       "features": [{"id": "route_closed.fid--28fd165a_"
                                           "1a115fe11b7_%x" % (
                                               (stamp * 4000 + n) *
                                               (-1 if stamp % 2 else 1)),
                                     "properties": {"code": i}}
                                    for n, i in enumerate(reversed(ids))]}
                      ).encode()


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
        self.assertIn(b'"code":"c"', self.snapshot(WFS))

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


SPLIT = {"sources": [
    dict(CONFIG["sources"][0], machine="server"),
    dict(CONFIG["sources"][1])]}


class TwoMachines(unittest.TestCase):
    """The server reads Dorset and Powys; the PC at home reads the councils
    that refuse data centres. Neither may read or write the other's."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        with open(os.path.join(self.home, "collector.json"), "w") as fh:
            json.dump(SPLIT, fh)

    def tearDown(self):
        shutil.rmtree(self.home)

    def beat(self, machine):
        path = os.path.join(self.home, hc.heartbeat_name(machine))
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return json.load(fh)

    def test_each_machine_reads_only_its_own_sources(self):
        server = Client({WFS: layer(1)})
        hc.collect(server, self.home, today="2026-10-08", machine="server")
        self.assertEqual([u for u, _lm in server.asked], [WFS])
        home = Client({PAGE: page("a"), PDF: b"%PDF"})
        hc.collect(home, self.home, today="2026-10-08", machine="home")
        self.assertEqual([u for u, _lm in home.asked], [PAGE, PDF],
                         "the documents a page links follow their page")

    def test_each_machine_keeps_its_own_heartbeat(self):
        hc.collect(Client({WFS: layer(1)}), self.home, today="2026-10-08",
                   machine="server")
        self.assertEqual(self.beat("server")["sources_read"], 1)
        self.assertIsNone(self.beat("home"),
                          "the server wrote the PC's heartbeat")
        self.assertEqual(hc.heartbeat_name("home"), "heartbeat.json")

    def test_a_pc_that_stops_is_noticed_while_the_server_carries_on(self):
        hc.collect(Client({PAGE: page("a"), PDF: b"%PDF"}), self.home,
                   today="2026-10-01", machine="home")
        hc.collect(Client({WFS: layer(1)}), self.home, today="2026-10-08",
                   machine="server")
        said = hc.stale(self.home, 3, today="2026-10-08")
        self.assertIn("the home collector last read its councils on "
                      "2026-10-01", said)
        self.assertNotIn("server", said)

    def test_and_a_server_that_stops_is_noticed_too(self):
        hc.collect(Client({PAGE: page("a"), PDF: b"%PDF"}), self.home,
                   today="2026-10-08", machine="home")
        self.assertIn("the server collector has never reported",
                      hc.stale(self.home, 3, today="2026-10-08"))


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
                         "collected directly from the council 2026-10-08")
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


class AClashNeverJamsTheClone(unittest.TestCase):
    """Real git, local only: a bare repository standing in for GitHub and two
    clones standing in for the two machines. If both ever change the same
    file, the loser's rebase fails - and a clone left mid-rebase would fail
    every run after it, silently. sync() must leave it level with GitHub."""

    def git(self, cwd, *args):
        import subprocess
        out = subprocess.run(["git"] + list(args), cwd=cwd,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True)
        self.assertEqual(out.returncode, 0, out.stdout)
        return out.stdout.strip()

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.origin = os.path.join(self.root, "origin.git")
        self.git(self.root, "init", "-q", "--bare", "-b", "main",
                 self.origin)
        self.a = self.clone("a")
        os.makedirs(os.path.join(self.a, "home-collected"))
        self.write(self.a, "seed")
        self.git(self.a, "add", "-A")
        self.git(self.a, "commit", "-q", "-m", "seed")
        self.git(self.a, "push", "-q", "-u", "origin", "main")
        self.b = self.clone("b")

    def tearDown(self):
        def unlock(func, path, _exc):
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(self.root, onerror=unlock)

    def clone(self, name):
        path = os.path.join(self.root, name)
        self.git(self.root, "clone", "-q", self.origin, path)
        self.git(path, "config", "user.name", name)
        self.git(path, "config", "user.email", "%s@example.invalid" % name)
        self.git(path, "config", "core.autocrlf", "false")
        return path

    def write(self, repo, text):
        with open(os.path.join(repo, "home-collected", "index.json"), "w",
                  newline="\n") as fh:
            fh.write(text + "\n")

    def test_a_failed_rebase_is_abandoned_and_the_clone_reset(self):
        self.write(self.a, "from a")
        hc.publish(self.a, "a", machine="server")
        self.write(self.b, "from b")
        with self.assertRaises(RuntimeError):
            hc.publish(self.b, "b", tries=1)
        hc.sync(self.b)
        git_dir = os.path.join(self.b, ".git")
        self.assertFalse(os.path.isdir(os.path.join(git_dir,
                                                    "rebase-merge")))
        self.assertFalse(os.path.isdir(os.path.join(git_dir,
                                                    "rebase-apply")))
        self.assertEqual(self.git(self.b, "rev-parse", "HEAD"),
                         self.git(self.a, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.b, "status", "--porcelain"), "")

    def test_a_clone_a_crash_left_mid_rebase_is_brought_back(self):
        self.write(self.a, "from a")
        hc.publish(self.a, "a", machine="server")
        self.write(self.b, "from b")
        self.git(self.b, "commit", "-q", "-am", "b")
        import subprocess
        subprocess.run(["git", "pull", "-q", "--rebase"], cwd=self.b,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertTrue(os.path.isdir(os.path.join(self.b, ".git",
                                                   "rebase-merge")) or
                        os.path.isdir(os.path.join(self.b, ".git",
                                                   "rebase-apply")),
                        "PREMISE: the clone is stuck mid-rebase")
        hc.sync(self.b)
        # HEAD alone proves nothing: mid-rebase it already sits on a's
        # commit. The rebase must be gone and the tree clean.
        for leftover in ("rebase-merge", "rebase-apply"):
            self.assertFalse(os.path.isdir(os.path.join(self.b, ".git",
                                                        leftover)))
        self.assertEqual(self.git(self.b, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.b, "rev-parse", "HEAD"),
                         self.git(self.a, "rev-parse", "HEAD"))


class WhichMachineReadsWhat(unittest.TestCase):
    """Measured 7 Oct 2026 from the Oracle server in London: Dorset and
    Powys answer it; Norfolk and Wiltshire (Cloudflare) refuse it with a
    403. A council moved to the server that refuses it would go unread."""

    def test_the_councils_that_refuse_data_centres_stay_at_home(self):
        with open(os.path.join(ROOT, "home-collected",
                               "collector.json")) as fh:
            sources = json.load(fh)["sources"]
        where = dict((hc.polite_http.host_of(s["url"]), hc.machine_of(s))
                     for s in sources)
        self.assertEqual(where, {"gi.dorsetcouncil.gov.uk": "server",
                                 "en.powys.gov.uk": "server",
                                 "www.norfolk.gov.uk": "home",
                                 "www.wiltshire.gov.uk": "home"})


if __name__ == "__main__":
    unittest.main(verbosity=1)

#!/usr/bin/env python3
"""The fetcher every council source goes through keeps the project's rules.

    python tools/test_polite_http.py

No network: every request is answered by a stand-in that records what was
asked. The rules are the owner's (7 October 2026): say who we are, obey
robots.txt, never work around a block, keep request rates gentle, and never
write to a council's service. Each test below is the one that goes red if a
rule is broken.
"""
import os
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import polite_http  # noqa: E402
from polite_http import FetchFailed, PoliteClient, Refused  # noqa: E402


class Stand(object):
    """Answers by URL; records every request and every sleep."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []
        self.slept = []
        self.now = 0.0

    def open(self, url, timeout):
        self.asked.append(url)
        for key, value in self.answers.items():
            if key in url:
                if isinstance(value, list):
                    return value.pop(0) if len(value) > 1 else value[0]
                return value
        return 404, {}, b"not here"

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now

    def client(self, **kw):
        return PoliteClient(opener=self.open, sleep=self.sleep,
                            clock=self.clock, log=lambda *a: None, **kw)


ROBOTS_OK = (200, {}, b"User-agent: *\nDisallow: /private/\n")


class Robots(unittest.TestCase):
    def test_a_disallowed_path_is_never_requested(self):
        stand = Stand({"/robots.txt": ROBOTS_OK})
        with self.assertRaises(Refused):
            stand.client().get("https://council.example.gov.uk/private/x")
        self.assertEqual(stand.asked,
                         ["https://council.example.gov.uk/robots.txt"])

    def test_an_allowed_path_is_fetched(self):
        stand = Stand({"/robots.txt": ROBOTS_OK,
                       "/open/": (200, {}, b"{}")})
        self.assertEqual(stand.client().get(
            "https://council.example.gov.uk/open/x"), b"{}")

    def test_a_missing_robots_file_means_no_rules(self):
        # RFC 9309: a 4xx robots.txt is "unavailable", and the crawler may
        # access anything. ArcGIS Online answers 403 for it on every host.
        stand = Stand({"/robots.txt": (403, {}, b""),
                       "/data": (200, {}, b"ok")})
        self.assertEqual(stand.client().get("https://x.example/data"), b"ok")

    def test_an_unreachable_robots_file_means_keep_out(self):
        stand = Stand({"/robots.txt": (503, {}, b"")})
        with self.assertRaises(Refused):
            stand.client().get("https://x.example/data")
        self.assertEqual(len(stand.asked), 1)

    def test_robots_is_read_once_per_host(self):
        stand = Stand({"/robots.txt": ROBOTS_OK, "/a": (200, {}, b"1"),
                       "/b": (200, {}, b"2")})
        client = stand.client()
        client.get("https://h.example/a")
        client.get("https://h.example/b")
        self.assertEqual(sum(1 for u in stand.asked if "robots" in u), 1)


class Rfc9309(unittest.TestCase):
    """Where the standard library's robots parser is wrong (measured on
    Norfolk's and Powys's files, 7 October 2026)."""

    def test_a_wildcard_disallow_blocks_what_it_names(self):
        r = polite_http.Robots("User-agent: *\nDisallow: /*.pdf\n")
        self.assertFalse(r.can_fetch("https://n.example/docs/order.pdf"))
        self.assertTrue(r.can_fetch("https://n.example/docs/order.html"))

    def test_two_groups_for_every_agent_are_combined(self):
        r = polite_http.Robots("User-agent: *\nDisallow: /a/\n\n"
                               "User-agent: *\nDisallow: /media/\n")
        self.assertFalse(r.can_fetch("https://p.example/media/x.zip"))
        self.assertFalse(r.can_fetch("https://p.example/a/b"))

    def test_the_longest_rule_wins_and_allow_wins_a_tie(self):
        r = polite_http.Robots("User-agent: *\nDisallow: /wp-admin/\n"
                               "Allow: /wp-admin/admin-ajax.php\n"
                               "Disallow: /p\nAllow: /p\n")
        self.assertTrue(r.can_fetch(
            "https://d.example/wp-admin/admin-ajax.php"))
        self.assertFalse(r.can_fetch("https://d.example/wp-admin/x"))
        self.assertTrue(r.can_fetch("https://d.example/page"))

    def test_a_dollar_anchors_the_end(self):
        r = polite_http.Robots("User-agent: *\nDisallow: /*.json$\n")
        self.assertFalse(r.can_fetch("https://h.example/a.json"))
        self.assertTrue(r.can_fetch("https://h.example/a.json?x=1"))

    def test_our_own_group_overrides_the_general_one(self):
        r = polite_http.Robots("User-agent: *\nDisallow: /\n\n"
                               "User-agent: TrailBlazer-datasets\n"
                               "Allow: /\n")
        self.assertTrue(r.can_fetch("https://h.example/x"))
        r = polite_http.Robots("User-agent: *\nAllow: /\n\n"
                               "User-agent: TrailBlazer-datasets\n"
                               "Disallow: /\n")
        self.assertFalse(r.can_fetch("https://h.example/x"))

    def test_an_empty_agent_line_is_nobody(self):
        r = polite_http.Robots("User-agent:\nDisallow: /\n\n"
                               "User-agent: *\nAllow: /\n")
        self.assertTrue(r.can_fetch("https://h.example/x"))

    def test_disallow_all_disallows_all(self):
        r = polite_http.Robots("User-agent: *\nDisallow: /\n")
        self.assertFalse(r.can_fetch("https://gis.example/arcgis/rest/x"))


class Blocks(unittest.TestCase):
    def test_a_blocked_host_gets_no_request_at_all(self):
        for url in ("https://www.hants.gov.uk/x",
                    "https://gis2.westberks.gov.uk/arcgis/rest/services",
                    "https://www.publicnoticeportal.uk/notice",
                    "https://www.thegazette.co.uk/all-notices",
                    "https://www.trf.org.uk/x",
                    "https://apps.derbyshire.gov.uk/applications/x"):
            stand = Stand({})
            with self.assertRaises(Refused):
                stand.client().get(url)
            self.assertEqual(stand.asked, [], url)

    def test_a_403_is_a_refusal_and_is_not_retried(self):
        stand = Stand({"/robots.txt": (404, {}, b""),
                       "/x": (403, {}, b"forbidden")})
        with self.assertRaises(Refused):
            stand.client().get("https://h.example/x")
        self.assertEqual(stand.asked.count("https://h.example/x"), 1)

    def test_a_challenge_page_is_a_refusal_even_with_a_200(self):
        stand = Stand({"/robots.txt": (404, {}, b""),
                       "/x": (200, {}, b"<title>Just a moment...</title>")})
        with self.assertRaises(Refused):
            stand.client().get("https://h.example/x")

    def test_only_https_is_fetched(self):
        with self.assertRaises(Refused):
            Stand({}).client().get("http://h.example/x")


class ReadOnly(unittest.TestCase):
    LAYER = ("https://maps.wiltshire.gov.uk/arcgis/rest/services/OpenData/"
             "PublicRightsofWay/FeatureServer/0")

    def test_an_edit_operation_is_refused_before_any_request(self):
        for op in ("applyEdits", "addFeatures", "updateFeatures",
                   "deleteFeatures"):
            stand = Stand({})
            with self.assertRaises(Refused):
                stand.client().get(self.LAYER + "/" + op + "?f=json")
            self.assertEqual(stand.asked, [], op)

    def test_metadata_and_query_are_allowed(self):
        stand = Stand({"/robots.txt": (404, {}, b""),
                       "FeatureServer": (200, {}, b"{}")})
        client = stand.client()
        client.get(self.LAYER + "?f=json")
        client.get(self.LAYER + "/query?where=1%3D1&f=json")

    def test_a_wordpress_uploads_folder_is_not_an_edit(self):
        stand = Stand({"/robots.txt": (404, {}, b""),
                       "/wp-content/uploads/": (200, {}, b"%PDF-1.4")})
        self.assertEqual(stand.client().get(
            "https://www.yorkshiredales.example/wp-content/uploads/sites/13/"
            "2019/08/CURRENT-TROS.pdf"), b"%PDF-1.4")

    def test_any_other_arcgis_path_is_refused(self):
        with self.assertRaises(Refused):
            Stand({}).client().get(self.LAYER + "/0/attachments/1")


class Gentle(unittest.TestCase):
    def test_two_requests_to_one_host_are_spaced(self):
        stand = Stand({"/robots.txt": (404, {}, b""), "/a": (200, {}, b"1")})
        client = stand.client(min_gap=2.0)
        client.get("https://h.example/a")
        client.get("https://h.example/a")
        self.assertTrue(stand.slept and min(stand.slept) > 0)
        self.assertAlmostEqual(sum(stand.slept), 4.0)

    def test_a_429_waits_as_long_as_it_is_told_then_retries(self):
        stand = Stand({"/robots.txt": (404, {}, b""),
                       "/a": [(429, {"Retry-After": "60"}, b""),
                              (200, {}, b"done")]})
        self.assertEqual(stand.client().get("https://h.example/a"), b"done")
        self.assertIn(60.0, stand.slept)

    def test_retries_run_out(self):
        stand = Stand({"/robots.txt": (404, {}, b""),
                       "/a": (503, {}, b"down")})
        with self.assertRaises(FetchFailed):
            stand.client(retries=2).get("https://h.example/a")
        self.assertEqual(stand.asked.count("https://h.example/a"), 3)

    def test_an_arcgis_error_inside_a_200_is_a_failure(self):
        stand = Stand({"/robots.txt": (404, {}, b""), "/q": (
            200, {}, b'{"error": {"code": 429, "message": "quota"}}')})
        with self.assertRaises(FetchFailed):
            stand.client().get_json("https://h.example/q")


class Honest(unittest.TestCase):
    def test_the_user_agent_names_this_repository(self):
        self.assertIn("github.com/LPSD-1/trailblazer-datasets",
                      polite_http.USER_AGENT)
        self.assertNotIn("Mozilla", polite_http.USER_AGENT)

    def test_the_real_opener_sends_it(self):
        seen = {}

        class Resp(object):
            status = 200
            headers = {}

            def read(self):
                return b"x"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(request, timeout):
            seen["ua"] = request.get_header("User-agent")
            seen["method"] = request.get_method()
            return Resp()

        real = urllib.request.urlopen
        urllib.request.urlopen = fake
        try:
            PoliteClient()._urlopen("https://h.example/x", 5)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(seen["ua"], polite_http.USER_AGENT)
        self.assertEqual(seen["method"], "GET")


if __name__ == "__main__":
    unittest.main(verbosity=1)

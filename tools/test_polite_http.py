#!/usr/bin/env python3
"""The fetcher every council source goes through keeps the project's rules.

    python tools/test_polite_http.py

No network: every request is answered by a stand-in that records what was
asked. The rules are the owner's (7 October 2026): say who we are, obey
robots.txt, never work around a block, keep request rates gentle, and never
write to a council's service. Each test below is the one that goes red if a
rule is broken.
"""
import json
import os
import sys
import unittest
import urllib.parse
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

    def test_only_a_200_or_a_4xx_robots_file_lets_anything_through(self):
        # RFC 9309 2.3.1: a 5xx is a complete disallow; a 304, a 3xx left
        # unfollowed or a 204 is not a robots file either, and its empty
        # body must not read as "no rules".
        for status in (304, 301, 204, 500, 503, None):
            self.assertEqual(polite_http.robots_verdict(status, ""),
                             "disallow-all", status)
        self.assertEqual(polite_http.robots_verdict(404, ""), "allow-all")
        self.assertIsInstance(polite_http.robots_verdict(200, ""),
                              polite_http.Robots)

    def test_robots_is_asked_for_without_a_documents_validators(self):
        # A conditional GET for a document must not send its
        # If-Modified-Since to robots.txt: a server answers that 304 with
        # no body, which used to read as an empty file - allow everything.
        sent = {}

        def opener(url, timeout):
            headers = dict(client._extra_headers)
            sent.setdefault(url, []).append(headers)
            if url.endswith("/robots.txt"):
                if "If-Modified-Since" in headers:
                    return 304, {}, b""
                return 200, {}, b"User-agent: *\nDisallow: /private/\n"
            return 200, {}, b"secret"

        client = PoliteClient(opener=opener, sleep=lambda s: None,
                              log=lambda *a: None)
        with self.assertRaises(Refused):
            client.get_if_changed("https://h.example/private/a.json",
                                  last_modified="Tue, 28 Apr 2026 "
                                                "07:35:24 GMT")
        self.assertEqual(sent["https://h.example/robots.txt"], [{}])
        self.assertNotIn("https://h.example/private/a.json", sent)

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

    def test_only_our_own_product_token_is_our_group(self):
        # RFC 9309 2.2.1: the group whose user-agent IS our product token,
        # any case. "data" is a substring of it and names someone else.
        r = polite_http.Robots("User-agent: data\nDisallow: /\n\n"
                               "User-agent: *\nAllow: /\n")
        self.assertTrue(r.can_fetch("https://h.example/x"))
        r = polite_http.Robots("User-agent: trailblazer\nDisallow: /\n\n"
                               "User-agent: *\nAllow: /\n")
        self.assertTrue(r.can_fetch("https://h.example/x"))
        r = polite_http.Robots("User-agent: *\nAllow: /\n\n"
                               "User-agent: TRAILBLAZER-DATASETS/1.0\n"
                               "Disallow: /\n")
        self.assertFalse(r.can_fetch("https://h.example/x"))


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

    WFS = "https://maps.cheshireeast.example/geoserver/CEOpenData/wfs"

    def test_a_wfs_transaction_is_refused_before_any_request(self):
        for op in ("Transaction", "transaction", "LockFeature",
                   "GetFeatureWithLock"):
            stand = Stand({})
            with self.assertRaises(Refused):
                stand.client().get(self.WFS + "?service=WFS&REQUEST=" + op)
            self.assertEqual(stand.asked, [], op)

    def test_a_wfs_getfeature_is_allowed(self):
        stand = Stand({"/robots.txt": (404, {}, b""),
                       "/wfs": (200, {}, b"{}")})
        stand.client().get(self.WFS + "?service=WFS&version=2.0.0&"
                           "request=GetFeature&typeNames=x")


class Conditional(unittest.TestCase):
    def test_an_unchanged_file_is_a_304_and_no_body(self):
        sent = []

        def opener(url, timeout):
            if url.endswith("/robots.txt"):
                return 404, {}, b""
            sent.append(dict(client._extra_headers))
            return 304, {}, b""

        client = PoliteClient(opener=opener, sleep=lambda s: None,
                              log=lambda *a: None)
        changed, body, _h = client.get_if_changed(
            "https://h.example/a.json", last_modified="Tue, 28 Apr 2026 "
                                                      "07:35:24 GMT")
        self.assertEqual((changed, body), (False, None))
        self.assertEqual(sent, [{"If-Modified-Since": "Tue, 28 Apr 2026 "
                                                      "07:35:24 GMT"}])
        self.assertEqual(client._extra_headers, {},
                         "a validator leaked into the next request")

    def test_a_changed_file_comes_back_with_its_validator(self):
        stand = Stand({"/robots.txt": (404, {}, b""),
                       "/a.json": (200, {"Last-Modified": "x"}, b"{}")})
        changed, body, headers = stand.client().get_if_changed(
            "https://h.example/a.json", last_modified="old")
        self.assertEqual((changed, body, headers["Last-Modified"]),
                         (True, b"{}", "x"))

    def test_a_304_to_a_plain_get_is_a_failure_not_an_empty_body(self):
        stand = Stand({"/robots.txt": (404, {}, b""),
                       "/a.json": (304, {}, b"")})
        with self.assertRaises(FetchFailed):
            stand.client().get("https://h.example/a.json")


class OwnersRobotsDecision(unittest.TestCase):
    """robots_override.json: the paths the owner chose to read anyway."""

    OVERRIDE = [{"id": "x-orders", "prefix": "https://c.example/library/",
                 "pattern": r"(?i)\.pdf$"}]
    DOC = "https://c.example/library/byway-7-order.pdf"
    ROBOTS = (200, {}, b"User-agent: *\nDisallow: /library/\n")

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "reads.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def client(self, stand, today="2026-10-08", log=True):
        return stand.client(overrides=self.OVERRIDE, today=today,
                            override_log=self.log if log else None)

    def test_an_overridden_path_is_read_logged_and_credited(self):
        stand = Stand({"/robots.txt": self.ROBOTS,
                       "/library/": (200, {}, b"%PDF-1.4")})
        client = self.client(stand)
        self.assertEqual(client.get(self.DOC), b"%PDF-1.4")
        self.assertEqual(client.overridden, {self.DOC: "x-orders"})
        with open(self.log) as fh:
            self.assertEqual(json.load(fh)[self.DOC]["read"], "2026-10-08")

    def test_at_most_weekly(self):
        stand = Stand({"/robots.txt": self.ROBOTS,
                       "/library/": (200, {}, b"%PDF-1.4")})
        self.client(stand).get(self.DOC)
        with self.assertRaises(Refused):
            self.client(stand, today="2026-10-14").get(self.DOC)
        self.assertEqual(self.client(stand, today="2026-10-15").get(
            self.DOC), b"%PDF-1.4")

    def test_everything_else_on_the_host_still_obeys_robots(self):
        stand = Stand({"/robots.txt": self.ROBOTS})
        # In the prefix, but not what the pattern names.
        with self.assertRaises(Refused):
            self.client(stand).get("https://c.example/library/minutes.docx")
        self.assertEqual(stand.asked, ["https://c.example/robots.txt"])

    def test_no_log_no_override(self):
        stand = Stand({"/robots.txt": self.ROBOTS})
        with self.assertRaises(Refused):
            self.client(stand, log=False).get(self.DOC)

    def test_a_403_on_an_overridden_path_is_still_a_refusal(self):
        stand = Stand({"/robots.txt": self.ROBOTS,
                       "/library/": (403, {}, b"Forbidden")})
        with self.assertRaises(Refused):
            self.client(stand).get(self.DOC)

    def test_a_whole_host_robots_block_opens_only_for_the_listed_path(self):
        stand = Stand({"/robots.txt": (200, {}, b"User-agent: *\n"
                                                b"Disallow: /\n"),
                       "/applications/": (200, {}, b"<html>register</html>")})
        client = stand.client(overrides=[{
            "id": "d", "prefix": "https://apps.derbyshire.gov.uk/"
                                 "applications/path-closure-register/"}],
            override_log=self.log, today="2026-10-08")
        self.assertIn(b"register", client.get(
            "https://apps.derbyshire.gov.uk/applications/"
            "path-closure-register/"))
        with self.assertRaises(Refused):
            client.get("https://apps.derbyshire.gov.uk/applications/"
                       "right-of-way/results.asp")

    def test_no_override_can_reach_a_challenge_or_ruled_out_host(self):
        for host in ("www.kent.gov.uk", "www.thegazette.co.uk",
                     "www.westberks.gov.uk"):
            path = os.path.join(self.tmp, "o.json")
            with open(path, "w") as fh:
                json.dump({"paths": [{"id": "bad", "prefix":
                                      "https://%s/x/" % host}]}, fh)
            with self.assertRaises(ValueError):
                polite_http.load_overrides(path)

    def test_a_challenge_host_stays_refused_even_if_an_override_names_it(self):
        stand = Stand({})
        client = stand.client(overrides=[{"id": "bad", "prefix":
                                          "https://www.kent.gov.uk/x/"}],
                              override_log=self.log)
        with self.assertRaises(Refused):
            client.get("https://www.kent.gov.uk/x/a.pdf")
        self.assertEqual(stand.asked, [])

    def test_the_owners_file_loads_and_names_only_what_was_decided(self):
        got = polite_http.load_overrides()
        self.assertEqual(sorted(e["authority"] for e in got),
                         ["Cambridgeshire", "Derbyshire", "Hertfordshire",
                          "Powys"])
        for e in got:
            self.assertTrue(e["prefix"].startswith("https://"))


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


class Transport(urllib.request.HTTPSHandler):
    """A stand-in for the network under the REAL opener: answers https by
    URL with (status, headers, body), so urllib's own redirect machinery
    runs exactly as it would against a server."""

    def __init__(self, answers):
        urllib.request.HTTPSHandler.__init__(self)
        self.answers, self.asked, self.methods = answers, [], []

    def https_open(self, req):
        import email.message
        import io
        import urllib.response
        url = req.full_url
        self.asked.append((url, dict(req.header_items())))
        self.methods.append(req.get_method())
        status, headers, body = self.answers.get(url, (404, {}, b""))
        msg = email.message.Message()
        for key, value in headers.items():
            msg[key] = value
        resp = urllib.response.addinfourl(io.BytesIO(body), msg, url,
                                          status)
        resp.msg = "stand-in"
        return resp


def real_client(answers, **kw):
    transport = Transport(answers)
    client = PoliteClient(sleep=lambda s: None, log=lambda *a: None, **kw)
    client._handlers = (transport,)
    return client, transport


class Redirects(unittest.TestCase):
    """urllib follows a redirect by itself, so every Location must pass the
    same checks as the URL asked for, or a council (or anyone between) could
    walk us to http, to a blocked host or to a path robots.txt forbids."""

    ROBOTS = (200, {}, b"User-agent: *\nDisallow: /private/\n")

    def go(self, location, extra=None):
        answers = {"https://h.example/robots.txt": self.ROBOTS,
                   "https://h.example/a": (302, {"Location": location}, b""),
                   "https://h.example/ok": (200, {}, b"followed")}
        answers.update(extra or {})
        return real_client(answers)

    def asked(self, transport):
        return [u for u, _h in transport.asked]

    def test_an_allowed_redirect_is_followed(self):
        client, transport = self.go("https://h.example/ok")
        self.assertEqual(client.get("https://h.example/a"), b"followed")
        self.assertIn("https://h.example/ok", self.asked(transport))

    def test_never_to_http(self):
        client, transport = self.go("http://h.example/ok")
        with self.assertRaises(Refused):
            client.get("https://h.example/a")
        self.assertNotIn("http://h.example/ok", self.asked(transport))
        self.assertEqual(self.asked(transport).count("https://h.example/a"),
                         1, "a refused redirect was retried")

    def test_never_to_a_blocked_host(self):
        client, transport = self.go("https://www.kent.gov.uk/x")
        with self.assertRaises(Refused):
            client.get("https://h.example/a")
        self.assertFalse([u for u in self.asked(transport)
                          if "kent.gov.uk" in u])

    def test_never_to_a_path_robots_disallows(self):
        client, transport = self.go("https://h.example/private/x",
                                    {"https://h.example/private/x":
                                     (200, {}, b"secret")})
        with self.assertRaises(Refused):
            client.get("https://h.example/a")
        self.assertNotIn("https://h.example/private/x",
                         self.asked(transport))

    def test_another_hosts_robots_is_read_before_following_to_it(self):
        client, transport = self.go(
            "https://other.example/private/y",
            {"https://other.example/robots.txt": self.ROBOTS,
             "https://other.example/private/y": (200, {}, b"secret")})
        with self.assertRaises(Refused):
            client.get("https://h.example/a")
        self.assertIn("https://other.example/robots.txt",
                      self.asked(transport))
        self.assertNotIn("https://other.example/private/y",
                         self.asked(transport))

    def test_never_to_an_edit_operation(self):
        client, transport = self.go(
            "https://h.example/arcgis/rest/services/x/FeatureServer/0/"
            "applyEdits")
        with self.assertRaises(Refused):
            client.get("https://h.example/a")
        self.assertFalse([u for u in self.asked(transport)
                          if "applyEdits" in u])


class Honest(unittest.TestCase):
    def test_the_user_agent_names_this_repository(self):
        self.assertIn("github.com/LPSD-1/trailblazer-datasets",
                      polite_http.USER_AGENT)
        self.assertNotIn("Mozilla", polite_http.USER_AGENT)

    def test_the_real_opener_sends_it(self):
        client, transport = real_client({"https://h.example/x":
                                         (200, {}, b"x")})
        self.assertEqual(client._urlopen("https://h.example/x", 5)[2], b"x")
        (_url, headers), = transport.asked
        self.assertEqual(headers.get("User-agent"), polite_http.USER_AGENT)
        self.assertEqual(transport.methods, ["GET"])


# ------------------------------------------------- the one POST (7 Oct 2026)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "wiltshire")
PAGE = "https://apps.wiltshire.gov.uk/RightsOfWay/Closure"
ACTION = "https://apps.wiltshire.gov.uk/RightsOfWay/Closure/Result"
SEARCH = {"RowType": "1", "ClosureType": "0", "Act": "Search"}


def _fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


class FormTransport(Transport):
    """Transport, also keeping each request's body and its Cookie header."""

    def __init__(self, answers):
        Transport.__init__(self, answers)
        self.bodies, self.cookies = [], []

    def https_open(self, req):
        self.bodies.append(req.data)
        self.cookies.append(req.get_header("Cookie") or
                            req.unredirected_hdrs.get("Cookie"))
        return Transport.https_open(self, req)


def form_client(page=None):
    answers = {
        "https://apps.wiltshire.gov.uk/robots.txt": (404, {}, b""),
        PAGE: (200, {"Set-Cookie": ".AspNetCore.Antiforgery.x=tok; "
                                   "path=/; secure"},
               page if page is not None else _fixture("form.html")),
        ACTION: (200, {}, _fixture("result-boat.html")),
    }
    transport = FormTransport(answers)
    client = PoliteClient(sleep=lambda s: None, log=lambda *a: None)
    client._handlers = (transport,)
    return client, transport


class TheOnePost(unittest.TestCase):
    """The owner approved one POST on 7 October 2026: Wiltshire's closures
    register's own search form, read only. Every other POST is refused
    before a request is made; the one that is sent carries the form's own
    fields and nothing else."""

    def posted(self, transport):
        return [(u, m) for (u, _h), m in zip(transport.asked,
                                             transport.methods)
                if m != "GET"]

    def test_the_approved_search_is_submitted_as_its_page_offers_it(self):
        client, transport = form_client()
        body = client.post_form(PAGE, ACTION, SEARCH)
        self.assertIn(b"Showing all 49 items", body)
        self.assertEqual(transport.methods, ["GET", "GET", "POST"])
        self.assertEqual([u for u, _h in transport.asked],
                         ["https://apps.wiltshire.gov.uk/robots.txt", PAGE,
                          ACTION])
        sent = dict(urllib.parse.parse_qsl(transport.bodies[2].decode(),
                                           keep_blank_values=True))
        # The form's own fields, exactly: its hidden ones as the page gave
        # them, its token, and the search; no button but the one pressed.
        self.assertEqual(sorted(sent), sorted([
            "AppID", "RowID", "Day", "Month", "Year", "Parish",
            "GridReference", "PostCode", "ClosureType", "RowType",
            "UserID", "Archived", "__RequestVerificationToken", "Act"]))
        self.assertEqual(sent["__RequestVerificationToken"], "FIXTURE-TOKEN")
        self.assertEqual(sent["Archived"], "ActiveOnly")
        self.assertEqual((sent["RowType"], sent["ClosureType"], sent["Act"]),
                         ("1", "0", "Search"))
        # The page's anti-forgery cookie goes back with the POST, and only
        # there; no request after it carries one.
        self.assertEqual(transport.cookies[2], ".AspNetCore.Antiforgery.x=tok")
        self.assertIsNone(client._cookies)
        (_u, headers), = [transport.asked[2]]
        self.assertEqual(headers.get("User-agent"), polite_http.USER_AGENT)

    def test_no_other_action_on_the_same_host(self):
        for action in ("https://apps.wiltshire.gov.uk/RightsOfWay/Map/Result",
                       "https://apps.wiltshire.gov.uk/RightsOfWay/Closure/"
                       "Create",
                       ACTION + "?id=1", ACTION + "/",
                       "http://apps.wiltshire.gov.uk/RightsOfWay/Closure/"
                       "Result"):
            client, transport = form_client()
            with self.assertRaises(Refused, msg=action):
                client.post_form(PAGE, action, SEARCH)
            self.assertEqual(transport.asked, [], action)

    def test_no_other_host(self):
        client, transport = form_client()
        with self.assertRaises(Refused):
            client.post_form("https://h.example/form",
                             "https://h.example/form/Result", SEARCH)
        self.assertEqual(transport.asked, [])

    def test_not_from_another_page(self):
        client, transport = form_client()
        with self.assertRaises(Refused):
            client.post_form("https://apps.wiltshire.gov.uk/RightsOfWay/Map",
                             ACTION, SEARCH)
        self.assertEqual(transport.asked, [])

    def test_no_field_beyond_the_search(self):
        for extra in ({"Delete": "1"}, {"Archived": "All"},
                      {"__RequestVerificationToken": "x"}, {"UserID": "me"}):
            client, transport = form_client()
            with self.assertRaises(Refused, msg=extra):
                client.post_form(PAGE, ACTION, dict(SEARCH, **extra))
            self.assertEqual(transport.asked, [], extra)

    def test_no_value_the_form_does_not_offer(self):
        for bad in ({"RowType": "9"}, {"Act": "Delete"},
                    {"ClosureType": "0; drop"}):
            client, transport = form_client()
            with self.assertRaises(Refused, msg=bad):
                client.post_form(PAGE, ACTION, dict(SEARCH, **bad))
            self.assertEqual(self.posted(transport), [], bad)

    def test_a_page_whose_form_posts_elsewhere_gets_no_post(self):
        page = _fixture("form.html").replace(
            b'action="/RightsOfWay/Closure/Result"',
            b'action="/RightsOfWay/Closure/Update"')
        client, transport = form_client(page)
        with self.assertRaises(FetchFailed):
            client.post_form(PAGE, ACTION, SEARCH)
        self.assertEqual(self.posted(transport), [])

    def test_a_get_never_carries_a_body_or_a_cookie(self):
        client, transport = form_client()
        client.post_form(PAGE, ACTION, SEARCH)
        client.get(PAGE)
        self.assertEqual(transport.methods[-1], "GET")
        self.assertIsNone(transport.bodies[-1])
        self.assertIsNone(transport.cookies[-1])

    def test_the_allowlist_is_that_one_form(self):
        self.assertEqual(sorted(polite_http.FORM_POSTS), [ACTION])
        self.assertIn("7 October 2026", polite_http.FORM_POSTS[ACTION][
            "decided"])
        self.assertIn("THE ONE POST", polite_http.__doc__)

    def test_council_fetchers_reach_the_web_only_through_this_client(self):
        # A council module opening its own connection could POST around
        # every check above.
        here = os.path.dirname(os.path.abspath(__file__))
        for name in ("council_sources", "council_orders", "council_ways",
                     "order_register", "home_collector", "dmmo_applications",
                     "wiltshire_closures"):
            with open(os.path.join(here, name + ".py"),
                      encoding="utf-8") as fh:
                text = fh.read()
            for needle in ("urllib.request", "http.client", "socket",
                           "requests"):
                self.assertNotRegex(text, r"(?m)^\s*(import|from)\s+%s"
                                    % needle.replace(".", r"\."), name)


if __name__ == "__main__":
    unittest.main(verbosity=1)

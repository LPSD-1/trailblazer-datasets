#!/usr/bin/env python3
"""The order register never loses an order list quietly.

    python tools/test_order_register_loud.py

Three ways it used to (the Mastiles Lane audit, 14 October 2026):
  1. a registered page read for the first time was snapshotted and never
     queued, so its orders were never transcribed;
  2. a page the reader could not see into (an ASP.NET page wrapped in one
     <form>) was stored as a 1-byte snapshot, so no change on it was ever
     seen;
  3. an approved order that failed matching was "held back" by `build` with
     exit 0 and nothing anyone would read.

No network: a stand-in client, a temporary register, synthetic byways.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import order_register as reg  # noqa: E402
from byway_match import Byways, Way  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402

# Derbyshire County Council's "Permanent Traffic Regulation Orders affecting
# public rights of way" page as served on 14 October 2026, trimmed to its
# shape: ASP.NET wraps the whole body, <main> included, in one <form>; the
# route list sits in <main>; a feedback widget's radio labels sit beside it.
DERBYSHIRE = b"""<!DOCTYPE html><html lang="en"><head>
<title>Permanent Traffic Regulation Orders affecting public rights of way -
Derbyshire County Council</title>
<script>window.dataLayer = window.dataLayer || [];</script></head>
<body><noscript><iframe src="https://example.invalid/ns"></iframe></noscript>
<form method="post" action="/leisure/countryside/access/rights-of-way/traffic-regulation-orders/permanent-traffic-regulation-orders-affecting-public-rights-of-way.aspx" id="form1">
<div class="aspNetHidden">
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="DiFhjkwR" />
</div>
<header><nav><a href="/home.aspx">Home</a></nav></header>
<main id="main">
<div class="row content"><div class="column large-9">
<h1>Permanent Traffic Regulation Orders affecting public rights of way</h1>
<p>Our TROs are attached to this page and affect the following routes:</p>
<ul>
<li>Beresford Dale - Hartington Town Quarter Byway Open to All Traffic 34</li>
<li>Jacobs Ladder - Stoney Middleton Byway Open to All Traffic 15</li>
</ul>
<h2>Permanent TROs made by the Peak District National Park Authority</h2>
<ul>
<li>Long Causeway, Outseats - Byway Open to All Traffic 48</li>
<li>Derby Lane - Monyash Byway Open to All Traffic 29</li>
</ul>
</div></div>
<section aria-label="Related documents"><div class="related-docs">
<h2>Related documents</h2><ul><li>
<a class="pdf" href="/site-elements/documents/pdf/tro-jacobs-ladder.pdf">
<span>TRO Jacobs Ladder, Stoney Middleton - Byway Open to All Traffic 15 - 898KB</span>
</a></li></ul></div></section>
<fieldset aria-label="Page feedback form"><legend>Feedback</legend>
<span><input value="I found what I wanted" type="radio" id="f1" />
<label for="f1">I found what I wanted</label></span>
<select name="why"><option>It's out of date</option></select>
<textarea name="more">Tell us more</textarea>
</fieldset>
</main>
<footer>Updated today</footer>
</form></body></html>"""

# Hertfordshire's shape: ASP.NET, no <main>, the orders in a table inside
# the one <form>. Before the fix only the <title> came out.
TABLE_IN_FORM = b"""<html><head><title>Rights of way - Permanent traffic
regulation orders | Hertfordshire County Council</title></head><body>
<form method="post" action="./ptros.aspx" id="aspnetForm">
<table><tr><th>Route</th><th>Order</th></tr>
<tr><td>Byway Open to All Traffic 3, Ashwell</td>
<td>Prohibition of motor vehicles 1998</td></tr>
<tr><td>Byway Open to All Traffic 21, Therfield</td>
<td>Seasonal, October to April 2004</td></tr></table></form></body></html>"""

LIST = ("<main><h1>Seasonal byway restrictions</h1><ul>"
        "<li>Arlington Byway 9a: closed to motor vehicles 1 October to 31 "
        "March</li><li>Firle Byway 10: no motor vehicles at any time</li>"
        "</ul></main>").encode()


class Client(object):
    def __init__(self, bodies):
        self.bodies = bodies
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        return self.bodies[url]


class Register(unittest.TestCase):
    URL = "https://www.council.example/byway-orders"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pages([{"id": "co/orders", "url": self.URL,
                     "council": "Example County Council",
                     "authorities": ["Example"]}])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def pages(self, pages):
        with open(os.path.join(self.tmp, "pages.json"), "w") as fh:
            json.dump(pages, fh)

    def check(self, body, url=None):
        return reg.check(Client({url or self.URL: body}), register=self.tmp,
                         today="2026-10-14",
                         manual_root=os.path.join(self.tmp, "manual"))

    def queued(self):
        with open(os.path.join(self.tmp, "changes.json")) as fh:
            return json.load(fh)

    def snapshot(self, page_id="co/orders"):
        out = []
        for path in reg.snapshot_paths(self.tmp, page_id):
            try:
                with open(path, "rb") as fh:
                    out.append(fh.read())
            except OSError:
                out.append(None)
        return out


class FirstRead(Register):
    """1. A page never read before is queued for transcription."""

    def test_a_page_never_read_is_queued_with_its_text(self):
        changed, unreachable, empty = self.check(LIST)
        self.assertEqual((unreachable, empty), ([], []))
        (entry,) = self.queued()
        self.assertEqual(changed, [entry])
        self.assertEqual(entry["page"], "co/orders")
        self.assertTrue(entry["new"])
        self.assertIn("transcribe", entry["diff"][0])
        self.assertIn("Firle Byway 10: no motor vehicles at any time",
                      entry["text"])

    def test_queued_until_accepted_and_no_snapshot_meanwhile(self):
        self.check(LIST)
        self.assertEqual(self.snapshot(), [None, None],
                         "a page nobody has transcribed was taken as read")
        self.check(LIST)
        self.assertEqual([c["page"] for c in self.queued()], ["co/orders"])
        reg.accept(["co/orders"], Client({}), register=self.tmp)
        changed, _u, _e = self.check(LIST)
        self.assertEqual((changed, self.queued()), ([], []))

    def test_a_page_read_before_and_unchanged_stays_quiet(self):
        self.check(LIST)
        reg.accept(["co/orders"], Client({}), register=self.tmp)
        before = self.snapshot()
        changed, unreachable, empty = self.check(LIST)
        self.assertEqual((changed, unreachable, empty), ([], [], []))
        self.assertEqual(self.snapshot(), before)

    def test_no_pages_at_all_is_an_empty_queue(self):
        self.pages([])
        self.assertEqual(self.check(LIST), ([], [], []))
        self.assertEqual(self.queued(), [])


class FormsAndTables(unittest.TestCase):
    """2a. The reader sees into ASP.NET pages."""

    def test_derbyshire_route_list_inside_the_form_is_read(self):
        lines = reg.page_text(DERBYSHIRE)
        for want in ("Jacobs Ladder - Stoney Middleton Byway Open to All "
                     "Traffic 15",
                     "Long Causeway, Outseats - Byway Open to All Traffic 48",
                     "TRO Jacobs Ladder, Stoney Middleton - Byway Open to "
                     "All Traffic 15 - 898KB"):
            self.assertIn(want, lines)

    def test_form_controls_and_chrome_are_not_read(self):
        text = "\n".join(reg.page_text(DERBYSHIRE))
        for noise in ("I found what I wanted", "out of date", "Tell us more",
                      "Home", "Updated today", "dataLayer"):
            self.assertNotIn(noise, text)

    def test_a_table_inside_the_form_is_read(self):
        lines = reg.page_text(TABLE_IN_FORM)
        self.assertIn("Byway Open to All Traffic 21, Therfield", lines)
        self.assertIn("Seasonal, October to April 2004", lines)


class EmptyText(Register):
    """2b. A page with no readable text is a failure naming the page."""

    def assert_reported(self, empty, unreachable, changed):
        self.assertEqual([e["page"] for e in empty], ["co/orders"])
        self.assertEqual(empty[0]["url"], self.URL)
        self.assertIn("no readable text", empty[0]["why"])
        self.assertEqual((changed, unreachable), ([], []))
        self.assertEqual(self.queued(), [])

    def test_an_empty_page_is_reported_and_never_snapshotted(self):
        body = b"<html><body><script>render()</script></body></html>"
        changed, unreachable, empty = self.check(body)
        self.assert_reported(empty, unreachable, changed)
        self.assertEqual(self.snapshot(), [None, None])

    def test_a_title_alone_is_near_empty(self):
        body = (b"<html><head><title>Rights of way - Permanent traffic "
                b"regulation orders | A County Council</title></head>"
                b"<body></body></html>")
        changed, unreachable, empty = self.check(body)
        self.assert_reported(empty, unreachable, changed)

    def test_the_threshold_is_a_count_of_characters(self):
        short = "x" * (reg.MIN_TEXT_CHARS - 1)
        _c, _u, empty = self.check(("<p>%s</p>" % short).encode())
        self.assertEqual(len(empty), 1)
        self.assertIn("%d characters" % len(short), empty[0]["why"])
        changed, _u, empty = self.check(("<p>%s</p>" % (short + "x"))
                                        .encode())
        self.assertEqual((len(changed), empty), (1, []))

    def test_a_page_that_goes_empty_keeps_its_last_snapshot(self):
        self.check(LIST)
        reg.accept(["co/orders"], Client({}), register=self.tmp)
        before = self.snapshot()
        self.assertTrue(before[1].startswith(b"Seasonal byway"))
        changed, unreachable, empty = self.check(b"<main></main>")
        self.assert_reported(empty, unreachable, changed)
        self.assertEqual(self.snapshot(), before)

    def test_a_filter_that_matches_nothing_is_not_an_empty_page(self):
        self.pages([{"id": "co/orders", "url": self.URL,
                     "council": "Example County Council",
                     "only": "(?i)byway closure"}])
        changed, unreachable, empty = self.check(LIST)
        self.assertEqual((unreachable, empty), ([], []))
        self.assertEqual(changed[0]["text"], [])

    def test_accept_refuses_to_store_an_empty_page(self):
        with self.assertRaises(reg.EmptyPage):
            reg.accept(["co/orders"],
                       Client({self.URL: b"<main></main>"}),
                       register=self.tmp)
        self.assertEqual(self.snapshot(), [None, None])

    def test_a_pdf_is_compared_by_its_bytes_not_its_text(self):
        digest, lines = reg.fingerprint({"url": self.URL}, b"%PDF")
        self.assertIsNone(lines)


def way(uid, authority, name, *bng):
    return Way(uid, authority, "Byway open to all traffic (BOAT) " + name,
               [[grid_to_wgs84(e, n) for e, n in bng]])


class HeldBack(unittest.TestCase):
    """3. An approved order that fails matching is said where it is read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.register = os.path.join(self.tmp, "register")
        os.makedirs(self.register)
        good = {"id": "es/arlington/9", "council": "East Sussex County "
                "Council", "authorities": ["East Sussex"],
                "parish": "Arlington", "path_as_written": "Byway 9",
                "paths": [["num", None, "9"]], "status": "approved",
                "source_url": "https://e.example/seasonal"}
        lost = dict(good, id="es/iden/28", parish="Iden",
                    paths=[["num", None, "28"]])
        draft = dict(lost, id="es/iden/29", status="needs-review")
        with open(os.path.join(self.register, "orders.json"), "w") as fh:
            json.dump([good, lost, draft], fh)
        self.byways = Byways([way("ES-%d" % i, "East Sussex",
                                  ("Arlington 9" if i == 0
                                   else "Firle %d" % (100 + i)),
                                  (556600 + i, 109150),
                                  (556900 + i, 109300))
                              for i in range(1000)])
        import byway_match
        self.real = (byway_match.load_byways, reg.REGISTER)
        byway_match.load_byways = lambda *a, **k: self.byways
        reg.REGISTER = self.register

    def tearDown(self):
        import byway_match
        byway_match.load_byways, reg.REGISTER = self.real
        shutil.rmtree(self.tmp)

    def build(self, *extra):
        out = os.path.join(self.tmp, "order-register.json")
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            rc = reg.main(["build", "--out", out] + list(extra))
        return rc, printed.getvalue()

    def test_a_held_order_is_a_workflow_warning(self):
        rc, printed = self.build()
        self.assertEqual(rc, 0)
        self.assertIn("::warning title=Order held back::es/iden/28: no byway "
                      "of ours matches", printed)
        self.assertNotIn("es/iden/29", printed)
        self.assertNotIn("es/arlington/9:", printed)

    def test_a_held_order_is_written_to_the_report(self):
        report = os.path.join(self.tmp, "held.txt")
        self.build("--report", report)
        with open(report, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines(),
                             ["HELD es/iden/28 - no byway of ours matches "
                              "its path or grid refs"])

    def test_nothing_held_writes_an_empty_report(self):
        with open(os.path.join(self.register, "orders.json"), "w") as fh:
            json.dump([], fh)
        report = os.path.join(self.tmp, "held.txt")
        rc, printed = self.build("--report", report)
        self.assertEqual(rc, 0)
        self.assertNotIn("::warning", printed)
        with open(report, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")


class CheckReport(unittest.TestCase):
    """The review issue's lines name each kind of page."""

    def test_report_lines(self):
        lines = reg.report_lines(
            [{"page": "a/new", "url": "https://a/1", "new": True},
             {"page": "a/changed", "url": "https://a/2", "new": False}],
            [{"page": "b", "url": "https://b", "why": "HTTP 500"}],
            [{"page": "c", "url": "https://c", "why": "no readable text "
              "(0 characters)"}], inbox=[])
        self.assertEqual(lines, [
            "NEW a/new https://a/1",
            "CHANGED a/changed https://a/2",
            "UNREADABLE b https://b - HTTP 500",
            "EMPTY c https://c - no readable text (0 characters)"])

    def patched(self, name, fake):
        real = getattr(reg, name)
        setattr(reg, name, fake)
        self.addCleanup(setattr, reg, name, real)

    def test_check_writes_the_empty_page_to_the_issue_report(self):
        empty = [{"page": "dy/ptro", "url": "https://dy.example/p",
                  "why": "no readable text (0 characters)"}]
        self.patched("check", lambda client, today: (
            [{"page": "yd/lanes", "url": "https://yd.example", "new": True}],
            [], empty))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        report = os.path.join(tmp, "report.txt")
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(reg.main(["check", "--report", report]), 0)
        with open(report, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[:2], [
            "NEW yd/lanes https://yd.example",
            "EMPTY dy/ptro https://dy.example/p - no readable text "
            "(0 characters)"])
        self.assertIn("1 to review (1 read for the first time), 0 could "
                      "not be read, 1 with no readable text",
                      printed.getvalue())

    def test_accept_of_an_empty_page_fails_the_run(self):
        def refuse(pages, client):
            raise reg.EmptyPage("no readable text (0 characters)")
        self.patched("accept", refuse)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(reg.main(["accept", "dy/ptro"]), 1)
        self.assertIn("::error::not accepted: no readable text",
                      printed.getvalue())


class Wiring(unittest.TestCase):
    """What the quarterly job and the documentation say match the code."""

    def workflow(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), ".github", "workflows",
            "order-register.yml")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_the_job_runs_these_tests_before_the_check(self):
        text = self.workflow()
        self.assertLess(text.index("python tools/test_order_register_loud.py"),
                        text.index("run: python tools/order_register.py "
                                   "check"))

    def test_held_orders_reach_the_review_issue(self):
        text = self.workflow()
        build = text.index("order_register.py build --out /tmp/")
        self.assertIn("--report /tmp/register-held.txt", text[build:])
        append = text.index(
            "cat /tmp/register-held.txt >> /tmp/register-report.txt")
        self.assertLess(build, append)
        self.assertLess(append, text.index("if [ -s /tmp/register-report.txt"))

    def test_the_issue_explains_each_kind_of_line(self):
        text = self.workflow()
        for word in ("NEW:", "(EMPTY)", "(HELD)"):
            self.assertIn(word, text)

    def test_the_docs_say_what_the_code_does(self):
        flat = lambda s: " ".join(s.split())  # noqa: E731
        self.assertIn("or that has no snapshot yet, because nobody has "
                      "transcribed it", flat(reg.__doc__))
        self.assertIn("text comes out empty is reported and keeps its last "
                      "snapshot", flat(reg.__doc__))
        self.assertIn("held back, which `build` says as a workflow warning",
                      flat(reg.__doc__))
        self.assertIn("A <form> is read", flat(reg._Text.__doc__))
        self.assertIn("Raises EmptyPage", flat(reg.fingerprint.__doc__))
        self.assertIn("Returns (changed, unreachable, empty)",
                      flat(reg.check.__doc__))
        self.assertIn("never quietly baselined", flat(reg.check.__doc__))


if __name__ == "__main__":
    unittest.main(verbosity=1)

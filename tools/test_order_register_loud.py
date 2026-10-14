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
import re
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
        self.assertIn("readable text (", empty[0]["why"])
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
        lost2 = dict(lost, id="es/iden/30", paths=[["num", None, "30"]])
        self.orders = [good, lost, draft, lost2]
        self.write_orders()
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

    def write_orders(self):
        with open(os.path.join(self.register, "orders.json"), "w") as fh:
            json.dump(self.orders, fh)

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
                              "its path or grid refs",
                              "HELD es/iden/30 - no byway of ours matches "
                              "its path or grid refs"])

    def test_every_held_order_is_a_warning_in_order(self):
        _rc, printed = self.build()
        warnings = [line for line in printed.splitlines()
                    if line.startswith("::warning")]
        self.assertEqual(warnings, [
            "::warning title=Order held back::es/iden/%d: no byway of ours "
            "matches its path or grid refs" % n for n in (28, 30)])

    def test_no_id_can_write_a_workflow_command(self):
        self.orders[1] = dict(self.orders[1], id="es/x\n::error::x")
        self.write_orders()
        _rc, printed = self.build()
        commands = [line for line in printed.splitlines()
                    if line.startswith("::")]
        self.assertEqual(len(commands), 2)
        for line in commands:
            self.assertTrue(line.startswith(
                "::warning title=Order held back::"), line)

    def test_an_odd_id_is_escaped_and_kept_to_one_line(self):
        self.orders[1] = dict(self.orders[1], id="es/50%\nx")
        self.write_orders()
        report = os.path.join(self.tmp, "held.txt")
        _rc, printed = self.build("--report", report)
        self.assertIn("::warning title=Order held back::es/50%25%0Ax: no",
                      printed)
        with open(report, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines()[0],
                             "HELD es/50% x - no byway of ours matches its "
                             "path or grid refs")

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

    def steps(self):
        import yaml
        steps = yaml.safe_load(self.workflow())["jobs"]["check"]["steps"]
        return dict((s.get("name"), (i, s)) for i, s in enumerate(steps))

    def test_a_failed_held_list_is_said_and_fails_the_run(self):
        steps = self.steps()
        held_at, held = steps["List approved orders held back from riders"]
        self.assertEqual((held["id"], held["continue-on-error"]),
                         ("held", True))
        failed = "steps.held.outcome == 'failure'"
        note_at, note = steps["Say the held-back list could not be built"]
        review_at, review = steps["Ask for a review of what changed"]
        fail_at, fail = steps["Fail the run if the held-back list could "
                              "not be built"]
        raise_at, alarm = steps["Raise an issue if this failed"]
        down_at, down = steps["Stand the alarm down if this worked"]
        self.assertEqual((note["if"], fail["if"]), (failed, failed))
        self.assertIn("$RUN_URL", note["run"])
        self.assertIn("RUN_URL", note["env"])
        self.assertIn(">> /tmp/register-report.txt", note["run"])
        self.assertIn("exit 1", fail["run"])
        self.assertEqual((review["if"], alarm["if"], down["if"]),
                         ("success()", "failure()", "success()"))
        self.assertTrue(held_at < note_at < review_at < fail_at < raise_at
                        < down_at)

    def test_the_tests_dependency_is_installed_first(self):
        steps = self.steps()
        at, step = steps["Install what the tests read"]
        self.assertEqual(step["run"].split(), ["pip", "install", "--quiet",
                                               "pyyaml"])
        self.assertLess(at, steps["Run the tests before trusting the "
                                  "check"][0])

    def test_the_permissions_comment_is_current(self):
        text = self.workflow()
        self.assertNotIn("snapshots of pages read for the first time", text)
        self.assertIn("contents: write   # changes.json and the override "
                      "read log", text)

    def test_the_issues_say_what_a_failed_held_list_means(self):
        text = " ".join(self.workflow().split())
        self.assertIn("HELD-LIST-FAILED means that list could not be built "
                      "this run", text)
        self.assertIn("if only the list of held-back orders failed, the "
                      "review issue says so (HELD-LIST-FAILED)", text)
        steps = self.steps()
        note = steps["Say the held-back list could not be built"][1]
        self.assertIn('echo "HELD-LIST-FAILED the list of approved orders '
                      'held back from riders could not be built - see '
                      '$RUN_URL"', note["run"])
        fail = steps["Fail the run if the held-back list could not be "
                     "built"][1]
        self.assertIn('echo "::error::the list of approved orders held back '
                      'from riders could not be built"', fail["run"])

    def test_the_issue_says_what_to_do_about_held_lines(self):
        self.assertIn("A HELD order stays listed until it matches", " ".join(
            self.workflow().split()))

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
        self.assertIn("order_register.py scrub [--dry-run]", reg.__doc__)



class ShortDocuments(unittest.TestCase):
    """Round 2, B1: the floor is for fetched web pages only. A document
    saved into manual/ is what it is, however short."""

    SHORT = "Byway N12 Brighstone: no motor vehicles 1 Oct to 30 Apr"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.manual = os.path.join(self.tmp, "manual")
        os.makedirs(os.path.join(self.manual, "IW"))
        with open(os.path.join(self.manual, "authorities.json"), "w") as fh:
            json.dump({"IW": {"authority": "Isle of Wight"}}, fh)
        with open(os.path.join(self.tmp, "pages.json"), "w") as fh:
            json.dump([], fh)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def save(self, name, text):
        with open(os.path.join(self.manual, "IW", name), "w",
                  newline="\n") as fh:
            fh.write(text)

    def check(self):
        return reg.check(Client({}), register=self.tmp, today="2026-10-14",
                         manual_root=self.manual)

    def test_a_short_txt_and_csv_are_queued_new_and_acceptable(self):
        self.save("orders-2026-10-08.txt", self.SHORT)
        self.save("orders-2026-10-09.csv", "path,restriction\nN12,closed\n")
        changed, unreachable, empty = self.check()
        self.assertEqual((unreachable, empty), ([], []))
        self.assertEqual([(c["page"], c["new"]) for c in changed],
                         [("manual/IW/orders-2026-10-08.txt", True),
                          ("manual/IW/orders-2026-10-09.csv", True)])
        self.assertIn(self.SHORT, changed[0]["text"])
        reg.accept([c["page"] for c in changed], Client({}),
                   register=self.tmp)
        self.assertEqual(self.check(), ([], [], []))

    def test_an_empty_saved_document_says_so(self):
        self.save("orders-2026-10-08.html", "<html><body></body></html>")
        _c, _u, empty = self.check()
        self.assertEqual(len(empty), 1)
        self.assertIn("saved document has no readable text",
                      empty[0]["why"])
        self.assertNotIn("layout", empty[0]["why"])


class ShortPages(Register):
    """Round 2, B1: a fetched page that was always short may change."""

    def seed(self, lines):
        reg._snapshot(self.tmp, {"id": "co/orders"},
                      reg.hashlib.sha256("\n".join(lines).encode())
                      .hexdigest(), lines)

    def test_an_aspnet_page_reading_empty_is_still_empty(self):
        _c, _u, empty = self.check(
            b'<html><body><form id="aspnetForm"><script>x()</script>'
            b'</form></body></html>')
        self.assertIn("no readable text (0 characters)", empty[0]["why"])
        self.assertIn("layout may have changed", empty[0]["why"])

    def test_too_little_text_is_said_as_such(self):
        _c, _u, empty = self.check(b"<p>Byway 5: closed</p>")
        self.assertIn("too little readable text (15 characters)",
                      empty[0]["why"])

    def test_a_page_that_was_always_short_is_changed_not_empty(self):
        self.seed(["Byway 5: closed all year"])
        changed, _u, empty = self.check(b"<p>Byway 5: open</p>")
        self.assertEqual(empty, [])
        self.assertEqual([(c["page"], c["new"]) for c in changed],
                         [("co/orders", False)])
        self.assertIn("+Byway 5: open", changed[0]["diff"])

    def test_a_long_page_that_turns_short_is_empty(self):
        self.seed(["Byway 5: closed all year " * 10])
        _c, _u, empty = self.check(b"<p>Byway 5: open</p>")
        self.assertEqual(len(empty), 1)

    def test_a_page_whose_snapshot_was_empty_stays_under_the_floor(self):
        self.seed([])
        _c, _u, empty = self.check(b"<p>Byway 5: open</p>")
        self.assertEqual(len(empty), 1)


class PendingStays(Register):
    """Round 2, L3: a page that cannot be read keeps its pending entry."""

    def test_unreadable_and_empty_keep_the_pending_entry(self):
        from polite_http import FetchFailed
        (entry,) = self.check(LIST)[0]

        class Failing(Client):
            def get(inner, url):
                raise FetchFailed("HTTP 500\nbody")

        for client in (Failing({}), Client({self.URL: b"<main></main>"})):
            changed, _u, _e = reg.check(
                client, register=self.tmp, today="2026-10-15",
                manual_root=os.path.join(self.tmp, "manual"))
            self.assertEqual(changed, [entry])
            self.assertEqual(self.queued(), [entry])


class PersonalData(Register):
    """Round 2-3, P1: no e-mail address or phone number is written, and
    nothing else is mistaken for one. Numbers are Ofcom drama ranges."""

    SCRUB = [
        "01632 960123", "(01632) 960123", "(0)1632 960123", "01632.960123",
        "(020) 7946 0123", "020 7946 0123", "0044 1632 960123",
        "+44 1632 960123", "+44 (0)1632 960 123", "+44 20 7946 0123",
        "07700 900123", "0808-157-0192", "0117 496 0123", "01632960123",
        "0163296012", "01632 96012", "0306 9990 123",
    ]
    KEEP = [
        "SU 01234 56789", "ST 05123 12345", "SU 0123 4567", "SU012345",
        "TQ 566 091", "511436 149706", "Order 2004 made 01/10/2026",
        "Closed 01-02-2026 05-03-2026", "Closed 03-02-2026 05-03-2026",
        "2026-01-02 2026-03-05", "Dates: 01-02-2026 09:00",
        "Ref: 2026-0123-4567-89", "016329601234", "016329601",
        "0123456789012345", "Byway 15/2", "Surrey 0692 4666",
        "Order 0123/2004", "Section 01 02 2026",
        # Round 4: a "-" or "." straight after a digit starts no number.
        "2026-01632 960123", "ref 12.01632 960123",
        "Order 2026-0116 496 0123",
    ]

    def test_every_phone_shape_is_replaced(self):
        for number in self.SCRUB:
            self.assertEqual(reg.scrub_text("Ring %s today" % number),
                             "Ring [phone removed] today", number)

    def test_prefixes_and_what_follows_are_kept(self):
        for text, want in (
                ("Tel: 01632 960123", "Tel: [phone removed]"),
                ("Tel:01632 960123", "Tel:[phone removed]"),
                ("01632 960123 ext 2", "[phone removed] ext 2"),
                ("01632 960123 9am", "[phone removed] 9am"),
                ("on 01632 960123 1 May", "on [phone removed] 1 May"),
                ("01632 96012 3 May", "[phone removed] 3 May"),
                ("Write to jo.bloggs@council.example, o'neil@x.example or "
                 "a+b@x.example",
                 "Write to [email removed], [email removed] or "
                 "[email removed]")):
            self.assertEqual(reg.scrub_text(text), want, text)

    def test_references_dates_and_grid_refs_stay(self):
        for keep in self.KEEP:
            self.assertEqual(reg.scrub_text(keep), keep, keep)

    AFTER_CAPITALS = [
        "TEL NO 01632 960123", "PHONE NO 01632 960123",
        "CONTACT ON 01632 960123", "RING ON 01632 960123",
        "CALL ON 01632 960123", "OR 01632 960123", "PH 01632 960123",
        "Contact JS 07700 900123", "NB 07700 900123", "PS 07700 900123",
        "BT 0808 157 0150", "Postcode DE4 3AQ 01632 960123",
        "Tel.01632 960123", "tel.01632 960123", "Tel-01632 960123",
        "TEL NO.01632 960123", "(01632)960123", "01632 - 960123",
        "016977 3456", "0800 1111", "0845 4647",
        "CONTACT US ON 01632 960123", "CALL ME ON 07700 900123",
        "BY 07700 900123", "FOR INFO CALL AT 01632 960123",
        "NO 01632 960123", "SO 01632 960123", "ST 07700 900123",
        "Phone TO 01632 960123", "011 74960123", "0116 4960123",
        # Round 4: grid-shaped digits after capitals that are not a square,
        # after a square that is part of a word or follows a digit, or in
        # unequal halves after a real square.
        "PH 01768 62345", "PH 0800 1111", "RING 01768 62345",
        "RING 0800 1111", "CASH 01768 62345", "5SU 01768 62345",
        "TEL NO 0161 496 0123", "NB 0808 157 0150", "SU 0116 496 0123",
    ]

    def test_a_number_after_capitals_or_punctuation_is_replaced(self):
        for text in self.AFTER_CAPITALS:
            got = reg.scrub_text(text)
            self.assertIn("[phone removed]", got, text)
            self.assertFalse(re.search(r"\d{4}", got), (text, got))

    def test_grid_references_after_a_real_square_stay(self):
        for keep in ("SU 0123 5678", "NB 01234 56789", "TQ 012345",
                     "HP 0123456789", "at SU 01234 56789."):
            self.assertEqual(reg.scrub_text(keep), keep, keep)

    # The 100 km squares of Great Britain, written out here so that a
    # square dropped from or added to the code's set is caught.
    SQUARES = (
        "HP HT HU HW HX HY HZ NA NB NC ND NF NG NH NJ NK NL NM NN NO NR NS "
        "NT NU NW NX NY NZ OV SC SD SE SH SJ SK SM SN SO SP SR SS ST SU SV "
        "SW SX SY SZ TA TF TG TL TM TQ TR TV").split()

    def test_every_square_of_the_grid_is_a_grid_reference(self):
        self.assertEqual(reg._GB_SQUARES, set(self.SQUARES))
        for square in self.SQUARES:
            keep = "%s 01234 56789" % square
            self.assertEqual(reg.scrub_text(keep), keep, keep)

    def page(self, who):
        return ("<main>%s<p>Questions to %s, telephone 01632 960123.</p>"
                "</main>" % (LIST.decode()[6:-7], who)).encode()

    def test_nothing_personal_reaches_changes_and_the_digest_is_steady(self):
        self.check(self.page("ann.example@council.example"))
        with open(os.path.join(self.tmp, "changes.json")) as fh:
            raw = fh.read()
        self.assertNotIn("ann.example", raw)
        self.assertNotIn("960123", raw)
        self.assertIn("[email removed]", raw)
        self.assertIn("[phone removed]", raw)
        reg.accept(["co/orders"], Client({}), register=self.tmp)
        with open(reg.snapshot_paths(self.tmp, "co/orders")[1]) as fh:
            self.assertNotIn("ann.example", fh.read())
        changed, _u, _e = self.check(self.page("bob.example@council.example"))
        self.assertEqual(changed, [], "the scrubbed text churned")

    def test_the_old_snapshot_is_scrubbed_before_it_is_diffed(self):
        lines = ["Questions to old.name@council.example"]
        reg._snapshot(self.tmp, {"id": "co/orders"}, "0" * 64, lines)
        changed, _u, _e = self.check(LIST)
        self.assertNotIn("old.name", json.dumps(changed))

    OLD = ["Byway 9 closed", "Ask c.d@council.example on 01632 960123"]

    def make_old_register(self):
        joined = "\n".join(self.OLD).encode()
        reg._snapshot(self.tmp, {"id": "co/orders"},
                      reg.hashlib.sha256(joined).hexdigest(), self.OLD)
        # A snapshot whose digest is of bytes (a PDF) keeps its digest.
        reg._snapshot(self.tmp, {"id": "co/pdf"}, "f" * 64,
                      ["Ask c.d@council.example"])
        reg._snapshot(self.tmp, {"id": "co/clean"}, "e" * 64,
                      ["Byway 9 closed"])
        # A text with no digest file beside it is scrubbed all the same.
        with open(reg.snapshot_paths(self.tmp, "co/bare")[1], "w") as fh:
            fh.write("Ask c.d@council.example\n")
        with open(os.path.join(self.tmp, "changes.json"), "w") as fh:
            json.dump([{"page": "co/orders", "digest": reg.hashlib.sha256(
                joined).hexdigest(), "text": self.OLD,
                "diff": ["+Ask c.d@council.example on 01632 960123"]},
                {"page": "co/pdf", "digest": "a" * 64,
                 "text": ["Ask c.d@council.example"]}], fh)

    def test_scrub_rewrites_the_old_snapshots_and_changes(self):
        self.make_old_register()
        counts = reg.scrub_register(self.tmp)
        self.assertEqual(counts, {"snapshots": 3, "changes": 2})
        h, t = reg.snapshot_paths(self.tmp, "co/orders")
        with open(t) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines, ["Byway 9 closed",
                                 "Ask [email removed] on [phone removed]"])
        with open(h) as fh:
            self.assertEqual(fh.read().strip(), reg.hashlib.sha256(
                "\n".join(lines).encode()).hexdigest())
        with open(reg.snapshot_paths(self.tmp, "co/pdf")[0]) as fh:
            self.assertEqual(fh.read().strip(), "f" * 64)
        with open(reg.snapshot_paths(self.tmp, "co/pdf")[1]) as fh:
            self.assertEqual(fh.read(), "Ask [email removed]\n")
        with open(reg.snapshot_paths(self.tmp, "co/bare")[1]) as fh:
            self.assertEqual(fh.read(), "Ask [email removed]\n")
        self.assertFalse(os.path.exists(
            reg.snapshot_paths(self.tmp, "co/bare")[0]))
        entry, pdf = self.queued()
        self.assertNotIn("c.d@", json.dumps([entry, pdf]))
        self.assertEqual(entry["digest"], reg.hashlib.sha256(
            "\n".join(lines).encode()).hexdigest())
        self.assertEqual(pdf["digest"], "a" * 64,
                         "a digest not of the text was retaken")
        self.assertEqual(reg.scrub_register(self.tmp),
                         {"snapshots": 0, "changes": 0})

    def test_scrub_dry_run_counts_and_writes_nothing(self):
        self.make_old_register()
        before = self.snapshot()
        with open(os.path.join(self.tmp, "changes.json"), "rb") as fh:
            changes = fh.read()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(reg.main(["scrub", "--dry-run", "--register",
                                       self.tmp]), 0)
        self.assertEqual(self.snapshot(), before)
        with open(os.path.join(self.tmp, "changes.json"), "rb") as fh:
            self.assertEqual(fh.read(), changes)
        self.assertIn("3 snapshot(s) and 2 change(s) would be scrubbed",
                      printed.getvalue())
        self.assertNotIn("c.d@", printed.getvalue())

    def test_scrub_for_real_says_what_it_did(self):
        self.make_old_register()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(reg.main(["scrub", "--register", self.tmp]), 0)
        self.assertIn("3 snapshot(s) and 2 change(s) scrubbed",
                      printed.getvalue())
        with open(reg.snapshot_paths(self.tmp, "co/orders")[1]) as fh:
            self.assertNotIn("c.d@", fh.read())

    def test_accept_of_a_page_that_was_always_short(self):
        reg._snapshot(self.tmp, {"id": "co/orders"}, "0" * 64,
                      ["Byway 5: closed all year"])
        reg.accept(["co/orders"], Client({self.URL: b"<p>Byway 5: open</p>"}),
                   register=self.tmp)
        self.assertEqual(self.snapshot()[1], b"Byway 5: open\n")


class WorkflowCommands(unittest.TestCase):
    """Round 2, L1: data in a workflow command or report line is escaped."""

    def test_escape(self):
        self.assertEqual(reg.workflow_escape("50% off\r\nnext"),
                         "50%25 off%0D%0Anext")
        self.assertEqual(reg.one_line("a\r\nb\n\nc"), "a b c")

    def test_changed_and_new_lines_are_one_line_each(self):
        lines = reg.report_lines(
            [{"page": "a\nb", "url": "https://a\r\n1", "new": True},
             {"page": "c", "url": "https://c", "new": False,
              "provenance": "saved\nby hand"}], [], [], inbox=[])
        self.assertEqual(lines, ["NEW a b https://a 1",
                                 "CHANGED c https://c (saved by hand)"])

    def test_report_lines_are_one_line_each(self):
        lines = reg.report_lines(
            [], [{"page": "b", "url": "https://b", "why": "HTTP 500\nx"}],
            [{"page": "c", "url": "https://c", "why": "no\r\ntext"}],
            inbox=[])
        self.assertEqual(lines, ["UNREADABLE b https://b - HTTP 500 x",
                                 "EMPTY c https://c - no text"])

    def test_accept_error_is_escaped(self):
        real = reg.accept

        def refuse(pages, client):
            raise reg.EmptyPage("100% gone\nnow")
        reg.accept = refuse
        self.addCleanup(setattr, reg, "accept", real)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            reg.main(["accept", "x"])
        self.assertIn("::error::not accepted: 100%25 gone%0Anow",
                      printed.getvalue())

if __name__ == "__main__":
    unittest.main(verbosity=1)

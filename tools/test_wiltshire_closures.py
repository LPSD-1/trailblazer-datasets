#!/usr/bin/env python3
"""Wiltshire's register of rights of way closures is read for what it says:
BOATs only, each closure's type kept (a permanent order is never a temporary
closure, a permanent seasonal order is dated by its season, a voluntary
closure is never drawn as an order), matched to our byways by the council's
own path codes, and a changed or failed register never blanks what was
published.

    python tools/test_wiltshire_closures.py

No network. The pages are the register's own, saved on 7 October 2026
(tools/fixtures/wiltshire/; the anti-forgery token in form.html replaced),
and the byways are synthetic, named the way our Wiltshire byways are.
"""
import collections
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import council_orders  # noqa: E402
import council_sources as cs  # noqa: E402
import wiltshire_closures as w  # noqa: E402
from byway_match import Byways, Way  # noqa: E402
from polite_http import FetchFailed, Refused  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "wiltshire")
DAY = "2026-10-07"


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


def detail(row):
    return fixture("detail-%s.html" % row.replace("$", "_"))


def clean(text):
    return cs.strip_personal(cs.clean_text(text or ""))


def candidate_for(row, body=None):
    entry = next(e for e in w.parse_results(fixture("result-boat.html"))
                 if e["row"] == row)
    found = w.parse_detail(body if body is not None else detail(row))
    return w.candidate(entry, found, cs.parse_date, cs.parse_season, clean)


def feature(item, day=DAY):
    item = dict(item, geometry={"type": "LineString",
                                "coordinates": [[-1.8, 51.4], [-1.79, 51.41]]})
    source = {"id": "wiltshire-closures", "name": "Wiltshire Council"}
    return council_orders.feature_of(item, source, day)


class Register(object):
    """Answers the register as the collector's snapshots would: the search
    and each detail page; a detail not saved is refused, as HomeClient
    refuses one the collector has not read."""

    def __init__(self, result=None, fail=None):
        self.result = result if result is not None else \
            fixture("result-boat.html")
        self.fail = fail
        self.posts = []

    def post_form(self, page, action, search):
        self.posts.append((page, action, dict(search)))
        if self.fail:
            raise self.fail
        return self.result

    def get(self, url):
        row = url.rsplit("=", 1)[-1]
        name = os.path.join(FIXTURES, "detail-%s.html"
                            % row.replace("$", "_"))
        if not os.path.exists(name):
            raise Refused("no snapshot of %s" % url)
        with open(name, "rb") as fh:
            return fh.read()


def wilts_way(uid, code):
    """A byway named as our Wiltshire byways are ("... (BOAT) AVEB 1")."""
    parish, number = code
    return Way(uid, "Wiltshire", "Byway open to all traffic (BOAT) %s %s"
               % (parish, number), [[(-1.85, 51.42), (-1.84, 51.43)]])


class TheResultPage(unittest.TestCase):
    def test_every_closure_on_the_page_is_read(self):
        entries = w.parse_results(fixture("result-boat.html"))
        self.assertEqual(len(entries), 49)
        self.assertEqual(collections.Counter(e["type"] for e in entries),
                         {"Voluntary": 35, "Permanent": 14})
        first = entries[0]
        self.assertEqual((first["row"], first["ref"], first["rights_of_way"],
                          first["parishes"]),
                         ("CHIP108$001", "CHIP/108/001", "CHIP108",
                          "Chippenham"))
        ridgeway = next(e for e in entries if e["ref"] == "AVEB/1/001")
        self.assertEqual(len(w.path_refs(ridgeway["rights_of_way"])), 17)

    def test_a_page_that_says_more_than_it_shows_is_a_changed_page(self):
        body = fixture("result-boat.html").decode("utf-8")
        cut = body.index('<div class="govuk-summary-card mb-3">')
        cut2 = body.index('<div class="govuk-summary-card mb-3">', cut + 10)
        with self.assertRaises(w.Changed):
            w.parse_results((body[:cut] + body[cut2:]).encode())

    def test_a_list_split_over_pages_is_a_changed_page(self):
        body = fixture("result-boat.html").decode("utf-8").replace(
            "Showing all 49 items", "Showing 1 to 30 of 49 items")
        with self.assertRaises(w.Changed):
            w.parse_results(body.encode())

    def test_a_page_without_a_count_is_never_read_as_no_closures(self):
        body = fixture("result-boat.html").decode("utf-8").replace(
            "Showing all 49 items", "")
        with self.assertRaises(w.Changed):
            w.parse_results(body.encode())

    def test_the_councils_own_no_closures_answer_is_an_empty_list(self):
        self.assertEqual(w.parse_results(
            b"<main><p>There are no closures that match your search "
            b"criteria</p></main>"), [])


class TheSearch(unittest.TestCase):
    def test_the_search_asks_for_byways_open_to_all_traffic(self):
        # RowType 1 must be the form's own BOAT option, or "BOATs only"
        # would rest on a number nobody checked.
        import polite_http
        form, = [f for f in polite_http.page_forms(fixture("form.html"),
                                                   w.FORM_PAGE)
                 if f["action"] == w.RESULT]
        self.assertEqual(form["method"], "post")
        body = fixture("form.html").decode("utf-8")
        self.assertIn('<option value="%s">Byway Open To All Traffic</option>'
                      % w.SEARCH["RowType"], body)
        self.assertIn(w.SEARCH["ClosureType"], form["choices"]["ClosureType"])
        self.assertEqual(form["fields"]["Archived"], "ActiveOnly")

    def test_the_detail_address_works_without_a_session(self):
        self.assertEqual(
            w.detail_url("CHIP108$001"),
            "https://apps.wiltshire.gov.uk/RightsOfWay/Closure/Detail"
            "?row=CHIP108$001")


class TheDetailPage(unittest.TestCase):
    def test_who_why_when_and_the_order(self):
        d = w.parse_detail(detail("CHIP108$001"))
        self.assertEqual(d["affecting"],
                         "All motorised users except motorcycles.")
        self.assertEqual(d["reason"], "Public safety.")
        self.assertEqual(d["row_type"], "Byway Open To All Traffic")
        self.assertEqual((d["start"], d["end"]),
                         ("25 August 2021", "1 January 2099"))
        self.assertEqual(d["documents"], [(
            "Order.pdf", "https://apps.wiltshire.gov.uk/RightsOfWay/Closure/"
                         "Download/Order.pdf?row=CHIP108$001")])

    def test_only_a_byway_open_to_all_traffic_is_a_candidate(self):
        body = detail("FOVA15$001").decode("utf-8").replace(
            "<dd>Byway Open To All Traffic</dd>", "<dd>Restricted Byway</dd>")
        self.assertIsNone(candidate_for("FOVA15$001", body.encode()))
        self.assertIsNotNone(candidate_for("FOVA15$001"))


class WhatEachTypeBecomes(unittest.TestCase):
    def test_a_permanent_order_is_permanent_never_a_temporary_closure(self):
        item = candidate_for("FOVA15$001")
        self.assertEqual((item["form"], item["vehicles"]),
                         ("permanent", "all_vehicles"))
        props = feature(item)["properties"]
        self.assertEqual(props["otype"], "prohibition")
        self.assertNotIn("oform", props, "a permanent order was dated")
        self.assertNotIn("end", props)
        self.assertEqual(props["start"], "2008-01-01")

    def test_the_councils_never_date_is_no_end(self):
        item = candidate_for("CHIP108$001")
        self.assertIsNone(item["end"], "1 January 2099 is 'never'")
        self.assertIsNotNone(feature(item, day="2030-01-01"))

    def test_motorcycles_exempt_in_any_of_the_councils_wordings(self):
        for row in ("CHIP108$001", "EGRE1$001", "FOVA14$001",
                    "SALS103$001"):
            item = candidate_for(row)
            self.assertEqual(item["vehicles"],
                             "motor_vehicles_except_motorcycles", row)
            self.assertEqual(feature(item)["properties"]["otype"],
                             "motors_except_motorcycles", row)

    def test_a_motor_ban_in_its_other_wordings(self):
        for row in ("AMES20$004", "AMES32$001", "CHUT32$001", "OSTG3$001",
                    "AVEB1$002", "HILP21$001"):
            self.assertEqual(candidate_for(row)["vehicles"],
                             "motor_vehicles", row)

    def test_a_permanent_seasonal_order_is_dated_by_its_season(self):
        # "Permanent seasonal closure from 1st October to 30th April every
        # year": drawn shut all year it would close the Ridgeway in summer.
        item = candidate_for("AVEB1$001")
        self.assertEqual(item["form"], "seasonal")
        self.assertEqual(item["season"], {"from": "10-01", "to": "04-30"})
        props = feature(item, day=DAY)["properties"]
        self.assertEqual((props["otype"], props["oform"]),
                         ("prohibition", "seasonal"))
        self.assertEqual((props["start"], props["end"]),
                         ("2026-10-01", "2027-04-30"))
        self.assertIsNone(feature(item, day="2026-06-01"),
                          "the Ridgeway order was published in June")

    def test_a_voluntary_closure_is_held_never_drawn_as_an_order(self):
        item = candidate_for("AVEB14$001")
        self.assertIn("not a legal order", item["label"])
        self.assertIn("not an order", item["title"])
        self.assertIn("review_only", item)
        byways = Byways([wilts_way("WT-14-a", ("AVEB", "14"))])
        items, _unmatched, review = cs.match([item], byways, "Wiltshire")
        self.assertEqual(items, [], "a voluntary closure was published")
        self.assertEqual([r["ways"] for r in review], [["WT-14-a"]])

    def test_a_closure_whose_page_was_not_read_is_held(self):
        entry = w.parse_results(fixture("result-boat.html"))[0]
        item = w.candidate(entry, None, cs.parse_date, cs.parse_season, clean)
        self.assertIn("review_only", item)

    def test_words_of_stopping_up_are_held_not_drawn(self):
        body = detail("FOVA15$001").decode("utf-8").replace(
            "<dd>Safety</dd>", "<dd>Byway stopped up by order</dd>")
        self.assertIn("review_only", candidate_for("FOVA15$001",
                                                   body.encode()))

    def _as(self, kind, start, end, reason="Safety"):
        body = detail("FOVA15$001").decode("utf-8")
        body = body.replace("<dd>Permanent</dd>", "<dd>%s</dd>" % kind)
        body = body.replace("<dd>1 January 2008</dd>", "<dd>%s</dd>" % start)
        body = re.sub(r"(<dt>End Closure</dt>\s*<dd>)&mdash;(</dd>)",
                      lambda m: m.group(1) + end + m.group(2), body)
        body = body.replace("<dd>Safety</dd>", "<dd>%s</dd>" % reason)
        return candidate_for("FOVA15$001", body.encode())

    def test_an_experimental_order_is_experimental(self):
        item = self._as("Experimental", "1 September 2026", "1 March 2028")
        props = feature(item)["properties"]
        self.assertEqual(props["oform"], "experimental")
        self.assertEqual(props["end"], "2028-03-01")

    def test_a_seasonal_closure_with_neither_season_nor_end_is_held(self):
        item = self._as("Seasonal", "1 September 2026", "&mdash;")
        byways = Byways([wilts_way("WT-15-a", ("FOVA", "15"))])
        items, _u, review = cs.match([item], byways, "Wiltshire")
        self.assertEqual(items, [])
        self.assertEqual(len(review), 1)

    def test_a_seasonal_closure_with_its_dates_is_dated(self):
        item = self._as("Seasonal", "1 October 2026", "31 March 2027")
        props = feature(item)["properties"]
        self.assertEqual((props["oform"], props["start"], props["end"]),
                         ("seasonal", "2026-10-01", "2027-03-31"))

    def test_a_special_closure_with_no_end_is_not_drawn_shut_for_ever(self):
        self.assertIn("review_only",
                      self._as("Special", "1 October 2026", "&mdash;"))
        item = self._as("Special", "1 October 2026", "9 October 2026")
        self.assertNotIn("review_only", item)
        self.assertEqual(feature(item)["properties"]["oform"], "seasonal")


class Matching(unittest.TestCase):
    def test_by_the_councils_own_path_codes(self):
        byways = Byways([wilts_way("WT-1-a", ("AVEB", "1")),
                         wilts_way("WT-19-b", ("OSTG", "19")),
                         wilts_way("WT-9-c", ("OSTG", "9")),
                         wilts_way("WT-1-other", ("AMES", "1"))])
        items, unmatched, _r = cs.match([candidate_for("AVEB1$001")],
                                        byways, "Wiltshire")
        self.assertEqual(unmatched, [])
        (item,) = items
        self.assertEqual(item["ways"], ["WT-1-a", "WT-19-b", "WT-9-c"])
        self.assertEqual(item["match"], "reference")

    def test_a_closure_on_no_byway_we_hold_is_unmatched_not_published(self):
        items, unmatched, _r = cs.match(
            [candidate_for("CHIP108$001")],
            Byways([wilts_way("WT-1-a", ("AVEB", "1"))]), "Wiltshire")
        self.assertEqual(items, [])
        self.assertEqual([u["ref"] for u in unmatched], ["CHIP/108/001"])

    def test_the_codes_split_as_our_wiltshire_byways_are_named(self):
        import council_ways
        for entry in w.parse_results(fixture("result-boat.html")):
            for code in entry["rights_of_way"].split(","):
                code = code.replace("(PART)", "").replace("(part)", "")
                parish, number, _part = w.path_refs(code)[0]
                self.assertEqual((parish, number),
                                 council_ways._split_code(code.strip()),
                                 code)

    def test_part_of_a_byway_is_said(self):
        item = candidate_for("CHUT34$001")
        self.assertTrue(item["partial"])
        self.assertIn("CHUT34 (part)", item["where"])


class KeepLastGood(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.byways = Byways([wilts_way("WT-%d" % i, code) for i, code in
                              enumerate([("FOVA", "14"), ("FOVA", "15"),
                                         ("CHIP", "108"), ("AMES", "20"),
                                         ("AVEB", "1")])])
        self.source = dict(cs.by_id()["wiltshire-closures"])
        self.path = os.path.join(self.tmp, "wiltshire-closures.json")

    def fetch(self, register):
        return cs.fetch_one(self.source, register, self.byways, self.tmp,
                            DAY)

    def quietly(self, register):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return self.fetch(register)

    def good(self):
        entry = self.quietly(Register())
        self.assertTrue(entry["ok"], entry)
        self.assertEqual(entry["records"], 49)
        self.assertEqual(entry["items"], 6)
        with open(self.path, "rb") as fh:
            return fh.read()

    def test_the_search_the_source_submits(self):
        register = Register()
        self.quietly(register)
        self.assertEqual(register.posts, [(w.FORM_PAGE, w.RESULT, w.SEARCH)])

    def test_a_register_that_cannot_be_read_leaves_its_file_alone(self):
        before = self.good()
        for fault in (FetchFailed("HTTP 503"), Refused("HTTP 403")):
            entry = self.quietly(Register(fail=fault))
            self.assertFalse(entry["ok"])
            with open(self.path, "rb") as fh:
                self.assertEqual(fh.read(), before)

    def test_a_changed_page_leaves_its_file_alone(self):
        before = self.good()
        body = fixture("result-boat.html").decode("utf-8")
        cut = body.index('<div class="govuk-summary-card mb-3">')
        cut2 = body.index('<div class="govuk-summary-card mb-3">', cut + 10)
        entry = self.quietly(Register(
            result=(body[:cut] + body[cut2:]).encode()))
        self.assertFalse(entry["ok"])
        self.assertIn("changed", entry["error"])
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_what_is_written_is_credited_and_typed(self):
        self.good()
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        by_ref = dict((i["ref"], i) for i in data["items"])
        self.assertEqual(sorted(by_ref), ["AMES/20/004", "AVEB/1/001",
                                          "AVEB/1/002", "CHIP/108/001",
                                          "FOVA/14/001", "FOVA/15/001"])
        self.assertEqual(by_ref["AVEB/1/001"]["form"], "seasonal")
        self.assertEqual(by_ref["FOVA/15/001"]["form"], "permanent")
        self.assertTrue(by_ref["CHIP/108/001"]["url"].endswith(
            "/Download/Order.pdf?row=CHIP108$001"))
        self.assertEqual(data["source"]["name"],
                         "Wiltshire Council - rights of way closures register")
        # No voluntary closure among what is published.
        self.assertFalse([i for i in data["items"]
                          if "oluntary" in i.get("title", "")])


if __name__ == "__main__":
    unittest.main(verbosity=1)

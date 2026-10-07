#!/usr/bin/env python3
"""The order register publishes only what a person approved, matches it to
the right byway, and flags a changed council page instead of acting on it.

    python tools/test_order_register.py

No network, no containers: synthetic byways, a stand-in client, and a
temporary register directory.
"""
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
from polite_http import FetchFailed  # noqa: E402


def way(uid, authority, name, *bng):
    return Way(uid, authority, "Byway open to all traffic (BOAT) " + name,
               [[grid_to_wgs84(e, n) for e, n in bng]])


# Arlington 9a and 9b run between TQ 566 091 and TQ 571 095.
BYWAYS = Byways([
    way("ES-9a", "East Sussex", "Arlington 9a", (556600, 109150),
        (556900, 109300)),
    way("ES-9b", "East Sussex", "Arlington 9b", (556900, 109300),
        (557150, 109550)),
    # Another parish's byway 9, nowhere near.
    way("ES-9x", "East Sussex", "Firle 9", (546000, 107000),
        (546500, 107000)),
    way("DY-15/1", "Derbyshire", "Stoney Middleton 15/1", (422000, 375000),
        (422300, 375000)),
    way("DY-15/2", "Derbyshire", "Stoney Middleton 15/2", (422300, 375000),
        (422600, 375100)),
    way("CV-3", "West Northamptonshire", "CV 003", (470000, 270000),
        (470500, 270000)),
])


def order(**kw):
    base = {"id": "east-sussex/arlington/9", "council": "East Sussex County "
            "Council", "authorities": ["East Sussex"], "parish": "Arlington",
            "path_as_written": "Byway 9a and b",
            "paths": [["num", None, "9a"], ["num", None, "9b"]],
            "vehicles": "motor_vehicles_except_motorcycles",
            "form": "seasonal", "season": {"from": "10-01", "to": "03-31"},
            "grid_refs": ["TQ 566 091", "TQ 571 095"],
            "source_url": "https://example.eastsussex/seasonal",
            "status": "approved"}
    base.update(kw)
    return base


class GridRefs(unittest.TestCase):
    def test_letters_and_digits(self):
        self.assertEqual(reg.grid_ref("TQ 566 091"), (556650.0, 109150.0))
        self.assertEqual(reg.grid_ref("SD842 921"), (384250.0, 492150.0))
        self.assertEqual(reg.grid_ref("TL 002218"), (500250.0, 221850.0))
        self.assertEqual(reg.grid_ref("511436 149706"), (511436.0, 149706.0))

    def test_what_cannot_be_placed_is_none(self):
        self.assertIsNone(reg.grid_ref("0692 4666"))
        self.assertIsNone(reg.grid_ref("TQ 529 2444"))
        self.assertIsNone(reg.grid_ref(""))


class Matching(unittest.TestCase):
    def test_by_parish_and_number(self):
        ways, how, problem = reg.match_order(BYWAYS, order())
        self.assertEqual((ways, how, problem),
                         (["ES-9a", "ES-9b"], "reference", None))

    def test_a_section_numbered_byway(self):
        o = order(authorities=["Derbyshire"], parish="Stoney Middleton",
                  paths=[["num", None, "15"]], grid_refs=[])
        self.assertEqual(reg.match_order(BYWAYS, o)[0],
                         ["DY-15/1", "DY-15/2"])

    def test_a_route_code(self):
        o = order(authorities=["West Northamptonshire"], parish="Brington",
                  paths=[["code", "CV", "3"]], grid_refs=[])
        self.assertEqual(reg.match_order(BYWAYS, o)[0], ["CV-3"])

    def test_a_reference_far_from_the_councils_grid_refs_is_held(self):
        o = order(parish="Firle", paths=[["num", None, "9"]])
        ways, _how, problem = reg.match_order(BYWAYS, o)
        self.assertEqual(ways, ["ES-9x"])
        self.assertIn("grid reference", problem)

    def test_grid_alone_is_a_suggestion_not_a_match(self):
        o = order(paths=[])
        ways, how, problem = reg.match_order(BYWAYS, o)
        self.assertEqual(how, "grid")
        self.assertIn("confirm", problem)

    def test_a_reviewer_can_pin_the_ways(self):
        o = order(paths=[], ways=["ES-9a"])
        self.assertEqual(reg.match_order(BYWAYS, o),
                         (["ES-9a"], "reviewed", None))
        gone = order(paths=[], ways=["ES-404"])
        self.assertIn("no longer", reg.match_order(BYWAYS, gone)[2])


class Build(unittest.TestCase):
    def test_only_approved_orders_are_published(self):
        orders = [order(), order(id="b", status="needs-review"),
                  order(id="c", status="listed-only")]
        data, problems = reg.build(BYWAYS, orders)
        self.assertEqual([i["id"] for i in data["items"]],
                         ["east-sussex/arlington/9"])
        self.assertEqual(problems, [])

    def test_an_item_is_what_the_order_build_reads(self):
        data, _ = reg.build(BYWAYS, [order()])
        item = data["items"][0]
        for key in ("id", "authority", "vehicles", "form", "season", "url",
                    "ways", "geometry", "where", "title"):
            self.assertIn(key, item)
        self.assertEqual(item["geometry"]["type"], "MultiLineString")
        self.assertIn("East Sussex County Council", item["source_name"])
        self.assertEqual(data["source"]["kind"], "register")
        self.assertEqual(data["source"]["authorities"], ["East Sussex"])
        self.assertIn("East Sussex County Council", data["source"]["name"])

    def test_a_held_order_is_listed_for_review_not_published(self):
        data, problems = reg.build(BYWAYS, [order(
            parish="Firle", paths=[["num", None, "9"]])])
        self.assertEqual(data["items"], [])
        self.assertEqual(len(data["review"]), 1)


class Text(unittest.TestCase):
    def test_the_main_content_is_read_and_the_chrome_is_not(self):
        body = (b"<html><head><script>var t=Date.now()</script></head><body>"
                b"<nav>Menu</nav><main><h2>Seasonal</h2><ul><li>Arlington "
                b"Byway 9a</li><li>Firle&nbsp;Byway 10</li></ul></main>"
                b"<footer>Updated today</footer></body></html>")
        self.assertEqual(reg.page_text(body),
                         ["Seasonal", "Arlington Byway 9a", "Firle Byway 10"])

    def test_a_filter_keeps_only_the_lines_it_names(self):
        body = (b"<urlset><url><loc>https://d/article/1/News</loc></url>"
                b"<url><loc>https://d/article/2/Byway-12-closure</loc></url>"
                b"</urlset>")
        self.assertEqual(reg.page_text(body, "(?i)byway"),
                         ["https://d/article/2/Byway-12-closure"])


class Client(object):
    def __init__(self, bodies):
        self.bodies = bodies

    def get(self, url):
        value = self.bodies[url]
        if isinstance(value, Exception):
            raise value
        return value


class Check(unittest.TestCase):
    URL = "https://example.eastsussex/seasonal"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "pages.json"), "w") as fh:
            json.dump([{"id": "es/seasonal", "url": self.URL,
                        "council": "East Sussex County Council"},
                       {"id": "wb/x", "url": "https://w.example/x",
                        "blocked": "bot challenge"}], fh)
        self.orders = os.path.join(self.tmp, "orders.json")
        with open(self.orders, "w") as fh:
            json.dump([order()], fh)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def page(self, items):
        return ("<main><ul>%s</ul></main>" % "".join(
            "<li>%s</li>" % i for i in items)).encode()

    def run_check(self, body):
        return reg.check(Client({self.URL: body}), register=self.tmp,
                         today="2027-01-01",
                         manual_root=os.path.join(self.tmp, "manual"))

    def test_first_read_takes_a_snapshot_and_flags_nothing(self):
        changed, unreachable, baselined = self.run_check(
            self.page(["Arlington Byway 9a"]))
        self.assertEqual((changed, unreachable, baselined),
                         ([], [], ["es/seasonal"]))

    def test_a_change_is_flagged_with_a_diff_and_nothing_moves(self):
        self.run_check(self.page(["Arlington Byway 9a"]))
        with open(self.orders, "rb") as fh:
            before = fh.read()
        changed, _u, _b = self.run_check(
            self.page(["Arlington Byway 9a", "Laughton Byway 26"]))
        self.assertEqual([c["page"] for c in changed], ["es/seasonal"])
        self.assertIn("+Laughton Byway 26", changed[0]["diff"])
        with open(self.orders, "rb") as fh:
            self.assertEqual(fh.read(), before,
                             "the register moved without a review")
        # And it stays flagged until a person accepts it.
        again, _u, _b = self.run_check(
            self.page(["Arlington Byway 9a", "Laughton Byway 26"]))
        self.assertEqual(len(again), 1)

    def test_accept_takes_the_page_as_reviewed(self):
        self.run_check(self.page(["a"]))
        self.run_check(self.page(["a", "b"]))
        reg.accept(["es/seasonal"], Client({self.URL: self.page(["a", "b"])}),
                   register=self.tmp)
        changed, _u, _b = self.run_check(self.page(["a", "b"]))
        self.assertEqual(changed, [])

    def test_an_unreadable_page_is_reported_not_treated_as_empty(self):
        self.run_check(self.page(["a"]))
        changed, unreachable, _b = self.run_check(FetchFailed("HTTP 500"))
        self.assertEqual(changed, [])
        self.assertEqual([u["page"] for u in unreachable], ["es/seasonal"])

    def test_a_blocked_page_is_never_fetched(self):
        # The stand-in has no answer for it: fetching it would KeyError.
        self.run_check(self.page(["a"]))


class FollowedDocuments(unittest.TestCase):
    """Documents linked from a page (`follow`) - the PDFs the owner chose
    to read despite robots.txt - are flagged for review when first read."""

    URL = "https://www.cambs.example/rights-of-way-restrictions"
    PDF = "https://www.cambs.example/asset-library/Soham-Byway-2016.pdf"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "pages.json"), "w") as fh:
            json.dump([{"id": "cambs/restrictions", "url": self.URL,
                        "council": "Cambridgeshire County Council",
                        "authorities": ["Cambridgeshire"],
                        "follow": r"/asset-library/[^?]+\.pdf$"}], fh)
        self.manual = os.path.join(self.tmp, "manual")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def client(self, pdf=b"%PDF-1.4 order", overridden=True):
        page = (b'<main><ul><li><a href="/asset-library/Soham-Byway-2016.pdf">'
                b'Soham Byway - 2016</a></li><li><a href="/news">News</a>'
                b'</li></ul></main>')
        client = Client({self.URL: page, self.PDF: pdf})
        client.overridden = {self.PDF: "cambridgeshire-byway-orders"} \
            if overridden else {}
        return client

    def check(self, client):
        return reg.check(client, register=self.tmp, today="2026-10-08",
                         manual_root=self.manual)

    def test_a_linked_pdf_is_flagged_new_and_credited_to_the_override(self):
        changed, unreachable, baselined = self.check(self.client())
        self.assertEqual(baselined, ["cambs/restrictions"])
        self.assertEqual(unreachable, [])
        self.assertEqual([c["page"] for c in changed],
                         ["cambs/restrictions/doc/soham-byway-2016.pdf"])
        self.assertTrue(changed[0]["new"])
        self.assertIn("owner decision", changed[0]["read_under"])

    def test_it_stays_flagged_until_accepted_and_accept_reads_nothing(self):
        self.check(self.client())
        changed, _u, _b = self.check(self.client())
        self.assertEqual(len(changed), 1)
        doc = changed[0]["page"]
        reg.accept([doc], Client({}), register=self.tmp)
        changed, _u, _b = self.check(self.client())
        self.assertEqual(changed, [])
        changed, _u, _b = self.check(self.client(pdf=b"%PDF-1.4 amended"))
        self.assertEqual([(c["page"], c["new"]) for c in changed],
                         [(doc, False)])

    def test_not_due_this_week_keeps_the_flag_and_reads_nothing(self):
        from polite_http import NotDue
        self.check(self.client())
        client = self.client(pdf=NotDue("read 2 days ago"))
        changed, unreachable, _b = self.check(client)
        self.assertEqual(len(changed), 1)
        self.assertEqual(unreachable, [])

    def test_only_links_the_pattern_names_are_followed(self):
        page = {"id": "p", "url": self.URL,
                "follow": r"/asset-library/[^?]+\.pdf$"}
        docs = reg.linked_documents(page, (
            b'<a href="/asset-library/a.pdf">a</a>'
            b'<a href="/asset-library/a.pdf#page=2">again</a>'
            b'<a href="https://elsewhere.example/asset-library/b.docx">b</a>'
            b'<a href="/news">c</a>'))
        self.assertEqual([d["url"] for d in docs],
                         ["https://www.cambs.example/asset-library/a.pdf"])


class ManualInbox(unittest.TestCase):
    """manual/<CODE>/: documents the owner saved by hand or a council sent,
    read like a snapshot; the authority's own pages are then not fetched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.manual = os.path.join(self.tmp, "manual")
        os.makedirs(os.path.join(self.manual, "IW"))
        with open(os.path.join(self.manual, "authorities.json"), "w") as fh:
            json.dump({"IW": {"authority": "Isle of Wight"}}, fh)
        with open(os.path.join(self.manual, "IW",
                               "byway-orders-2026-10-08.html"), "w") as fh:
            fh.write("<main><p>Byway N12 Brighstone: no motor vehicles "
                     "1 Oct to 30 Apr</p></main>")
        with open(os.path.join(self.tmp, "pages.json"), "w") as fh:
            json.dump([{"id": "iow/2998", "url": "https://www.iow.example/x",
                        "council": "Isle of Wight Council",
                        "authorities": ["Isle of Wight"]}], fh)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_a_saved_page_is_flagged_with_how_and_when_it_came(self):
        # No answers at all: any request would KeyError.
        changed, unreachable, _b = reg.check(
            Client({}), register=self.tmp, today="2026-10-08",
            manual_root=self.manual)
        self.assertEqual(unreachable, [])
        self.assertEqual([c["page"] for c in changed],
                         ["manual/IW/byway-orders-2026-10-08.html"])
        self.assertEqual(changed[0]["provenance"],
                         "saved by hand 2026-10-08")
        self.assertIn("Byway N12 Brighstone: no motor vehicles 1 Oct to 30 "
                      "Apr", changed[0]["diff"])

    def test_a_council_reply_says_so(self):
        with open(os.path.join(self.manual, "IW", "manifest.json"),
                  "w") as fh:
            json.dump({"files": {"byway-orders-2026-10-08.html": {
                "how": "eir", "date": "2026-09-30"}}}, fh)
        changed, _u, _b = reg.check(Client({}), register=self.tmp,
                                    today="2026-10-08",
                                    manual_root=self.manual)
        self.assertEqual(changed[0]["provenance"],
                         "supplied by the council 2026-09-30")

    def test_automated_on_reads_the_page_as_well(self):
        with open(os.path.join(self.manual, "IW", "manifest.json"),
                  "w") as fh:
            json.dump({"automated": "on"}, fh)
        _c, unreachable, baselined = reg.check(
            Client({"https://www.iow.example/x": b"<main>x</main>"}),
            register=self.tmp, today="2026-10-08", manual_root=self.manual)
        self.assertEqual(baselined, ["iow/2998"])

    def test_the_coverage_table_says_how_each_authority_came(self):
        import manual_inbox
        docs = dict((d["id"], d) for b in manual_inbox.inboxes(self.manual)
                    for d in b["docs"])
        items = [{"authority": "Isle of Wight",
                  "url": manual_inbox.PAGES_BASE +
                  "manual/IW/byway-orders-2026-10-08.html"},
                 {"authority": "Cambridgeshire",
                  "url": "https://www.cambs.example/asset-library/x.pdf"},
                 {"authority": "Essex", "url": "https://essex.example/a"}]
        got = reg.provenance_of(items, overrides=[{
            "id": "cambridgeshire-byway-orders",
            "prefix": "https://www.cambs.example/asset-library/"}],
            manual=docs)
        self.assertEqual(got, {
            "Cambridgeshire": ["robots.txt overridden by owner decision "
                               "(cambridgeshire-byway-orders)"],
            "Isle of Wight": ["saved by hand 2026-10-08"]})


if __name__ == "__main__":
    unittest.main(verbosity=1)

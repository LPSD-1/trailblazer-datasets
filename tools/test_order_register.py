#!/usr/bin/env python3
"""The order register publishes only what a person approved, matches it to
the right byway, and flags a changed council page instead of acting on it.

    python tools/test_order_register.py

No network, no containers: synthetic byways, a stand-in client, and a
temporary register directory.
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
HERE = os.path.dirname(os.path.abspath(__file__))

import council_orders as CO  # noqa: E402
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


class CollectedDocuments(unittest.TestCase):
    """The collector keeps a council's order PDF as its digest only (the
    owner's ruling, 7 October 2026). The check, which is what CI does with
    those documents, must work from that exactly as it did from the bytes:
    flag it once, credit it, and flag it again only when the digest moves."""

    PAGE = "https://en.powys.example/article/2446"
    PDF = "https://en.powys.example/media/10190/Gap-Road/pdf/gap.pdf?m=1"
    BYTES = b"%PDF-1.4 the Gap Road order"

    def setUp(self):
        import hashlib
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home-collected")
        os.makedirs(os.path.join(self.home, "snapshots"))
        with open(os.path.join(self.home, "collector.json"), "w") as fh:
            json.dump({"sources": [{"id": "powys-2446", "url": self.PAGE,
                                    "kind": "page"}]}, fh)
        with open(os.path.join(self.home, "snapshots", "p.html"),
                  "wb") as fh:
            fh.write(b'<main><a href="/media/10190/Gap-Road/pdf/gap.pdf'
                     b'?m=1">Gap Road</a></main>')
        self.digest = hashlib.sha256(self.BYTES).hexdigest()
        self.index({"digest": self.digest})
        self.register = os.path.join(self.tmp, "register")
        os.makedirs(self.register)
        with open(os.path.join(self.register, "pages.json"), "w") as fh:
            json.dump([{"id": "powys/2446", "url": self.PAGE,
                        "council": "Powys County Council",
                        "authorities": ["Powys"],
                        "follow": r"^https://en\.powys\.example/media/"}],
                      fh)

    def index(self, doc):
        with open(os.path.join(self.home, "index.json"), "w") as fh:
            json.dump({self.PAGE: {"id": "powys-2446", "kind": "page",
                                   "file": "p.html", "digest": "x",
                                   "collected": "2026-10-07"},
                       self.PDF: dict({"id": "powys-2446/doc/gap.pdf",
                                       "kind": "document",
                                       "collected": "2026-10-07",
                                       "override": "powys-order-documents"},
                                      **doc)}, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def check(self):
        from home_collector import HomeClient
        # The network stand-in answers nothing: any request would KeyError.
        return reg.check(HomeClient(Client({}), self.home),
                         register=self.register, today="2026-10-08",
                         manual_root=os.path.join(self.tmp, "manual"))

    def test_a_held_pdf_is_flagged_by_its_digest_and_credited(self):
        changed, unreachable, baselined = self.check()
        self.assertEqual(unreachable, [])
        self.assertEqual(baselined, ["powys/2446"])
        (doc,) = changed
        self.assertEqual(doc["page"], "powys/2446/doc/gap.pdf")
        self.assertTrue(doc["new"])
        self.assertEqual(doc["digest"], self.digest,
                         "not the digest fingerprint() takes of the bytes")
        self.assertEqual(doc["digest"], reg.fingerprint(
            {"url": self.PDF}, self.BYTES)[0])
        self.assertEqual(doc["provenance"],
                         "collected directly from the council 2026-10-07")
        self.assertIn("powys-order-documents", doc["read_under"])

    def test_accepted_it_stays_quiet_until_the_digest_moves(self):
        changed, _u, _b = self.check()
        reg.accept([changed[0]["page"]], Client({}), register=self.register)
        self.assertEqual(self.check()[0], [])
        self.index({"digest": "0" * 64})
        changed, _u, _b = self.check()
        self.assertEqual([(c["page"], c["new"]) for c in changed],
                         [("powys/2446/doc/gap.pdf", False)])

    def test_the_real_collected_powys_documents_still_check(self):
        # The committed home-collected/, as CI reads it: every Powys
        # document the register already flagged comes out with the digest
        # it was flagged with, from the digest alone, and nothing asks the
        # network.
        from home_collector import HOME, HomeClient
        with open(os.path.join(os.path.dirname(HERE), "tro", "register",
                               "pages.json")) as fh:
            pages = [p for p in json.load(fh)
                     if p["id"].startswith("powys")]
        with open(os.path.join(os.path.dirname(HERE), "tro", "register",
                               "changes.json")) as fh:
            flagged = dict((c["page"], c["digest"]) for c in json.load(fh)
                           if "/doc/" in c["page"]
                           and c["page"].startswith("powys"))
        self.assertTrue(pages and flagged)
        with open(os.path.join(self.register, "pages.json"), "w") as fh:
            json.dump(pages, fh)
        changed, unreachable, _b = reg.check(
            HomeClient(Client({}), HOME), register=self.register,
            today="2026-10-08",
            manual_root=os.path.join(self.tmp, "manual"))
        self.assertEqual(unreachable, [])
        got = dict((c["page"], c["digest"]) for c in changed
                   if "/doc/" in c["page"])
        self.assertEqual(got, flagged)


class ReadUnderTheOwnersDecision(unittest.TestCase):
    """CI wraps the real client in a HomeClient. The real client records an
    overridden read only when it makes it, so `read_under` must come from
    the client as it is after the read, not as it was when wrapped."""

    URL = "https://www.cambs.example/rights-of-way-restrictions"
    PDF = "https://www.cambs.example/asset-library/Soham-Byway-2016.pdf"

    def test_a_document_read_under_the_override_says_so(self):
        from home_collector import HomeClient
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(tmp, "pages.json"), "w") as fh:
            json.dump([{"id": "cambs/restrictions", "url": self.URL,
                        "council": "Cambridgeshire County Council",
                        "authorities": ["Cambridgeshire"],
                        "follow": r"/asset-library/[^?]+\.pdf$"}], fh)

        class Overriding(Client):
            def get(inner, url):
                if url == self.PDF:
                    inner.overridden[url] = "cambridgeshire-byway-orders"
                return Client.get(inner, url)

        real = Overriding({self.URL: b'<main><a href="/asset-library/'
                                     b'Soham-Byway-2016.pdf">x</a></main>',
                           self.PDF: b"%PDF-1.4 order"})
        real.overridden = {}
        home = os.path.join(tmp, "home-collected")
        os.makedirs(home)
        changed, _u, _b = reg.check(HomeClient(real, home), register=tmp,
                                    today="2026-10-08",
                                    manual_root=os.path.join(tmp, "manual"))
        self.assertEqual([c["page"] for c in changed],
                         ["cambs/restrictions/doc/soham-byway-2016.pdf"])
        self.assertIn("cambridgeshire-byway-orders",
                      changed[0].get("read_under") or "",
                      "the override read was not credited")


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



ROAD = Way("DY-UCR-stoney-middleton-15", "Derbyshire",
           "Unsurfaced unclassified road (UCR) Stoney Middleton 15/3",
           [[grid_to_wgs84(422600, 375100), grid_to_wgs84(422900, 375100)]],
           way_class="ucr")
WITH_ROAD = Byways(list(BYWAYS.ways.values()) + [ROAD])


class OnARoad(unittest.TestCase):
    """Review, 8 Oct 2026: a reviewer's pin to a UCR is a road's order, and
    the sections fallback is for byways only."""

    def test_an_order_pinned_to_a_road_is_labelled_road(self):
        data, _ = reg.build(WITH_ROAD, [order(paths=[], ways=[ROAD.uid],
                                              vehicles="all_users")])
        item = data["items"][0]
        self.assertEqual(item["on"], "ucr")
        self.assertEqual(CO.label_for(item)[2], "Road closed")

    def test_an_order_on_a_byway_keeps_its_byway_label(self):
        data, _ = reg.build(WITH_ROAD, [order(vehicles="all_users")])
        self.assertNotIn("on", data["items"][0])
        self.assertEqual(CO.label_for(data["items"][0])[2], "Byway closed")

    def test_the_sections_fallback_finds_byways_not_roads(self):
        # "Stoney Middleton 15" is BOAT 15/1 and 15/2; the road numbered
        # 15/3 is not Byway 15.
        o = order(authorities=["Derbyshire"], parish="Stoney Middleton",
                  paths=[["num", None, "15"]], grid_refs=[])
        self.assertEqual(reg.match_order(WITH_ROAD, o)[0],
                         ["DY-15/1", "DY-15/2"])

    def test_the_broken_checkout_floor_counts_byways_alone(self):
        many_roads = Byways([Way("R-%d" % i, "Devon", "UCR %d" % i,
                                 [[(-4.0 + i * 1e-4, 51.0),
                                   (-4.0 + i * 1e-4, 51.001)]],
                                 way_class="ucr") for i in range(1200)])
        self.assertEqual(len(many_roads), 1200)
        self.assertEqual(many_roads.count(), 0)
        import byway_match
        real = byway_match.load_byways
        byway_match.load_byways = lambda *a, **k: many_roads
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(reg.main(["build", "--out",
                                           os.devnull]), 1)
            self.assertIn("only 0 byways", out.getvalue())
        finally:
            byway_match.load_byways = real


if __name__ == "__main__":
    unittest.main(verbosity=1)

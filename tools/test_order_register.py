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
                         today="2027-01-01")

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


if __name__ == "__main__":
    unittest.main(verbosity=1)

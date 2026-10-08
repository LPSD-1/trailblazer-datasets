#!/usr/bin/env python3
"""Planning Inspectorate decisions are read off GOV.UK correctly, kept only
when they concern a byway, and pinned only on the byway they are about.

    python tools/test_pins_decisions.py

No network. The page fixture is the shape of the GOV.UK content API's body
for "Rights of way order information: decisions and maps published in 2026"
(7 October 2026), trimmed; the letter text is the opening of the Odell
decision (ROW/3339981M), with no names in it.
"""
import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pins_decisions as pins  # noqa: E402
from byway_match import Byways, Way  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402

BODY = """<div class="govspeak"><p>All decisions and maps are published as a
joint document.</p>
<p>15 September 2026 - <a href="#bedford-borough-council">ROW/3339981M
Bedford Borough Council</a></p>
<h2 id="bedford-borough-council">Bedford Borough Council</h2>
<p>(Definitive Map and Statement for the Former North Bedfordshire Borough)
(Odell: Byway Open to All Traffic No. 42) Modification Order 2022</p>
<ul><li><span class="gem-c-attachment-link">
<a class="govuk-link" href="https://assets.example/ROW_3339981M.docx">ROW/3339981M
 Decision date: 26 August 2026</a></span></li></ul>
<hr>
<p>(Melchbourne &amp; Yielden: part of Public Footpath No. Y17) Public Path
Diversion Order 2024</p>
<ul><li><span class="gem-c-attachment-link">
<a class="govuk-link" href="https://assets.example/ROW_3351834.docx">ROW/3351834
Decision date: 8 June 2026</a></span></li></ul>
<h2 id="west-berkshire-district-council">West Berkshire District Council</h2>
<p>(Byway Open to All Traffic Cold Ash 5 (part) Width) Definitive Map
Modification Order 2023</p>
<ul><li><span class="gem-c-attachment-link">
<a class="govuk-link" href="https://assets.example/ROW_3334428.pdf">ROW/3334428
Decision date: 13 March 2025</a></span></li></ul>
</div>"""

LETTER = ("Order Ref: ROW/3339981M\nThis Order is made under section 53 (2) "
          "(b) of the Wildlife and Countryside Act 1981.\nThe Order is dated "
          "6 October 2022 and proposes to modify the Definitive Map and "
          "Statement for the area by adding a byway open to all traffic as "
          "shown on the Order map.\nSummary of Decision: The Order is not "
          "confirmed.\nThe effect of the Order, if confirmed with the "
          "modifications previously proposed would be to record the Order "
          "route as a restricted byway. Points A SP 9712 5893 and "
          "SP 9771 5920.\n")


def docx(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body>" + "".join(
            "<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % line
            for line in text.split("\n")) + "</w:body></w:document>")
    return buf.getvalue()


class Page(unittest.TestCase):
    def test_every_decision_with_its_council_title_and_date(self):
        got = pins.parse_page("orders", "slug", BODY)
        self.assertEqual([(d["council"], d["ref"], d["decided"]) for d in got],
                         [("Bedford Borough Council", "ROW/3339981M",
                           "2026-08-26"),
                          ("Bedford Borough Council", "ROW/3351834",
                           "2026-06-08"),
                          ("West Berkshire District Council", "ROW/3334428",
                           "2025-03-13")])
        self.assertIn("Odell: Byway Open to All Traffic No. 42",
                      got[0]["title"])
        self.assertEqual(got[0]["letter"], "https://assets.example/"
                                           "ROW_3339981M.docx")

    def test_the_index_line_is_not_a_title(self):
        got = pins.parse_page("orders", "slug", BODY)
        self.assertFalse(any(d["title"].startswith("15 September")
                             for d in got))


class Letter(unittest.TestCase):
    def test_the_summary_line_decides_the_outcome(self):
        self.assertEqual(pins.outcome_of(LETTER), "not-confirmed")
        self.assertEqual(pins.outcome_of(
            "Summary of Decision: The Order is confirmed subject to the "
            "modifications set out below."), "confirmed-with-modifications")
        self.assertEqual(pins.outcome_of(
            "Summary of Decision: The Order is confirmed."), "confirmed")
        self.assertEqual(pins.outcome_of(
            "Decision: The appeal is allowed."), "allowed")

    def test_what_the_order_does_comes_from_its_purpose_not_the_reasoning(
            self):
        self.assertEqual(pins.effect_of("(Odell: BOAT No. 42) Modification "
                                        "Order", LETTER),
                         "add-or-upgrade-to-boat")

    def test_a_docx_is_read(self):
        self.assertIn("Summary of Decision", pins.letter_text(docx(LETTER)))
        self.assertEqual(pins.letter_text(b"%PDF-1.7"), "")

    def test_grid_references_are_found(self):
        self.assertEqual(pins.grid_refs_in(LETTER),
                         ["SP 9712 5893", "SP 9771 5920"])


TABLE = [{"swa": "335", "name": "Bedford Borough Council",
          "lanes": ["Bedford"]}]


def way(uid, name, *bng):
    return Way(uid, "Bedford", "Byway open to all traffic (BOAT) " + name,
               [[grid_to_wgs84(e, n) for e, n in bng]])


class Matching(unittest.TestCase):
    def decision(self, **kw):
        base = {"council": "Bedford Borough Council",
                "title": "(Odell: Byway Open to All Traffic No. 42) "
                         "Modification Order 2022",
                "grid_refs": ["SP 9712 5893", "SP 9771 5920"]}
        base.update(kw)
        return base

    def test_by_the_titles_parish_and_number(self):
        byways = Byways([way("BF-42", "ODELL 42", (497000, 258900),
                             (497700, 259200))])
        self.assertEqual(pins.match(self.decision(), byways, TABLE),
                         (["BF-42"], "reference"))

    def test_never_on_a_byway_whose_number_contradicts_the_title(self):
        # BOAT 42 was never added; the byways it would have joined pass
        # near both of its ends.
        byways = Byways([
            way("BF-2", "ODELL 2", (497120, 258930), (497710, 259200)),
        ])
        self.assertEqual(pins.match(self.decision(), byways, TABLE),
                         ([], None))

    def test_an_unnumbered_route_needs_both_ends_on_one_byway(self):
        d = self.decision(title="(A Byway Open to All Traffic, Odell at "
                                "Crabb's Lane) Modification Order")
        one_end = Byways([way("BF-7", "ODELL 7", (497120, 258930),
                              (496000, 258000))])
        self.assertEqual(pins.match(d, one_end, TABLE), ([], None))
        both = Byways([way("BF-8", "ODELL 8", (497120, 258930),
                           (497710, 259200))])
        self.assertEqual(pins.match(d, both, TABLE), (["BF-8"], "grid"))

    def test_council_headings_find_their_lanes(self):
        table = [{"name": "West Berkshire Council",
                  "lanes": ["West Berkshire"]}]
        self.assertEqual(pins.council_lanes("West Berkshire District "
                                            "Council", table),
                         ["West Berkshire"])


class Fetch(unittest.TestCase):
    def test_only_boat_decisions_are_kept_and_letters_read_once(self):
        asked = []

        class Client(object):
            requests = 0

            def get_json(self, url):
                asked.append(url)
                return {"details": {"body": BODY}}

            def get(self, url):
                asked.append(url)
                return docx(LETTER)

        saved = pins.PAGES
        pins.PAGES = [("orders", "one-page")]
        try:
            first = pins.fetch(Client(), [])
            letters = [u for u in asked if u.endswith(".docx")]
            self.assertEqual(letters, ["https://assets.example/"
                                       "ROW_3339981M.docx"],
                             "only a BOAT decision's letter is read")
            self.assertEqual([d["ref"] for d in first],
                             ["ROW/3334428", "ROW/3339981M"])
            odell = [d for d in first if d["ref"] == "ROW/3339981M"][0]
            self.assertEqual(odell["outcome"], "not-confirmed")
            del asked[:]
            pins.fetch(Client(), first)
            self.assertEqual([u for u in asked if u.endswith(".docx")], [],
                             "a letter already read was fetched again")
        finally:
            pins.PAGES = saved



def _fake_load_byways(seen):
    """load_byways as it behaves: 1,000 byways, and an unsurfaced road unless
    the caller asks for byways alone. `seen` gets what was handed back."""
    def load(pattern=None, include_ucr=True):
        ways = [Way("XX-%d" % i, "Devon", "Byway open to all traffic (BOAT) "
                    "Abbotsham %d" % i, [[(-4.2 + i * 1e-4, 51.0),
                                          (-4.2 + i * 1e-4, 51.001)]])
                for i in range(1000)]
        if include_ucr:
            ways.append(Way("DN-UCR-abbotsham-301", "Devon",
                            "Rocky Lane (Abbotsham UCR 301)",
                            [[(-4.25, 51.02), (-4.25, 51.03)]],
                            way_class="ucr"))
        seen.append(Byways(ways))
        return seen[-1]
    return load


class _Stop(Exception):
    pass


class DefinitiveMapOnly(unittest.TestCase):
    """A Planning Inspectorate decision is about the definitive map, which no UCR is on: main() asks for
    byways alone (review, 8 Oct 2026: reverting include_ucr=False there
    left every test green)."""

    def test_main_matches_against_byways_and_never_a_road(self):
        import byway_match
        seen = []
        real = byway_match.load_byways
        byway_match.load_byways = _fake_load_byways(seen)
        try:
            import build_tro
            real_table = build_tro.load_authority_table

            def stop():
                raise _Stop()
            build_tro.load_authority_table = stop
            try:
                with self.assertRaises(_Stop):
                    pins.main(["--offline"])
            finally:
                build_tro.load_authority_table = real_table
        finally:
            byway_match.load_byways = real
        self.assertEqual(len(seen), 1)
        self.assertNotIn("DN-UCR-abbotsham-301", seen[0].ways)
        self.assertEqual(seen[0].count("ucr"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)

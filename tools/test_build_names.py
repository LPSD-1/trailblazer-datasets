#!/usr/bin/env python3
"""Is a names pack sorted the way the app searches it?

    python -m unittest tools.test_build_names   (from the repo root)
    python tools/test_build_names.py

A .tbnames pack is sorted by `fold` and binary-searched by the app's
`foldName`. If the two disagree about a single letter, every name holding that
letter is filed where the app never looks, and "nothing found" is
indistinguishable from "no data for your area". The vectors below are the
ones test/names_pack_test.dart holds `foldName` to in the app, letter for
letter.
"""
import csv
import io
import os
import struct
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_names  # noqa: E402

VECTORS = {
    "Dôl-y-Bont": "dolybont",
    "Glyndŵr Road": "glyndwrroad",
    "TŶ MAWR": "tymawr",
    "Penrhiw-pâl": "penrhiwpal",
    "Æthelstan's Œuvre": "aethelstansoeuvre",
    "Straße": "strasse",
    "Łódź": "lodz",
    "ẀYL": "wyl",
    "İfor": "ifor",
    "Café × 2 ÷ 3": "cafe23",
    # The strings from before accents were folded, unchanged.
    "IP30 0PA": "ip300pa",
    "ip30  0pa": "ip300pa",
    "Devil's Bridge": "devilsbridge",
    "Ffordd-y-Mynydd": "ffordd" "ymynydd",
    "B4574": "b4574",
    "   ": "",
}

# Every accented letter the fold knows, lower case and upper, and what they
# fold to: the same two strings as test/names_pack_test.dart.
ALL_LOWER = (
    "àáâãäåāăąæç"
    "ćĉċčðďđèéêë"
    "ēĕėęěĝğġģĥħ"
    "ìíîïĩīĭįıĳĵ"
    "ķĸĺļľŀłñńņň"
    "ŉŋòóôõöøōŏő"
    "œŕŗřśŝşšſßţ"
    "ťŧþùúûüũūŭů"
    "űųŵẁẃẅýÿŷỳź"
    "żž"
)
ALL_LOWER_FOLDED = (
    "aaaaaaaaaaecccccdddeeeeeeeeegggghhiiiiiiiiiijjkklllllnnnnnnoooooooooo"
    "errrssssssstttthuuuuuuuuuuwwwwyyyyzzz"
)
ALL_UPPER = (
    "ÀÁÂÃÄÅĀĂĄÆÇ"
    "ĆĈĊČÐĎĐÈÉÊË"
    "ĒĔĖĘĚĜĞĠĢĤĦ"
    "ÌÍÎÏĨĪĬĮIĲĴĶ"
    "ĹĻĽĿŁÑŃŅŇŊÒ"
    "ÓÔÕÖØŌŎŐŒŔŖ"
    "ŘŚŜŞŠSŢŤŦÞÙÚ"
    "ÛÜŨŪŬŮŰŲŴẀẂ"
    "ẄÝŸŶỲŹŻŽ"
)
ALL_UPPER_FOLDED = (
    "aaaaaaaaaaecccccdddeeeeeeeeegggghhiiiiiiiiiijjklllllnnnnnoooooooooo"
    "errrssssstttthuuuuuuuuuuwwwwyyyyzzz"
)


def _names_in_file_order(pack):
    """Every record's name, in the order the pack files them."""
    count, = struct.unpack_from("<I", pack, 5)
    contexts, = struct.unpack_from("<H", pack, 9)
    at = 11
    for _ in range(contexts):
        at += 1 + pack[at]
    names_at, = struct.unpack_from("<I", pack, at)
    at += 4
    out = []
    for i in range(count):
        offset, length = struct.unpack_from("<IB", pack, at + i * 14)
        out.append(pack[names_at + offset:names_at + offset + length]
                   .decode("utf-8"))
    return out


class Fold(unittest.TestCase):
    def test_the_vectors_the_app_is_held_to(self):
        for name, folded in VECTORS.items():
            self.assertEqual(build_names.fold(name), folded, name)

    def test_every_accented_letter(self):
        self.assertEqual(
            "".join(build_names.ACCENTED.values()), ALL_LOWER,
            "the table is not the one the app's vectors were cut from")
        self.assertEqual(build_names.fold(ALL_LOWER), ALL_LOWER_FOLDED)
        self.assertEqual(build_names.fold(ALL_UPPER), ALL_UPPER_FOLDED)

    def test_a_welsh_name_is_filed_where_it_is_typed(self):
        # The bug, end to end: typed on an ordinary keyboard, Dol-y-Bont must
        # fold to the key its record is sorted under.
        self.assertEqual(build_names.fold("Dol-y-Bont"),
                         build_names.fold("Dôl-y-Bont"))


class Pack(unittest.TestCase):
    def test_records_are_sorted_by_the_accent_fold(self):
        rows = [
            ("Dolau", 1, "Ceredigion", 260000, 280000),
            ("Dôl-y-Bont", 2, "Ceredigion", 262000, 288000),
            ("Dlyn Road", 1, "Ceredigion", 261000, 281000),
            ("Domen Road", 1, "Ceredigion", 263000, 282000),
        ]
        names = _names_in_file_order(build_names.build_pack(rows))
        self.assertEqual(
            names, ["Dlyn Road", "Dolau", "Dôl-y-Bont", "Domen Road"])
        folded = [build_names.fold(n) for n in names]
        self.assertEqual(folded, sorted(folded))


def _contexts_of(pack):
    """The pack's context string table, in index order."""
    contexts, = struct.unpack_from("<H", pack, 9)
    at = 11
    out = []
    for _ in range(contexts):
        length = pack[at]
        out.append(pack[at + 1:at + 1 + length].decode("utf-8"))
        at += 1 + length
    return out


def _open_names_zip(folder, rows):
    """A minimal OS Open Names zip: one Data/*.csv holding `rows`.

    Each row is a dict of column index -> value on an otherwise empty
    30-column line, which is the shape `read_records` reads.
    """
    text = io.StringIO()
    writer = csv.writer(text)
    for values in rows:
        line = [""] * 30
        for col, value in values.items():
            line[col] = value
        writer.writerow(line)
    path = os.path.join(folder, "opname_csv_gb.zip")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Data/SO02.csv", text.getvalue().encode("utf-8"))
    return path


def _road(context_col, context, name="Heol Saeson"):
    return {
        build_names.C_NAME1: name,
        build_names.C_LOCAL_TYPE: "Named Road",
        build_names.C_X: "304500",
        build_names.C_Y: "228500",
        context_col: context,
        build_names.C_REGION: "Wales",
        build_names.C_COUNTRY: "Wales",
    }


class WelshContext(unittest.TestCase):
    """OS writes a Welsh authority as "Welsh - English". Where the two halves
    are the same name, a rider's place row read "Place - Powys - Powys"
    (device finding D). Said once; a real pair of names keeps both."""

    def _contexts(self, rows):
        with tempfile.TemporaryDirectory() as folder:
            source = _open_names_zip(folder, rows)
            records = list(build_names.read_records(source, "region"))
        # PREMISE: the rows were read at all. Zero records would pass every
        # assertion about what the records say.
        self.assertEqual(len(records), len(rows), "rows were not read")
        return [r[3] for r in records]

    def test_a_district_pair_of_one_name_is_said_once(self):
        # The shape the fix item names: POPULATED_PLACE empty, the pair on
        # DISTRICT_BOROUGH.
        self.assertEqual(
            self._contexts([_road(build_names.C_DISTRICT_BOROUGH,
                                  "Powys - Powys")]),
            ["Powys"])

    def test_a_county_pair_of_one_name_is_said_once(self):
        # The shape OS actually ships: every one of the 161,785 Welsh pairs in
        # opname_csv_gb.zip is on COUNTY_UNITARY, with DISTRICT_BOROUGH empty.
        contexts = self._contexts([
            _road(build_names.C_COUNTY, "Powys - Powys"),
            _road(build_names.C_COUNTY,
                  "Rhondda Cynon Taf - Rhondda Cynon Taf"),
            _road(build_names.C_COUNTY, "Blaenau Gwent - Blaenau Gwent"),
        ])
        self.assertEqual(
            contexts, ["Powys", "Rhondda Cynon Taf", "Blaenau Gwent"])

    def test_halves_that_differ_only_by_accent_or_case_are_one_name(self):
        # Equal as the app compares names, so still one name: keep the first
        # (the Welsh, accent and all).
        self.assertEqual(
            self._contexts([_road(build_names.C_COUNTY,
                                  "Sir Ynys Môn - sir ynys mon")]),
            ["Sir Ynys Môn"])

    def test_a_real_pair_keeps_both_languages(self):
        # Sir Benfro and Pembrokeshire are two names for one place; a rider may
        # know either, so both stay. Only a pair that says nothing twice folds.
        contexts = self._contexts([
            _road(build_names.C_COUNTY, "Sir Benfro - Pembrokeshire"),
            _road(build_names.C_COUNTY,
                  "Castell-nedd Port Talbot - Neath Port Talbot"),
        ])
        self.assertEqual(contexts, [
            "Sir Benfro - Pembrokeshire",
            "Castell-nedd Port Talbot - Neath Port Talbot",
        ])

    def test_a_populated_place_is_untouched(self):
        self.assertEqual(
            self._contexts([_road(build_names.C_POPULATED_PLACE, "Brecon")]),
            ["Brecon"])

    def test_a_hyphen_without_spaces_is_never_split(self):
        # Only " - " separates the two languages. A hyphen inside a name
        # is part of it, even where the parts read the same either side
        # (no such place is in the source today; this holds the rule).
        self.assertEqual(
            self._contexts([_road(build_names.C_POPULATED_PLACE,
                                  "Tre-tre")]),
            ["Tre-tre"])

    def test_the_built_pack_says_powys_once(self):
        # Through build_pack as well, since that writes the context table the
        # app reads.
        with tempfile.TemporaryDirectory() as folder:
            source = _open_names_zip(
                folder, [_road(build_names.C_COUNTY, "Powys - Powys")])
            rows = [r[1:] for r in build_names.read_records(source, "region")]
        self.assertEqual(len(rows), 1, "PREMISE: the row was read")
        self.assertEqual(_contexts_of(build_names.build_pack(rows)),
                         ["", "Powys"])


class PublishedPacks(unittest.TestCase):
    """The tracked packs riders download, not a fixture: the tool being right
    is no use while names/ still holds a pack built before it was."""

    PUBLISHED = ("east-anglia", "midlands", "north", "south-east",
                 "south-west", "wales")

    def test_no_published_context_says_one_name_twice(self):
        names = os.path.join(os.path.dirname(HERE), "names")
        welsh_pairs = 0
        doubled = []
        for slug in self.PUBLISHED:
            path = os.path.join(names, "%s.tbnames" % slug)
            self.assertTrue(os.path.exists(path), "missing %s" % path)
            with open(path, "rb") as fh:
                contexts = _contexts_of(fh.read())
            for text in contexts:
                halves = text.split(" - ")
                if len(halves) != 2:
                    continue
                if slug == "wales":
                    welsh_pairs += 1
                if build_names.fold(halves[0]) == build_names.fold(halves[1]):
                    doubled.append("%s: %s" % (slug, text))
        # PREMISE: the Welsh pack was read and still carries its bilingual
        # pairs, so an empty `doubled` means folded, not unread.
        self.assertGreater(welsh_pairs, 0, "no Welsh pairs read")
        self.assertEqual(doubled, [])


if __name__ == "__main__":
    unittest.main()

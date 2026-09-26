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
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


if __name__ == "__main__":
    unittest.main()

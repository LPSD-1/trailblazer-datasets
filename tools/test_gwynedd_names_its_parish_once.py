#!/usr/bin/env python3
"""A Gwynedd byway is named with its parish once.

    python tools/test_gwynedd_names_its_parish_once.py

THE DEFECT (measured 3 Oct 2026, rowmaps' Gwynedd byways run through this
builder's own normalise and join_pieces). council_reference put the parish
in front of the council number, and Gwynedd's number already carries it:
'GY|Abermaw|Prow Abermaw Rhif 2' published as "Byway open to all traffic
(BOAT) Abermaw Prow Abermaw Rhif 2". 87 of 113 Gwynedd references are
'Prow <parish> Rhif N', and every one of them repeated its parish, on the
lane sheet, the record card and every search row.

THE FIX: the parish is left out when the number names it as a whole word.
Every other council's name is as it was, and the names stay one per record,
because the join (join_pieces) is keyed on them.

Exit 0 when all hold, 1 on any failure.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_packages  # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("    got  %r\n    want %r" % (got, want))
        FAILED.append(name)


CR = build_packages.council_reference
_BOAT = "byway_open_to_all_traffic"


def _name(ref, code="GY", authority="Gwynedd"):
    feature = build_packages.normalise(
        {"type": "Feature",
         "geometry": {"type": "LineString",
                      "coordinates": [[-4.05, 52.72], [-4.04, 52.73]]},
         "properties": {"Name": ref, "Description": "BO|GY:1|0.100|none"}},
        code, authority, _BOAT)
    return feature["properties"]["name"], feature["properties"]["lane_uid"]


# Real references, from rowmaps' Gwynedd byways.
check("Abermaw's number names its parish, so the parish is not repeated",
      CR("GY|Abermaw|Prow Abermaw Rhif 2", "GY"), ("Prow Abermaw Rhif 2",))
check("a two-word parish is found in the number too",
      CR("GY|Dyffryn Ardudwy|Prow Dyffryn Ardudwy Rhif 3", "GY"),
      ("Prow Dyffryn Ardudwy Rhif 3",))
check("a number with a note in it still names its parish",
      CR("GY|Beddgelert|Prow Beddgelert (byw) Rhif 22b", "GY"),
      ("Prow Beddgelert (byw) Rhif 22b",))
check("a piece suffix is dropped and the parish still said once",
      CR("GY|Abermaw|Prow Abermaw Rhif 2#1", "GY"), ("Prow Abermaw Rhif 2",))

abermaw, abermaw_uid = _name("GY|Abermaw|Prow Abermaw Rhif 2")
check("the published name says Abermaw once",
      abermaw, "Byway open to all traffic (BOAT) Prow Abermaw Rhif 2")
check("the id does not move: authority, number and the line's hash",
      abermaw_uid.startswith("GY-Prow Abermaw Rhif 2-"), True)

# NOT A SUBSTRING. A parish whose name only begins another word stays.
check("a parish that is only part of a word in the number is kept",
      CR("GY|Llan|Prow Llanbedr Rhif 4", "GY"),
      ("Llan", "Prow Llanbedr Rhif 4"))

# A PARISH ENDING IN A BRACKET. Nottinghamshire's 'Gamston (B)' runs straight
# into its number, and was live as "Gamston (B) Gamston (B)BOAT4" (ways-
# midlands, c825e54): a whole word ends at the bracket, not at a space.
check("a parish that ends in a bracket is found in the number too",
      CR("NT|Gamston (B)|Gamston (B)BOAT4", "NT"), ("Gamston (B)BOAT4",))

# EVERY OTHER COUNCIL AS IT WAS (the lane-data-4 references).
check("Wiltshire keeps its parish code", CR("WT|LACO|24", "WT"),
      ("LACO", "24"))
check("Derbyshire keeps its parish",
      CR("DY|Great Hucklow-WD41|18/3", "DY"), ("Great Hucklow", "18/3"))
check("Kent's piece keeps its parish", CR("KT|SR|74#1", "KT"), ("SR", "74"))

# ONE NAME PER RECORD: join_pieces joins on the name, so two records that
# were two names before must still be two.
refs = ["GY|Abermaw|Prow Abermaw Rhif 2", "GY|Abermaw|Prow Abermaw Rhif 3",
        "GY|Barmouth|Prow Abermaw Rhif 2", "GY|Llanbedr|Prow Llanbedr Rhif 2",
        "GY|Dyffryn Ardudwy|Prow Dyffryn Ardudwy Rhif 2"]
check("PREMISE: five distinct references", len(set(refs)), 5)
check("five distinct references are five distinct names",
      len({CR(r, "GY") for r in refs}), 5)

sys.exit(1 if FAILED else 0)

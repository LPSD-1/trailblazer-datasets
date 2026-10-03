#!/usr/bin/env python3
"""WAYS-SCHEMA says also_recorded_by can carry ONE authority's own double.

FEATURES-12 RESIDUE, lane-data-2. duplicate_ways.py now takes out one
authority's byte-identical doubles (pass 0, `_twins`) and pairs one
authority's records against each other (`same_authority=True`), and what it
drops rides on the record kept in `also_recorded_by` - the same key the
cross-authority merge writes, read by the app's sheet, record card and backup
restore. docs/WAYS-SCHEMA.md still documents that key as "one way, two
authorities": a record "is dropped only in favour of records it lies wholly
within 15 m of ... same class, OTHER AUTHORITY". A reader of the schema -
the app among them, whose `AlsoRecordedBy` doc says "Another authority's
record" - is told an entry is never the kept way's own authority, and the
data now carries exactly that (Surrey 526 x6, CB-3 x3, HS-Y349BY11 x2, BK-15
x1 on the published data, measured by the verifier).

PREMISE: duplicate_ways.py really does drop same-authority records now.

Run from the repository root (or under pytest):
    python test/hunt/residue/test_the_schema_says_one_authority_can_record_a_way_twice.py
Exit 0 clean, 1 on the defect.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _section(text, heading):
    at = text.find(heading)
    if at < 0:
        return ""
    nxt = re.search(r"\n#{2,3} ", text[at + 1:])
    return text[at:] if nxt is None else text[at:at + 1 + nxt.start()]


def test_premise_one_authority_s_doubles_are_dropped():
    src = _read("tools/duplicate_ways.py")
    assert re.search(r"def _twins\b", src), "PREMISE: pass 0 (_twins) exists"
    assert "same_authority=True" in src, (
        "PREMISE: one authority's records are paired against each other")


def test_the_schema_does_not_say_every_entry_is_another_authority():
    sec = _section(_read("docs/WAYS-SCHEMA.md"), "### `also_recorded_by`")
    assert sec, "PREMISE: the also_recorded_by section was found"
    folded = re.sub(r"\s+", " ", sec)
    stale = [m.group(0) for m in re.finditer(
        r"one way, two authorities|same class, other authority", folded)]
    says_same = re.search(
        r"same authority|one authority's (own )?doubles?|"
        r"by one authority twice|recorded twice by one",
        folded, re.IGNORECASE)
    assert not stale or says_same, (
        "docs/WAYS-SCHEMA.md documents also_recorded_by as another "
        "authority's record only (%s), and never says an entry can be the "
        "kept way's own authority - which duplicate_ways.py now writes"
        % "; ".join(repr(s) for s in stale))


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except AssertionError as e:
                failed += 1
                print("FAIL %s\n     %s" % (name, e))
    sys.exit(1 if failed else 0)

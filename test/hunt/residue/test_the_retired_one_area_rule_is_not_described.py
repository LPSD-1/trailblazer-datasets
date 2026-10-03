#!/usr/bin/env python3
"""The retired one-area rule is not still described as the rule.

FEATURES-12 RESIDUE, lane-data-1. Until 2 Oct 2026 a way two areas both carry
was drawn in the tiles of ONE of them (`build_containers.build_all` passed
`tile_exclude=claimed`), so a rider holding only the other area did not see
it. The fix draws it in every area that carries it, the app's tile merge
(`PmTilesServer._tileFor`, `mergeVectorTiles`' seenIds) draws it once, and
`check_containers.py` now REFUSES an area whose records have no tiles.

The words describing the old rule were left in five places, each of which now
tells a reader the opposite of what the build and its guard do:

  tools/build_map_container.py    "ONE OWNER PER LANE, FOR TILES, ACROSS A
                                   VEHICLE'S AREAS" (and "only the TILES get
                                   an owner")
  tools/validate_container.py     "no tiles, but N records. Legitimate when
                                   every lane is drawn by an earlier area" -
                                   a fault now, which check_containers refuses
  .github/workflows/refresh-data.yml  the guard refuses "a lane claimed by two
                                   areas" - it refuses the opposite now
  tools/make_changeset_fixture.py "A way straddling two regions is drawn by
                                   ONE of them (build_containers' `claimed`)"
  docs/CUTOVER-REHEARSAL.md       "lanes claimed by two areas"

PREMISE: build_containers no longer passes the claimed set as tile_exclude,
so the old rule really is retired and these words really are stale.

Run from the repository root (or under pytest):
    python test/hunt/residue/test_the_retired_one_area_rule_is_not_described.py
Exit 0 clean, 1 on the defect.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return re.sub(r"\s+", " ", f.read())


STALE = [
    ("tools/build_map_container.py",
     r"ONE OWNER PER LANE|only the TILES get an owner"),
    ("tools/validate_container.py",
     r"Legitimate when every lane"),
    (".github/workflows/refresh-data.yml",
     r"a lane claimed by two areas"),
    ("tools/make_changeset_fixture.py",
     r"drawn by ONE of them \(build_containers' `claimed`\)"),
    ("docs/CUTOVER-REHEARSAL.md",
     r"lanes claimed by two areas"),
]


def _code(rel):
    """The file's code with its comments taken out."""
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return "".join(line.split("#", 1)[0] + "\n" for line in f)


def test_premise_the_one_area_rule_is_retired():
    build = _code("tools/build_containers.py")
    assert "def build_all" in build, "PREMISE: build_all is in build_containers"
    assert not re.search(r"tile_exclude\s*=", build), (
        "PREMISE: build_all still passes tile_exclude=claimed - the old rule "
        "is live and these words are not stale")


def test_no_file_still_describes_the_one_area_rule():
    found = []
    for rel, pattern in STALE:
        m = re.search(pattern, _read(rel))
        if m:
            found.append("%s: %r" % (rel, m.group(0)))
    assert not found, (
        "the retired one-area rule is still described as the rule in:\n  "
        + "\n  ".join(found))


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

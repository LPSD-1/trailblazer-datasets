#!/usr/bin/env python3
"""The retired one-area rule's words are caught even wrapped over comment
lines, and validate_container.py does not describe the agreement check as
comparing containers against each other.

FEATURES-13 TRIAGE of the features-12 verifier, lane-data-1.

1. test_the_retired_one_area_rule_is_not_described.py collapses whitespace
   but leaves the "#" between wrapped comment lines, so its pattern
   "only the TILES get an owner" never matched the old paragraph as it was
   written ("...only the TILES get an\\n    # owner") - not even at the commit
   that still carried it. And two of the old phrases in validate_container.py's
   docstring - "one owning area per lane", "the boundary rule working" - are
   not in its list at all. Here the comment markers of wrapped lines are taken
   out first, the list is complete, and a CONTROL proves every pattern fires
   on the text of 278fd05, the last commit that still described the rule.

2. tools/validate_container.py says "`check_containers.py` compares
   containers AGAINST EACH OTHER - tiles versus records in each area, every
   record drawn by its own area, the tile size ceiling". Every check it then
   lists is per container: check_containers' own docstring says duty (1) is
   "Checked per container", and the cross-area "one lane belongs to one
   area" duty is the retired one. The sentence still teaches a reader the
   old, cross-container model.

Run from the repository root (or under pytest):
    python test/hunt/residue/test_the_one_area_rule_words_are_caught_across_wraps.py
Exit 0 clean, 1 on the defect.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

# The last commit whose five files still described the one-area rule.
BEFORE = "278fd05"


def _flatten(text):
    # A comment wrapped over lines leaves its "#" or "#:" between the words;
    # take those out too, or a phrase split by a wrap never matches.
    text = re.sub(r"\n[ \t]*#:?", " ", text)
    return re.sub(r"\s+", " ", text)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return _flatten(f.read())


STALE = [
    ("tools/build_map_container.py",
     r"ONE OWNER PER LANE|only the TILES get an owner"),
    ("tools/validate_container.py",
     r"Legitimate when every lane|one owning area per lane"
     r"|the boundary rule working"),
    (".github/workflows/refresh-data.yml",
     r"a lane claimed by two areas"),
    ("tools/make_changeset_fixture.py",
     r"drawn by ONE of them \(build_containers' `claimed`\)"),
    ("docs/CUTOVER-REHEARSAL.md",
     r"lanes claimed by two areas"),
]


def _at(commit, rel):
    try:
        r = subprocess.run(["git", "show", "%s:%s" % (commit, rel)],
                           cwd=ROOT, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return _flatten(r.stdout.decode("utf-8", "replace"))


def test_control_every_pattern_fires_on_the_text_that_described_the_rule():
    missed = []
    read = 0
    for rel, pattern in STALE:
        old = _at(BEFORE, rel)
        if old is None:
            continue
        read += 1
        for alt in pattern.split("|"):
            if not re.search(alt, old):
                missed.append("%s: %r" % (rel, alt))
    if read == 0:
        import pytest
        pytest.skip("git or commit %s not available" % BEFORE)
    assert read == len(STALE), (
        "PREMISE: all five files were read at %s (got %d)" % (BEFORE, read))
    assert not missed, (
        "CONTROL: these patterns match nothing even in the text that "
        "described the old rule, so they can never fire:\n  "
        + "\n  ".join(missed))


def test_no_file_still_describes_the_one_area_rule_even_wrapped():
    found = []
    for rel, pattern in STALE:
        m = re.search(pattern, _read(rel))
        if m:
            found.append("%s: %r" % (rel, m.group(0)))
    assert not found, (
        "the retired one-area rule is still described:\n  "
        + "\n  ".join(found))


def test_validate_container_does_not_say_containers_are_compared_to_each_other():
    check = _read("tools/check_containers.py")
    assert "Checked per container" in check, (
        "PREMISE: check_containers.py checks tiles against records per "
        "container")
    assert "RETIRED" in check, (
        "PREMISE: the cross-area one-owner duty is retired")
    doc = _read("tools/validate_container.py")
    m = re.search(r"compares containers AGAINST EACH OTHER", doc,
                  re.IGNORECASE)
    assert not m, (
        "tools/validate_container.py says check_containers.py %r, then lists "
        "only per-container checks; the cross-container duty is the retired "
        "one-area rule" % m.group(0))


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except AssertionError as e:
                failed += 1
                print("FAIL %s\n%s" % (name, e))
            except Exception as e:  # pytest.skip outside pytest
                print("skip %s: %s" % (name, e))
    sys.exit(1 if failed else 0)

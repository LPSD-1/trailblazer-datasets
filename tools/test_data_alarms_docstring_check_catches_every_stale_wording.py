#!/usr/bin/env python3
"""The alarm hunt's docstring check catches the stale claim however it is worded.

    python tools/test_data_alarms_docstring_check_catches_every_stale_wording.py

Exit 0: every stale wording is caught. Exit 1: at least one is not. Exit 3:
the harness could not reach a verdict (PREMISE), which is not a pass.

It was a hunt_* red proof until the guard was fixed (res9); it is test_* now,
so refresh-data.yml's `for t in tools/test_*.py` loop runs it.

THE DEFECT (features-9 verifier residue, res8-data-alarm-hunt-docstring).
tools/hunt_data_pipeline_alarms.py once said in its docstring that it is
named hunt_* so the tools/test_*.py glob skips it - false since the test_*
wrappers began running it from that glob. The guard,
test_data_alarms_premise_names_the_check.py `docstring_wrong()`, looked for
the stale claim with

    re.search(r"glob (does not|leaves it|never) ", flat)
    or "Rename it in the fix" in flat

so the item's OWN wording, "the tools/test_*.py glob skips it", passed it,
and so did "glob doesn't", "glob won't", "glob will not" and "glob ignores
it". The verifier's mutation M8 (the docstring put back to "...glob skips
it.") stayed green. The guard now matches every wording below.

WHAT IS RUN. The real docstring_wrong(), with its TOOLS pointed at a
throwaway copy of the hunt and of every tools/test_*.py that runs it, the
hunt's docstring carrying ONE stale sentence. Premises: the unmodified
docstring is clean, and a wording the guard already knows ("glob does not
run it") is caught - so a red here is the guard's pattern, not the harness.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_data_alarms_premise_names_the_check as guard  # noqa: E402

TOOLS = os.path.dirname(os.path.abspath(__file__))
HUNT = guard.HUNT_NAME

# The sentence a regression would put back, in the words a person might use.
KNOWN = "Named hunt_* so the tools/test_*.py glob does not run it."
STALE = [
    "Named hunt_* so the tools/test_*.py glob skips it.",
    "Named hunt_* so the tools/test_*.py glob doesn't run it.",
    "Named hunt_* so the tools/test_*.py glob won't run it.",
    "Named hunt_* so the tools/test_*.py glob will not run it.",
    "Named hunt_* so the tools/test_*.py glob ignores it.",
    # res9 verifier: neighbours of those the first fix still let through.
    "Named hunt_* so the tools/test_*.py glob will skip it.",
    "Named hunt_* so the tools/test_*.py glob excludes it.",
    "Named hunt_* so the tools/test_*.py glob misses it.",
    "Named hunt_* so the tools/test_*.py glob cannot see it.",
    "Named hunt_* so the tools/test_*.py glob can't see it.",
    "Named hunt_* so the tools/test_*.py glob passes over it.",
    "Named hunt_* so the tools/test_*.py Glob skips it.",
    "Named hunt_* so the tools/test_*.py glob does NOT run it.",
    "Named hunt_* so the tools/test_*.py glob doesn’t run it.",
    "Named hunt_* so the tools/test_*.py glob won’t run it.",
    "Named hunt_* so it is not run by the tools/test_*.py glob.",
    "Named hunt_* to keep it out of the tools/test_*.py glob.",
]


class Premise(Exception):
    pass


def _flagged(extra):
    """docstring_wrong() over a copy of tools/ whose hunt docstring has
    [extra] added after its first line (None: unchanged)."""
    with open(os.path.join(TOOLS, HUNT), encoding="utf-8") as f:
        text = f.read()
    if extra is not None:
        head = text.index('"""') + 3
        eol = text.index("\n", head)
        text = text[:eol] + "\n\n" + extra + text[eol:]
    tmp = tempfile.mkdtemp(prefix="docstring-guard-")
    real = guard.TOOLS
    try:
        for name in guard.wrappers():
            shutil.copyfile(os.path.join(TOOLS, name), os.path.join(tmp, name))
        with open(os.path.join(tmp, HUNT), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write(text)
        guard.TOOLS = tmp
        # Only the stale-glob finding counts: the guard's other finding (a
        # wrapper left unnamed) must not make a missed wording look caught.
        return [w for w in guard.docstring_wrong()
                if "leaves the hunt alone" in w]
    finally:
        guard.TOOLS = real
        shutil.rmtree(tmp, ignore_errors=True)


def premise():
    clean = _flagged(None)
    if clean:
        raise Premise("the unmodified hunt docstring should pass the guard; "
                      "it said: %s" % clean)
    if not _flagged(KNOWN):
        raise Premise("the guard should already catch %r; the harness is "
                      "not reaching it" % KNOWN)


def missed():
    return [s for s in STALE if not _flagged(s)]


def test_every_stale_wording_is_caught():
    premise()
    left = missed()
    assert not left, "the docstring guard lets these through: %s" % left


def main():
    try:
        premise()
    except (Premise, guard.Premise) as e:
        print("PREMISE  %s" % e)
        return 3
    left = missed()
    for s in STALE:
        print("%-4s %s" % ("FAIL" if s in left else "ok", s))
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())

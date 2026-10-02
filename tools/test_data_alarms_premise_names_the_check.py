#!/usr/bin/env python3
"""A red alarm-hunt premise names the check that is red, not just "red".

    python tools/test_data_alarms_premise_names_the_check.py

Exit 0: every case holds. Exit 1: at least one does not. Exit 3: the harness
could not reach a verdict (PREMISE), which is not a pass.

THE DEFECT. tools/test_data_alarms_follow_up_fires_on_failure.py (and its
companion test_data_alarms_follow_up_refuses_what_never_runs.py) run the real
tools/hunt_data_pipeline_alarms.py from refresh-data.yml's
`for t in tools/test_*.py` loop. Their premise needs the WHOLE hunt green on
the unmodified workflows, because a mutation is judged by "exactly one FAIL".
So when checks 1-3 go red - a publishing job that lost its failure alarm, a
clobbered asset pushed once, a dry run standing a real alarm down - the lane
refresh stops publishing with "PREMISE the unmodified workflows should be
green with three soft steps told", which is about check 4's soft steps and
names neither the check nor the workflow that is actually wrong.

And the hunt's own docstring said it was named hunt_* so that glob would skip
it - true once, false since the test_* wrappers began running it from there.

WHAT IS RUN. The real premise() of the wrapper, with its run_hunt pointed at a
throwaway copy of the real workflows carrying ONE defect for check 1, 2 or 3.
The message must name that check and that workflow, and no other check.
The premise of THIS test: the unmodified workflows pass the wrapper's premise,
and each mutated copy makes the real hunt exit 1 with exactly one FAIL line -
so a red here is the message, not a mutation that missed.
"""
import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_data_alarms_follow_up_fires_on_failure as base  # noqa: E402

TOOLS = os.path.dirname(os.path.abspath(__file__))
HUNT_NAME = "hunt_data_pipeline_alarms.py"

# No line ending in any anchor: a Windows checkout has some workflows CRLF.
RETRY = "          for attempt in 1 2 3; do"
# The refresh job's stand-down, not the conditions job's: same `if:`, so the
# step name is part of the anchor, joined by whatever ending the file has.
STAND_DOWN = "      - name: Stand the alarm down if this worked"
with open(os.path.join(base.WORKFLOWS, "refresh-data.yml"), encoding="utf-8",
          newline="") as _f:
    _eol = re.search(re.escape(STAND_DOWN) + r"(\r?\n)", _f.read())
STAND_DOWN_IF = STAND_DOWN + (_eol.group(1) if _eol else "\n") + (
    "        if: ${{ success() && !inputs.dry_run && "
    "steps.publish.outcome == 'success' }}")

# (label, file, old, new, the check that goes red)
CASES = [
    ("a publishing job's alarm no longer fires on failure", "height.yml",
     "        if: failure()", "        if: cancelled()", 1),
    ("a clobbered asset is followed by one bare push", "height.yml",
     RETRY, "          git push\n" + RETRY, 2),
    ("a dry run stands the real alarm down", "refresh-data.yml",
     STAND_DOWN_IF, STAND_DOWN + "\n        if: ${{ success() }}", 3),
]


class Premise(Exception):
    pass


def premise_message(file, old, new):
    """The wrapper's real premise() over the workflows with one line changed:
    the Premise message it raises, or None if it passed."""
    rc, out = base.run_hunt(file, old, new)
    fails = [l for l in out.splitlines() if l.startswith("FAIL")]
    if rc != 1 or len(fails) != 1:
        raise Premise("the mutation in %s should make the hunt exit 1 with "
                      "one FAIL line; got exit %d, %d FAIL line(s):\n%s"
                      % (file, rc, len(fails), out))
    real = base.run_hunt
    base.run_hunt = lambda *a, **k: real(file, old, new)
    try:
        base.premise()
    except base.Premise as e:
        return str(e)
    finally:
        base.run_hunt = real
    return None


def check(label, file, old, new, n):
    msg = premise_message(file, old, new)
    if msg is None:
        return "%s: the hunt is red on check %d, but the premise passed" \
               % (label, n)
    others = [m for m in range(1, 5)
              if m != n and re.search(r"\bcheck %d\b" % m, msg)]
    if not re.search(r"\bcheck %d\b" % n, msg) or file not in msg or others:
        return "%s: the premise must name check %d and %s (and no other " \
               "check); it said:\n%s" % (label, n, file, msg)
    # The headline, not just a dump below it: "should be green with three
    # soft steps told" points the owner at check 4 while check N is red.
    head = msg.splitlines()[0]
    if not re.search(r"\bcheck %d\b" % n, head) or "soft step" in head:
        return "%s: the premise's first line must name check %d, not the " \
               "soft steps; it said:\n%s" % (label, n, head)
    return None


def wrappers():
    """The tools/test_*.py files that run the hunt: by its path, or through
    the wrapper that does."""
    found = []
    for name in sorted(os.listdir(TOOLS)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        with open(os.path.join(TOOLS, name), encoding="utf-8") as f:
            text = f.read()
        if re.search(r"""["']%s["']""" % re.escape(HUNT_NAME), text) or \
                re.search(r"^import test_data_alarms_follow_up_fires_on_"
                          r"failure\b", text, re.M):
            found.append(name)
    return found


def docstring_wrong():
    with open(os.path.join(TOOLS, HUNT_NAME), encoding="utf-8") as f:
        doc = ast.get_docstring(ast.parse(f.read())) or ""
    runs = wrappers()
    if not runs:
        raise Premise("no tools/test_*.py runs %s; expected the "
                      "follow-up wrappers" % HUNT_NAME)
    flat = " ".join(doc.split())
    wrong = []
    if re.search(r"glob (does not|leaves it|never) ", flat) \
            or "Rename it in the fix" in flat:
        wrong.append("the docstring still says the tools/test_*.py glob "
                     "leaves the hunt alone")
    unnamed = [w for w in runs if w not in flat]
    if unnamed:
        wrong.append("the docstring does not name %s, which run(s) it from "
                     "that glob" % ", ".join(unnamed))
    return wrong


def test_premise():
    base.premise()


def test_premise_names_the_red_check():
    base.premise()
    wrong = [w for w in (check(*c) for c in CASES) if w]
    assert not wrong, "\n".join(wrong)


def test_docstring_says_who_runs_it():
    wrong = docstring_wrong()
    assert not wrong, "\n".join(wrong)


def main():
    try:
        base.premise()
    except base.Premise as e:
        print("PREMISE  %s" % e)
        return 3
    bad = 0
    for c in CASES:
        try:
            wrong = check(*c)
        except (Premise, base.Premise) as e:
            print("PREMISE  %s" % e)
            return 3
        print("%-4s %s" % ("FAIL" if wrong else "ok", wrong or c[0]))
        bad += bool(wrong)
    try:
        wrong = docstring_wrong()
    except Premise as e:
        print("PREMISE  %s" % e)
        return 3
    for w in wrong:
        print("FAIL %s" % w)
    print("%-4s the hunt's docstring says who runs it"
          % ("FAIL" if wrong else "ok"))
    bad += bool(wrong)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

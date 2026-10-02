#!/usr/bin/env python3
"""A soft step's follow-up alarm must run when that step FAILS.

    python tools/test_data_alarms_follow_up_fires_on_failure.py

Exit 0: every case below holds. Exit 1: at least one does not. Exit 3: the
harness could not reach a verdict (PREMISE), which is not a pass.

THE DEFECT. tools/hunt_data_pipeline_alarms.py check 4 holds every
`continue-on-error: true` step to a later step that raises an issue when it
fails. It accepted any follow-up whose `if:` merely MENTIONED
`steps.<id>.outcome`, so `if: steps.cutage.outcome == 'cancelled'` passed:
the alarm step then never runs on the failure it exists to report, the soft
step goes orange, the run stays green, and nobody is told.

WHAT IS RUN. The real hunt, as a subprocess, against a throwaway copy of the
real workflow files with ONE `if:` rewritten (HUNT_WORKFLOWS points it there).
The premise is the unmodified copy: green, with all three soft steps seen
and each one told - so a mutation going red is the guard, not a harness that
reds on everything. That premise needs ALL FOUR hunt checks green, and this
file runs from refresh-data.yml's tools/test_*.py loop: when checks 1-3 go
red on the real workflows, lane publishing stops here, and the PREMISE
message names the red check and quotes its FAIL line so the owner fixes the
workflow and not this file.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUNT = os.path.join(ROOT, "tools", "hunt_data_pipeline_alarms.py")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")

TRO = "traffic-orders.yml"
TRO_IF = "if: steps.cutage.outcome == 'failure'"
TRO_STEP = "Say so when the extract has stopped moving"
TRIPS = "refresh-data.yml"
TRIPS_IF = "if: steps.trips.outcome == 'failure'"
TRIPS_STEP = "Build the ready-made trips"


class Premise(Exception):
    pass


def run_hunt(file=None, old=None, new=None):
    """(exit code, output) of the hunt over the workflows, one line changed."""
    tmp = tempfile.mkdtemp(prefix="hunt_alarms_")
    try:
        wf = os.path.join(tmp, "workflows")
        shutil.copytree(WORKFLOWS, wf)
        if file:
            path = os.path.join(wf, file)
            with open(path, encoding="utf-8", newline="") as f:
                text = f.read()
            if text.count(old) != 1:
                raise Premise("%s has %d copies of %r, not one"
                              % (file, text.count(old), old))
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text.replace(old, new))
        env = dict(os.environ, HUNT_WORKFLOWS=wf)
        p = subprocess.run([sys.executable, HUNT], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
        return p.returncode, p.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def premise():
    """The hunt is green on the unmodified workflows, three soft steps told.

    A red premise stops lane publishing, so its message says WHICH hunt
    check is red and quotes that FAIL line, or the owner looks in the wrong
    place (this file, or check 4's soft steps).
    """
    rc, out = run_hunt()
    told = [l for l in out.splitlines()
            if "continue-on-error; issue on failure: yes" in l]
    if rc == 0 and len(told) == 3:
        return
    fails = [l for l in out.splitlines() if l.startswith("FAIL")]
    if fails:
        checks = sorted(set(re.findall(r"^FAIL\s+check (\d+):", out, re.M)),
                        key=int)
        raise Premise(
            "tools/hunt_data_pipeline_alarms.py is red on the UNMODIFIED "
            "workflows at %s (exit %d), before this test changed a line: a "
            "real alarm defect in .github/workflows, to be fixed there. No "
            "mutation can be judged until the hunt is green again.\n%s"
            % (" and ".join("check %s" % c for c in checks)
               or "a check it did not number", rc, "\n".join(fails)))
    raise Premise("the unmodified workflows should be green with three "
                  "soft steps told (hunt check 4); got exit %d, %d told:\n%s"
                  % (rc, len(told), out))


# (label, file, old, new, step the FAIL must name or None for "stays green")
CASES = [
    ("cancelled is not a failure", TRO, TRO_IF,
     "if: steps.cutage.outcome == 'cancelled'", TRO_STEP),
    ("success is the opposite", TRO, TRO_IF,
     "if: steps.cutage.outcome == 'success'", TRO_STEP),
    ("skipped never fires on a failure", TRIPS, TRIPS_IF,
     "if: steps.trips.outcome == 'skipped'", TRIPS_STEP),
    ("negated", TRO, TRO_IF,
     "if: ${{ !(steps.cutage.outcome == 'failure') }}", TRO_STEP),
    ("conclusion is 'success' under continue-on-error", TRO, TRO_IF,
     "if: steps.cutage.conclusion == 'failure'", TRO_STEP),
    ("another step's outcome", TRO, TRO_IF,
     "if: steps.cutag.outcome == 'failure'", TRO_STEP),
    # Must stay green: these do run when the step fails.
    ("wrapped in ${{ }}", TRO, TRO_IF,
     "if: ${{ steps.cutage.outcome == 'failure' }}", None),
    ("!= 'success' fires on failure", TRO, TRO_IF,
     "if: steps.cutage.outcome != 'success'", None),
    ("case and spacing", TRIPS, TRIPS_IF,
     "if: always() && steps.trips.outcome=='FAILURE'", None),
]


def check(label, file, old, new, step):
    rc, out = run_hunt(file, old, new)
    fails = [l for l in out.splitlines() if l.startswith("FAIL")]
    if step is None:
        if rc != 0:
            return "%s: `%s` runs on a failure, but the hunt went red " \
                   "(exit %d):\n%s" % (label, new, rc, "\n".join(fails))
        return None
    named = [l for l in fails if file in l and step in l]
    if rc != 1 or not named or len(fails) != 1:
        return "%s: `%s` never runs when `%s` fails, and the hunt said " \
               "exit %d with %d FAIL line(s) naming it (of %d)" \
               % (label, new, step, rc, len(named), len(fails))
    return None


def test_premise():
    premise()


def test_follow_up_must_fire_on_failure():
    premise()
    wrong = [w for w in (check(*c) for c in CASES) if w]
    assert not wrong, "\n".join(wrong)


def main():
    try:
        premise()
    except Premise as e:
        print("PREMISE  %s" % e)
        return 3
    bad = 0
    for c in CASES:
        try:
            wrong = check(*c)
        except Premise as e:
            print("PREMISE  %s" % e)
            return 3
        print("%-4s %s" % ("FAIL" if wrong else "ok", wrong or c[0]))
        bad += bool(wrong)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

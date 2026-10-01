#!/usr/bin/env python3
"""Does the traffic-order workflow RUN the council checks it depends on?

    python tools/test_the_council_gate_is_wired_in.py

RED on the tree as of 2026-10-01. build_tro.py now reads
tools/tro_authorities.csv and writes the `authorities` block the lane sheet
names the council from, and check_build.py has two gates for it
(check_council_table, check_council_coverage). But:

  * .github/workflows/traffic-orders.yml - the ONLY workflow that builds the
    order pack - runs none of the council test files before "Build the pack",
    and never runs check_build.py, so check_council_coverage prints "THIS GATE
    DID NOT RUN" everywhere (refresh-data.yml builds no tro index);
  * tools/tro_authorities.csv is not tracked by git. Pushed without it, every
    traffic-orders run exits "REFUSING TO PUBLISH: the council table could not
    be read" and the order pack stops updating.

This file never calls the gates. It reads the shipped workflow and asks git,
as test_the_conditions_pipeline_is_wired_in.py does for the conditions.
Each scanner is also run over an empty workflow and must complain: zero
subjects is BLIND, not a pass.
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "traffic-orders.yml")

COUNCIL_TESTS = [
    "tools/test_tro_authorities.py",
    "tools/test_tro_council_table.py",
    "tools/test_tro_council_wiring.py",
]

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def runs(text):
    """Every `run:` command line in the workflow, continuation lines joined."""
    lines = []
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    in_run = False
    indent = 0
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = re.match(r"^(\s*)(- )?run:\s*(.*)$", line)
        if m:
            body = m.group(3).strip()
            if body in ("|", ">", "|-", ">-"):
                in_run = True
                indent = len(m.group(1)) + (2 if m.group(2) else 0)
            else:
                in_run = False
                lines.append(body)
            continue
        if in_run:
            if stripped and (len(line) - len(line.lstrip())) <= indent:
                in_run = False
            elif stripped:
                lines.append(stripped)
    return lines


def problems_in(text):
    found = []
    commands = runs(text)
    build_at = next((i for i, c in enumerate(commands)
                     if "tools/build_tro.py" in c and "--key" in c), None)
    if build_at is None:
        found.append("no `python tools/build_tro.py --key ...` step")
        build_at = len(commands)
    for t in COUNCIL_TESTS:
        at = next((i for i, c in enumerate(commands)
                   if re.search(r"python3?\s+" + re.escape(t), c)), None)
        if at is None:
            found.append("%s is never run" % t)
        elif at > build_at:
            found.append("%s runs only after the pack is built" % t)
    gate = [i for i, c in enumerate(commands)
            if "tools/check_build.py" in c and "--closures-new" in c]
    if not gate:
        found.append("check_build.py --closures-new is never run, so "
                     "check_council_coverage never runs on a built index")
    elif gate[0] < build_at:
        found.append("check_build.py --closures-new runs before the build")
    return found


def tracked(path):
    try:
        out = subprocess.run(["git", "ls-files", "--error-unmatch", path],
                             cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    except OSError as e:
        return None, str(e)
    return out.returncode == 0, out.stderr.decode("utf-8", "replace").strip()


def main():
    with open(WORKFLOW, encoding="utf-8") as fh:
        text = fh.read()

    # BLIND guard: the scanner must complain about a workflow with nothing in.
    empty = problems_in("name: x\non: push\njobs: {}\n")
    check("PREMISE: an empty workflow is reported", len(empty) >= 4, empty)
    commands = runs(text)
    check("PREMISE: the shipped workflow has run steps", len(commands) > 10,
          len(commands))
    check("PREMISE: the scanner sees the existing order tests",
          any("tools/test_build_tro.py" in c for c in commands))

    for p in problems_in(text):
        check("traffic-orders.yml", False, p)

    ok, why = tracked("tools/tro_authorities.csv")
    check("tools/tro_authorities.csv is tracked by git (build_tro.py "
          "refuses to publish without it)", ok is True, why)

    print("%d passed, %d failed" % (_passed, len(_failed)))
    for f in _failed:
        print("  FAIL", f)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()

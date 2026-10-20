#!/usr/bin/env python3
"""Does the golden build run on every pull request, and on pushes to main?

    python tools/test_golden_runs_on_every_change.py

tools/golden.py proves that identical inputs give identical bytes; two clock
bugs shipped before it existed (see its header). It used to run only inside
the scheduled lane refresh, so a pull request that broke it went green and the
break showed up after the merge as a stopped lane refresh. golden.yml runs it
on every pull request to main and on pushes to main that touch tools/.

Parsed as YAML, not grepped: a job with no runner, a renamed `jobs:` or a
`types:` filter on pull_request must turn this red, and a text search does
not see any of them.
"""
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, ".github", "workflows", "golden.yml")
CHECKOUT = re.compile(r"^actions/checkout@[0-9a-f]{40}$")
SETUP_PY = re.compile(r"^actions/setup-python@[0-9a-f]{40}$")

#: The run steps after checkout and setup-python, in order and exactly.
RUNS = ["pip install --quiet cryptography Pillow pyyaml",
        "python tools/golden.py",
        "python tools/golden.py --mutate"]

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def load(path):
    with open(path, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    # YAML 1.1 reads a bare `on` key as True.
    if isinstance(wf, dict) and True in wf and "on" not in wf:
        wf["on"] = wf.pop(True)
    return wf


def main():
    try:
        wf = load(PATH)
    except (OSError, yaml.YAMLError) as exc:
        print("FAIL golden.yml cannot be read: %s" % exc)
        return 1
    check("it is named", wf.get("name") == "Golden build", wf.get("name"))
    on = wf.get("on") or {}
    check("triggers: pull requests, pushes, on demand",
          isinstance(on, dict) and set(on) == {"pull_request", "push",
                                               "workflow_dispatch"}, on)
    # Exactly branches: [main] - a `types:` filter would skip some pull
    # request events and a `paths:` filter would skip whole pull requests,
    # leaving a required check pending for ever.
    check("every pull request to main, unfiltered",
          on.get("pull_request") == {"branches": ["main"]},
          on.get("pull_request"))
    push = on.get("push") or {}
    check("pushes to main", push.get("branches") == ["main"], push)
    check("pushes that touch the builders", "tools/**" in (
        push.get("paths") or []), push)
    check("push has no other filters", set(push) <= {"branches", "paths"},
          push)
    check("read-only token", wf.get("permissions") == {"contents": "read"},
          wf.get("permissions"))
    check("a newer push replaces an older run of the same ref",
          wf.get("concurrency") == {"group": "golden-${{ github.ref }}",
                                    "cancel-in-progress": True},
          wf.get("concurrency"))
    jobs = wf.get("jobs") or {}
    check("one job, golden", list(jobs) == ["golden"], list(jobs))
    job = jobs.get("golden") or {}
    check("it has a runner", job.get("runs-on") == "ubuntu-latest",
          job.get("runs-on"))
    check("it has a timeout", isinstance(job.get("timeout-minutes"), int)
          and 0 < job["timeout-minutes"] <= 30, job.get("timeout-minutes"))
    for key in ("if", "continue-on-error", "permissions", "env"):
        check("the job has no %s" % key, key not in job, job.get(key))
    steps = job.get("steps") or []
    check("five steps", len(steps) == 2 + len(RUNS), len(steps))
    steps += [{}] * max(0, 2 + len(RUNS) - len(steps))
    co, py = steps[0], steps[1]
    check("checkout first, pinned", CHECKOUT.match(str(co.get("uses"))), co)
    check("the checkout keeps no token",
          (co.get("with") or {}).get("persist-credentials") is False, co)
    check("then setup-python 3.12, pinned",
          SETUP_PY.match(str(py.get("uses"))) and (py.get("with") or {})
          .get("python-version") == "3.12", py)
    check("then exactly the install and both golden claims, in order",
          [s.get("run") for s in steps[2:]] == RUNS,
          [s.get("run") for s in steps[2:]])
    for step in steps:
        check("step %s cannot be skipped or fail quietly" % (
            step.get("name") or step.get("uses")),
            "if" not in step and "continue-on-error" not in step
            and "env" not in step, step)
    with open(PATH, encoding="utf-8") as fh:
        text = fh.read()
    check("no secrets", "secrets." not in text)
    for f in _failed:
        print("FAIL", f)
    print("%d passed, %d failed" % (_passed, len(_failed)))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

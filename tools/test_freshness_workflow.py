#!/usr/bin/env python3
"""Does freshness.yml run the freshness check, go red on stale data, and ring?

    python tools/test_freshness_workflow.py

Parsed as YAML, not grepped: a check that only looks for a string stays green
when the job loses its runner, its checkout or its exit code. The lane
refresh runs this by its tools/test_*.py glob, with pyyaml installed.
freshness.yml itself runs only tools/test_check_freshness.py, which needs the
standard library alone.
"""
import datetime
import os
import re
import shutil
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import check_freshness as F  # noqa: E402

PATH = os.path.join(ROOT, ".github", "workflows", "freshness.yml")
CHECKOUT = re.compile(r"^actions/checkout@[0-9a-f]{40}$")
SETUP_PY = re.compile(r"^actions/setup-python@[0-9a-f]{40}$")

#: The check's step, exactly. pipefail makes the step take the checker's exit
#: code rather than tee's; anything added between or after could swallow it.
CHECK_RUN = ("set -o pipefail\n"
             "python tools/check_freshness.py | tee /tmp/freshness.txt\n")

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


def run_step_with(run, checker_rc):
    """Run the step's own script in bash with a stand-in for python."""
    bash = shutil.which("bash")
    if not bash:
        return None
    stub = ("python() { echo 'STALE x: y'; return %d; }\n" % checker_rc)
    proc = subprocess.run([bash, "-e", "-c", stub + run],
                          capture_output=True, text=True)
    return proc.returncode


def main():
    try:
        wf = load(PATH)
    except (OSError, yaml.YAMLError) as exc:
        print("FAIL freshness.yml cannot be read: %s" % exc)
        return 1
    check("it is named", wf.get("name") == "Data freshness", wf.get("name"))
    on = wf.get("on") or {}
    check("triggers are the schedule and on demand only",
          isinstance(on, dict) and set(on) == {"schedule",
                                               "workflow_dispatch"}, on)
    crons = [c.get("cron") for c in (on.get("schedule") or [])
             if isinstance(c, dict)]
    try:
        gap = F.cadence(crons)
    except ValueError as exc:
        gap = None
        check("its cron reads", False, exc)
    check("it runs at least every six hours", gap is not None
          and gap <= datetime.timedelta(hours=6), (crons, gap))
    check("the checker reads the same cron", F.workflow_crons(PATH) == crons,
          (F.workflow_crons(PATH), crons))
    check("least privilege", wf.get("permissions") == {
        "contents": "read", "issues": "write"}, wf.get("permissions"))
    check("one run at a time, never cut short", wf.get("concurrency") == {
        "group": "freshness", "cancel-in-progress": False},
        wf.get("concurrency"))
    jobs = wf.get("jobs") or {}
    check("one job, freshness", list(jobs) == ["freshness"], list(jobs))
    job = jobs.get("freshness") or {}
    check("it has a runner", job.get("runs-on") == "ubuntu-latest",
          job.get("runs-on"))
    check("it has a timeout", isinstance(job.get("timeout-minutes"), int)
          and 0 < job["timeout-minutes"] <= 30, job.get("timeout-minutes"))
    for key in ("if", "continue-on-error", "permissions", "env"):
        check("the job has no %s" % key, key not in job, job.get(key))
    steps = job.get("steps") or []
    check("six steps", len(steps) == 6, len(steps))
    steps += [{}] * (6 - len(steps))
    co, py, tests, run, ring, calm = steps[:6]
    check("checkout first, pinned", CHECKOUT.match(str(co.get("uses"))),
          co)
    check("the checkout keeps no token",
          (co.get("with") or {}).get("persist-credentials") is False, co)
    check("then setup-python 3.12, pinned",
          SETUP_PY.match(str(py.get("uses"))) and (py.get("with") or {})
          .get("python-version") == "3.12", py)
    check("then the checker's tests", tests.get("run") ==
          "python tools/test_check_freshness.py", tests)
    check("then the check itself, exactly", run.get("run") == CHECK_RUN,
          run.get("run"))
    for step in (co, py, tests, run):
        check("%s has no if/continue-on-error" % step.get("name", step.get(
            "uses")), "if" not in step and "continue-on-error" not in step,
            step)
    for step in steps:
        check("no step may fail quietly: %s" % step.get("name"),
              "continue-on-error" not in step, step)
    rc_stale = run_step_with(run.get("run") or "", 1)
    rc_fresh = run_step_with(run.get("run") or "", 0)
    if rc_stale is not None:
        check("the step fails when the checker does", rc_stale != 0, rc_stale)
        check("and passes when it passes", rc_fresh == 0, rc_fresh)
    check("an issue is raised on failure", ring.get("if") == "failure()"
          and "gh issue create --label data-stale" in (ring.get("run") or ""),
          ring)
    check("one issue, not one per run",
          "--label data-stale --state open" in (ring.get("run") or "")
          and 'if [ -n "$EXISTING" ]' in (ring.get("run") or ""), ring)
    check("stood down on success", calm.get("if") == "success()"
          and "gh issue close" in (calm.get("run") or ""), calm)
    with open(PATH, encoding="utf-8") as fh:
        text = fh.read()
    check("no secrets", "secrets." not in text)
    for f in _failed:
        print("FAIL", f)
    print("%d passed, %d failed" % (_passed, len(_failed)))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

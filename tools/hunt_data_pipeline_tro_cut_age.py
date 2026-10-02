#!/usr/bin/env python3
"""The traffic orders can stop moving for weeks, and every run is green.

    python tools/hunt_data_pipeline_tro_cut_age.py

Exit 0: something in the traffic-order pipeline objects when the extract it
publishes has not moved for weeks. Exit 1: nothing does (the defect).

Named hunt_*, not test_*, so the lane refresh's tools/test_*.py glob does not
pick a red proof up and stop lane data publishing. Rename it in the fix.

THE LIVE STATE THIS WAS FOUND IN. tro/index.json says `"generated":
"2026-09-06"`, and has said it on every traffic-order commit since 16
September - the latest on 1 October, "Traffic orders: data cut 2026-09-06".
The job runs four times a day and has been green throughout, because
dtro_fetch.py takes whatever extract `/dtros/all` hands back and build_tro.py
stamps the pack with that extract's own date. When DfT stops re-cutting the
national extract, the pipeline faithfully republishes the same picture and
nothing compares that date with today:

  * check_build.py --orders-only checks the closure count, the council
    table and coverage - not the cut's age;
  * "Has anything changed?" sees an identical pack and publishes nothing,
    successfully;
  * the failure issue lists "serving a stale extract" as a cause, but only a
    FAILED run raises it, and a stale extract fails nothing;
  * tools/dtro_events.py, the live /events half the fetcher's own docstring
    says exists "for the deltas", is not run by any workflow.

A rider's phone does show the cut's age (order_age.dart), so the rider is
told the orders are weeks old. The owner is told nothing, and "1,124 orders
changed nationally in a single day" (traffic-orders.yml) is the scale of
what goes missing.

HOW IT IS SHOWN. The real gate, check_build.py --orders-only, is run over
the real published index with only its cut date moved: once to today (the
premise - the gate passes a healthy index), once to 40 days ago. The second
must be refused, or the workflow must carry a step that raises an issue
about the cut's age.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "tro", "index.json")
WORKFLOW = os.path.join(os.environ.get("HUNT_WORKFLOWS") or
                        os.path.join(ROOT, ".github", "workflows"),
                        "traffic-orders.yml")


def gate(index, cut, tmp, name):
    body = json.loads(json.dumps(index))
    body["generated"] = cut
    for pack in body.get("packs", []):
        pack["generated"] = cut
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(body, handle)
    run = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "check_build.py"),
         "--orders-only", "--closures-previous", path, "--closures-new", path],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True)
    return run.returncode, run.stdout


def workflow_has_age_alarm():
    """A step, other than the failure alarm, that raises an issue about the
    cut's date - the non-blocking way to fix this."""
    text = open(WORKFLOW, encoding="utf-8").read()
    steps = re.split(r"\n\s*- name: ", text)
    for step in steps:
        head = step.split("\n", 1)[0]
        if "if: failure()" in step:
            continue
        if "gh issue" in step and re.search(r"generated|cut", step) \
                and re.search(r"days|age|old", step):
            return head
    return None


def main():
    index = json.load(open(INDEX, encoding="utf-8"))
    today = datetime.datetime.now(datetime.timezone.utc).date()
    live = index.get("generated") or ""
    try:
        age = (today - datetime.date.fromisoformat(live[:10])).days
        print("live: tro/index.json cut %s, %d days before today (%s)"
              % (live, age, today))
    except ValueError:
        print("live: tro/index.json cut %r is not a date" % live)

    with tempfile.TemporaryDirectory(prefix="dp-tro-age-") as tmp:
        fresh_code, fresh_log = gate(index, today.isoformat(), tmp,
                                     "fresh.json")
        old = (today - datetime.timedelta(days=40)).isoformat()
        old_code, old_log = gate(index, old, tmp, "old.json")

    print("gate over the published index cut today:       exit %d"
          % fresh_code)
    print("gate over the published index cut 40 days ago: exit %d"
          % old_code)
    if fresh_code != 0:
        print(fresh_log)
        print("PREMISE FAILED: the gate refuses the published index even "
              "with today's cut, so its verdict on the old one says nothing")
        return 3

    alarm = workflow_has_age_alarm()
    if old_code != 0:
        print("ok    the gate refuses an extract 40 days old")
        return 0
    if alarm:
        print("ok    traffic-orders.yml raises an issue on the cut's age "
              "(step %r)" % alarm)
        return 0
    print("FAIL  an extract cut 40 days ago passes the order gate (\"OK to "
          "publish.\"), and no step in traffic-orders.yml raises an issue "
          "about the cut's age. A stalled D-TRO extract is published green, "
          "four times a day, for as long as it stays stalled.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

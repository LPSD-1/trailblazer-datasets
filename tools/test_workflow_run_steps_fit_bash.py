#!/usr/bin/env python3
"""Every workflow `run:` body stays under RUN_LIMIT bytes.

    python tools/test_workflow_run_steps_fit_bash.py

Exit 0: every step fits. Exit 1: a step is too long, or the check itself
cannot tell a long step from a short one. Exit 3: no workflow could be read
(PREMISE): a check that reads nothing must say so, never pass.

WHY. A `run:` body is handed to the shell whole. Run locally on Windows
(tools/run_workflow_locally.py) it goes through `bash -c`, whose command line
is limited to about 8 KB; the satellite Publish step had reached 7,115 bytes
when this was written (9 Oct 2026), so one more paragraph of comment would
have made it unrunnable there while CI stayed green. RUN_LIMIT leaves a
margin under that. A step that needs more belongs in a tools/*.py entry point.
"""
import glob
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_LIMIT = 7500


def too_long(workflow, limit=RUN_LIMIT):
    """[(job, step name, bytes)] for every run body over `limit` bytes."""
    out = []
    for job_id, job in ((workflow or {}).get("jobs") or {}).items():
        for i, step in enumerate((job or {}).get("steps") or []):
            body = (step or {}).get("run")
            if isinstance(body, str) and len(body.encode("utf-8")) > limit:
                out.append((job_id, step.get("name") or "step %d" % i,
                            len(body.encode("utf-8"))))
    return out


def main():
    problems = []
    # The check can fail: a body one byte over is caught, one at the limit
    # is not.
    over = {"jobs": {"j": {"steps": [
        {"name": "long", "run": "x" * (RUN_LIMIT + 1)},
        {"name": "fits", "run": "x" * RUN_LIMIT}]}}}
    if too_long(over) != [("j", "long", RUN_LIMIT + 1)]:
        problems.append("the check does not tell %d bytes from %d: %r"
                        % (RUN_LIMIT + 1, RUN_LIMIT, too_long(over)))
    paths = sorted(glob.glob(os.path.join(ROOT, ".github", "workflows",
                                          "*.yml")))
    read = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            workflow = yaml.safe_load(f)
        if not isinstance(workflow, dict) or not workflow.get("jobs"):
            problems.append("%s has no jobs to check" % os.path.basename(path))
            continue
        read += 1
        for job, name, size in too_long(workflow):
            problems.append("%s job %s, step %r: run body is %d bytes, over "
                            "%d; move its logic into a tools/*.py entry point"
                            % (os.path.basename(path), job, name, size,
                               RUN_LIMIT))
    if read == 0:
        print("PREMISE: no workflow could be read under .github/workflows")
        return 3
    for p in problems:
        print("FAIL: " + p)
    print("%d workflows read; %d problems" % (read, len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

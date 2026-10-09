#!/usr/bin/env python3
"""The hunt reads every way a workflow can write a job, a step and a run.

    python tools/test_data_alarms_parser_sweep.py

Exit 0: every case below holds. Exit 1: at least one does not. Exit 3: the
harness could not reach a verdict (PREMISE), which is not a pass.

THE DEFECT. tools/hunt_data_pipeline_alarms.py reads workflows as text. It
missed a job line with a trailing comment (`review:  # x`), so the job's
steps vanished from checks 1, 2 and 4. It started a step only on `- name:`
or `- uses:`, so the `continue-on-error` of a step written `- run:`, `- id:`,
`- if:` or `- env:` landed on the step before it. It kept the body of
`run: |` alone, so a `git push` under `run: |-`, `run: >`, `run: >-` or
`run: | # why` was never seen, and a job publishing that way was never held
to check 1.

WHAT IS RUN. The real hunt, as a subprocess, over the real workflows plus one
generated file: a publishing job for every pair of step-start form and run
style, each with a soft step and no alarm at all. Every one must be flagged
by check 1 (its push seen) and by check 4 (its soft step, by name). Beside
them, jobs that DO have a failure alarm, in every run style, must not be
flagged by check 1, so the sweep cannot pass by flagging everything.
"""
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_data_alarms_check4_scope as scope  # noqa: E402

FILE = "zz-sweep.yml"

# How a run body is written: (label, lines). {cmd} is the command; the
# first line follows `run:` and the rest sit 10 spaces in.
STYLES = [
    ("|", ["|", "{cmd}"]),
    ("|-", ["|-", "{cmd}"]),
    ("|+", ["|+", "{cmd}"]),
    (">", [">", "{cmd}"]),
    (">-", [">-", "{cmd}"]),
    (">+", [">+", "{cmd}"]),
    ("| # why", ["| # why", "{cmd}"]),
    (">- # why", [">- # why", "{cmd}"]),
    ("single line", ["{cmd}"]),
    ("single line # why", ["{cmd}  # why"]),
    ("double-quoted", ['"{cmd}"']),
    ("single-quoted", ["'{cmd}'"]),
    ("plain, continued", ["echo start &&", "{cmd}"]),
]

# How the soft step starts. {run} is the run key's value lines.
FORMS = [
    "name", "run", "id", "if", "env", "uses", "with", "shell",
    "working-directory", "timeout-minutes", "continue-on-error",
]

SOFT_VALUES = ["true", "true  # it may fail", "${{ true }}", "'true'",
               '"true"']


def run_lines(style, cmd, first_key="run"):
    """The run key's lines, at 8 spaces (the first one after `first_key`)."""
    head, *rest = [l.format(cmd=cmd) for l in style[1]]
    return ["%s: %s" % (first_key, head)] + ["  " + l for l in rest]


def step(form, name, style, cmd, soft):
    """One step's lines, the first starting `- `, the rest at 8 spaces."""
    keys = {
        "name": ["name: %s" % name],
        "id": ["id: s%s" % hashlib.sha1(name.encode()).hexdigest()[:8]],
        "if": ["if: always()"],
        "env": ["env:", "  X: y"],
        "uses": ["uses: ./.github/actions/x"],
        "with": ["with:", "  a: b"],
        "shell": ["shell: bash"],
        "working-directory": ["working-directory: ."],
        "timeout-minutes": ["timeout-minutes: 5"],
        "continue-on-error": ["continue-on-error: %s" % soft],
        "run": run_lines(style, cmd),
    }
    order = [form] + [k for k in ("name", "continue-on-error", "run")
                      if k != form]
    if form == "with":
        order.append("uses")
    lines = []
    for k in order:
        if k == "run" and form == "uses":
            continue  # a step has uses or run, not both
        lines += keys[k]
    out = ["      - " + lines[0]]
    out += ["        " + l for l in lines[1:]]
    return out


def sweep_text():
    """(text, expected job names, expected soft step names, alarmed jobs)."""
    out = ["name: sweep", "on:", "  workflow_dispatch:", "jobs:"]
    jobs, softs, alarmed = [], [], []
    n = 0
    for f in FORMS:
        for st in STYLES:
            n += 1
            job = "j%d" % n
            # No " #" in a name: YAML reads it as a comment.
            soft_name = "Soft %d %s %s" % (n, f, st[0].replace("#", "c"))
            out.append("  %s:%s" % (job, "  # job %d" % n if n % 2 else ""))
            out += ["    runs-on: ubuntu-latest", "    steps:",
                    "      - name: Push %d" % n]
            out += ["        " + l for l in run_lines(st, "git push")]
            out += step(f, soft_name, st, "echo hi",
                        SOFT_VALUES[n % len(SOFT_VALUES)])
            jobs.append(job)
            softs.append(soft_name)
    # Alarmed jobs: the push and a failure() issue, each in every style and
    # the alarm starting `- if:`. check 1 must stay quiet for these.
    for st in STYLES:
        n += 1
        job = "a%d" % n
        out.append("  %s:  # alarmed" % job)
        out += ["    runs-on: ubuntu-latest", "    steps:",
                "      - name: Push %d" % n]
        out += ["        " + l for l in run_lines(st, "git push")]
        out += ["      - if: failure()", "        name: Alarm %d" % n]
        out += ["        " + l for l in run_lines(st, "gh issue create")]
        alarmed.append(job)
    return "\n".join(out) + "\n", jobs, softs, alarmed


def sweep_wrong():
    text, jobs, softs, alarmed = sweep_text()
    rc, out = scope.hunt(extra_files={FILE: text})
    lines = out.splitlines()
    fails = [l for l in lines if l.startswith("FAIL")]
    wrong = []
    for j in jobs:
        if not any(l.startswith("FAIL  check 1: %s job `%s` " % (FILE, j))
                   for l in fails):
            wrong.append("check 1 missed publishing job %s" % j)
    for s in softs:
        if not any(l.startswith("FAIL  check 4: %s `%s` " % (FILE, s))
                   for l in fails):
            wrong.append("check 4 missed soft step %r" % s)
    for j in alarmed:
        if any(l.startswith("FAIL  check 1: %s job `%s` " % (FILE, j))
               for l in fails):
            wrong.append("check 1 flagged alarmed job %s" % j)
    if rc != 1:
        wrong.append("exit %d, not 1" % rc)
    return wrong, out


def run_text_wrong():
    """In-process: each style's run text is exactly the command, with no
    indicator, quote or comment left on it and nothing of it lost."""
    h = scope.load_hunt()
    text, jobs, _, _ = sweep_text()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, FILE)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        parsed = h.jobs_of(path)
    wrong = []
    for n, j in enumerate(jobs[:len(STYLES)]):
        style = STYLES[n]
        want = "echo start && git push" if style[0] == "plain, continued" \
            else "git push"
        run = parsed.get(j, [{}])[0].get("run", "")
        got = " ".join(l.strip() for l in run.split("\n") if l.strip())
        if got != want:
            wrong.append("style %r read as %r, not %r" % (style[0], got, want))
    return wrong


def test_every_form_and_style_is_read():
    wrong, out = sweep_wrong()
    wrong += run_text_wrong()
    assert not wrong, "\n".join(wrong[:20])


def main():
    try:
        wrong, out = sweep_wrong()
        wrong += run_text_wrong()
    except scope.base.Premise as e:
        print("PREMISE  %s" % e)
        return 3
    n = len(FORMS) * len(STYLES)
    if wrong:
        for w in wrong[:40]:
            print("FAIL %s" % w)
        print("... %d wrong in all" % len(wrong))
        return 1
    print("ok   %d publishing jobs and %d soft steps flagged, %d alarmed "
          "jobs left alone" % (n, n, len(STYLES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

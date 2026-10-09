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
A second file writes steps as YAML flow mappings (`- {name: x, ...}`), which
GitHub accepts: its soft steps must be flagged, and a step that is
`continue-on-error: false`, or soft and told, must not.
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


# Steps written as YAML flow mappings, which GitHub accepts. f1-f3 publish
# with no alarm; h1 and h2 are alarmed and every soft step there is told.
FLOW_FILE = "zz-flow.yml"
FLOW = r"""name: flow
on:
  workflow_dispatch:
jobs:
  f1:
    runs-on: ubuntu-latest
    steps:
      - name: Push f1
        run: git push
      - {name: Flow soft one, continue-on-error: true, run: echo hi}
  f2:
    runs-on: ubuntu-latest
    steps:
      - name: Push f2
        run: git push
      - {name: 'Flow soft, two',
         with: {a: b, c: [d, e]},   # why
         # a comment line
         run: echo it's here,
         continue-on-error: "true"}
  f3:
    runs-on: ubuntu-latest
    steps:
      - {name: Flow push, run: git push}
      - {name: Flow soft three, run: 'it''s, {x', continue-on-error: true}
      - {name: Flow soft four, run: "a \", {x", continue-on-error: true}
  h1:
    runs-on: ubuntu-latest
    steps:
      - name: Push h1
        run: git push
      - {name: Flow hard, continue-on-error: false, run: echo hi}
      - if: failure()
        run: gh issue create
  h2:
    runs-on: ubuntu-latest
    steps:
      - {name: Push h2, run: git push}
      - {id: soft5, name: Flow soft told, continue-on-error: true, run: echo hi}
      - {if: "steps.soft5.outcome == 'failure'", name: Tell, run: gh issue create}
      - {if: failure(), name: Alarm h2, run: gh issue create}
"""
FLOW_SOFT = ["Flow soft one", "Flow soft, two", "Flow soft three",
             "Flow soft four"]
FLOW_QUIET = ["Flow hard", "Flow soft told"]


def flow_wrong():
    """A soft flow-mapping step is held to check 4 and its push to check 1;
    `continue-on-error: false`, and a told soft step, are left alone."""
    rc, out = scope.hunt(extra_files={FLOW_FILE: FLOW})
    fails = [l for l in out.splitlines() if l.startswith("FAIL")]
    wrong = []
    for s in FLOW_SOFT:
        if not any(l.startswith("FAIL  check 4: %s `%s` " % (FLOW_FILE, s))
                   for l in fails):
            wrong.append("check 4 missed flow soft step %r" % s)
    for s in FLOW_QUIET:
        if any("`%s`" % s in l for l in fails):
            wrong.append("flow step %r flagged" % s)
    for j in ("f1", "f2", "f3"):
        if not any(l.startswith("FAIL  check 1: %s job `%s` " % (FLOW_FILE, j))
                   for l in fails):
            wrong.append("check 1 missed flow job %s" % j)
    for j in ("h1", "h2"):
        if any(l.startswith("FAIL  check 1: %s job `%s` " % (FLOW_FILE, j))
               for l in fails):
            wrong.append("check 1 flagged alarmed flow job %s" % j)
    if any("cannot read" in l for l in fails):
        wrong.append("a readable flow step called unreadable")
    if rc != 1:
        wrong.append("flow file: exit %d, not 1" % rc)
    if wrong:
        wrong.append(out)
    return wrong


def flow_fields_wrong():
    """In-process: each flow step's fields, exactly."""
    h = scope.load_hunt()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, FLOW_FILE)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(FLOW)
        parsed = h.jobs_of(path)
    want = {
        "f2": [None, dict(name="Flow soft, two", run="echo it's here",
                          soft=True, id="", uses="")],
        "f3": [dict(name="Flow push", run="git push", soft=False),
               dict(name="Flow soft three", soft=True),
               # Read after a quoted comma, brace and escaped quote.
               dict(name="Flow soft four", soft=True)],
        "h1": [None, dict(name="Flow hard", soft=False, run="echo hi"),
               None],
        "h2": [dict(name="Push h2", run="git push", soft=False),
               dict(id="soft5", name="Flow soft told", soft=True),
               dict(name="Tell", run="gh issue create",
                    **{"if": "steps.soft5.outcome == 'failure'"}),
               dict(name="Alarm h2", **{"if": "failure()"})],
    }
    wrong = []
    for job, steps in want.items():
        got = parsed.get(job, [])
        if len(got) != len(steps):
            wrong.append("job %s read as %d steps, not %d"
                         % (job, len(got), len(steps)))
            continue
        for n, fields in enumerate(steps):
            for k, v in (fields or {}).items():
                if got[n].get(k) != v:
                    wrong.append("%s step %d %s read as %r, not %r"
                                 % (job, n + 1, k, got[n].get(k), v))
    return wrong


# Forms the reader does not parse, each a soft `git push` (PyYAML reads
# continue-on-error: True in all four) in a job that is otherwise alarmed.
# The reader must fail closed: check 4 names the file and the line.
UNREAD_HEAD = """name: z
on:
  workflow_dispatch:
permissions:
  contents: write
jobs:
  ship:
    runs-on: ubuntu-latest
"""
UNREAD_ALARM = "      - if: failure()\n        run: gh issue create\n"
UNREAD = [
    ("a bare - with a flow mapping below it", 11,
     "    steps:\n      -\n"
     "        {name: Pub, continue-on-error: true, run: git push}\n"
     + UNREAD_ALARM),
    ("a flow sequence of steps", 9,
     "    steps: [\n"
     "      {name: Pub, continue-on-error: true, run: git push},\n"
     "      {if: failure(), run: gh issue create}\n    ]\n"),
    ("a complex key", 11,
     "    steps:\n      - name: Pub\n        ? continue-on-error\n"
     "        : true\n        run: git push\n" + UNREAD_ALARM),
    ("a flow sequence on the line below steps:", 10,
     "    steps:\n"
     "      [{name: Pub, continue-on-error: true, run: git push},\n"
     "       {if: failure(), run: gh issue create}]\n"),
    ("a JSON-style key with no space", 10,
     '    steps:\n      - {name: Pub, "continue-on-error":true, '
     "run: git push}\n" + UNREAD_ALARM),
]
# The same above the steps: at the job's key level and the top level.
# (label, job named in the FAIL, line, whole workflow text)
SOFT_FLOW = "{name: Pub, continue-on-error: true, run: git push}"
TOP = UNREAD_HEAD.split("jobs:\n")[0]  # lines 1-5; `jobs` is line 6
UNREAD_ABOVE = [
    ("a JSON-style steps key", "ship", 9,
     UNREAD_HEAD + '    "steps":[%s]\n' % SOFT_FLOW),
    ("a quoted steps key", "ship", 9,
     UNREAD_HEAD + "    'steps':\n      - " + SOFT_FLOW + "\n"
     + UNREAD_ALARM),
    ("a complex steps key", "ship", 9,
     UNREAD_HEAD + "    ? steps\n    : [%s]\n" % SOFT_FLOW),
    ("a job line indented less than the job's keys", "ship", 9,
     UNREAD_HEAD.replace("    runs-on", "      runs-on")
     + "    timeout-minutes: 5\n      steps:\n        - name: Pub\n"
     "          run: git push\n        - if: failure()\n"
     "          run: gh issue create\n"),
    ("a job written as one flow mapping", "ship", 7,
     TOP + "jobs:\n  ship: {runs-on: x, steps: [%s]}\n" % SOFT_FLOW),
    ("a JSON-style jobs key", "(workflow)", 6,
     TOP + '"jobs":{ship: {runs-on: x, steps: [%s]}}\n' % SOFT_FLOW),
    ("a quoted jobs key", "(workflow)", 6,
     TOP + "'jobs':\n" + UNREAD_HEAD.split("jobs:\n")[1]
     + "    steps:\n      - " + SOFT_FLOW + "\n" + UNREAD_ALARM),
    ("jobs as one flow mapping", "(workflow)", 6,
     TOP + "jobs: {ship: {runs-on: x, steps: [%s]}}\n" % SOFT_FLOW),
    ("a top-level complex key", "(workflow)", 6,
     TOP + "? jobs\n: {ship: {runs-on: x, steps: [%s]}}\n" % SOFT_FLOW),
    # A sequence at its key's indent is fine, but `jobs` must be a mapping.
    ("jobs as a sequence", "(workflow)", 7,
     TOP + "jobs:\n- ship:\n    runs-on: x\n    steps:\n    - %s\n"
     % SOFT_FLOW),
]


def unread_wrong():
    """Each unread form: exit 1, a check 4 FAIL naming z.yml, its line and
    a cause in brackets."""
    wrong = []
    cases = [(l, "ship", n, UNREAD_HEAD + b) for l, n, b in UNREAD]
    for label, job, line, text in cases + UNREAD_ABOVE:
        rc, out = scope.hunt(extra_files={"z.yml": text})
        want = "FAIL  check 4: z.yml job `%s` line %d: step the alarm " \
            "cannot read (" % (job, line)
        hit = [l for l in out.splitlines() if l.startswith(want)]
        if rc != 1 or not hit or "), so nobody" not in hit[0]:
            wrong.append("%s: exit %d, no %r\n%s" % (label, rc, want, out))
    return wrong


# Block sequences at their key's own indent, which GitHub accepts: read as
# steps, held to check 4 as usual, and never called unreadable.
SAME_FILE = "zz-same.yml"
SAME = """name: same
on:
- workflow_dispatch
jobs:
  s1:
    runs-on: ubuntu-latest
    needs:
    - build
    steps:
    - name: Push s1
      run: git push
    - name: Same soft
      continue-on-error: true
      run: git push
    - if: failure()
      run: gh issue create
  s2:
    runs-on: ubuntu-latest
    steps:
    - name: Push s2
      run: git push
    - name: Same hard
      run: echo hi
    - if: failure()
      run: gh issue create
"""


def same_indent_wrong():
    rc, out = scope.hunt(extra_files={SAME_FILE: SAME})
    fails = [l for l in out.splitlines() if l.startswith("FAIL")]
    mine = [l for l in fails if SAME_FILE in l]
    wrong = []
    if not any(l.startswith("FAIL  check 4: %s `Same soft` " % SAME_FILE)
               for l in mine):
        wrong.append("same-indent soft step not flagged by check 4")
    if any("cannot read" in l or "s2" in l or "Same hard" in l
           or "check 1" in l for l in mine):
        wrong.append("same-indent workflow flagged beyond `Same soft`")
    if len(mine) != 1 or rc != 1:
        wrong.append("same-indent: exit %d, %d FAILs, not 1 and 1"
                     % (rc, len(mine)))
    h = scope.load_hunt()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, SAME_FILE)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(SAME)
        names = [s["name"] for s in h.jobs_of(path).get("s2", [])]
    if names[:2] != ["Push s2", "Same hard"] or len(names) != 3:
        wrong.append("same-indent s2 read as %r" % names)
    if wrong:
        wrong.append(out)
    return wrong


def test_every_form_and_style_is_read():
    wrong, out = sweep_wrong()
    wrong += run_text_wrong()
    wrong += flow_wrong() + flow_fields_wrong() + unread_wrong()
    wrong += same_indent_wrong()
    assert not wrong, "\n".join(wrong[:20])


def main():
    try:
        wrong, out = sweep_wrong()
        wrong += run_text_wrong()
        wrong += flow_wrong() + flow_fields_wrong() + unread_wrong()
        wrong += same_indent_wrong()
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

#!/usr/bin/env python3
"""Hunt check 4 spares one reviewed job, byte for byte, and nothing else.

    python tools/test_data_alarms_check4_scope.py

Exit 0: every case below holds. Exit 1: at least one does not. Exit 3: the
harness could not reach a verdict (PREMISE), which is not a pass.

THE DEFECT. tools/hunt_data_pipeline_alarms.py check 4 exists because a
`continue-on-error` step in a job that PUBLISHES lets what riders download
age out with nobody told. independent-review.yml's `review` job publishes
nothing (it posts a status and a comment on the pull request) and its two
soft steps fail closed in the step after them, yet check 4 flagged them, so
the alarm suites exited 3 and "Run every tool suite" stopped lane refreshes.

THE RULE HELD HERE. Check 4 spares a job only when its (file, job) is on
NON_PUBLISHING_JOBS AND the job's text, with its workflow's top-level keys,
still hashes to the sha256 pinned there. Any edit re-arms check 4 until a
reviewer re-pins. Two rules were rejected, each by a measured escape: "jobs
that git push" (four workflows publish with `gh release upload` and push
only by coincidence), and a list of publishing verbs (19 ways round it were
found: `git -C . push`, `gh -R x release`, a push in a called script, ...).
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_data_alarms_follow_up_fires_on_failure as base  # noqa: E402

REVIEW = "independent-review.yml"
REVIEW_SOFT = ("Mint the ledger token", "Fetch the capture")
SETTLE = "python3 tools/review_ledger.py settle"
TRO_IF = base.TRO_IF

# A job nobody has reviewed: soft step, no follow-up. %s is its run body.
EXTRA = """name: extra
on:
  workflow_dispatch:
jobs:
  ship:
    runs-on: ubuntu-latest
    steps:
      - name: Soft
        continue-on-error: true
        run: |
          %s
"""

# A second job in independent-review.yml, with a soft step and no alarm.
SECOND_JOB = """
  second:
    runs-on: ubuntu-latest
    steps:
      - name: Second soft
        continue-on-error: true
        run: echo hi
"""


def load_hunt():
    spec = importlib.util.spec_from_file_location("hunt", base.HUNT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def real(name):
    with open(os.path.join(base.WORKFLOWS, name), encoding="utf-8",
              newline="") as f:
        return f.read().replace("\r\n", "\n")


def hunt(edit=None, extra=None, extra_files=None, append=None):
    """(exit, output) of the hunt over a copy of the real workflows.

    edit=(file, old, new) replaces one string; extra is a run body for a new
    unreviewed job; extra_files={name: text} adds files; append=(file, text)
    appends to one."""
    tmp = tempfile.mkdtemp(prefix="hunt_scope_")
    try:
        wf = os.path.join(tmp, "workflows")
        shutil.copytree(base.WORKFLOWS, wf)
        if edit:
            file, old, new = edit
            text = real(file)
            if text.count(old) != 1:
                raise base.Premise("%s has %d copies of %r, not one"
                                   % (file, text.count(old), old))
            with open(os.path.join(wf, file), "w", encoding="utf-8",
                      newline="\n") as f:
                f.write(text.replace(old, new))
        if append:
            file, more = append
            with open(os.path.join(wf, file), "w", encoding="utf-8",
                      newline="\n") as f:
                f.write(real(file).rstrip("\n") + "\n" + more)
        files = dict(extra_files or {})
        if extra is not None:
            files["zz-extra.yml"] = EXTRA % extra
        for name, text in files.items():
            with open(os.path.join(wf, name), "w", encoding="utf-8",
                      newline="\n") as f:
                f.write(text)
        env = dict(os.environ, HUNT_WORKFLOWS=wf)
        p = subprocess.run([sys.executable, base.HUNT], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
        return p.returncode, p.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check4(out, file, step):
    return [l for l in out.splitlines() if l.startswith("FAIL  check 4:")
            and "%s `%s`" % (file, step) in l]


def flagged(label, steps_by_file, **kw):
    """'' when the hunt exits 1 with a check-4 FAIL naming every step."""
    rc, out = hunt(**kw)
    missing = [(f, s) for f, steps in steps_by_file for s in steps
               if not check4(out, f, s)]
    if rc == 1 and not missing:
        return "", out
    return "%s: expected exit 1 flagging %s, got exit %d:\n%s" % (
        label, missing, rc, out), out


def spared_wrong():
    """The real review job: green, both soft steps read and spared."""
    rc, out = hunt()
    hits = [l for s in REVIEW_SOFT for l in check4(out, REVIEW, s)]
    spared = all(any(s in l and "pinned" in l and "does not publish" in l
                     for l in out.splitlines()) for s in REVIEW_SOFT)
    if rc == 0 and not hits and spared:
        return ""
    return "the real review job: exit %d, spared=%s\n%s" % (rc, spared, out)


def one_char_wrong():
    """Every one-character change in the pinned text un-spares the job."""
    h = load_hunt()
    text = real(REVIEW)
    if not h.is_spared(REVIEW, "review", text):
        return "the unmodified review job is not spared in-process"
    region = h.pinned_lines(text, "review")
    if not region or len(region) < 100:
        raise base.Premise("pinned region of %s is %d lines"
                           % (REVIEW, len(region or [])))
    lines = text.split("\n")
    missed = []
    for i in region:
        line = lines[i]
        for k in range(len(line) + 1):
            # One character changed (or added at the end of the line).
            ch = line[k] if k < len(line) else ""
            new = "x" if ch != "x" else "y"
            mut = lines[:i] + [line[:k] + new + line[k + 1:]] + lines[i + 1:]
            if h.is_spared(REVIEW, "review", "\n".join(mut)):
                missed.append("line %d col %d" % (i + 1, k + 1))
    # Lines outside the region are other jobs only: none here today.
    inside = set(region)
    outside = [i + 1 for i, l in enumerate(lines) if l.strip()
               and i not in inside]
    wrong = []
    if missed:
        wrong.append("%d one-character edits left the job spared, e.g. %s"
                     % (len(missed), ", ".join(missed[:5])))
    if outside:
        wrong.append("lines outside the pinned text: %s" % outside[:5])
    return "; ".join(wrong)


def edit_message_wrong():
    """An edited review job is flagged, and the hunt says to re-pin."""
    wrong, out = flagged(
        "a one-character edit to the review job",
        [(REVIEW, REVIEW_SOFT)],
        edit=(REVIEW, SETTLE, SETTLE + " "))
    if wrong:
        return wrong
    want = [l for l in out.splitlines() if l.startswith("FAIL  check 4:")
            and "NON_PUBLISHING_JOBS" in l and "re-pin" in l]
    if len(want) < len(REVIEW_SOFT):
        return "edited job flagged, but the FAIL does not say to re-pin:\n" \
            + out
    return ""


CASES = [
    # A publishing job's soft step with no alarm is still flagged.
    ("a pushing job's soft step without its follow-up",
     [("traffic-orders.yml", ["Say so when the extract has stopped moving"])],
     dict(edit=("traffic-orders.yml", TRO_IF, "if: always()"))),
    # Publishes by release upload alone, with no git push anywhere.
    ("a release-only job", [("zz-extra.yml", ["Soft"])],
     dict(extra="gh release upload v1 x.pmtiles --clobber")),
    # Publishes nothing, but nobody has reviewed it: flagged until listed.
    ("an unlisted job that publishes nothing", [("zz-extra.yml", ["Soft"])],
     dict(extra="echo hi")),
    # The pinned job gains a publish (any edit re-arms).
    ("the review job gains a git push", [(REVIEW, REVIEW_SOFT)],
     dict(edit=(REVIEW, SETTLE, "git -C . push\n          " + SETTLE))),
    # The same job, byte for byte, in a file with another name.
    ("the review workflow copied to another name",
     [("zz-review.yml", REVIEW_SOFT)],
     dict(extra_files={"zz-review.yml": real(REVIEW)})),
    ("... to a name it is a prefix of",
     [("independent-review.yml2.yml", REVIEW_SOFT)],
     dict(extra_files={"independent-review.yml2.yml": real(REVIEW)})),
    ("... to a name ending in it",
     [("x-independent-review.yml", REVIEW_SOFT)],
     dict(extra_files={"x-independent-review.yml": real(REVIEW)})),
    ("... to its prefix", [("independent-review.y.yml", REVIEW_SOFT)],
     dict(extra_files={"independent-review.y.yml": real(REVIEW)})),
    # A second job in the pinned file is not spared by the file's entry.
    ("a second job in independent-review.yml", [(REVIEW, ["Second soft"])],
     dict(append=(REVIEW, SECOND_JOB))),
    # A job named with the pinned job's name as a prefix.
    ("a job named reviewer in independent-review.yml",
     [(REVIEW, ["Reviewer soft"])],
     dict(append=(REVIEW, SECOND_JOB.replace("second:", "reviewer:")
                  .replace("Second soft", "Reviewer soft")))),
]


def second_job_leaves_review_spared():
    """A second job does not re-arm the review job: the pin is per job."""
    rc, out = hunt(append=(REVIEW, SECOND_JOB))
    hits = [l for s in REVIEW_SOFT for l in check4(out, REVIEW, s)]
    return "" if not hits else "a second job re-armed the review job:\n" + out


def all_wrong():
    wrong = []
    for label, fn in (("spared", spared_wrong),
                      ("one character", one_char_wrong),
                      ("re-pin message", edit_message_wrong),
                      ("per job", second_job_leaves_review_spared)):
        w = fn()
        print("%-4s %s" % ("FAIL" if w else "ok", w or label))
        if w:
            wrong.append(w)
    for label, steps, kw in CASES:
        w, _ = flagged(label, steps, **kw)
        print("%-4s %s" % ("FAIL" if w else "ok", w or label))
        if w:
            wrong.append(w)
    return wrong


def test_check4_scope():
    wrong = all_wrong()
    assert not wrong, "\n".join(wrong)


def main():
    try:
        return 1 if all_wrong() else 0
    except base.Premise as e:
        print("PREMISE  %s" % e)
        return 3
    except AttributeError as e:
        print("FAIL the hunt has no pin API: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

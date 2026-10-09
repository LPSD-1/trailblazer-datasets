#!/usr/bin/env python3
"""Hunt check 4 spares a reviewed non-publishing job, and nothing else.

    python tools/test_data_alarms_check4_scope.py

Exit 0: every case below holds. Exit 1: at least one does not. Exit 3: the
harness could not reach a verdict (PREMISE), which is not a pass.

THE DEFECT. tools/hunt_data_pipeline_alarms.py check 4 exists because a
`continue-on-error` step in a job that PUBLISHES lets what riders download
age out with nobody told. independent-review.yml's `review` job publishes
nothing (it posts a status and a comment on the pull request) and its two
soft steps fail closed in the step after them, yet check 4 flagged them, so
every alarm suite exited 3 and "Run every tool suite" stopped lane refreshes.

THE RULE HELD HERE. Check 4 skips only jobs on a reviewed list in the hunt
(NON_PUBLISHING_JOBS), and only while they still publish nothing. Scoping it
to "jobs that git push" was rejected: four workflows publish release assets
with `gh release upload`, and they push only by coincidence, so a future
release-only job would slip through unflagged. Each case below is the real
hunt run as a subprocess over a throwaway copy of the real workflows.
"""
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


def hunt(edit=None, extra=None):
    """(exit, output) of the hunt; edit=(file, old, new), extra=run body."""
    tmp = tempfile.mkdtemp(prefix="hunt_scope_")
    try:
        wf = os.path.join(tmp, "workflows")
        shutil.copytree(base.WORKFLOWS, wf)
        if edit:
            file, old, new = edit
            path = os.path.join(wf, file)
            with open(path, encoding="utf-8", newline="") as f:
                text = f.read()
            if text.count(old) != 1:
                raise base.Premise("%s has %d copies of %r, not one"
                                   % (file, text.count(old), old))
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text.replace(old, new))
        if extra is not None:
            with open(os.path.join(wf, "zz-extra.yml"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(EXTRA % extra)
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


def flagged(label, edit, extra, file, steps):
    """'' when the hunt exits 1 with a check-4 FAIL naming every step."""
    rc, out = hunt(edit, extra)
    missing = [s for s in steps if not check4(out, file, s)]
    if rc == 1 and not missing:
        return ""
    return "%s: expected exit 1 flagging %s, got exit %d:\n%s" % (
        label, missing or steps, rc, out)


def test_review_job_not_flagged():
    rc, out = hunt()
    hits = [l for s in REVIEW_SOFT for l in check4(out, REVIEW, s)]
    assert rc == 0 and not hits, "exit %d\n%s" % (rc, out)
    # Spared, not unseen: the hunt still reads both soft steps.
    for s in REVIEW_SOFT:
        assert any(s in l and "does not publish" in l
                   for l in out.splitlines()), out


CASES = [
    # A publishing job's soft step with no alarm is still flagged.
    ("a pushing job's soft step without its follow-up",
     ("traffic-orders.yml", TRO_IF, "if: always()"), None,
     "traffic-orders.yml", ["Say so when the extract has stopped moving"]),
    # Publishes by release upload alone, with no git push anywhere.
    ("a release-only job", None, "gh release upload v1 x.pmtiles --clobber",
     "zz-extra.yml", ["Soft"]),
    # Publishes nothing, but nobody has reviewed it: flagged until listed.
    ("an unlisted job that publishes nothing", None, "echo hi",
     "zz-extra.yml", ["Soft"]),
    # The listed job starts publishing: the exemption no longer holds.
    ("the review job gains a git push",
     (REVIEW, SETTLE, "git push origin HEAD:main\n          " + SETTLE),
     None, REVIEW, list(REVIEW_SOFT)),
    ("the review job gains a release upload",
     (REVIEW, SETTLE, "gh release upload v1 a.rd5\n          " + SETTLE),
     None, REVIEW, list(REVIEW_SOFT)),
    ("the review job gains a Pages deploy",
     (REVIEW, "      - name: Settle the ledger",
      "      - name: Deploy\n        uses: actions/deploy-pages@v4\n"
      "      - name: Settle the ledger"),
     None, REVIEW, list(REVIEW_SOFT)),
] + [
    # Every other way of publishing, each on its own, so none can be lost.
    ("the review job gains `%s`" % run.split("\n")[0],
     (REVIEW, SETTLE, run + "\n          " + SETTLE),
     None, REVIEW, list(REVIEW_SOFT))
    for run in ("gh release create v2 a.rd5", "gh release edit v1 --latest",
                "gh release delete v0 --yes",
                "gh api -X PUT repos/$REPO/contents/catalogue.json -f x=y")
] + [
    # A bare `- uses:` step, the form with no `- name:` line.
    ("the review job gains a bare `- uses: %s`" % uses,
     (REVIEW, "      - name: Settle the ledger",
      "      - uses: %s\n      - name: Settle the ledger" % uses),
     None, REVIEW, list(REVIEW_SOFT))
    for uses in ("actions/upload-pages-artifact@v3",
                 "peaceiris/actions-gh-pages@v4")
]


def test_publishing_jobs_still_flagged():
    wrong = [w for w in (flagged(*c) for c in CASES) if w]
    assert not wrong, "\n".join(wrong)


def main():
    bad = 0
    try:
        rc, out = hunt()
        hits = [l for s in REVIEW_SOFT for l in check4(out, REVIEW, s)]
        spared = all(any(s in l and "does not publish" in l
                         for l in out.splitlines()) for s in REVIEW_SOFT)
        ok = rc == 0 and not hits and spared
        print("%-4s the review job's soft steps are spared, and still read"
              % ("ok" if ok else "FAIL"))
        if not ok:
            print(out)
        bad += not ok
        for c in CASES:
            wrong = flagged(*c)
            print("%-4s %s" % ("FAIL" if wrong else "ok", wrong or c[0]))
            bad += bool(wrong)
    except base.Premise as e:
        print("PREMISE  %s" % e)
        return 3
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

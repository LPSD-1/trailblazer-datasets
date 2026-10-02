"""A traffic-order extract weeks old is said out loud, and still published.

    python tools/test_tro_cut_age_alarm.py

THE LIVE STATE THIS WAS FOUND IN (2 Oct 2026). tro/index.json said
`"generated": "2026-09-06"` on every traffic-order commit from 16 September to
1 October. dtro_fetch.py takes whatever /dtros/all serves, build_tro.py stamps
the pack with the extract's own date, and when DfT stopped re-cutting the
extract every run rebuilt the identical pack, published nothing and went
green. The failure alarm only rings on a FAILED run, so the owner was told
nothing for 26 days while about 1,124 orders a day changed nationally.

THE FIX, and what this pins:

  * check_build.order_cut_age / report_order_cut_age measure the cut's age
    against MAX_ORDER_CUT_AGE_DAYS, and `--orders-cut-age INDEX` prints it as
    key=value lines and exits 0 - it warns, it never refuses;
  * traffic-orders.yml's "Say so when the extract has stopped moving" step
    reads those lines and opens ONE issue under its own label, updates it
    while the cut stays old, and closes it when a fresh cut arrives.

The step is run for real: its `run:` block is cut out of the workflow and
executed by bash -eo pipefail with a stand-in `gh` that records its calls.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import check_build  # noqa: E402

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "traffic-orders.yml")
STEP = "Say so when the extract has stopped moving"
LABEL = "traffic-orders-stale"
TODAY = datetime.datetime.now(datetime.timezone.utc).date()


def steps_of(text):
    """(name, body) for each step of the build job, in order, as text."""
    out = []
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if line.startswith("      - ") and not line.startswith("       "):
            if current:
                out.append(current)
            name = stripped[2:]
            name = name[len("name: "):] if name.startswith("name: ") else name
            current = [name, []]
        elif current is not None:
            current[1].append(line)
    if current:
        out.append(current)
    return [(name, "\n".join(body)) for name, body in out]


def run_block(text, step):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "- name: %s" % step:
            indent = len(line) - len(line.lstrip())
            for j in range(i + 1, len(lines)):
                l = lines[j]
                ind = len(l) - len(l.lstrip())
                if l.strip() and ind <= indent:
                    break
                if l.strip() == "run: |":
                    body = []
                    for b in lines[j + 1:]:
                        if b.strip() and len(b) - len(b.lstrip()) <= ind:
                            break
                        body.append(b)
                    cut = min(len(b) - len(b.lstrip())
                              for b in body if b.strip())
                    return "\n".join(b[cut:] if b.strip() else ""
                                     for b in body).rstrip() + "\n"
            raise AssertionError("step %r has no run block" % step)
    raise AssertionError("PREMISE: no step %r in %s" % (step, WORKFLOW))


def find_bash():
    if os.name != "nt":
        return shutil.which("bash")
    # Not shutil.which on Windows: System32\bash.exe is WSL.
    try:
        exec_path = subprocess.run(["git", "--exec-path"], check=True,
                                   stdout=subprocess.PIPE,
                                   universal_newlines=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    git_root = os.path.normpath(os.path.join(exec_path, "..", "..", ".."))
    for candidate in (os.path.join(git_root, "bin", "bash.exe"),
                      os.path.join(git_root, "usr", "bin", "bash.exe")):
        if os.path.isfile(candidate):
            return candidate
    return None


BASH = find_bash()

FAKE_GH = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_GH_LOG"
if [ "$FAKE_GH_FAIL" = "1" ]; then
  echo "gh: HTTP 403" >&2
  exit 1
fi
if [ "$1 $2" = "issue list" ]; then
  [ -n "$FAKE_GH_OPEN" ] && printf '%s\n' $FAKE_GH_OPEN
fi
exit 0
'''


def index_cut(days_ago):
    cut = (TODAY - datetime.timedelta(days=days_ago)).isoformat()
    return {"generated": cut, "packs": [{"id": "gb-tro", "generated": cut,
                                         "features": 9000, "councils": 178,
                                         "councils_publishing": 60}]}


class TheCutAgeIsMeasured(unittest.TestCase):
    def test_age_in_days_from_the_index(self):
        self.assertEqual(check_build.order_cut_age(index_cut(40), TODAY), 40)
        self.assertEqual(check_build.order_cut_age(index_cut(0), TODAY), 0)

    def test_an_index_without_a_date_has_no_age(self):
        self.assertIsNone(check_build.order_cut_age({}, TODAY))
        self.assertIsNone(check_build.order_cut_age(None, TODAY))
        self.assertIsNone(
            check_build.order_cut_age({"generated": "soon"}, TODAY))

    def test_the_boundary(self):
        limit = check_build.MAX_ORDER_CUT_AGE_DAYS
        self.assertGreater(limit, 0, "PREMISE: a limit is set")
        _, at = check_build.report_order_cut_age(index_cut(limit), TODAY)
        _, over = check_build.report_order_cut_age(index_cut(limit + 1),
                                                   TODAY)
        self.assertFalse(at, "a cut exactly at the limit is not stale")
        self.assertTrue(over, "a cut a day over the limit is stale")

    def test_no_date_reads_as_stale_not_fresh(self):
        lines, stale = check_build.report_order_cut_age({}, TODAY)
        self.assertTrue(stale)
        self.assertIn("stale=true", lines)


class TheCommandWarnsAndNeverRefuses(unittest.TestCase):
    def cli(self, index):
        with tempfile.TemporaryDirectory(prefix="tro-age-") as tmp:
            path = os.path.join(tmp, "index.json")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(index, fh)
            run = subprocess.run(
                [sys.executable, os.path.join(HERE, "check_build.py"),
                 "--orders-cut-age", path], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True)
        return run.returncode, run.stdout

    def test_forty_days_old(self):
        code, out = self.cli(index_cut(40))
        self.assertEqual(code, 0, out)
        self.assertIn("age=40", out.splitlines())
        self.assertIn("stale=true", out.splitlines())

    def test_today(self):
        code, out = self.cli(index_cut(0))
        self.assertEqual(code, 0, out)
        self.assertIn("stale=false", out.splitlines())


class TheWorkflowStep(unittest.TestCase):
    """The real step text, run under bash -eo pipefail."""

    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.text = fh.read()

    def setUp(self):
        if not BASH:
            self.skipTest("BLIND: bash is needed to run the workflow step")
        self.tmp = tempfile.mkdtemp(prefix="tro-age-step-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.write(os.path.join(self.bin, "gh"), FAKE_GH)
        self.write(os.path.join(self.bin, "python"),
                   '#!/usr/bin/env bash\nexec "%s" "$@"\n'
                   % sys.executable.replace("\\", "/"))
        for name in ("gh", "python"):
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.work = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.work, "tools"))
        os.makedirs(os.path.join(self.work, "tro"))
        shutil.copy(os.path.join(HERE, "check_build.py"),
                    os.path.join(self.work, "tools", "check_build.py"))
        self.log = os.path.join(self.tmp, "gh.log")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def write(path, text):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def run_step(self, index, open_issues="", gh_fails=False):
        if index is not None:
            self.write(os.path.join(self.work, "tro", "index.json"),
                       json.dumps(index))
        script = os.path.join(self.tmp, "step.sh")
        self.write(script, run_block(self.text, STEP))
        env = dict(os.environ)
        sep = ";" if os.name == "nt" else ":"
        env.update({
            "PATH": self.bin + sep + env.get("PATH", ""),
            "FAKE_GH_LOG": self.log.replace("\\", "/"),
            "FAKE_GH_OPEN": open_issues,
            "FAKE_GH_FAIL": "1" if gh_fails else "0",
            "GH_TOKEN": "x",
            "RUN_URL": "https://example.invalid/runs/1",
        })
        run = subprocess.run([BASH, "--noprofile", "--norc", "-eo",
                              "pipefail", script], cwd=self.work, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True)
        calls = []
        if os.path.exists(self.log):
            with open(self.log, encoding="utf-8") as fh:
                calls = [c for c in fh.read().splitlines() if c]
        return run.returncode, run.stdout, calls

    @staticmethod
    def calls_of(calls, verb):
        return [c for c in calls if c.startswith("issue %s" % verb)]

    def test_premise_the_fake_gh_is_the_one_called(self):
        code, out, calls = self.run_step(index_cut(0))
        self.assertEqual(code, 0, out)
        self.assertTrue(self.calls_of(calls, "list"),
                        "PREMISE: the step never reached the stand-in gh, so "
                        "nothing below says anything:\n" + out)

    def test_an_extract_forty_days_old_opens_one_issue(self):
        code, out, calls = self.run_step(index_cut(40))
        self.assertEqual(code, 0, out)
        created = self.calls_of(calls, "create")
        self.assertEqual(len(created), 1, calls)
        self.assertIn("--label %s" % LABEL, created[0])
        self.assertIn("40 days", created[0])
        self.assertIn("::warning::", out)

    def test_an_open_issue_is_updated_not_duplicated(self):
        code, out, calls = self.run_step(index_cut(40), open_issues="12")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "create"), [], calls)
        edited = self.calls_of(calls, "edit")
        self.assertEqual(len(edited), 1, calls)
        self.assertTrue(edited[0].startswith("issue edit 12 "), edited)

    def test_a_fresh_cut_closes_the_issue(self):
        code, out, calls = self.run_step(index_cut(1), open_issues="12 13")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "create"), [], calls)
        self.assertEqual(self.calls_of(calls, "edit"), [], calls)
        closed = self.calls_of(calls, "close")
        self.assertEqual([c.split()[2] for c in closed], ["12", "13"], calls)

    def test_a_fresh_cut_with_nothing_open_says_nothing(self):
        code, out, calls = self.run_step(index_cut(0))
        self.assertEqual(code, 0, out)
        self.assertEqual(
            [c for c in calls if not c.startswith("issue list")], [], calls)

    def test_no_readable_index_is_stale_not_fresh(self):
        code, out, calls = self.run_step(None, open_issues="12")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "close"), [],
                         "an unreadable index closed the alarm: %s" % calls)
        self.assertEqual(len(self.calls_of(calls, "edit")), 1, calls)

    def test_a_broken_gh_never_fails_the_job(self):
        code, out, calls = self.run_step(index_cut(40), gh_fails=True)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.calls_of(calls, "create"), calls)


class TheStepIsWiredWhereItRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.steps = steps_of(fh.read())
        cls.names = [n for n, _ in cls.steps]

    def body(self, name):
        self.assertIn(name, self.names, "PREMISE: step %r is missing" % name)
        return dict(self.steps)[name]

    def test_after_the_build_and_before_the_publish_decision(self):
        for name in ("Build the pack", STEP, "Has anything changed?"):
            self.assertIn(name, self.names, "PREMISE: %r" % name)
        self.assertLess(self.names.index("Build the pack"),
                        self.names.index(STEP))
        self.assertLess(self.names.index(STEP),
                        self.names.index("Has anything changed?"))

    def test_not_gated_on_publishing_and_cannot_fail_the_job(self):
        body = self.body(STEP)
        lines = [l.strip() for l in body.splitlines()]
        self.assertFalse([l for l in lines if l.startswith("if:")],
                         "a stalled extract is the run that publishes "
                         "nothing; this step must not depend on publishing")
        self.assertIn("continue-on-error: true", lines)

    def test_its_label_is_not_the_one_stood_down_on_success(self):
        body = self.body(STEP)
        self.assertIn("--label %s" % LABEL, body)
        stand_down = self.body("Stand the alarm down if this worked")
        self.assertIn("--label traffic-orders ", stand_down,
                      "PREMISE: the stand-down closes `traffic-orders`")
        self.assertNotIn("--label traffic-orders ", body)


if __name__ == "__main__":
    unittest.main()

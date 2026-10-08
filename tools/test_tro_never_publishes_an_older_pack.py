"""A traffic-orders run never publishes a pack older than the one riders have.

    python tools/test_tro_never_publishes_an_older_pack.py

THE DEFECT (8 October 2026). build_tro.py stops at its time budget and dates
the pack by how far the events feed got; the next run continues from its
cache. A run with no cache - the first on a new machine, run by
tools/run_workflow_locally.py - got from the 6 September extract to 9
September, and "Has anything changed?" saw a different index and published
it over the 8 October pack. Riders lost a month of orders until the next
GitHub run.

WHAT IS RUN. The real `run:` block of "Has anything changed?", read from the
workflow, under bash -eo pipefail, in a throwaway git repository holding a
committed tro/index.json (the published one) and a built one in the working
tree.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_workflow_locally as rl  # noqa: E402

STEP = "Has anything changed?"


def step_script(force=False):
    _path, wf = rl.load_workflow("traffic-orders")
    for step in wf["jobs"]["build"]["steps"]:
        if step.get("name") == STEP:
            ctx = rl.Context({"inputs": {"force": force}}, HERE)
            return ctx.substitute(step["run"])
    raise AssertionError("PREMISE: no step %r in traffic-orders.yml" % STEP)


class Decision(unittest.TestCase):

    def decide(self, published, built, force=False):
        with tempfile.TemporaryDirectory() as d:
            d = d.replace("\\", "/")
            tmp = os.path.join(d, "tmp").replace("\\", "/")
            repo = os.path.join(d, "repo")
            os.makedirs(tmp)
            os.makedirs(os.path.join(repo, "tro"))
            index = os.path.join(repo, "tro", "index.json")

            def write(path, generated, sha):
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump({"cut": "2026-09-06", "generated": generated,
                               "packs": [{"sha256": sha}]}, fh)

            git = lambda *a: subprocess.run(  # noqa: E731
                ["git", "-c", "user.name=t", "-c", "user.email=t@t"] +
                list(a), cwd=repo, check=True, capture_output=True)
            git("init", "-q")
            write(index, *published)
            git("add", ".")
            git("commit", "-qm", "published")
            # "Keep the published index to compare against".
            write(os.path.join(tmp, "tro-previous.json"), *published)
            write(index, *built)
            out = os.path.join(tmp, "out")
            open(out, "w").close()
            script = step_script(force).replace("/tmp", tmp)
            env = dict(os.environ, GITHUB_OUTPUT=out)
            r = subprocess.run([rl.find_bash(), "--noprofile", "--norc",
                                "-eo", "pipefail", "-c", script], cwd=repo,
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return rl.read_kv_file(out).get("publish"), r.stdout

    def test_an_older_pack_is_not_published(self):
        publish, said = self.decide(("2026-10-08", "a" * 64),
                                    ("2026-09-09", "b" * 64))
        self.assertEqual(publish, "false")
        self.assertIn("2026-09-09", said)
        self.assertIn("2026-10-08", said)

    def test_not_even_when_forced(self):
        publish, _ = self.decide(("2026-10-08", "a" * 64),
                                 ("2026-09-09", "b" * 64), force=True)
        self.assertEqual(publish, "false")

    def test_a_newer_pack_is_published(self):
        publish, _ = self.decide(("2026-10-07", "a" * 64),
                                 ("2026-10-08", "b" * 64))
        self.assertEqual(publish, "true")

    def test_the_same_day_with_new_orders_is_published(self):
        publish, _ = self.decide(("2026-10-08", "a" * 64),
                                 ("2026-10-08", "b" * 64))
        self.assertEqual(publish, "true")

    def test_an_identical_pack_is_not_published(self):
        publish, _ = self.decide(("2026-10-08", "a" * 64),
                                 ("2026-10-08", "a" * 64))
        self.assertEqual(publish, "false")


if __name__ == "__main__":
    unittest.main()

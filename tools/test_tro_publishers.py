#!/usr/bin/env python3
"""tro/publishers.json: written by the order build, committed by its workflow.

    python tools/test_tro_publishers.py

The public status page (tools/build_status.py) says how many records each
authority has published to D-TRO. The count is in the sealed order pack,
which the status job cannot open, so build_tro.py also writes the pack's
`authorities` rows to tro/publishers.json beside tro/index.json, and
traffic-orders.yml commits it with the index. Two halves, two ways to lose
it without a word:

  * the build stops writing it, or writes it somewhere the job never adds;
  * the commit step leaves it out - or adds it, and then a lost push race
    runs `git reset --hard origin/main`, which puts back main's copy, and
    the retry commits the index without it. That is the exact shape
    tools/test_tro_push_race_keeps_its_index.py records for the index.

WHAT IS RUN. The real build_tro.main over a two-record corpus with sealing
stubbed (as test_tro_authorities.py does), and the real `run:` block of
"Commit the index and the catalogue", cut out of the workflow, in a
throwaway clone of a throwaway bare remote - with the stand-in catalogue
script of test_tro_push_race_keeps_its_index.py.
"""
import csv
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

import test_tro_push_race_keeps_its_index as race  # noqa: E402

OLD = {"generated": "2026-10-01", "authorities": [
    {"swa": "2460", "name": "Leicestershire County Council", "records": 1,
     "newest": "2026-09-01"}]}
NEW = {"generated": "2026-10-08", "authorities": [
    {"swa": "2460", "name": "Leicestershire County Council", "records": 2,
     "newest": "2026-10-08"}]}


class TheBuildWritesIt(unittest.TestCase):
    """The real build_tro.main, sealing stubbed."""

    def test_beside_the_index_with_the_packs_authority_rows(self):
        import build_tro
        tmp = tempfile.mkdtemp(prefix="tro-publishers-")
        self.addCleanup(shutil.rmtree, tmp, True)
        corpus = os.path.join(tmp, "dtros_20260601_000000.csv")
        records = [build_tro._check_record(
            "miscRoadClosure", "THE DERBYSHIRE (HOOTON LANE) ORDER 2026")]
        with open(corpus, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Id", "SchemaVersion", "Data"])
            for i, record in enumerate(records):
                writer.writerow(["p-%d" % i, "3.4.0", json.dumps(record)])
        index_dir = os.path.join(tmp, "tro")
        checkout_copy = os.path.join(ROOT, "tro", "publishers.json")
        before = os.path.getmtime(checkout_copy) \
            if os.path.exists(checkout_copy) else None
        saved = (build_tro.pack, build_tro.load_key, sys.argv, sys.stdout)
        build_tro.pack = lambda body, key: body
        build_tro.load_key = lambda path: b"k" * 32
        sys.argv = ["build_tro.py", "--key", "unused", "--csv", corpus,
                    "--out", os.path.join(tmp, "out"),
                    "--index", os.path.join(index_dir, "index.json")]
        sys.stdout = open(os.devnull, "w")
        try:
            build_tro.main()
        finally:
            sys.stdout.close()
            build_tro.pack, build_tro.load_key, sys.argv, sys.stdout = saved
        with open(os.path.join(tmp, "out", "gb-tro.tbpack"), "rb") as fh:
            pack = json.loads(fh.read().decode("utf-8"))
        path = os.path.join(index_dir, "publishers.json")
        self.assertTrue(os.path.isfile(path),
                        "no publishers.json beside the index: the status "
                        "page says every D-TRO count is unknown")
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\r", raw)
        got = json.loads(raw.decode("utf-8"))
        self.assertEqual(got["generated"], pack["generated"])
        self.assertEqual(
            got["authorities"],
            [{"swa": a["swa"], "name": a["name"], "records": a["records"],
              "newest": a["newest"]} for a in pack["authorities"]])
        self.assertTrue([a for a in got["authorities"] if a["records"]],
                        "PREMISE: the corpus's council has records")
        after = os.path.getmtime(checkout_copy) \
            if os.path.exists(checkout_copy) else None
        self.assertEqual(before, after, "a build pointed at a temporary "
                         "index wrote into the checkout's tro/")


class TheWorkflowCommitsIt(unittest.TestCase):
    """The real commit step, run in a throwaway clone."""

    @classmethod
    def setUpClass(cls):
        with open(race.WORKFLOW, encoding="utf-8") as fh:
            cls.text = fh.read()

    def setUp(self):
        if not race.BASH or not shutil.which("git"):
            self.skipTest("BLIND: bash and git are needed to run the step")
        self.tmp = tempfile.mkdtemp(prefix="tro-publishers-commit-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def seed(self, with_publishers):
        remote = os.path.join(self.tmp, "remote.git")
        race.sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main",
                remote)
        seed = os.path.join(self.tmp, "seed")
        race.sh(self.tmp, "git", "init", "-q", "-b", "main", seed)
        race.write(os.path.join(seed, ".gitignore"), "dist/\n")
        race.write(os.path.join(seed, "tools", "rebuild_catalogue.sh"),
                   race.STUB_REBUILD)
        race.write(os.path.join(seed, "tools", "verify_catalogue.py"),
                   "import sys\nsys.exit(0)\n")
        race.write_json(os.path.join(seed, "tro", "index.json"),
                        race.tro_index(race.OLD))
        if with_publishers:
            race.write_json(os.path.join(seed, "tro", "publishers.json"),
                            OLD)
        race.write(os.path.join(seed, "published", "feed.json"), "one\n")
        race.write_json(os.path.join(seed, "manifest.json"), {})
        race.sh(seed, race.BASH, "tools/rebuild_catalogue.sh",
                "manifest.json", "catalogue.json")
        race.sh(seed, "git", "add", "-A")
        race.sh(seed, "git", "commit", "-q", "-m", "published")
        race.sh(seed, "git", "remote", "add", "origin", remote)
        race.sh(seed, "git", "push", "-q", "-u", "origin", "main")
        return remote

    def commit_step(self, race_lost, build_writes=True, seeded=True):
        remote = self.seed(seeded)
        job = os.path.join(self.tmp, "job")
        race.sh(self.tmp, "git", "clone", "-q", remote, job)
        # What the build left: the new index, and beside it the new counts.
        race.write_json(os.path.join(job, "tro", "index.json"),
                        race.tro_index(race.NEW))
        if build_writes:
            race.write_json(os.path.join(job, "tro", "publishers.json"), NEW)
        race.sh(job, race.BASH, "tools/rebuild_catalogue.sh",
                "manifest.json", "dist/catalogue.json")
        if race_lost:
            other = os.path.join(self.tmp, "other")
            race.sh(self.tmp, "git", "clone", "-q", remote, other)
            race.write(os.path.join(other, "published", "feed.json"),
                       "two\n")
            race.sh(other, race.BASH, "tools/rebuild_catalogue.sh",
                    "manifest.json", "catalogue.json")
            race.sh(other, "git", "add", "-A")
            race.sh(other, "git", "commit", "-q", "-m", "Conditions")
            race.sh(other, "git", "push", "-q")
        script = os.path.join(self.tmp, "step.sh")
        race.write(script, race.run_block(self.text, race.STEP))
        run = subprocess.run([race.BASH, "--noprofile", "--norc", "-eo",
                              "pipefail", script], cwd=job, env=race.ENV,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             universal_newlines=True)
        look = os.path.join(self.tmp, "look")
        race.sh(self.tmp, "git", "clone", "-q", remote, look)
        index = json.loads(race.sh(look, "git", "show",
                                   "HEAD:tro/index.json"))
        try:
            published = json.loads(race.sh(look, "git", "show",
                                           "HEAD:tro/publishers.json"))
        except RuntimeError:
            published = None
        return run.returncode, run.stdout, index, published

    def test_it_is_committed_with_the_index(self):
        code, log, index, published = self.commit_step(race_lost=False)
        self.assertEqual(code, 0, log)
        self.assertEqual(index["packs"][0]["sha256"], race.NEW, log)
        self.assertEqual(published, NEW,
                         "tro/publishers.json was not committed with the "
                         "index:\n" + log)

    def test_a_lost_push_race_still_commits_the_new_counts(self):
        code, log, index, published = self.commit_step(race_lost=True)
        self.assertIn("push rejected", log,
                      "PREMISE: the push was never rejected, so the retry "
                      "path did not run:\n" + log)
        self.assertEqual(code, 0, log)
        self.assertEqual(index["packs"][0]["sha256"], race.NEW, log)
        self.assertEqual(published, NEW,
                         "after the reset the retry committed main's old "
                         "counts (or none):\n" + log)

    def test_the_first_ever_counts_are_committed_through_a_race(self):
        # Before the first run main has no tro/publishers.json at all.
        code, log, _, published = self.commit_step(race_lost=True,
                                                   seeded=False)
        self.assertIn("push rejected", log, "PREMISE:\n" + log)
        self.assertEqual(code, 0, log)
        self.assertEqual(published, NEW, log)

    def test_without_the_file_the_orders_still_publish(self):
        code, log, index, published = self.commit_step(
            race_lost=True, build_writes=False, seeded=False)
        self.assertEqual(code, 0, "a missing publishers.json stopped the "
                         "order index publishing:\n" + log)
        self.assertEqual(index["packs"][0]["sha256"], race.NEW, log)
        self.assertIsNone(published, log)


if __name__ == "__main__":
    unittest.main()

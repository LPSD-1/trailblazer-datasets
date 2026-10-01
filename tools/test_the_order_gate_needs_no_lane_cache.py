#!/usr/bin/env python3
"""The traffic-order gate must not depend on the lane data.

    python tools/test_the_order_gate_needs_no_lane_cache.py

RED on the tree as of 2026-10-01. traffic-orders.yml gates the order pack
with check_build.py, and check_build.py also ran the LANE gates:
check_authorities (at least MIN_AUTHORITIES councils with data in cache/),
check_totals and check_declared_bounds on the committed manifest. cache/ is
gitignored and only refresh-data.yml writes it, so a rowmaps cache that was
evicted or half-written - or a lane-side fault in manifest.json - stopped
closures publishing, though nothing in the order pack had changed. A rider
then rides into a closure made this morning because the LANE fetch was short.

This file does not call check_build's functions. It lifts the shell of the
shipped "Check the built pack" step out of traffic-orders.yml and runs it, as
the runner would, in a scratch checkout holding a good tro/index.json and no
cache/ at all. Only /tmp/ and the path to check_build.py are rewritten.

It must still be able to refuse: the same step is also run over a collapsed
closure count, an index with no council block and no index at all, and each
must exit non-zero. A gate that passes everything would pass the first case
too.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "traffic-orders.yml")
CHECK = os.path.join(HERE, "check_build.py")
STEP = "Check the built pack"


def gate_step(text):
    """The `run: |` body of the gate step, dedented, or None."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*- name:\s*" + re.escape(STEP), line):
            break
    else:
        return None
    step_indent = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i + 1, len(lines)):
        line = lines[j]
        indent = len(line) - len(line.lstrip())
        if line.strip() and indent <= step_indent:
            return None  # the next step began before any run:
        one = re.match(r"^\s*run:\s*([^|>\s].*)$", line)
        if one:
            return one.group(1).strip() + "\n"
        m = re.match(r"^(\s*)run:\s*[|>]-?\s*$", line)
        if m:
            key = len(m.group(1))
            body = []
            for k in range(j + 1, len(lines)):
                b = lines[k]
                if b.strip() and len(b) - len(b.lstrip()) <= key:
                    break
                body.append(b)
            pad = min(len(b) - len(b.lstrip()) for b in body if b.strip())
            return "\n".join(b[pad:] for b in body).strip() + "\n"
    return None


def find_bash():
    # On Windows the PATH can hold WSL's bash, which cannot see this drive
    # the same way; Git's own bash is the one a developer runs CI shell with.
    for p in (r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.name == "nt" and os.path.isfile(p):
            return p
    return shutil.which("bash")


def posix(path):
    return path.replace("\\", "/")


GOOD = {"generated": "2026-10-01",
        "packs": [{"id": "gb-tro", "kind": "tro", "features": 36584,
                   "councils": 152, "councils_publishing": 61}]}
PUBLISHED = {"generated": "2026-09-30",
             "packs": [{"id": "gb-tro", "kind": "tro", "features": 35000}]}
# A committed lane manifest with a lane-side fault in it: a region in the
# Atlantic, which check_declared_bounds refuses. Nothing about orders.
BAD_LANES = {"regions": [{"id": "south-west",
                          "bounds": [-40.0, 50.0, -30.0, 51.0]}],
             "packages": []}


class TheShippedOrderGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.script = gate_step(fh.read())
        cls.bash = find_bash()

    def test_premise_the_step_was_found_and_runs_check_build(self):
        self.assertIsNotNone(self.script, "no `%s` step with a run: | body "
                             "in traffic-orders.yml" % STEP)
        self.assertIn("tools/check_build.py", self.script)
        self.assertIn("--closures-new", self.script)
        self.assertIsNotNone(self.bash, "no bash to run the step with")

    def run_step(self, new_index, manifest=None, previous=PUBLISHED):
        self.assertIsNotNone(self.script, "PREMISE: the gate step exists")
        self.assertIsNotNone(self.bash, "PREMISE: bash exists")
        tmp = tempfile.mkdtemp(prefix="order-gate-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "tro"))
        os.makedirs(os.path.join(tmp, "tmp"))
        if new_index is not None:
            with open(os.path.join(tmp, "tro", "index.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(new_index, fh)
        with open(os.path.join(tmp, "tmp", "tro-previous.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(previous, fh)
        if manifest is not None:
            with open(os.path.join(tmp, "manifest.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(manifest, fh)
        self.assertFalse(os.path.exists(os.path.join(tmp, "cache")),
                         "PREMISE: no lane cache in the scratch checkout")
        script = self.script.replace("/tmp/", posix(tmp) + "/tmp/")
        script = re.sub(r"\bpython3?\s+tools/check_build\.py",
                        lambda _: '"%s" "%s"' % (posix(sys.executable),
                                                 posix(CHECK)),
                        script)
        self.assertIn(posix(CHECK), script,
                      "PREMISE: the step's check_build.py call was rewritten")
        run = subprocess.run([self.bash, "-e", "-c", script], cwd=tmp,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return run.returncode, run.stdout.decode("utf-8", "replace")

    # ------------------------------------------------------- must publish

    def test_a_good_pack_publishes_with_no_lane_cache(self):
        code, out = self.run_step(GOOD)
        self.assertIn("live closures: 36584", out,
                      "PREMISE: the closure gate read the built index\n" + out)
        self.assertEqual(code, 0, out)

    def test_a_lane_fault_in_the_committed_manifest_does_not_stop_orders(self):
        code, out = self.run_step(GOOD, manifest=BAD_LANES)
        self.assertIn("live closures: 36584", out, out)
        self.assertEqual(code, 0, out)

    # -------------------------------------------------- must still refuse

    def test_a_collapsed_closure_count_is_still_refused(self):
        collapsed = json.loads(json.dumps(GOOD))
        collapsed["packs"][0]["features"] = 100
        code, out = self.run_step(collapsed)
        self.assertNotEqual(code, 0, out)
        self.assertIn("live closures fell", out, out)

    def test_a_pack_built_without_the_council_table_is_still_refused(self):
        bare = {"generated": "2026-10-01",
                "packs": [{"id": "gb-tro", "kind": "tro",
                           "features": 36584}]}
        code, out = self.run_step(bare)
        self.assertNotEqual(code, 0, out)
        self.assertIn("built without the council table", out, out)

    def test_no_index_at_all_is_refused_not_skipped(self):
        # The lane workflow writes no order index, and there "THIS GATE DID
        # NOT RUN" is right. This job exists to build one: absent is a fault.
        code, out = self.run_step(None)
        self.assertNotEqual(code, 0, out)
        self.assertIn("wrote no traffic-order index", out, out)
        self.assertNotIn("THIS GATE DID NOT RUN", out, out)


if __name__ == "__main__":
    unittest.main()

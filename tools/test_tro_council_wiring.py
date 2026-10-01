"""The two council refusals no other test reaches (verifier, 2026-10-01).

* build_tro.main refuses when live orders come from listed councils and the
  per-authority count says nobody publishes (mutation M5 survived without it);
* check_build.main actually calls the council gates (M9, M10 survived).
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_tro  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "check_build.py")


class TheBuilderRefusesABrokenCount(unittest.TestCase):
    def test_live_orders_and_a_count_of_nobody_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="tro-wiring-")
        self.addCleanup(shutil.rmtree, tmp, True)
        corpus = os.path.join(tmp, "dtros_20260601_000000.csv")
        rec = build_tro._check_record(
            "miscRoadClosure", "THE LEICESTERSHIRE (HOOTON LANE) ORDER 2026")
        rec["source"]["currentTraOwner"] = 2460
        with open(corpus, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Id", "SchemaVersion", "Data"])
            writer.writerow(["w-0", "3.4.0", json.dumps(rec)])
        saved = (build_tro.pack, build_tro.load_key, sys.argv,
                 build_tro.AuthorityTally.add)
        build_tro.pack = lambda body, key: body
        build_tro.load_key = lambda path: b"k" * 32
        # The count breaks: every record is read and none is tallied.
        build_tro.AuthorityTally.add = lambda self, *a, **k: None
        sys.argv = ["build_tro.py", "--key", "unused", "--csv", corpus,
                    "--out", os.path.join(tmp, "out"),
                    "--index", os.path.join(tmp, "index.json")]
        try:
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    build_tro.main()
        finally:
            (build_tro.pack, build_tro.load_key, sys.argv,
             build_tro.AuthorityTally.add) = saved
        self.assertIn("per-authority count is broken",
                      str(raised.exception.code))
        self.assertFalse(os.path.exists(os.path.join(tmp, "out",
                                                     "gb-tro.tbpack")),
                         "a pack was written despite the refusal")


class TheGateIsCalledFromMain(unittest.TestCase):
    def run_main(self, index, table=None):
        tmp = tempfile.mkdtemp(prefix="council-gate-")
        self.addCleanup(shutil.rmtree, tmp, True)
        manifest = os.path.join(tmp, "manifest.json")
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump({"packages": []}, fh)
        new_index = os.path.join(tmp, "tro.json")
        with open(new_index, "w", encoding="utf-8") as fh:
            json.dump(index, fh)
        args = [sys.executable, CHECK, "--previous", manifest,
                "--new", manifest, "--cache", os.path.join(tmp, "cache"),
                "--closures-previous", os.path.join(tmp, "none.json"),
                "--closures-new", new_index]
        if table:
            args += ["--councils", table]
        return subprocess.run(args, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              universal_newlines=True).stdout

    INDEX = {"generated": "2026-10-01",
             "packs": [{"id": "gb-tro", "kind": "tro", "features": 36584}]}

    def test_an_index_without_councils_is_named_by_main(self):
        out = self.run_main(self.INDEX)
        self.assertIn("live closures: 36584", out,
                      "PREMISE: the closures gate read the index")
        self.assertIn("built without the council table", out)

    def test_a_table_with_a_duplicate_code_is_named_by_main(self):
        tmp = tempfile.mkdtemp(prefix="council-table-")
        self.addCleanup(shutil.rmtree, tmp, True)
        table = os.path.join(tmp, "t.csv")
        with open(table, "w", encoding="utf-8", newline="") as fh:
            fh.write("swa_code,display_name,lane_names\n"
                     "1050,Derbyshire County Council,Derbyshire\n"
                     "1050,Derby City Council,City of Derby\n")
        out = self.run_main(self.INDEX, table)
        self.assertIn("swa_code 1050 is already line 2", out)


if __name__ == "__main__":
    unittest.main()

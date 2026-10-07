#!/usr/bin/env python3
"""The councils' own orders reach the sealed pack, credited, beside D-TRO's.

    python tools/test_build_tro_council.py

Runs the real tools/build_tro.py over a one-order D-TRO extract and a
tro/council/ directory holding one council source, then opens the pack. What
must hold:

  * the council's order is in the pack, in the shape the app reads, with its
    source, its source's name and its own notice URL;
  * the pack's attribution names the source, and the council's row in the
    `authorities` block lists both D-TRO and the council source;
  * the index counts the council's orders apart from the total;
  * a council file that cannot be read REFUSES the build, so every rider
    keeps yesterday's pack rather than one with the councils' orders gone;
  * an offline --csv build without --council is unchanged: no council
    orders (the other suites' exact counts depend on it).
"""
import base64
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

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa
except ImportError:  # pragma: no cover - CI installs it
    print("SKIP: the cryptography package is not installed "
          "(pip install cryptography)")
    sys.exit(2)

from test_tro_events_move_the_pack import record, unseal  # noqa: E402

EXTRACT = "dtros_20260906_010013.csv"
LINE = "SRID=27700;LINESTRING(464946.33 293262.84, 464918.98 293239.27)"

COUNCIL = {
    "source": {"id": "essex-prow-tros", "authority": "Essex",
               "name": "Essex County Council - PRoW traffic regulation "
                       "orders",
               "kind": "council-layer", "licence": "Published by the council",
               "endpoint": "https://example.essex/FeatureServer/0"},
    "records": 1,
    "items": [{
        "id": "g1", "authority": "Essex", "ref": "Basildon Byway 83",
        "title": "Essex County Council: Prohibition of Driving Order",
        "where": "Byway 83, Basildon", "vehicles": "motor_vehicles",
        "form": "permanent", "url": "https://example.essex/tro.pdf",
        "ways": ["EX-83-ba8d9da643"], "match": "geometry",
        "geometry": {"type": "LineString",
                     "coordinates": [[0.45, 51.58], [0.455, 51.581]]}}],
    "unmatched": [], "review": []}


class CouncilOrdersInThePack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tro-council-")
        cls.key = os.urandom(32)
        cls.key_path = os.path.join(cls.tmp, "throwaway.key")
        with open(cls.key_path, "w") as fh:
            fh.write(base64.b64encode(cls.key).decode())
        cls.csv = os.path.join(cls.tmp, EXTRACT)
        with open(cls.csv, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Id", "SchemaVersion", "Data"])
            writer.writerow(["order-a", "3.4.0",
                             json.dumps(record("order-a", LINE, tra=1585))])
        cls.council = os.path.join(cls.tmp, "council")
        os.makedirs(cls.council)
        with open(os.path.join(cls.council, "essex-prow-tros.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(COUNCIL, fh)
        with open(os.path.join(cls.council, "status.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"essex-prow-tros": {"last_ok": "2026-10-07"}}, fh)
        cls.proc, cls.pack, cls.index = cls.build("with", cls.council)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def build(cls, name, council=None):
        out = os.path.join(cls.tmp, name)
        index = os.path.join(cls.tmp, name + ".json")
        cmd = [sys.executable, os.path.join(HERE, "build_tro.py"),
               "--key", cls.key_path, "--csv", cls.csv, "--out", out,
               "--index", index]
        if council:
            cmd += ["--council", council]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              universal_newlines=True, cwd=ROOT)
        pack = idx = None
        if proc.returncode == 0:
            pack = unseal(os.path.join(out, "gb-tro.tbpack"), cls.key)
            with open(index, encoding="utf-8") as fh:
                idx = json.load(fh)
        return proc, pack, idx

    def built(self):
        if self.proc.returncode != 0:
            self.fail("build_tro.py --council failed (exit %d):\n%s"
                      % (self.proc.returncode, self.proc.stdout[-2000:]))
        return self.pack

    def council_features(self, pack=None):
        pack = pack or self.built()
        return [f for f in pack["features"]
                if f["properties"].get("source") == "essex-prow-tros"]

    def test_the_councils_order_is_in_the_pack_as_the_app_reads_it(self):
        got = self.council_features()
        self.assertEqual(len(got), 1)
        p = got[0]["properties"]
        self.assertEqual(p["code"], "movementOrderProhibitedAccess")
        self.assertEqual(p["otype"], "prohibition")
        self.assertEqual(p["label"], "No motor vehicles")
        self.assertEqual(p["tra"], "1585")
        self.assertEqual(p["url"], "https://example.essex/tro.pdf")
        self.assertIn("Essex County Council", p["source_name"])
        self.assertEqual(len([f for f in self.built()["features"]
                              if f["properties"].get("dtro")]), 1)

    def test_the_source_is_credited_by_name(self):
        pack = self.built()
        self.assertIn("Essex County Council - PRoW traffic regulation orders",
                      pack["attribution"])
        self.assertIn("D-TRO", pack["attribution"])
        self.assertEqual([s["id"] for s in pack["sources"]],
                         ["essex-prow-tros"])

    def test_the_council_row_lists_which_sources_cover_it(self):
        rows = dict((a["swa"], a) for a in self.built()["authorities"])
        self.assertEqual(rows["1585"]["sources"], ["dtro", "essex-prow-tros"])
        dorset = [a for a in self.built()["authorities"]
                  if "Dorset" in a["lanes"]][0]
        self.assertEqual(dorset["sources"], [])

    def test_the_index_counts_council_orders_apart(self):
        self.built()
        pack = self.index["packs"][0]
        self.assertEqual(pack["features"], 2)
        self.assertEqual(pack["council_features"], 1)

    def test_an_unreadable_council_file_refuses_the_build(self):
        broken = os.path.join(self.tmp, "broken")
        os.makedirs(broken)
        with open(os.path.join(broken, "essex-prow-tros.json"), "w",
                  encoding="utf-8") as fh:
            fh.write('{"source": {"id": "essex-prow-tros"}, "items": [')
        proc, _pack, _idx = self.build("broken", broken)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REFUSING TO PUBLISH", proc.stdout)

    def test_an_offline_build_without_council_is_unchanged(self):
        proc, pack, _idx = self.build("offline")
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:])
        self.assertEqual(self.council_features(pack), [])
        self.assertEqual(len(pack["features"]), 1)


class StreetWorksDirectory(unittest.TestCase):
    """Street Manager's closures live in their own directory (their own job
    writes them) and are read with the councils' by the publishing build."""

    def test_the_publishing_build_reads_both_directories(self):
        import build_tro
        tmp = tempfile.mkdtemp(prefix="tro-dirs-")
        try:
            a, b = os.path.join(tmp, "council"), os.path.join(tmp, "works")
            os.makedirs(a)
            os.makedirs(b)
            with open(os.path.join(a, "essex-prow-tros.json"), "w") as fh:
                json.dump(COUNCIL, fh)
            sm = dict(COUNCIL, source=dict(COUNCIL["source"],
                                           id="street-manager",
                                           kind="street-manager"))
            with open(os.path.join(b, "street-manager.json"), "w") as fh:
                json.dump(sm, fh)
            got = build_tro.read_council_dir([a, b])
            self.assertEqual([src["id"] for src, _items in got],
                             ["essex-prow-tros", "street-manager"])
            self.assertTrue(build_tro.STREETWORKS_DIR.replace(
                "\\", "/").endswith("tro/streetworks/orders"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)

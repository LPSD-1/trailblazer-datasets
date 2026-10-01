"""The council table and the per-authority count, edge by edge (TRO_SPEC 3.7).

tools/test_tro_authorities.py proves the block exists and counts a lapsed
order. This holds the rest of what an incorrect entry would get wrong:

* every lane authority the published ways containers carry is in the table,
  mapped or explicitly unmapped - read off the containers themselves, so a
  name that changes there turns this red;
* a handful of codes pinned against GeoPlace's SWA list of 30 Sep 2026;
* leading zeros, records naming no authority, `newest` from either source,
  and a date past the cut;
* build_tro refuses a missing table, and check_build refuses a pack built
  without one.

Run as `python tools/test_tro_council_table.py`.
"""
import csv
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_tro  # noqa: E402
import check_build  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OVERVIEW = os.path.join(ROOT, "containers", "ways-overview.tbmap")


def lane_authorities():
    """The lane authority names the published containers carry."""
    db = sqlite3.connect("file:%s?mode=ro" % OVERVIEW.replace(os.sep, "/"),
                         uri=True)
    try:
        row = db.execute(
            "SELECT value FROM meta WHERE key = 'authorities'").fetchone()
    finally:
        db.close()
    return json.loads(row[0])


class TheTable(unittest.TestCase):
    def setUp(self):
        self.mapped, self.unmapped = build_tro.load_authority_table()

    def test_every_lane_authority_in_the_containers_is_in_it(self):
        names = lane_authorities()
        self.assertGreaterEqual(len(names), 100,
                                "PREMISE: the overview carries the 108 names")
        listed = {}
        for row in self.mapped + self.unmapped:
            for lane in row["lanes"]:
                listed.setdefault(check_build._lane_key(lane), []).append(row)
        absent = [n for n in names if check_build._lane_key(n) not in listed]
        self.assertEqual(absent, [], "lane authorities the table lacks")
        # A council's own name maps to exactly one SWA code. Only a National
        # Park and "Cumbria" (abolished 2023, two successors) map to more.
        several = sorted(n for n in names
                         if len([r for r in listed[check_build._lane_key(n)]
                                 if r in self.mapped]) != 1)
        self.assertEqual(several, ["Cumbria", "Lake District National Park"])

    def test_codes_pinned_against_geoplace(self):
        by_swa = {r["swa"]: r for r in self.mapped}
        for swa, name, lane in (
                ("1050", "Derbyshire County Council", "Derbyshire"),
                ("2460", "Leicestershire County Council", "Leicestershire"),
                ("6850", "Powys County Council", "Powys"),
                ("935", "Westmorland and Furness Council",
                 "Westmorland and Furness"),
                ("4410", "City of Doncaster Council", "Doncaster"),
                ("1260", "Bournemouth, Christchurch and Poole Council",
                 "Bournemouth, Christchurch and Poole")):
            self.assertIn(swa, by_swa)
            self.assertEqual(by_swa[swa]["name"], name)
            self.assertIn(lane, by_swa[swa]["lanes"])

    def test_the_park_authorities_are_explicitly_unmapped(self):
        names = sorted(r["name"] for r in self.unmapped)
        self.assertEqual(names, ["Bannau Brycheiniog National Park Authority",
                                 "Lake District National Park Authority"])

    def test_the_real_table_passes_the_gate(self):
        problems = []
        with redirect_stdout(io.StringIO()):
            check_build.check_council_table(check_build.COUNCIL_TABLE,
                                            os.path.join(ROOT, "cache"),
                                            problems)
        self.assertEqual(problems, [])


class TheReader(unittest.TestCase):
    def write(self, text):
        handle, path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return path

    def test_one_code_on_two_rows_is_refused_by_both_readers(self):
        path = self.write("swa_code,display_name,lane_names\n"
                          "1050,Derbyshire County Council,Derbyshire\n"
                          "01050,Derby City Council,City of Derby\n")
        with self.assertRaises(ValueError):
            build_tro.load_authority_table(path)
        problems = []
        with redirect_stdout(io.StringIO()):
            check_build.check_council_table(path, "/nowhere", problems)
        self.assertEqual(len(problems), 1, problems)

    def test_a_code_that_is_not_a_number_is_refused(self):
        path = self.write("swa_code,display_name,lane_names\n"
                          "DY,Derbyshire County Council,Derbyshire\n")
        with self.assertRaises(ValueError):
            build_tro.load_authority_table(path)


class TheCount(unittest.TestCase):
    """The real `main` over a small corpus, sealing stubbed."""

    def build(self, records, extra_column=None, table=None, expect_exit=False):
        tmp = tempfile.mkdtemp(prefix="tro-councils-")
        self.addCleanup(shutil.rmtree, tmp, True)
        corpus = os.path.join(tmp, "dtros_20260601_000000.csv")
        with open(corpus, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Id", "SchemaVersion", "Data"]
                            + ([extra_column] if extra_column else []))
            for i, item in enumerate(records):
                record, published = (item if extra_column else (item, None))
                writer.writerow(["c-%d" % i, "3.4.0", json.dumps(record)]
                                + ([published] if extra_column else []))
        saved = (build_tro.pack, build_tro.load_key, sys.argv)
        build_tro.pack = lambda body, key: body
        build_tro.load_key = lambda path: b"k" * 32
        sys.argv = ["build_tro.py", "--key", "unused", "--csv", corpus,
                    "--out", os.path.join(tmp, "out"),
                    "--index", os.path.join(tmp, "index.json")]
        if table:
            sys.argv += ["--authorities", table]
        try:
            with redirect_stdout(io.StringIO()):
                if expect_exit:
                    with self.assertRaises(SystemExit) as raised:
                        build_tro.main()
                    return str(raised.exception.code)
                build_tro.main()
        finally:
            build_tro.pack, build_tro.load_key, sys.argv = saved
        with open(os.path.join(tmp, "out", "gb-tro.tbpack"), "rb") as fh:
            built = json.loads(fh.read().decode("utf8"))
        with open(os.path.join(tmp, "index.json"), encoding="utf-8") as fh:
            index = json.load(fh)
        return built, index

    def record(self, tra=2460, made=None, end=None, owner_key="traCreator"):
        rec = build_tro._check_record(
            "miscRoadClosure", "THE LEICESTERSHIRE (HOOTON LANE) ORDER 2026",
            end=end)
        source = rec["source"]
        del source["traCreator"], source["currentTraOwner"]
        if tra is not None:
            source[owner_key] = tra
        if made is not None:
            source["madeDate"] = made
        return rec

    def row(self, built, swa):
        rows = [r for r in built["authorities"] if r["swa"] == swa]
        self.assertEqual(len(rows), 1, "%s not listed once" % swa)
        return rows[0]

    def test_leading_zeros_and_no_authority(self):
        built, index = self.build([
            self.record(tra="02460", made="2026-04-01"),
            self.record(tra=2460, made="2026-05-20",
                        end="2026-05-15T00:00:00"),
            self.record(tra=None),
        ])
        self.assertEqual(len(built["features"]), 2,
                         "PREMISE: two live orders, one lapsed")
        leics = self.row(built, "2460")
        self.assertEqual(leics["records"], 2)
        self.assertEqual(leics["newest"], "2026-05-20")
        self.assertEqual(built["authorities_newest_from"], "madeDate")
        derbys = self.row(built, "1050")
        self.assertEqual((derbys["records"], derbys["newest"]), (0, None))
        self.assertIn("Derbyshire", derbys["lanes"])
        self.assertFalse([r for r in built["authorities"]
                          if "National Park Authority" in r["name"]],
                         "an unmapped row reached the pack")
        pack = index["packs"][0]
        self.assertEqual(pack["councils"], len(built["authorities"]))
        self.assertEqual(pack["councils_publishing"], 1)

    def test_newest_prefers_a_publication_column_and_ignores_the_future(self):
        built, _ = self.build([
            (self.record(made="2020-01-01"), "2026-05-30T09:00:00Z"),
            (self.record(made="2020-01-01"), "2026-07-01T09:00:00Z"),
            (self.record(made="2020-01-01"), "not a date"),
        ], extra_column="PublicationTime")
        self.assertEqual(built["authorities_newest_from"], "PublicationTime")
        leics = self.row(built, "2460")
        self.assertEqual(leics["records"], 3)
        # 1 July is past the 1 June cut: a typo, not news.
        self.assertEqual(leics["newest"], "2026-05-30")

    def test_current_owner_wins_as_it_does_for_the_feature(self):
        rec = self.record(tra=2460)
        rec["source"]["currentTraOwner"] = 1050
        built, _ = self.build([rec])
        self.assertEqual(built["features"][0]["properties"]["tra"], 1050)
        self.assertEqual(self.row(built, "1050")["records"], 1)
        self.assertEqual(self.row(built, "2460")["records"], 0)

    def test_a_missing_table_refuses_the_build(self):
        message = self.build([self.record()], table=os.path.join(
            tempfile.gettempdir(), "no-such-tro-authorities.csv"),
            expect_exit=True)
        self.assertIn("council table", message)


class TheGate(unittest.TestCase):
    def index(self, **extra):
        pack = {"id": "gb-tro", "kind": "tro", "features": 36584}
        pack.update(extra)
        return {"generated": "2026-10-01", "packs": [pack]}

    def run_gate(self, index):
        problems = []
        with redirect_stdout(io.StringIO()) as out:
            check_build.check_council_coverage(index, problems)
        return problems, out.getvalue()

    def test_a_pack_built_without_the_table_is_refused(self):
        problems, _ = self.run_gate(self.index())
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("without the council table", problems[0])

    def test_live_orders_and_nobody_publishing_is_refused(self):
        problems, _ = self.run_gate(self.index(councils=178,
                                               councils_publishing=0))
        self.assertEqual(len(problems), 1, problems)

    def test_a_good_index_passes(self):
        problems, out = self.run_gate(self.index(councils=178,
                                                 councils_publishing=91))
        self.assertEqual(problems, [])
        self.assertIn("178 listed", out)

    def test_no_index_says_it_did_not_run(self):
        problems, out = self.run_gate(None)
        self.assertEqual(problems, [])
        self.assertIn("DID NOT RUN", out)


if __name__ == "__main__":
    unittest.main()

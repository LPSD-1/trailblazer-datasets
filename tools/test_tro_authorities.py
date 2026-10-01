"""The order pack says which councils publish (TRO_SPEC.md 3.7, October 2026).

The app side is built: `TrafficOrders.coverage` reads a top-level
`authorities` block out of gb-tro.tbpack, and the lane sheet, the order sheet
and What's shut near you each name the council from it. Without the block,
every pack parses to `coverage == null` and no rider sees any of it, while
docs/guide.md already says the sheets "now name the council".

The data side has to supply two things, and both are held here:

* `tools/tro_authorities.csv`, the hand-built table (columns `swa_code`,
  `display_name`, `lane_names`);
* `build_tro.py` counting EVERY record per `tra` - before expired and
  far-future orders are dropped - and writing `{swa, name, lanes, records,
  newest}` rows under `authorities`.

RED until both exist. Run as `python tools/test_tro_authorities.py`.
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest

import build_tro

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "tro_authorities.csv")


class TheMappingTable(unittest.TestCase):
    def test_the_table_exists_with_its_three_columns(self):
        self.assertTrue(
            os.path.exists(TABLE),
            "tools/tro_authorities.csv does not exist: no pack can say which "
            "council publishes, so the app's council lines never show")
        with open(TABLE, encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle))
        for column in ("swa_code", "display_name", "lane_names"):
            self.assertIn(column, header)


class ThePack(unittest.TestCase):
    """The real `main`, over a two-record corpus, with the sealing stubbed."""

    def build(self, records):
        tmp = tempfile.mkdtemp(prefix="tro-authorities-")
        self.addCleanup(shutil.rmtree, tmp, True)
        corpus = os.path.join(tmp, "dtros_20260601_000000.csv")
        with open(corpus, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Id", "SchemaVersion", "Data"])
            for i, record in enumerate(records):
                writer.writerow(["auth-%d" % i, "3.4.0", json.dumps(record)])
        saved = (build_tro.pack, build_tro.load_key, sys.argv)
        build_tro.pack = lambda body, key: body
        build_tro.load_key = lambda path: b"k" * 32
        sys.argv = ["build_tro.py", "--key", "unused", "--csv", corpus,
                    "--out", os.path.join(tmp, "out"),
                    "--index", os.path.join(tmp, "index.json")]
        try:
            build_tro.main()
        finally:
            build_tro.pack, build_tro.load_key, sys.argv = saved
        with open(os.path.join(tmp, "out", "gb-tro.tbpack"), "rb") as handle:
            return json.loads(handle.read().decode("utf8"))

    def test_the_pack_counts_every_record_per_authority(self):
        live = build_tro._check_record(
            "miscRoadClosure", "THE DERBYSHIRE (HOOTON LANE) ORDER 2026")
        # Ended a fortnight before the cut: dropped from `features`, and still
        # a record this authority published.
        lapsed = build_tro._check_record(
            "miscRoadClosure", "THE DERBYSHIRE (FOOLOW) ORDER 2026",
            end="2026-05-15T00:00:00")
        built = self.build([live, lapsed])

        self.assertEqual(len(built["features"]), 1,
                         "PREMISE: one order in force, one lapsed")
        self.assertIn(
            "authorities", built,
            "the pack carries no `authorities` block, so every phone reads "
            "coverage as unknown and names no council")
        rows = [r for r in built["authorities"]
                if str(r.get("swa", "")).lstrip("0") == "2460"]
        self.assertEqual(len(rows), 1,
                         "authority 2460 is not listed once: %r"
                         % built["authorities"])
        row = rows[0]
        for key in ("swa", "name", "lanes", "records", "newest"):
            self.assertIn(key, row)
        self.assertEqual(row["records"], 2,
                         "records must count the lapsed order too")


if __name__ == "__main__":
    unittest.main()

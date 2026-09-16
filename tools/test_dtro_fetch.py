#!/usr/bin/env python3
"""The corpus directory, and what it is allowed to keep.

    python tools/test_dtro_fetch.py

The extract's filename carries the date the cut was made, so a new cut arrives
under a NEW name rather than replacing the old one. Each is about half a
gigabyte and the workflow caches this directory between runs, so without
pruning it grows by an extract per cut for ever - and when it passes the
repository's cache allowance, what GitHub evicts is whatever was least recently
used, which is the council-data cache that turns the lane refresh's 25-minute
fetch into seconds.

Nothing here touches the network: pruning is a function of what is on disk.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dtro_fetch import _forget_older_cuts  # noqa: E402


class ForgetOlderCuts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="corpus")

    def write(self, name, body="x"):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            fh.write(body)

    def names(self):
        return sorted(os.listdir(self.dir))

    def test_an_earlier_cut_and_its_marker_both_go(self):
        self.write("dtros_all_2026-09-05.csv")
        self.write("dtros_all_2026-09-05.csv.done")
        self.write("dtros_all_2026-09-16.csv")
        self.write("dtros_all_2026-09-16.csv.done")

        _forget_older_cuts(self.dir, keep="dtros_all_2026-09-16.csv")

        self.assertEqual(
            self.names(),
            ["dtros_all_2026-09-16.csv", "dtros_all_2026-09-16.csv.done"])

    def test_the_cut_in_use_survives_with_its_marker(self):
        # The marker is what makes "the file is here" mean "the file is whole".
        # Removing it alongside the file it describes would make every run
        # re-download an extract it already had.
        self.write("dtros_all_2026-09-16.csv")
        self.write("dtros_all_2026-09-16.csv.done")

        _forget_older_cuts(self.dir, keep="dtros_all_2026-09-16.csv")

        self.assertEqual(
            self.names(),
            ["dtros_all_2026-09-16.csv", "dtros_all_2026-09-16.csv.done"])

    def test_an_abandoned_part_file_is_cleared_too(self):
        # A half-written download from a run that died. It is never valid - the
        # marker is written only after the size check - and it is the same half
        # gigabyte as a real one.
        self.write("dtros_all_2026-09-05.csv.part")
        self.write("dtros_all_2026-09-16.csv")

        _forget_older_cuts(self.dir, keep="dtros_all_2026-09-16.csv")

        self.assertEqual(self.names(), ["dtros_all_2026-09-16.csv"])

    def test_a_part_file_for_the_CURRENT_cut_also_goes(self):
        # Reached when a download failed and the next run succeeded: the name
        # is the same, so a stale `.part` would sit beside the finished file
        # for ever.
        self.write("dtros_all_2026-09-16.csv")
        self.write("dtros_all_2026-09-16.csv.part")

        _forget_older_cuts(self.dir, keep="dtros_all_2026-09-16.csv")

        self.assertEqual(self.names(), ["dtros_all_2026-09-16.csv"])

    def test_an_empty_directory_is_not_an_error(self):
        _forget_older_cuts(self.dir, keep="dtros_all_2026-09-16.csv")
        self.assertEqual(self.names(), [])

    def test_several_old_cuts_all_go(self):
        # What the cache actually accumulated: nine days of daily cuts is four
        # and a half gigabytes.
        for day in range(5, 16):
            self.write("dtros_all_2026-09-%02d.csv" % day)
            self.write("dtros_all_2026-09-%02d.csv.done" % day)
        self.write("dtros_all_2026-09-16.csv")

        _forget_older_cuts(self.dir, keep="dtros_all_2026-09-16.csv")

        self.assertEqual(self.names(), ["dtros_all_2026-09-16.csv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

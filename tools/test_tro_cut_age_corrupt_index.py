"""A corrupt tro/index.json keeps the stale-extract alarm open, never closes it.

    python tools/test_tro_cut_age_corrupt_index.py

traffic-orders.yml's "Say so when the extract has stopped moving" treats
ANYTHING but an explicit `stale=false` as stale, so a crash in
`check_build.py --orders-cut-age` cannot read as "fresh" and close the issue.
test_tro_cut_age_alarm.py only pinned the missing-file case, and a missing
file still prints an explicit `stale=true`; so changing the step's test from
`!= "false"` to `= "true"` stayed green. A corrupt index makes check_build
crash with a JSONDecodeError and print no `stale=` line at all: this is the
case that tells the two tests apart.

The step is run for real, through test_tro_cut_age_alarm.TheWorkflowStep.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_tro_cut_age_alarm import TheWorkflowStep  # noqa: E402


class ACorruptIndex(TheWorkflowStep):
    def run_raw(self, raw, open_issues=""):
        self.write(os.path.join(self.work, "tro", "index.json"), raw)
        return self.run_step(None, open_issues=open_issues)

    def test_premise_check_build_prints_no_verdict_on_a_corrupt_index(self):
        code, out, _ = self.run_raw("{not json", open_issues="12")
        self.assertEqual(code, 0, out)
        self.assertNotIn("stale=", out,
                         "PREMISE: the corrupt index no longer crashes "
                         "check_build, so this case is the missing-file case "
                         "again and proves nothing:\n" + out)

    def test_a_corrupt_index_does_not_close_the_alarm(self):
        code, out, calls = self.run_raw("{not json", open_issues="12")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_of(calls, "close"), [],
                         "a corrupt index closed the stale-extract alarm, as "
                         "if the extract were fresh: %s" % calls)
        edited = self.calls_of(calls, "edit")
        self.assertEqual(len(edited), 1, calls)
        self.assertTrue(edited[0].startswith("issue edit 12 "), edited)

    def test_a_corrupt_index_with_nothing_open_opens_one(self):
        code, out, calls = self.run_raw("{not json")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls_of(calls, "create")), 1, calls)


# Without this, unittest would also collect the imported parent class here and
# run its 7 tests a second time. ACorruptIndex still inherits and re-runs them
# (10 tests = these 3 + the parent's 7), which is harmless: same step, same
# stand-in gh.
del TheWorkflowStep

if __name__ == "__main__":
    unittest.main()

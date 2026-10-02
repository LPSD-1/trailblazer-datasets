"""The GeoPlace council-code check is committed and CI reaches it (triage, 2026-10-01).

Residue of data-geoplace-check-untracked. tools/test_tro_council_codes_match_geoplace.py
pins tools/tro_authorities.csv against GeoPlace's own SWA code list
(tools/geoplace_swa_org_active_2026-09-30.csv). Two things keep it from
protecting anything:

* both files are untracked, so no CI checkout has them;
* even committed, nothing runs it. refresh-data.yml's "Run every tool suite"
  loop stops at tools/test_changeset_fixture_after_is_a_real_build.py, which
  exits BLIND (2) because that workflow never checks the app repo out, and
  that test sorts before the GeoPlace one. traffic-orders.yml, the job that
  builds the order pack and already runs test_tro_council_table.py, does not
  name it.

Either fix is enough for the second test: name the check in a workflow's
explicit list, or have refresh-data check the app repo out beside this one
(which also unblocks the whole refresh pipeline).
"""
import glob
import os
import re
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHECK = "test_tro_council_codes_match_geoplace.py"
CSV = "geoplace_swa_org_active_2026-09-30.csv"
BLOCKER = "test_changeset_fixture_after_is_a_real_build.py"


def _live(text):
    """A workflow's lines with YAML comments dropped, so a commented-out
    command or a name in prose does not count as wiring."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _workflows():
    out = {}
    for path in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")):
        with open(path, encoding="utf-8") as fh:
            out[os.path.basename(path)] = fh.read()
    return out


class TheCheckIsCommittedAndTheListIsFetched(unittest.TestCase):
    """The check is committed; GeoPlace's list is not (it is their list and
    this repository is public). traffic-orders.yml fetches it at run time."""

    def test_the_check_is_tracked(self):
        done = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "tools/" + CHECK],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, "tools/%s is not committed" % CHECK)

    def test_the_list_is_never_committed_and_is_fetched(self):
        done = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "tools/" + CSV],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertNotEqual(done.returncode, 0,
                            "GeoPlace's list must not be committed")
        orders = _workflows().get("traffic-orders.yml", "")
        self.assertIn("static.geoplace.co.uk/downloads/", orders,
                      "traffic-orders.yml must fetch the list it checks against")


class TheCheckIsReachedByCi(unittest.TestCase):
    def test_a_workflow_runs_the_check(self):
        flows = _workflows()
        self.assertTrue(flows, "PREMISE: no workflows were read")
        refresh = flows.get("refresh-data.yml", "")
        self.assertIn("for t in tools/test_*.py", refresh,
                      "PREMISE: refresh-data runs every tool suite by glob")
        named = sorted(f for f, text in flows.items()
                       if re.search(r"^\s*python3? tools/" + re.escape(CHECK)
                                    + r"\s*$", _live(text), re.M))
        # The glob reaches the check only if nothing before it stops the
        # loop; the changeset-fixture test sorts first and needs the app.
        suites = sorted(os.path.basename(p)
                        for p in glob.glob(os.path.join(HERE, "test_*.py")))
        self.assertLess(suites.index(BLOCKER), suites.index(CHECK),
                        "PREMISE: the blocker sorts before the check")
        app_checked_out = re.search(r"^\s*path:\s*\S*greenroadmap-app",
                                    _live(refresh), re.M) is not None
        self.assertTrue(
            named or app_checked_out,
            "no workflow names %s, and refresh-data's loop exits at %s "
            "(BLIND: no app checkout) before reaching it" % (CHECK, BLOCKER))


if __name__ == "__main__":
    unittest.main()

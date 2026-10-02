"""The order pack's publish and clear-up keep what they must.

    python tools/test_tro_publish_keeps_what_it_must.py

Companion to test_tro_push_race_keeps_its_index.py (same harness: the real
`run:` blocks of traffic-orders.yml under bash, a throwaway release behind a
stand-in `gh`). The features-7 verifier found four protections nothing held:

1. THE PACK MAIN NAMES, WHEN IT IS OLD. Orders that go back to an earlier
   cut name a pack already in the release from more than three days ago.
   Its upload is skipped, and only `now` in the clear-up's `keep` stops it
   being deleted - without that, every phone gets a 404. The existing test
   only names a fresh pack, which the three-day window keeps anyway.
2. A HALF-UPLOADED ASSET. An upload that died part-way leaves its name in a
   state other than "uploaded"; real `gh` refuses to upload over any name
   it finds. The publish removes it first. The old stand-in ignored `--jq`
   and every state, so neither state filter was ever run.
3. The naming step refuses a built file whose sha256 is not the index's.
4. The clear-up deletes nothing when it cannot read which pack main names.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_tro_push_race_keeps_its_index as base  # noqa: E402
from test_tro_push_race_keeps_its_index import (  # noqa: E402
    BASH, NEW_BYTES, OLD_BYTES, PRUNE_STEP, built_index, run_block, sha,
    write, write_json)

# The stand-in with asset STATE (FAKE_STATE/<name> holds it; absent is
# "uploaded") and the two `--jq` filters the publish step uses evaluated.
FAKE_GH_STATES = r'''import datetime, json, os, shutil, sys
store = os.environ["FAKE_RELEASE"]
states = os.environ["FAKE_STATE"]
a = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(" ".join(a) + "\n")
def state(name):
    p = os.path.join(states, name)
    return open(p).read().strip() if os.path.exists(p) else "uploaded"
if a[:2] == ["release", "view"]:
    if not os.path.isdir(store):
        sys.exit(1)
    assets = []
    for name in sorted(os.listdir(store)):
        st = os.stat(os.path.join(store, name))
        made = datetime.datetime.fromtimestamp(st.st_mtime,
                                               datetime.timezone.utc)
        assets.append({"name": name, "size": st.st_size, "state": state(name),
                       "createdAt": made.strftime("%Y-%m-%dT%H:%M:%SZ")})
    if "--jq" in a:
        jq = a[a.index("--jq") + 1].replace(" ", "")
        if jq == '.assets[]|select(.state=="uploaded")|.name':
            pick = [x for x in assets if x["state"] == "uploaded"]
        elif jq == '.assets[]|select(.state!="uploaded")|.name':
            pick = [x for x in assets if x["state"] != "uploaded"]
        else:
            sys.exit("fake gh: --jq filter not understood: " + jq)
        for x in pick:
            print(x["name"])
    elif "--json" in a:
        print(json.dumps({"assets": assets}))
    sys.exit(0)
if a[:2] == ["release", "create"]:
    os.makedirs(store, exist_ok=True)
    sys.exit(0)
if a[:2] == ["release", "upload"]:
    for f in [x for x in a[3:] if not x.startswith("--")]:
        path = f.split("#", 1)[0]
        dest = os.path.join(store, os.path.basename(path))
        if os.path.exists(dest) and "--clobber" not in a:
            sys.exit("asset under the same name already exists: "
                     + os.path.basename(path))
        shutil.copyfile(path, dest)
        p = os.path.join(states, os.path.basename(path))
        if os.path.exists(p):
            os.remove(p)
    sys.exit(0)
if a[:2] == ["release", "delete-asset"]:
    os.remove(os.path.join(store, a[3]))
    p = os.path.join(states, a[3])
    if os.path.exists(p):
        os.remove(p)
    sys.exit(0)
sys.exit("fake gh: not implemented: " + " ".join(a))
'''


class ThePublishKeepsWhatItMust(base.EveryPushRejected):
    def setUp(self):
        super().setUp()
        self.states = os.path.join(self.tmp, "states")
        os.makedirs(self.states)
        write(os.path.join(self.bin, "gh.py"), FAKE_GH_STATES)
        self.env["FAKE_STATE"] = self.states

    # The inherited tests run in their own file; not again here.
    def test_premise_with_the_push_accepted_the_new_pack_is_served(self):
        pass

    def test_every_push_rejected_still_serves_the_committed_hash(self):
        pass

    def test_a_forced_republish_never_replaces_an_asset_in_place(self):
        pass

    def test_old_packs_are_cleared_and_every_named_one_kept(self):
        pass

    def test_the_previous_index_the_clear_up_reads_is_the_one_kept(self):
        pass

    def test_the_pack_main_names_is_kept_however_old(self):
        named = "gb-tro-%s.tbpack" % sha(NEW_BYTES)[:16]
        stale = "gb-tro-1111111111111111.tbpack"

        def release():
            self.age(named, 10, NEW_BYTES)   # an earlier cut, back again
            self.age(stale, 10)

        code, ran, log, pack, _ = self.run_publish(
            reject=False, until=PRUNE_STEP, before=release)
        self.assertEqual(code, 0, log)
        self.assertEqual(ran[-1], PRUNE_STEP, log)
        self.assertEqual(pack["file"].rsplit("/", 1)[1], named,
                         "PREMISE: main names the old pack:\n" + log)
        self.assertIn("is already published", log,
                      "PREMISE: its upload was skipped:\n" + log)
        held = sorted(os.listdir(self.store))
        self.assertNotIn(stale, held, "PREMISE: nothing was cleared, so the "
                         "keep below proves nothing:\n" + log)
        self.assertIn(named, held, "the pack main names, uploaded ten days "
                      "ago, was cleared: every phone gets a 404\n" + log)

    def test_a_half_uploaded_asset_does_not_stop_the_publish(self):
        named = "gb-tro-%s.tbpack" % sha(NEW_BYTES)[:16]

        def release():
            self.age(named, 0.01, NEW_BYTES[:5])
            write(os.path.join(self.states, named), "starter\n")

        code, ran, log, pack, _ = self.run_publish(reject=False,
                                                   before=release)
        self.assertIn("Publish the pack", ran, "PREMISE:\n" + log)
        self.assertEqual(code, 0, "a half-uploaded asset under the name "
                         "stopped the publish:\n" + log)
        self.assertIn("removing a half-uploaded", log, log)
        self.assertEqual(sha(self.served(pack["file"])), sha(NEW_BYTES), log)

    def test_the_naming_step_refuses_bytes_the_index_does_not_describe(self):
        job = os.path.join(self.tmp, "naming")
        os.makedirs(os.path.join(job, "dist", "tro"))
        with open(os.path.join(job, "dist", "tro", "gb-tro.tbpack"),
                  "wb") as fh:
            fh.write(NEW_BYTES)
        write_json(os.path.join(job, "tro", "index.json"),
                   built_index(OLD_BYTES))
        script = os.path.join(self.tmp, "naming.sh")
        write(script, run_block(self.text, "Name the pack after its contents"))
        run = subprocess.run([BASH, "--noprofile", "--norc", "-eo",
                              "pipefail", script], cwd=job, env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True)
        self.assertNotEqual(run.returncode, 0,
                            "the built file is not what the index says, and "
                            "the step named and published it:\n" + run.stdout)
        self.assertEqual(sorted(os.listdir(os.path.join(job, "dist", "tro"))),
                         ["gb-tro.tbpack"], run.stdout)

    def test_the_clear_up_deletes_nothing_when_it_cannot_read_the_index(self):
        stale = "gb-tro-1111111111111111.tbpack"
        os.makedirs(self.store)
        self.age(stale, 10)
        job = os.path.join(self.tmp, "prune")
        os.makedirs(os.path.join(job, "dist"))
        os.makedirs(os.path.join(job, "tro"))
        write(os.path.join(job, "tro", "index.json"), "{not json")
        script = os.path.join(self.tmp, "prune.sh")
        write(script, run_block(self.text, PRUNE_STEP))
        run = subprocess.run([BASH, "--noprofile", "--norc", "-eo",
                              "pipefail", script], cwd=job, env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True)
        listed = [c for c in self.gh_calls() if c.startswith("release view")]
        self.assertTrue(listed, "PREMISE: the step listed the release:\n"
                        + run.stdout)
        self.assertIn(stale, os.listdir(self.store),
                      "the clear-up could not tell which pack main names and "
                      "deleted packs anyway:\n" + run.stdout)


if __name__ == "__main__":
    unittest.main()

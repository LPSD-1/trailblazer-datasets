"""A lost push race never leaves the order index naming the old pack.

    python tools/test_tro_push_race_keeps_its_index.py

THE DEFECT. traffic-orders.yml uploads gb-tro.tbpack over the stable release
asset (`--clobber`), then commits tro/index.json and catalogue.json. When the
push is rejected because another workflow committed first (five workflows
commit catalogue.json, in five concurrency groups), the retry loop runs
`git reset --hard origin/main`. dist/ is gitignored and survives; but
tro/index.json is TRACKED, so the reset put it back to the OLD hash. The
catalogue was rebuilt from that, nothing differed from main, `commit_it ||
exit 0` exited 0 - and the job was green while the served pack's bytes and
the published hash disagreed. Every rider's order download then failed its
checksum until a later run republished.

Related, same root: "Rebuild the catalogue" (and verify_catalogue's refusal)
ran AFTER the upload, so a refusal there also left new bytes under the old
hash. It now runs before the upload.

WHAT IS RUN. The real `run:` block of "Commit the index and the catalogue",
cut out of the workflow, under bash -eo pipefail, in a throwaway clone of a
throwaway bare remote that another clone pushes to first. Only
tools/rebuild_catalogue.sh and verify_catalogue.py are stand-ins: the
catalogue is written from whatever tro/index.json is in the working tree,
which is the property the real one has and the only one this depends on.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "traffic-orders.yml")
STEP = "Commit the index and the catalogue"
OLD = "a" * 64
NEW = "b" * 64


def run_block(text, step):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "- name: %s" % step:
            indent = len(line) - len(line.lstrip())
            for j in range(i + 1, len(lines)):
                l = lines[j]
                ind = len(l) - len(l.lstrip())
                if l.strip() and ind <= indent:
                    break
                if l.strip() == "run: |":
                    body = []
                    for b in lines[j + 1:]:
                        if b.strip() and len(b) - len(b.lstrip()) <= ind:
                            break
                        body.append(b)
                    cut = min(len(b) - len(b.lstrip())
                              for b in body if b.strip())
                    script = "\n".join(b[cut:] if b.strip() else ""
                                       for b in body).rstrip() + "\n"
                    if "${{" in script:
                        raise AssertionError("step %r has an expression this "
                                             "test does not substitute" % step)
                    return script
            raise AssertionError("step %r has no run block" % step)
    raise AssertionError("PREMISE: no step %r in %s" % (step, WORKFLOW))


def step_names(text):
    return [l.strip()[len("- name: "):] for l in text.splitlines()
            if l.strip().startswith("- name: ")]


def find_bash():
    if os.name != "nt":
        return shutil.which("bash")
    # Not shutil.which on Windows: System32\bash.exe is WSL.
    try:
        exec_path = subprocess.run(["git", "--exec-path"], check=True,
                                   stdout=subprocess.PIPE,
                                   universal_newlines=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    git_root = os.path.normpath(os.path.join(exec_path, "..", "..", ".."))
    for candidate in (os.path.join(git_root, "bin", "bash.exe"),
                      os.path.join(git_root, "usr", "bin", "bash.exe")):
        if os.path.isfile(candidate):
            return candidate
    return None


BASH = find_bash()
ENV = dict(os.environ)
ENV.update({
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid",
})

# The catalogue is a FUNCTION of the indexes in the working tree - the real
# rebuild_catalogue.sh's contract, and the property the race turns on.
STUB_REBUILD = r'''#!/usr/bin/env bash
set -eo pipefail
python - "$@" <<'PY'
import json, os, sys
out = sys.argv[2]
tro = json.load(open("tro/index.json"))["packs"][0]
feed = open("published/feed.json").read()
cat = {"tro": {"sha256": tro["sha256"]}, "feed": feed}
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
with open(out, "w", newline="\n") as h:
    json.dump(cat, h, indent=1, sort_keys=True)
    h.write("\n")
PY
'''


def tro_index(sha):
    return {"generated": "2026-10-02", "packs": [{
        "id": "gb-tro", "sha256": sha, "bytes": 10,
        "file": "https://example.invalid/releases/download/tro/gb-tro.tbpack"
    }]}


def sh(cwd, *args):
    run = subprocess.run(list(args), cwd=cwd, env=ENV,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    if run.returncode != 0:
        raise RuntimeError("%s failed:\n%s" % (" ".join(args), run.stdout))
    return run.stdout


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_json(path, obj):
    write(path, json.dumps(obj, indent=1, sort_keys=True) + "\n")


class ThePushRace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.text = fh.read()

    def setUp(self):
        if not BASH or not shutil.which("git"):
            self.skipTest("BLIND: bash and git are needed to run the step")
        self.tmp = tempfile.mkdtemp(prefix="tro-race-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rebuild(self, repo, out):
        sh(repo, BASH, "tools/rebuild_catalogue.sh", "manifest.json", out)

    def seed(self):
        remote = os.path.join(self.tmp, "remote.git")
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", remote)
        seed = os.path.join(self.tmp, "seed")
        sh(self.tmp, "git", "init", "-q", "-b", "main", seed)
        write(os.path.join(seed, ".gitignore"), "dist/\n")
        write(os.path.join(seed, "tools", "rebuild_catalogue.sh"),
              STUB_REBUILD)
        write(os.path.join(seed, "tools", "verify_catalogue.py"),
              "import sys\nsys.exit(0)\n")
        write_json(os.path.join(seed, "tro", "index.json"), tro_index(OLD))
        write(os.path.join(seed, "published", "feed.json"), "one\n")
        write_json(os.path.join(seed, "manifest.json"), {})
        self.rebuild(seed, "catalogue.json")
        sh(seed, "git", "add", "-A")
        sh(seed, "git", "commit", "-q", "-m", "published")
        sh(seed, "git", "remote", "add", "origin", remote)
        sh(seed, "git", "push", "-q", "-u", "origin", "main")
        return remote

    def clone(self, remote, name):
        path = os.path.join(self.tmp, name)
        sh(self.tmp, "git", "clone", "-q", remote, path)
        return path

    def published(self, remote, name):
        look = self.clone(remote, name)
        index = json.loads(sh(look, "git", "show", "HEAD:tro/index.json"))
        cat = json.loads(sh(look, "git", "show", "HEAD:catalogue.json"))
        feed = sh(look, "git", "show", "HEAD:published/feed.json")
        return index["packs"][0]["sha256"], cat["tro"]["sha256"], feed

    def run_commit_step(self, race):
        remote = self.seed()
        job = self.clone(remote, "job")
        # The build wrote the NEW index, "Publish the pack" replaced the
        # served asset with the NEW bytes, and the catalogue was rebuilt.
        write_json(os.path.join(job, "tro", "index.json"), tro_index(NEW))
        self.rebuild(job, "dist/catalogue.json")
        if race:
            other = self.clone(remote, "conditions")
            write(os.path.join(other, "published", "feed.json"), "two\n")
            self.rebuild(other, "catalogue.json")
            sh(other, "git", "add", "-A")
            sh(other, "git", "commit", "-q", "-m", "Conditions")
            sh(other, "git", "push", "-q")
        script = os.path.join(self.tmp, "step.sh")
        write(script, run_block(self.text, STEP))
        run = subprocess.run([BASH, "--noprofile", "--norc", "-eo",
                              "pipefail", script], cwd=job, env=ENV,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True)
        return run.returncode, run.stdout, self.published(remote, "look")

    def test_premise_without_a_race_the_new_hash_is_published(self):
        code, log, (index, cat, _) = self.run_commit_step(race=False)
        self.assertEqual(code, 0, log)
        self.assertEqual((index, cat), (NEW, NEW), log)

    def test_a_lost_push_race_still_publishes_the_served_hash(self):
        code, log, (index, cat, feed) = self.run_commit_step(race=True)
        self.assertIn("push rejected", log,
                      "PREMISE: the push was never rejected, so the retry "
                      "path did not run:\n" + log)
        self.assertEqual(code, 0, log)
        self.assertEqual(feed, "two\n",
                         "the other workflow's commit was lost:\n" + log)
        self.assertEqual(index, NEW,
                         "tro/index.json names %s.. but the served pack is "
                         "%s..:\n%s" % (index[:6], NEW[:6], log))
        self.assertEqual(cat, NEW,
                         "catalogue.json names %s.. but the served pack is "
                         "%s..:\n%s" % (cat[:6], NEW[:6], log))


class TheCatalogueIsCheckedBeforeTheUpload(unittest.TestCase):
    def test_rebuild_and_verify_come_before_publish(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()
        names = step_names(text)
        for name in ("Rebuild the catalogue", "Publish the pack", STEP):
            self.assertIn(name, names, "PREMISE: %r" % name)
        self.assertIn("python tools/verify_catalogue.py dist/catalogue.json",
                      run_block(text, "Rebuild the catalogue"),
                      "PREMISE: the rebuild step verifies the catalogue")
        self.assertLess(names.index("Rebuild the catalogue"),
                        names.index("Publish the pack"),
                        "a verify_catalogue refusal after the upload leaves "
                        "new bytes served under the old hash")


# ---------------------------------------------------------------------------
# EVERY PUSH REJECTED: THE SERVED ASSET STILL MATCHES THE COMMITTED HASH.
#
# The retry loop above can lose all three attempts (a busy morning: five
# workflows commit catalogue.json). The job then exits 1 and raises the alarm,
# which is right - but "Publish the pack" had already replaced the release
# asset. With a STABLE asset name (gb-tro.tbpack, --clobber) the committed
# tro/index.json still named the OLD sha256 while the URL it gives served the
# NEW bytes, so every phone that fetched in that window failed its checksum
# until the next good run. Only a content-addressed asset name closes this:
# the new bytes go up under a new name, and the committed index keeps
# pointing at bytes that still match it.
#
# WHAT IS RUN. Every step from after "Build the pack" to the commit step, in
# workflow order, exactly as cut out of the workflow - so a step the fix adds
# is run without this test naming it - except the ones in SKIP, each of which
# says why. `gh` is a stand-in that keeps the release as a directory, and the
# bare remote's pre-receive hook rejects every push.
# ---------------------------------------------------------------------------
import hashlib  # noqa: E402
import time  # noqa: E402

BUILD_STEP = "Build the pack"
SKIP = {
    "Check the built pack before publishing it":
        "check_build.py's gates; they judge counts, not the asset name",
    "Say so when the extract has stopped moving": "an alarm; publishes nothing",
    "Say so if the stale-extract alarm could not run":
        "an alarm; publishes nothing",
    "Forget the credentials": "deletes the key; publishes nothing",
    "Has anything changed?": "taken as publish=true, the path under test",
    "Check the published pack matches the index":
        "fetches the URL over the network; the release here is a directory",
}
OLD_BYTES = b"the pack riders already have\n"
NEW_BYTES = b"the pack this run built\n"
BASE = "https://example.invalid/releases/download/tro"

PRUNE_STEP = "Clear away packs nothing names any more"

# `--jq` is not evaluated: with it, the stand-in prints the names of the
# assets (what the publish step's filter yields for fully uploaded assets);
# without it, the JSON `gh release view --json assets` prints, createdAt
# taken from the file's mtime.
FAKE_GH = r'''import datetime, json, os, shutil, sys
store = os.environ["FAKE_RELEASE"]
a = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(" ".join(a) + "\n")
if a[:2] == ["release", "view"]:
    if not os.path.isdir(store):
        sys.exit(1)
    if "--jq" in a:
        for name in sorted(os.listdir(store)):
            print(name)
    elif "--json" in a:
        assets = []
        for name in sorted(os.listdir(store)):
            st = os.stat(os.path.join(store, name))
            made = datetime.datetime.fromtimestamp(st.st_mtime,
                                                   datetime.timezone.utc)
            assets.append({"name": name, "size": st.st_size,
                           "state": "uploaded",
                           "createdAt": made.strftime("%Y-%m-%dT%H:%M:%SZ")})
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
            sys.exit("asset already exists: " + os.path.basename(path))
        shutil.copyfile(path, dest)
    sys.exit(0)
if a[:2] == ["release", "delete-asset"]:
    os.remove(os.path.join(store, a[3]))
    sys.exit(0)
sys.exit("fake gh: not implemented: " + " ".join(a))
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def built_index(data):
    """tro/index.json as build_tro.py writes it: the asset under its stable
    build name, which is all build_tro.py knows."""
    return {"generated": "2026-10-02", "packs": [{
        "id": "gb-tro", "kind": "tro", "label": "Traffic orders",
        "sha256": sha(data), "bytes": len(data), "generated": "2026-10-02",
        "file": BASE + "/gb-tro.tbpack"}]}


class EveryPushRejected(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.text = fh.read()

    def setUp(self):
        if not BASH or not shutil.which("git"):
            self.skipTest("BLIND: bash and git are needed to run the steps")
        self.tmp = tempfile.mkdtemp(prefix="tro-asset-")
        self.store = os.path.join(self.tmp, "release")
        self.bin = os.path.join(self.tmp, "bin")
        write(os.path.join(self.bin, "gh.py"), FAKE_GH)
        write(os.path.join(self.bin, "gh"),
              '#!/usr/bin/env bash\nexec python "$(dirname "$0")/gh.py" "$@"\n')
        # The retry loop sleeps 5, 10 and 15 seconds; not here.
        write(os.path.join(self.bin, "sleep"), "#!/usr/bin/env bash\nexit 0\n")
        self.env = dict(ENV)
        self.env.update({
            "PATH": self.bin + os.pathsep + ENV.get("PATH", ""),
            "FAKE_RELEASE": self.store,
            "FAKE_GH_LOG": os.path.join(self.tmp, "gh.log"),
            "GH_TOKEN": "fake",
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def steps_to_run(self, until):
        names = step_names(self.text)
        for name in (BUILD_STEP, "Publish the pack", STEP, until):
            self.assertIn(name, names, "PREMISE: %r" % name)
        between = names[names.index(BUILD_STEP) + 1:names.index(until) + 1]
        for name in SKIP:
            self.assertIn(name, between, "PREMISE: SKIP names %r, which is "
                          "not between the build and the commit" % name)
        return [n for n in between if n not in SKIP]

    def seed(self, reject):
        remote = os.path.join(self.tmp, "remote.git")
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", remote)
        seed = os.path.join(self.tmp, "seed")
        sh(self.tmp, "git", "init", "-q", "-b", "main", seed)
        write(os.path.join(seed, ".gitignore"), "dist/\n")
        write(os.path.join(seed, "tools", "rebuild_catalogue.sh"),
              STUB_REBUILD)
        write(os.path.join(seed, "tools", "verify_catalogue.py"),
              "import sys\nsys.exit(0)\n")
        write_json(os.path.join(seed, "tro", "index.json"),
                   built_index(OLD_BYTES))
        write(os.path.join(seed, "published", "feed.json"), "one\n")
        write_json(os.path.join(seed, "manifest.json"), {})
        sh(seed, BASH, "tools/rebuild_catalogue.sh", "manifest.json",
           "catalogue.json")
        sh(seed, "git", "add", "-A")
        sh(seed, "git", "commit", "-q", "-m", "published")
        sh(seed, "git", "remote", "add", "origin", remote)
        sh(seed, "git", "push", "-q", "-u", "origin", "main")
        # What riders are served before this run: the old bytes, under the
        # name the committed index gives.
        os.makedirs(self.store)
        with open(os.path.join(self.store, "gb-tro.tbpack"), "wb") as fh:
            fh.write(OLD_BYTES)
        if reject:
            write(os.path.join(remote, "hooks", "pre-receive"),
                  "#!/bin/sh\necho 'rejected: another workflow got there "
                  "first' >&2\nexit 1\n")
        return remote

    def age(self, name, days, data=b"x"):
        """Put `name` in the release, uploaded `days` ago."""
        path = os.path.join(self.store, name)
        with open(path, "wb") as fh:
            fh.write(data)
        when = time.time() - days * 86400
        os.utime(path, (when, when))

    def gh_calls(self):
        try:
            with open(self.env["FAKE_GH_LOG"]) as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def run_publish(self, reject, until=STEP, before=None):
        remote = self.seed(reject)
        if before:
            before()
        job = os.path.join(self.tmp, "job")
        sh(self.tmp, "git", "clone", "-q", remote, job)
        # What "Build the pack" leaves behind.
        os.makedirs(os.path.join(job, "dist", "tro"))
        with open(os.path.join(job, "dist", "tro", "gb-tro.tbpack"),
                  "wb") as fh:
            fh.write(NEW_BYTES)
        write(os.path.join(job, "dist", "tro", "gb-tro.tbpack.sha256"),
              sha(NEW_BYTES) + "\n")
        write_json(os.path.join(job, "tro", "index.json"),
                   built_index(NEW_BYTES))
        log, ran, code = [], [], 0
        for i, name in enumerate(self.steps_to_run(until)):
            script = os.path.join(self.tmp, "step%02d.sh" % i)
            write(script, run_block(self.text, name))
            run = subprocess.run([BASH, "--noprofile", "--norc", "-eo",
                                  "pipefail", script], cwd=job, env=self.env,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 universal_newlines=True)
            ran.append(name)
            log.append("== %s (exit %d)\n%s" % (name, run.returncode,
                                                run.stdout))
            if run.returncode != 0:
                code = run.returncode
                break   # as Actions does: later steps are skipped
        look = os.path.join(self.tmp, "look")
        sh(self.tmp, "git", "clone", "-q", remote, look)
        index = json.loads(sh(look, "git", "show", "HEAD:tro/index.json"))
        cat = json.loads(sh(look, "git", "show", "HEAD:catalogue.json"))
        return code, ran, "\n".join(log), index["packs"][0], cat

    def served(self, url):
        """The bytes a phone gets from `url`: the release asset it names."""
        prefix = BASE + "/"
        self.assertTrue(url.startswith(prefix),
                        "the index's file %r is not in the release" % url)
        path = os.path.join(self.store, url[len(prefix):])
        self.assertTrue(os.path.isfile(path),
                        "the index names %r, which the release does not hold "
                        "(holds %s)" % (url, sorted(os.listdir(self.store))))
        with open(path, "rb") as fh:
            return fh.read()

    def assert_new_bytes_uploaded(self, ran, log):
        self.assertIn("Publish the pack", ran,
                      "PREMISE: the publish step never ran:\n" + log)
        uploaded = []
        for name in os.listdir(self.store):
            with open(os.path.join(self.store, name), "rb") as fh:
                if fh.read() == NEW_BYTES:
                    uploaded.append(name)
        self.assertTrue(uploaded, "PREMISE: the new pack was never uploaded, "
                        "so there was nothing to get wrong:\n" + log)

    def test_premise_with_the_push_accepted_the_new_pack_is_served(self):
        code, ran, log, pack, cat = self.run_publish(reject=False)
        self.assertEqual(code, 0, log)
        self.assertEqual(ran[-1], STEP, log)
        self.assert_new_bytes_uploaded(ran, log)
        self.assertEqual(pack["sha256"], sha(NEW_BYTES), log)
        self.assertEqual(cat["tro"]["sha256"], sha(NEW_BYTES), log)
        self.assertEqual(sha(self.served(pack["file"])), pack["sha256"],
                         "the committed index and the asset it names "
                         "disagree on a clean run:\n" + log)

    def test_every_push_rejected_still_serves_the_committed_hash(self):
        code, ran, log, pack, _ = self.run_publish(reject=True)
        self.assertIn("could not push after three attempts", log,
                      "PREMISE: the retry loop did not lose all three "
                      "attempts:\n" + log)
        self.assertEqual(code, 1, log)
        self.assert_new_bytes_uploaded(ran, log)
        self.assertEqual(pack["sha256"], sha(OLD_BYTES),
                         "PREMISE: every push was rejected, so main must "
                         "still hold the old index:\n" + log)
        got = sha(self.served(pack["file"]))
        self.assertEqual(got, pack["sha256"],
                         "the committed tro/index.json says %s.. but %s "
                         "serves %s..: every phone fetching orders now fails "
                         "its checksum until a later run lands\n%s"
                         % (pack["sha256"][:12], pack["file"], got[:12], log))

    def test_a_forced_republish_never_replaces_an_asset_in_place(self):
        # workflow_dispatch with force=true on an unchanged pack: the name is
        # already in the release, and may be the one main names. Replacing it
        # (--clobber) deletes it first; phones fetching in that gap get 404.
        name = "gb-tro-%s.tbpack" % sha(NEW_BYTES)[:16]
        code, ran, log, pack, _ = self.run_publish(
            reject=False, before=lambda: self.age(name, 0.1, NEW_BYTES))
        self.assertEqual(code, 0, log)
        self.assertEqual(pack["file"].rsplit("/", 1)[1], name,
                         "PREMISE: the asset is not named as this test "
                         "pre-seeded it, so nothing was already there:\n"
                         + log)
        uploads = [c for c in self.gh_calls() if c.startswith("release upload")]
        self.assertEqual(uploads, [], "an asset already published was "
                         "uploaded over:\n%s\n%s" % (uploads, log))
        self.assertEqual(sha(self.served(pack["file"])), pack["sha256"], log)

    def test_old_packs_are_cleared_and_every_named_one_kept(self):
        stale = "gb-tro-1111111111111111.tbpack"
        recent = "gb-tro-2222222222222222.tbpack"
        previous = "gb-tro-3333333333333333.tbpack"
        previous_index = os.path.join(self.tmp, "tro-previous.json")
        index = built_index(OLD_BYTES)
        index["packs"][0]["file"] = BASE + "/" + previous
        write_json(previous_index, index)
        self.env["PREVIOUS_INDEX"] = previous_index

        def release():
            self.age("gb-tro.tbpack", 30, OLD_BYTES)   # before hashed names
            self.age(stale, 5)
            self.age(recent, 1)
            self.age(previous, 20)
            self.age("notes.txt", 30)

        code, ran, log, pack, _ = self.run_publish(
            reject=False, until=PRUNE_STEP, before=release)
        self.assertEqual(code, 0, log)
        self.assertEqual(ran[-1], PRUNE_STEP, log)
        held = sorted(os.listdir(self.store))
        self.assertNotIn(stale, held, "PREMISE: nothing was cleared, so the "
                         "keeps below prove nothing:\n" + log)
        self.assertEqual(sha(self.served(pack["file"])), pack["sha256"],
                         "the pack main now names was cleared:\n" + log)
        for name, why in ((previous, "the pack main named a minute ago"),
                          (recent, "a pack uploaded inside three days"),
                          ("gb-tro.tbpack", "the pre-hash asset old "
                           "catalogues name"),
                          ("notes.txt", "an asset that is not a pack")):
            self.assertIn(name, held, "cleared %s: %s\n%s" % (why, name, log))

    def test_the_previous_index_the_clear_up_reads_is_the_one_kept(self):
        # The test above hands the step PREVIOUS_INDEX itself; this is what
        # makes the real run hand it the same thing.
        def section(name):
            lines = self.text.splitlines()
            start = [i for i, l in enumerate(lines)
                     if l.strip() == "- name: %s" % name]
            self.assertEqual(len(start), 1, "PREMISE: step %r" % name)
            indent = len(lines[start[0]]) - len(lines[start[0]].lstrip())
            out = []
            for l in lines[start[0] + 1:]:
                if l.strip() and len(l) - len(l.lstrip()) <= indent:
                    break
                out.append(l)
            return "\n".join(out)
        self.assertIn("PREVIOUS_INDEX: /tmp/tro-previous.json",
                      section(PRUNE_STEP))
        self.assertIn("> /tmp/tro-previous.json",
                      section("Keep the published index to compare against"))


if __name__ == "__main__":
    unittest.main()

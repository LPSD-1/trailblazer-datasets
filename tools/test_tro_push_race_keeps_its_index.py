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


if __name__ == "__main__":
    unittest.main()

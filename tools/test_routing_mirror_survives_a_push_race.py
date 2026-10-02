#!/usr/bin/env python3
"""The routing mirror never leaves served tiles and their published hash apart.

    python tools/test_routing_mirror_survives_a_push_race.py

Exit 0: every scenario below holds. Exit 1: at least one does not. Exit 2:
bash or git is not available, so nothing could be run (BLIND). Exit 3: the
harness could not reach a verdict (PREMISE), which is not a pass.

THE DEFECT. mirror-routing.yml uploads every tile to the `routing` release
under its STABLE name (E0_N50.rd5 ...) with `--clobber`, and the sha256 a
phone checks a download against is whatever routing/index.json and
catalogue.json on main say. The job used to upload, then push the index ONCE
with no retry, on the argument that "a rejected push costs one cycle and
nothing else". Once the bytes are replaced that is false: a push lost to the
conditions job (which commits after the 03:23 lane refresh, inside this
job's three-hour window from 04:23 on Tuesdays) left the release serving new
bytes and main naming the old hash for a week - every routing-tile download
failing its checksum on every phone - while the failure issue told the owner
"nothing was published ... riders are unaffected either way".

WHAT IS RUN. The real `run:` blocks of the real "Publish" and "Raise an
issue if this run failed" steps, cut out of the workflow file, executed by
bash -eo pipefail (what Actions uses) in a throwaway clone of a throwaway
bare remote. `gh` is a stub that records what it was asked to do, and the
repository's catalogue tools are stubs whose catalogue is a FUNCTION of
routing/index.json - the real rebuild_catalogue.sh's contract, and the only
property these scenarios depend on.

  A. Another workflow pushes while the mirror runs. The mirror must still
     land an index and a catalogue naming the uploaded bytes, and keep the
     other workflow's commit.
  B. The satellite job's prune withdraws a tile from routing/index.json
     meanwhile. The re-record merges this run's tiles in; it must not put
     the withdrawn one back by copying its whole stale index over main's.
  C. Every push is refused. The step must FAIL (not exit 0 green), and the
     issue it raises must say riders are affected, not "unaffected".
  D. The catalogue check refuses before anything is uploaded. Nothing may
     have been uploaded, and the issue may then say nothing was published.

HUNT_WORKFLOWS points the harness at another copy of the workflows, to watch
it go red against the unfixed one.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.environ.get("HUNT_WORKFLOWS") or \
    os.path.join(ROOT, ".github", "workflows")
WORKFLOW = "mirror-routing.yml"
JOB = "mirror"

OLD_SHA = "a" * 64


class Premise(Exception):
    pass


def step_script(step):
    """The `run: |` block of a named step in the mirror job, dedented."""
    lines = open(os.path.join(WORKFLOWS, WORKFLOW), encoding="utf-8") \
        .read().split("\n")
    in_jobs = False
    job = None
    for i, line in enumerate(lines):
        if line.rstrip() == "jobs:":
            in_jobs = True
        elif in_jobs and line.startswith("  ") and not line.startswith("   ") \
                and line.rstrip().endswith(":") \
                and not line.lstrip().startswith("#"):
            job = line.strip()[:-1]
        elif job == JOB and line.strip() == "- name: %s" % step:
            step_indent = len(line) - len(line.lstrip())
            for j in range(i + 1, len(lines)):
                l = lines[j]
                ind = len(l) - len(l.lstrip())
                if l.strip() and ind <= step_indent:
                    break
                if l.strip() == "run: |":
                    body = []
                    for b in lines[j + 1:]:
                        if b.strip() and len(b) - len(b.lstrip()) <= ind:
                            break
                        body.append(b)
                    cut = min(len(b) - len(b.lstrip())
                              for b in body if b.strip())
                    text = "\n".join(b[cut:] if b.strip() else ""
                                     for b in body).rstrip() + "\n"
                    if "${{" in text:
                        raise Premise("step %r has an expression in its run "
                                      "block this harness cannot substitute"
                                      % step)
                    return text
            raise Premise("step %r has no run block" % step)
    raise Premise("no step %r in job %r of %s" % (step, JOB, WORKFLOW))


def find_bash():
    if os.name != "nt":
        return shutil.which("bash")
    # Not shutil.which on Windows: System32\bash.exe is WSL, whose git and
    # python belong to another machine.
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
BASE_ENV = dict(os.environ)
BASE_ENV.update({
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    # No detached background gc: on Windows it holds the throwaway clone open
    # after the step has returned, and the clone cannot then be deleted.
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "gc.auto", "GIT_CONFIG_VALUE_0": "0",
    "GIT_CONFIG_KEY_1": "maintenance.auto", "GIT_CONFIG_VALUE_1": "false",
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@invalid",
})


def sh(cwd, *args):
    run = subprocess.run(list(args), cwd=cwd, env=BASE_ENV,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    if run.returncode != 0:
        raise Premise("%s failed in %s:\n%s" % (" ".join(args), cwd,
                                                run.stdout))
    return run.stdout


def write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    kw = {} if isinstance(data, bytes) else {"encoding": "utf-8",
                                             "newline": "\n"}
    with open(path, mode, **kw) as handle:
        handle.write(data)


def write_json(path, obj):
    write(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def executable(path):
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP
             | stat.S_IXOTH)


# The catalogue is a function of the indexes in the working tree, which is
# the real rebuild_catalogue.sh's contract.
STUB_REBUILD = r'''#!/usr/bin/env bash
set -eo pipefail
python - "$@" <<'PY'
import glob, json, os, sys
out = sys.argv[2]
tiles = json.load(open("routing/index.json"))["tiles"]
feeds = sorted(glob.glob("published/*/*.json"))
cat = {"routing": {n: {"sha256": t["sha256"], "bytes": t["bytes"],
                       "file": os.environ.get("REPO_URL", "") +
                       "/releases/download/routing/%s.rd5" % n}
                   for n, t in tiles.items()},
       "feeds": [open(f).read() for f in feeds]}
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
with open(out, "w", newline="\n") as h:
    json.dump(cat, h, indent=1, sort_keys=True)
    h.write("\n")
PY
'''

STUB_REPORT = "import sys\nprint('routing report (stub)')\nsys.exit(0)\n"
STUB_VERIFY_OK = "import sys\nsys.exit(0)\n"
STUB_VERIFY_REFUSES = ("import sys\nprint('refused: catalogue lost a pack')\n"
                       "sys.exit(1)\n")

# Records every call; the issue body goes to its own file so it can be read.
STUB_GH = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$1 $2" in
  "issue create"|"issue comment")
    prev=
    for a in "$@"; do
      if [ "$prev" = "--body" ]; then printf '%s\n' "$a" >> "$GH_ISSUES"; fi
      if [ "$prev" = "--title" ]; then printf 'TITLE %s\n' "$a" >> "$GH_ISSUES"; fi
      prev=$a
    done ;;
  "issue list") ;;
esac
exit 0
'''


def tile_entry(sha, size):
    return {"bytes": size, "sha256": sha, "upstream_bytes": size,
            "mirrored_at": 1790000000.0}


def seed(tmp):
    remote = os.path.join(tmp, "remote.git")
    sh(tmp, "git", "init", "-q", "--bare", "-b", "main", remote)
    s = os.path.join(tmp, "seed")
    sh(tmp, "git", "init", "-q", "-b", "main", s)
    write(os.path.join(s, ".gitignore"), "dist/\n")
    write(os.path.join(s, "tools", "rebuild_catalogue.sh"), STUB_REBUILD)
    write(os.path.join(s, "tools", "build_catalogue.py"), STUB_REPORT)
    write(os.path.join(s, "tools", "verify_catalogue.py"), STUB_VERIFY_OK)
    write_json(os.path.join(s, "manifest.json"), {"packages": []})
    write_json(os.path.join(s, "routing", "index.json"), {"tiles": {
        "E0_N50": tile_entry(OLD_SHA, 2000),
        "W10_N45": tile_entry("c" * 64, 3000)}})
    write_json(os.path.join(s, "published", "wet", "south-west.json"),
               {"as_of": "2026-10-01T18:00:00Z"})
    sh(s, BASH, "tools/rebuild_catalogue.sh", "manifest.json",
       "catalogue.json")
    sh(s, "git", "add", "-A")
    sh(s, "git", "commit", "-q", "-m", "published")
    sh(s, "git", "remote", "add", "origin", remote)
    sh(s, "git", "push", "-q", "-u", "origin", "main")
    return remote


def clone(tmp, remote, name):
    path = os.path.join(tmp, name)
    sh(tmp, "git", "clone", "-q", remote, path)
    return path


def mirror_took_its_copy(job):
    """What "Take our own copy" leaves: a tile in dist/ and our index entry."""
    data = b"rd5 tile, rebuilt upstream " + b"x" * 3000
    write(os.path.join(job, "dist", "routing", "E0_N50.rd5"), data)
    sha = hashlib.sha256(data).hexdigest()
    path = os.path.join(job, "routing", "index.json")
    index = json.load(open(path, encoding="utf-8"))
    index["tiles"]["E0_N50"] = dict(tile_entry(sha, len(data)),
                                    mirrored_at=1790900000.0)
    write_json(path, index)
    return sha


def conditions_pushes(tmp, remote):
    other = clone(tmp, remote, "conditions-run")
    write_json(os.path.join(other, "published", "wet", "south-west.json"),
               {"as_of": "2026-10-06T04:30:00Z"})
    sh(other, BASH, "tools/rebuild_catalogue.sh", "manifest.json",
       "catalogue.json")
    sh(other, "git", "add", "-A")
    sh(other, "git", "commit", "-q", "-m", "Conditions: rain and rivers")
    sh(other, "git", "push", "-q")


def satellite_prunes_w10_n45(tmp, remote):
    other = clone(tmp, remote, "prune-run")
    path = os.path.join(other, "routing", "index.json")
    index = json.load(open(path, encoding="utf-8"))
    del index["tiles"]["W10_N45"]
    write_json(path, index)
    sh(other, BASH, "tools/rebuild_catalogue.sh", "manifest.json",
       "catalogue.json")
    sh(other, "git", "add", "-A")
    sh(other, "git", "commit", "-q", "-m", "Prune withdrawn packs")
    sh(other, "git", "push", "-q")


def refuse_every_push(remote):
    hook = os.path.join(remote, "hooks", "pre-receive")
    write(hook, "#!/bin/sh\necho 'refused by test' >&2\nexit 1\n")
    executable(hook)


def run_step(tmp, repo, step):
    script = step_script(step)
    bindir = os.path.join(tmp, "bin")
    if not os.path.exists(os.path.join(bindir, "gh")):
        write(os.path.join(bindir, "gh"), STUB_GH)
        executable(os.path.join(bindir, "gh"))
    runner_temp = os.path.join(tmp, "runner-temp")
    os.makedirs(runner_temp, exist_ok=True)
    path = os.path.join(tmp, "step-%s.sh" % len(os.listdir(tmp)))
    write(path, script)
    env = dict(BASE_ENV)
    env.update({
        "PATH": bindir + os.pathsep + env.get("PATH", ""),
        "GH_LOG": os.path.join(tmp, "gh.log"),
        "GH_ISSUES": os.path.join(tmp, "issues.txt"),
        "GH_TOKEN": "test",
        "RUNNER_TEMP": runner_temp,
        "REPO_URL": "https://example.invalid/datasets",
        "RUN_URL": "https://example.invalid/datasets/actions/runs/1",
        "GITHUB_STEP_SUMMARY": os.path.join(tmp, "summary.md"),
        "GITHUB_OUTPUT": os.path.join(tmp, "output.txt"),
    })
    run = subprocess.run([BASH, "--noprofile", "--norc", "-eo", "pipefail",
                          os.path.abspath(path)], cwd=repo, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    return run.returncode, run.stdout


def read(path):
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def uploads(tmp):
    return [l for l in read(os.path.join(tmp, "gh.log")).splitlines()
            if l.startswith("release upload")]


def published(tmp, remote):
    look = clone(tmp, remote, "look-%d" % len(os.listdir(tmp)))
    index = json.loads(sh(look, "git", "show", "HEAD:routing/index.json"))
    cat = json.loads(sh(look, "git", "show", "HEAD:catalogue.json"))
    feed = json.loads(sh(look, "git", "show",
                         "HEAD:published/wet/south-west.json"))
    return index["tiles"], cat["routing"], feed


def marker(tmp):
    return os.path.exists(os.path.join(tmp, "runner-temp",
                                       "routing-uploaded"))


def scenario_a(tmp):
    remote = seed(tmp)
    job = clone(tmp, remote, "mirror-job")
    served = mirror_took_its_copy(job)
    conditions_pushes(tmp, remote)
    code, log = run_step(tmp, job, "Publish")
    tiles, cat, feed = published(tmp, remote)
    return "A. another workflow pushed while the mirror ran", log, [
        ("PREMISE the step uploaded a tile with --clobber",
         any("E0_N50.rd5" in u and "--clobber" in u for u in uploads(tmp))),
        ("the step succeeded (exit %s)" % code, code == 0),
        ("routing/index.json on main names the uploaded bytes",
         tiles.get("E0_N50", {}).get("sha256") == served),
        ("catalogue.json on main names the uploaded bytes",
         cat.get("E0_N50", {}).get("sha256") == served),
        ("the other workflow's commit is still on main",
         feed.get("as_of") == "2026-10-06T04:30:00Z"),
        ("no half-published marker is left for the alarm", not marker(tmp)),
    ]


def scenario_b(tmp):
    remote = seed(tmp)
    job = clone(tmp, remote, "mirror-job")
    served = mirror_took_its_copy(job)
    satellite_prunes_w10_n45(tmp, remote)
    code, log = run_step(tmp, job, "Publish")
    tiles, cat, _ = published(tmp, remote)
    return "B. the satellite prune withdrew a tile meanwhile", log, [
        ("PREMISE the step uploaded a tile", bool(uploads(tmp))),
        ("the step succeeded (exit %s)" % code, code == 0),
        ("routing/index.json on main names the uploaded bytes",
         tiles.get("E0_N50", {}).get("sha256") == served),
        ("catalogue.json on main names the uploaded bytes",
         cat.get("E0_N50", {}).get("sha256") == served),
        ("the withdrawn tile stays withdrawn from the index",
         "W10_N45" not in tiles),
        ("the withdrawn tile stays withdrawn from the catalogue",
         "W10_N45" not in cat),
    ]


def scenario_c(tmp):
    remote = seed(tmp)
    job = clone(tmp, remote, "mirror-job")
    mirror_took_its_copy(job)
    refuse_every_push(remote)
    code, log = run_step(tmp, job, "Publish")
    icode, ilog = run_step(tmp, job, "Raise an issue if this run failed")
    issue = read(os.path.join(tmp, "issues.txt"))
    tiles, _, _ = published(tmp, remote)
    return "C. every push is refused", log + ilog, [
        ("PREMISE the step uploaded a tile", bool(uploads(tmp))),
        ("PREMISE main still names the old hash",
         tiles["E0_N50"]["sha256"] == OLD_SHA),
        ("PREMISE the alarm step ran and raised an issue (exit %s)" % icode,
         icode == 0 and bool(issue.strip())),
        ("the step FAILS rather than going green (exit %s)" % code,
         code != 0),
        ("the step tried more than once",
         log.count("push rejected") >= 2 or log.count("refused by test") >= 2),
        ("the issue says riders are affected",
         "riders are affected" in issue.lower()),
        ("the issue does not say riders are unaffected",
         "unaffected" not in issue.lower()),
        ("the issue does not say nothing was published",
         "nothing was published" not in issue.lower()),
    ]


def scenario_d(tmp):
    remote = seed(tmp)
    job = clone(tmp, remote, "mirror-job")
    mirror_took_its_copy(job)
    write(os.path.join(job, "tools", "verify_catalogue.py"),
          STUB_VERIFY_REFUSES)
    code, log = run_step(tmp, job, "Publish")
    icode, ilog = run_step(tmp, job, "Raise an issue if this run failed")
    issue = read(os.path.join(tmp, "issues.txt"))
    return "D. the catalogue check refuses", log + ilog, [
        ("PREMISE the refusal was reached", "refused: catalogue" in log),
        ("PREMISE the alarm step ran and raised an issue (exit %s)" % icode,
         icode == 0 and bool(issue.strip())),
        ("the step fails (exit %s)" % code, code != 0),
        ("nothing was uploaded before the refusal (%d uploads)"
         % len(uploads(tmp)), not uploads(tmp)),
        ("the issue says nothing was published",
         "nothing was published" in issue.lower()),
    ]


def _writable_then_retry(func, path, _exc):
    # git writes its objects read-only, and Windows will not delete a
    # read-only file: without this every run leaves its remotes behind.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def main():
    if not BASH or not shutil.which("git"):
        print("BLIND: bash and git are needed to run the workflow steps")
        return 2
    failed = premise = 0
    for scenario in (scenario_a, scenario_b, scenario_c, scenario_d):
        tmp = tempfile.mkdtemp(prefix="routing-race-")
        try:
            name, log, facts = scenario(tmp)
        except Premise as e:
            print("%s\n  PREMISE could not run: %s" % (scenario.__name__, e))
            premise += 1
            continue
        finally:
            # onexc from 3.12, where onerror is deprecated; same handler.
            shutil.rmtree(tmp, **({"onexc": _writable_then_retry}
                                  if sys.version_info >= (3, 12)
                                  else {"onerror": _writable_then_retry}))
        print(name)
        bad = False
        for label, ok in facts:
            print("  %s  %s" % ("ok  " if ok else "FAIL", label))
            if not ok:
                bad = True
                if label.startswith("PREMISE"):
                    premise += 1
                else:
                    failed += 1
        if bad:
            print("  --- step log ---")
            print("  " + log.replace("\n", "\n  "))
    if premise:
        print("PREMISE: %d scenario fact(s) could not be established; this "
              "is no verdict" % premise)
        return 3
    print("%s: %d failed" % ("FAIL" if failed else "PASS", failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

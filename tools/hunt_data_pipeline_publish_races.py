#!/usr/bin/env python3
"""A lost push race publishes a catalogue whose hash is not the file's.

    python tools/hunt_data_pipeline_publish_races.py

Exit 0: every scenario below leaves the published catalogue and indexes in
step with the bytes riders download. Exit 1: at least one does not (the
defect). Exit 2: bash or git is not available, so nothing could be run.

Named hunt_*, not test_*, ON PURPOSE: refresh-data.yml runs every
tools/test_*.py by glob and stops the lane refresh on exit 1, so a red proof
committed under that name would stop lane data publishing - the outage this
hunt is about. Rename it test_* in the commit that fixes the workflows.

WHAT IS RUN. The real `run:` block of the real step, cut out of the real
workflow file, executed by bash -eo pipefail (what Actions uses) inside a
throwaway clone of a throwaway bare remote. Only the repository's own tools
are stubbed - rebuild_catalogue.sh writes a catalogue from whatever indexes
are in the working tree, which is the property the real one has and the only
one these scenarios depend on - and `gh` is never reached.

THE THREE SCENARIOS, each a push race against another workflow (five
workflows commit catalogue.json to main, in five concurrency groups):

  A. traffic-orders.yml "Commit the index and the catalogue". The pack was
     already uploaded over the release asset (`--clobber`, stable name).
     The push is rejected; the loop runs `git reset --hard origin/main`,
     which puts tro/index.json back to the OLD hash, rebuilds the catalogue
     from it, finds nothing to commit and exits 0. Green job, and the index
     and catalogue name a hash the served file no longer has.

  B. refresh-data.yml job `conditions`, "Publish". A traffic-orders run
     published a new pack + index + catalogue while this job ran. The
     re-anchor keeps OUR catalogue.json whole ("ours-wins"), built before
     that commit, so it reverts the order pack's hash in the catalogue to
     the old one while tro/index.json and the release asset say new.

  C. refresh-data.yml job `refresh`, "Publish". Same re-anchor, same
     result, over a 25-120 minute window.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# HUNT_WORKFLOWS points the harness at a mutated copy, to watch it go green
# against a fixed workflow before it is trusted red against this one.
WORKFLOWS = os.environ.get("HUNT_WORKFLOWS") or     os.path.join(ROOT, ".github", "workflows")

OLD = "a" * 64
NEW = "b" * 64


# ---------------------------------------------------------------------------
# The workflow, read as text: a `run: |` block under a named step in a job.
# ---------------------------------------------------------------------------

def step_script(workflow, job, step):
    lines = open(os.path.join(WORKFLOWS, workflow), encoding="utf-8").read() \
        .split("\n")
    in_jobs = False
    current_job = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.rstrip() == "jobs:":
            in_jobs = True
        elif in_jobs and len(line) > 2 and line.startswith("  ") \
                and not line.startswith("   ") and line.rstrip().endswith(":") \
                and not line.lstrip().startswith("#"):
            current_job = line.strip()[:-1]
        elif current_job == job and line.strip() == "- name: %s" % step:
            step_indent = len(line) - len(line.lstrip())
            j = i + 1
            while j < len(lines):
                l = lines[j]
                stripped = l.strip()
                ind = len(l) - len(l.lstrip())
                if stripped and ind <= step_indent:
                    break  # next step, or the end of the job
                if stripped == "run: |":
                    run_indent = ind
                    body = []
                    k = j + 1
                    while k < len(lines):
                        b = lines[k]
                        if b.strip() and (len(b) - len(b.lstrip())) <= run_indent:
                            break
                        body.append(b)
                        k += 1
                    content = [b for b in body if b.strip()]
                    cut = min(len(b) - len(b.lstrip()) for b in content)
                    text = "\n".join(b[cut:] if b.strip() else ""
                                     for b in body).rstrip() + "\n"
                    if "${{" in text:
                        raise SystemExit("step %s/%s has an expression this "
                                         "harness does not substitute" %
                                         (job, step))
                    return text
                j += 1
            raise SystemExit("step %r in job %r has no run block"
                             % (step, job))
        i += 1
    raise SystemExit("no step %r in job %r of %s" % (step, job, workflow))


# ---------------------------------------------------------------------------
# A throwaway remote and its clones.
# ---------------------------------------------------------------------------

def find_bash():
    if os.name != "nt":
        return shutil.which("bash")
    # NOT shutil.which on Windows: System32\bash.exe is WSL, whose git and
    # python are another machine's.
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
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "hunt", "GIT_AUTHOR_EMAIL": "hunt@invalid",
    "GIT_COMMITTER_NAME": "hunt", "GIT_COMMITTER_EMAIL": "hunt@invalid",
})


def sh(cwd, *args):
    run = subprocess.run(list(args), cwd=cwd, env=ENV,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    if run.returncode != 0:
        raise RuntimeError("%s failed in %s:\n%s"
                           % (" ".join(args), cwd, run.stdout))
    return run.stdout


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_json(path, obj):
    write(path, json.dumps(obj, indent=1, sort_keys=True) + "\n")


def read_json_at(repo, ref, path):
    return json.loads(sh(repo, "git", "show", "%s:%s" % (ref, path)))


# The stand-in for tools/rebuild_catalogue.sh: the catalogue is a FUNCTION of
# the indexes in the working tree. That is the real one's contract too, and it
# is why "ours-wins" on catalogue.json is only sound if every index it reads
# is ours as well.
STUB_REBUILD = r'''#!/usr/bin/env bash
set -eo pipefail
python - "$@" <<'PY'
import glob, hashlib, json, os, sys
out = sys.argv[2]
tro = json.load(open("tro/index.json"))["packs"][0]
feeds = []
for path in sorted(glob.glob("published/*/*.json")):
    raw = open(path, "rb").read()
    feeds.append({"id": path, "file": path.replace(os.sep, "/"),
                  "sha256": hashlib.sha256(raw).hexdigest(),
                  "bytes": len(raw)})
cat = {"schema": 2, "baseUrl": "", "tro": {"id": tro["id"],
       "sha256": tro["sha256"], "generated": tro["generated"]},
       "conditions": {"feeds": feeds}}
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
with open(out, "w", newline="\n") as h:
    json.dump(cat, h, indent=1, sort_keys=True)
    h.write("\n")
PY
'''


def tro_index(sha):
    return {"generated": "2026-09-06", "packs": [{
        "id": "gb-tro", "kind": "tro", "sha256": sha, "bytes": 10,
        "generated": "2026-09-06",
        "file": "https://example.invalid/releases/download/tro/gb-tro.tbpack"}]}


def seed(tmp):
    """A remote holding a published state: order pack OLD, one feed."""
    remote = os.path.join(tmp, "remote.git")
    sh(tmp, "git", "init", "-q", "--bare", "-b", "main", remote)
    seed_dir = os.path.join(tmp, "seed")
    sh(tmp, "git", "init", "-q", "-b", "main", seed_dir)
    write(os.path.join(seed_dir, ".gitignore"), "dist/\n")
    write(os.path.join(seed_dir, "tools", "rebuild_catalogue.sh"),
          STUB_REBUILD)
    for name in ("verify_catalogue.py", "validate_catalogue.py"):
        write(os.path.join(seed_dir, "tools", name),
              "import sys\nsys.exit(0)\n")
    write_json(os.path.join(seed_dir, "tro", "index.json"), tro_index(OLD))
    write_json(os.path.join(seed_dir, "published", "wet", "south-west.json"),
               {"region": "south-west", "as_of": "2026-10-01T18:00:00Z",
                "stations": {"1": 0.1}})
    write_json(os.path.join(seed_dir, "manifest.json"),
               {"generated": "2026-09-26T18:40:10Z",
                "packages": [{"id": "gb-south-west", "laneCount": 100}]})
    # Lane-refresh state, for scenario C.
    write(os.path.join(seed_dir, "packages", "gb-south-west.tbpack"), "v1\n")
    write(os.path.join(seed_dir, "containers", "ways-south-west.tbmap"),
          "v1\n")
    write_json(os.path.join(seed_dir, "containers", "manifest.json"),
               {"containers": []})
    write_json(os.path.join(seed_dir, "changes", "index.json"),
               {"changesets": []})
    write(os.path.join(seed_dir, "trips", "gb.tbtrips"), "trips\n")
    sh(seed_dir, BASH, "tools/rebuild_catalogue.sh", "manifest.json", "catalogue.json")
    sh(seed_dir, "git", "add", "-A")
    sh(seed_dir, "git", "commit", "-q", "-m", "published")
    sh(seed_dir, "git", "remote", "add", "origin", remote)
    sh(seed_dir, "git", "push", "-q", "-u", "origin", "main")
    return remote


def clone(tmp, remote, name):
    path = os.path.join(tmp, name)
    sh(tmp, "git", "clone", "-q", remote, path)
    return path


def traffic_orders_publishes_new(tmp, remote):
    """Another workflow's run lands on main: order pack NEW, with its
    catalogue rebuilt by the recipe on top of it."""
    other = clone(tmp, remote, "tro-run")
    write_json(os.path.join(other, "tro", "index.json"), tro_index(NEW))
    sh(other, BASH, "tools/rebuild_catalogue.sh", "manifest.json",
       "catalogue.json")
    sh(other, "git", "add", "-A")
    sh(other, "git", "commit", "-q", "-m", "Traffic orders: data cut")
    sh(other, "git", "push", "-q")


def conditions_publishes(tmp, remote):
    """The conditions job lands on main: a feed moved, nothing else."""
    other = clone(tmp, remote, "conditions-run")
    write_json(os.path.join(other, "published", "wet", "south-west.json"),
               {"region": "south-west", "as_of": "2026-10-02T00:00:00Z",
                "stations": {"1": 2.4}})
    sh(other, BASH, "tools/rebuild_catalogue.sh", "manifest.json",
       "catalogue.json")
    sh(other, "git", "add", "-A")
    sh(other, "git", "commit", "-q", "-m", "Conditions: rain and river levels")
    sh(other, "git", "push", "-q")


def run_step(repo, script):
    path = os.path.join(repo, "..", os.path.basename(repo) + "-step.sh")
    write(path, script)
    run = subprocess.run([BASH, "--noprofile", "--norc", "-eo", "pipefail",
                          os.path.abspath(path)], cwd=repo, env=ENV,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    return run.returncode, run.stdout


def published_state(tmp, remote):
    look = clone(tmp, remote, "look-%d" % len(os.listdir(tmp)))
    return (read_json_at(look, "HEAD", "tro/index.json")["packs"][0]["sha256"],
            read_json_at(look, "HEAD", "catalogue.json")["tro"]["sha256"])


# ---------------------------------------------------------------------------
# The scenarios.
# ---------------------------------------------------------------------------

def scenario_a(tmp):
    remote = seed(tmp)
    job = clone(tmp, remote, "tro-job")
    # The job's build: a new pack, uploaded over the release asset by "Publish
    # the pack" (stable name, --clobber), and the index that names it.
    served = NEW
    write_json(os.path.join(job, "tro", "index.json"), tro_index(NEW))
    # "Rebuild the catalogue" ran before the commit step.
    sh(job, BASH, "tools/rebuild_catalogue.sh", "manifest.json",
       "dist/catalogue.json")
    # Meanwhile, another workflow pushed.
    conditions_publishes(tmp, remote)
    code, log = run_step(job, step_script("traffic-orders.yml", "build",
                                          "Commit the index and the catalogue"))
    index_sha, cat_sha = published_state(tmp, remote)
    return {
        "name": "A. traffic-orders: push race after the release asset was "
                "replaced",
        "exit": code, "log": log,
        "facts": [
            ("PREMISE the step ran to an exit", code is not None),
            ("tro/index.json names the served pack", index_sha == served),
            ("catalogue.json names the served pack", cat_sha == served),
        ],
        "detail": "served %s.., index says %s.., catalogue says %s.., step "
                  "exited %d" % (served[:6], index_sha[:6], cat_sha[:6], code),
    }


def scenario_b(tmp):
    remote = seed(tmp)
    job = clone(tmp, remote, "conditions-job")
    # The job's feeds.
    write_json(os.path.join(job, "published", "wet", "south-west.json"),
               {"region": "south-west", "as_of": "2026-10-02T00:00:00Z",
                "stations": {"1": 3.1}})
    # Meanwhile the traffic-orders job published a new pack.
    traffic_orders_publishes_new(tmp, remote)
    code, log = run_step(job, step_script("refresh-data.yml", "conditions",
                                          "Publish"))
    index_sha, cat_sha = published_state(tmp, remote)
    return {
        "name": "B. conditions Publish re-anchors over a traffic-orders "
                "publish",
        "exit": code, "log": log,
        "facts": [
            ("PREMISE the step published (exit 0)", code == 0),
            ("PREMISE the order index on main is the new one",
             index_sha == NEW),
            ("catalogue.json names the order pack the index and the release "
             "serve", cat_sha == index_sha),
        ],
        "detail": "index says %s.., catalogue says %s.., step exited %d"
                  % (index_sha[:6], cat_sha[:6], code),
    }


def scenario_c(tmp):
    remote = seed(tmp)
    job = clone(tmp, remote, "refresh-job")
    # The job's build, in dist/ as build_packages / build_containers leave it.
    write(os.path.join(job, "dist", "packages", "gb-south-west.tbpack"),
          "v2\n")
    write(os.path.join(job, "dist", "containers", "ways-south-west.tbmap"),
          "v2\n")
    write_json(os.path.join(job, "dist", "containers", "manifest.json"),
               {"containers": [{"id": "ways-south-west"}]})
    write_json(os.path.join(job, "dist", "manifest.json"),
               {"generated": "2026-10-02T03:30:00Z",
                "packages": [{"id": "gb-south-west", "laneCount": 101}]})
    # "Build the catalogue" ran early in the job.
    sh(job, BASH, "tools/rebuild_catalogue.sh", "dist/manifest.json",
       "dist/catalogue.json")
    traffic_orders_publishes_new(tmp, remote)
    code, log = run_step(job, step_script("refresh-data.yml", "refresh",
                                          "Publish"))
    index_sha, cat_sha = published_state(tmp, remote)
    return {
        "name": "C. lane refresh Publish re-anchors over a traffic-orders "
                "publish",
        "exit": code, "log": log,
        "facts": [
            ("PREMISE the step published (exit 0)", code == 0),
            ("PREMISE the order index on main is the new one",
             index_sha == NEW),
            ("catalogue.json names the order pack the index and the release "
             "serve", cat_sha == index_sha),
        ],
        "detail": "index says %s.., catalogue says %s.., step exited %d"
                  % (index_sha[:6], cat_sha[:6], code),
    }


def main():
    if not BASH or not shutil.which("git"):
        print("BLIND: bash and git are needed to run the workflow steps")
        return 2
    failed = 0
    for scenario in (scenario_a, scenario_b, scenario_c):
        tmp = tempfile.mkdtemp(prefix="dp-race-")
        try:
            result = scenario(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(result["name"])
        bad = False
        for label, ok in result["facts"]:
            print("  %s  %s" % ("ok  " if ok else "FAIL", label))
            if not ok:
                bad = True
                if label.startswith("PREMISE"):
                    print(result["log"])
        print("  -> %s" % result["detail"])
        failed += bad
    print("\n%d of 3 scenarios publish a catalogue out of step with the bytes "
          "served" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # a broken harness is not a proven defect
        print("HARNESS ERROR (not a verdict): %r" % (e,))
        sys.exit(3)

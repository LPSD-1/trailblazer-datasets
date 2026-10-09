#!/usr/bin/env python3
"""An imagery area's alarm must be raised however its run ends, and must not
stay open for ever once nothing can close it.

    python tools/hunt_data_pipeline_satellite_alarm_lifecycle.py

Exit 0: every case holds. Exit 1: a defect below is back. Exit 2: bash is not
available (BLIND). Exit 3: the harness could not reach a verdict (PREMISE),
which is not a pass.

Named hunt_*, not test_*, for the reason tools/hunt_data_pipeline_satellite_
retry.py gives: the lane refresh runs every tools/test_*.py, and a fault in
the IMAGERY workflow must not stop LANE data publishing. satellite.yml runs
it itself, in "Check the alarm wiring", before it fetches anything.

WHAT WAS WRONG (1): A CANCELLED OR TIMED-OUT RUN RAISED NOTHING. "Raise an
issue if this run failed" was `if: failure()`. A job that reaches its
timeout-minutes (350) is CANCELLED, not failed, and so is one somebody stops
by hand; `failure()` is false for both. The step that says "riders cannot
download this area's imagery" when the packs were uploaded and never
recorded did not run - and a run timed out inside Publish's upload is
exactly that case. Nothing was raised and the next run started over.

WHAT WAS WRONG (2): A WITHDRAWN AREA'S ALARM NEVER CLOSED. "Stand the alarm
down if this worked" closes an area's alarm only on a run that RECORDED that
area. The planner (tools/satellite_plan.py) only ever picks an area the
catalogue lists with lanes, so once an area is withdrawn - merged into a
neighbour, dropped from the catalogue - no run will record it again and its
alarm stays open for ever: the "alarm nobody can switch off" this workflow's
own comments say people learn to scroll past.

HOW IT IS SHOWN. The real steps are cut out of the workflow and run by bash
(-e, pipefail, as the runner does) with `gh` replaced by a fake issue
tracker that answers `--json ...,comments` in gh's real shape, in a scratch
workspace holding a fixture catalogue.json and the real tools/satellite_
plan.py. The real `if:` of each step is evaluated under each job status.

  (1) Under a CANCELLED job the failure step must run and must not be
      skipped; under SUCCESS it must not; the stand-down must not run under
      a cancelled job. Run under cancelled after the upload, it must raise
      the checksum alarm; run under cancelled before it, an alarm that says
      it was cancelled or timed out.
  (2) Alarms are raised for a withdrawn area (gone from the catalogue), an
      area still listed but no longer planned (no lanes), and Wales (planned,
      not yet recorded). A green run that found nothing due must close the
      withdrawn one, leave Wales alone, and leave the unplanned one OPEN with
      exactly one comment saying no run will close it (its packs are still
      served). A second green run must not comment again. An unreadable or
      empty catalogue must close nothing.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(os.environ.get("HUNT_WORKFLOWS") or
                        os.path.join(ROOT, ".github", "workflows"),
                        "satellite.yml")
PLANNER = os.path.join(ROOT, "tools", "satellite_plan.py")

FAIL_STEP = "Raise an issue if this run failed"
STAND_STEP = "Stand the alarm down if this worked"


class Premise(Exception):
    """The harness could not reach a verdict; not a pass, not a defect."""


def find_bash():
    if os.name != "nt":
        return shutil.which("bash")
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


def indent(line):
    return len(line) - len(line.lstrip())


def step(name):
    """(if, env, run lines) of the imagery job's step `name`, dedented."""
    lines = open(WORKFLOW, encoding="utf-8").read().replace("\r\n", "\n") \
        .split("\n")
    for i, line in enumerate(lines):
        if line.strip() != "- name: %s" % name:
            continue
        step_indent = indent(line)
        cond, env, run = "", {}, None
        for j in range(i + 1, len(lines)):
            l = lines[j]
            if l.strip() and indent(l) <= step_indent:
                break
            if indent(l) == step_indent + 2 and l.strip().startswith("if:"):
                cond = l.strip()[3:].strip()
            if l.strip() == "env:":
                for e in lines[j + 1:]:
                    if e.strip() and indent(e) <= indent(l):
                        break
                    if e.strip():
                        key, _, value = e.strip().partition(":")
                        env[key.strip()] = value.strip()
            if l.strip() == "run: |":
                body = []
                for b in lines[j + 1:]:
                    if b.strip() and indent(b) <= indent(l):
                        break
                    body.append(b)
                while body and not body[-1].strip():
                    body.pop()
                cut = min(indent(b) for b in body if b.strip())
                run = [b[cut:] if b.strip() else "" for b in body]
        if run is None:
            raise Premise("step %r has no run block in %s" % (name, WORKFLOW))
        return cond, env, run
    raise Premise("no step %r in %s" % (name, WORKFLOW))


def runs_under(cond, status):
    """Would a step with `if: cond` run in a job whose status is `status`
    ('success', 'failure' or 'cancelled')? GitHub's status functions, and its
    rule that an `if:` naming none of them is `success() && (...)`. Anything
    else in the expression is refused rather than guessed at."""
    expr = cond.strip()
    m = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expr)
    if m:
        expr = m.group(1)
    if not expr:
        expr = "success()"
    values = {"success()": status == "success",
              "failure()": status == "failure",
              "cancelled()": status == "cancelled",
              "always()": True}
    if not any(f in expr for f in values):
        expr = "success() && (%s)" % expr
    py = expr
    for f, v in values.items():
        py = py.replace(f, " %s " % v)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    if re.sub(r"\b(True|False|and|or|not)\b|[()\s]", "", py):
        raise Premise("cannot evaluate `if: %s` here" % cond)
    return bool(eval(py, {"__builtins__": {}}, {}))


def evaluate(text, context):
    """Every ${{ expr }} replaced as Actions would, unknowns as ''."""
    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}",
                  lambda m: context.get(m.group(1).strip(), ""), text)


# A fake `gh` keeping issues in a JSON file. Comments come back the way the
# real `gh issue list --json comments` gives them: objects with a `body`.
FAKE_GH = r'''
import json, sys
store = sys.argv[1]
args = sys.argv[2:]
issues = json.load(open(store))
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
def save():
    json.dump(issues, open(store, "w"))
if args[:2] == ["issue", "list"]:
    rows = [i for i in issues if i["state"] == opt("--state", "open")
            and (opt("--label") is None or opt("--label") in i["labels"])]
    rows.sort(key=lambda i: -i["number"])
    fields = (opt("--json") or "number").split(",")
    out = []
    for i in rows:
        row = {}
        for f in fields:
            if f == "comments":
                row[f] = [{"body": c, "author": {"login": "bot"}}
                          for c in i["comments"]]
            else:
                row[f] = i[f]
        out.append(row)
    if opt("--jq") is not None:
        print("fake gh: unsupported --jq")
        sys.exit(9)
    print(json.dumps(out))
elif args[:2] == ["issue", "create"]:
    issues.append({"number": len(issues) + 1, "state": "open",
                   "title": opt("--title"), "body": opt("--body"),
                   "labels": [opt("--label")], "comments": []})
    save()
elif args[:2] == ["issue", "comment"]:
    [i for i in issues if i["number"] == int(args[2])][0]["comments"].append(
        opt("--body"))
    save()
elif args[:2] == ["issue", "close"]:
    issue = [i for i in issues if i["number"] == int(args[2])][0]
    issue["state"] = "closed"
    if opt("--comment"):
        issue["comments"].append(opt("--comment"))
    save()
elif args[:2] == ["label", "create"]:
    pass
else:
    print("fake gh: unsupported %r" % args)
    sys.exit(9)
'''


def area(area_id, lanes=True):
    packs = [{"id": "%s-lanes" % area_id, "kind": "lanes"}] if lanes else \
        [{"id": "%s-routing" % area_id, "kind": "routing"}]
    return {"id": area_id, "label": area_id,
            "bounds": {"west": -4.0, "south": 51.0, "east": -3.0,
                       "north": 52.0},
            "packs": packs}


# Wales and the North publish lanes and are planned. `gb-isle` is still
# listed but publishes no lanes, so the planner never picks it again. And
# `gb-oldarea` is not in the catalogue at all: withdrawn.
CATALOGUE = {"continents": [{"countries": [{"code": "GB", "label": "GB",
             "areas": [area("gb-wales"), area("gb-north"),
                       area("gb-isle", lanes=False)]}]}]}


class Workspace:
    def __init__(self, bash, catalogue):
        self.bash = bash
        self.dir = tempfile.mkdtemp(prefix="hunt-sat-lifecycle-")
        os.makedirs(os.path.join(self.dir, "tools"))
        shutil.copy(PLANNER, os.path.join(self.dir, "tools"))
        self.fake = os.path.join(self.dir, "fake_gh.py")
        self.store = os.path.join(self.dir, "issues.json")
        with open(self.fake, "w") as f:
            f.write(FAKE_GH)
        with open(self.store, "w") as f:
            json.dump([], f)
        self.set_catalogue(catalogue)

    def set_catalogue(self, catalogue):
        path = os.path.join(self.dir, "catalogue.json")
        if catalogue is None:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w", encoding="utf-8") as f:
            if isinstance(catalogue, str):
                f.write(catalogue)
            else:
                json.dump(catalogue, f)

    def issues(self):
        with open(self.store) as f:
            return json.load(f)

    def run(self, name, outputs, prelude_extra="", ok_codes=(0,)):
        """The real step `name`, after a job with these outputs and status."""
        _, env_raw, script = step(name)
        context = {
            "secrets.GITHUB_TOKEN": "token",
            "github.server_url": "https://github.example",
            "github.repository": "owner/repo",
            "github.run_id": outputs["run"],
            "job.status": outputs.get("status", ""),
            "steps.plan.outputs.id": outputs.get("id", ""),
            "steps.plan.outputs.area": outputs.get("area", ""),
            "steps.plan.outputs.work": outputs.get("work", ""),
            "steps.publish.outputs.uploaded": outputs.get("uploaded", ""),
            "steps.publish.outputs.recorded": outputs.get("recorded", ""),
            "steps.plan.outputs.plan": outputs.get("plan", ""),
            "steps.fetch.outputs.blocked": outputs.get("blocked", ""),
            "steps.remember.outputs.remembered":
                outputs.get("remembered", ""),
        }
        env = dict(os.environ)
        self.output = os.path.join(self.dir, "github_output")
        env["GITHUB_OUTPUT"] = self.output
        for key, value in env_raw.items():
            env[key] = evaluate(value, context)
        py = sys.executable.replace("\\", "/")
        # Python on Windows ends its lines CRLF; the runner's end LF. Strip
        # the CR so `read` here sees what it sees on ubuntu.
        prelude = ('python() { "%s" "$@" | tr -d "\\r"; }\n'
                   'gh() { "%s" "%s" "%s" "$@"; }\n'
                   % (py, py, self.fake.replace("\\", "/"),
                      self.store.replace("\\", "/")))
        body = evaluate("\n".join(script), context)
        run = subprocess.run([self.bash, "--noprofile", "--norc", "-eo",
                              "pipefail", "-c",
                              prelude + prelude_extra + body],
                             cwd=self.dir, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             universal_newlines=True)
        if "fake gh: unsupported" in run.stdout:
            raise Premise("%r asked the fake gh for something it does not "
                          "model:\n%s" % (name, run.stdout))
        if run.returncode not in ok_codes:
            raise Premise("%r exited %d:\n%s" % (name, run.returncode,
                                                 run.stdout))
        self.returncode = run.returncode
        return run.stdout

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def about(issues, key, state=None):
    return [i for i in issues if ("Area: `%s`" % key) in (i["body"] or "")
            and (state is None or i["state"] == state)]


def check_cancelled(bash, problems):
    fail_if, _, _ = step(FAIL_STEP)
    stand_if, _, _ = step(STAND_STEP)
    print("failure step `if: %s`; stand-down `if: %s`" % (fail_if, stand_if))
    for status, want in (("failure", True), ("cancelled", True),
                         ("success", False)):
        got = runs_under(fail_if, status)
        print("  job %-9s -> failure step runs: %s" % (status, got))
        if got != want:
            problems.append("under a %s job the failure step %s, so %s"
                            % (status, "does not run" if want else "runs",
                               "a run that timed out or was cancelled "
                               "raises no alarm" if want else
                               "a green run raises an alarm"))
    if runs_under(stand_if, "cancelled") or runs_under(stand_if, "failure"):
        problems.append("the stand-down runs on a job that did not succeed")
    if not runs_under(stand_if, "success"):
        raise Premise("the stand-down does not run on a green job")

    ws = Workspace(bash, CATALOGUE)
    try:
        # Timed out inside Publish's upload: packs replaced, nothing recorded.
        ws.run(FAIL_STEP, {"run": "11", "status": "cancelled",
                           "id": "gb-wales-satellite", "area": "gb-wales",
                           "work": "true", "uploaded": "true"})
        # Cancelled while still fetching.
        ws.run(FAIL_STEP, {"run": "12", "status": "cancelled",
                           "id": "gb-north-satellite", "area": "gb-north",
                           "work": "true"})
        issues = ws.issues()
    finally:
        ws.close()
    wales = about(issues, "gb-wales-satellite", "open")
    north = about(issues, "gb-north-satellite", "open")
    if not wales or not north:
        raise Premise("the failure step, run, raised no alarm: %s" % issues)
    print("  cancelled after upload: %s" % wales[0]["title"])
    print("  cancelled before:       %s" % north[0]["title"])
    if "checksum" not in wales[0]["title"]:
        problems.append("a run cancelled after its upload did not raise the "
                        "checksum alarm: %r" % wales[0]["title"])
    # The TITLE, not the body: the body's list of causes mentions timing
    # out whatever happened, so it would pass with the status never read.
    said = north[0]["title"].lower()
    if "cancelled" not in said and "timed out" not in said:
        problems.append("a cancelled run's alarm does not say it was "
                        "cancelled or timed out: %r" % north[0]["title"])

    ws = Workspace(bash, CATALOGUE)
    try:
        ws.run(FAIL_STEP, {"run": "13", "status": "failure",
                           "id": "gb-north-satellite", "area": "gb-north",
                           "work": "true"})
        failed = about(ws.issues(), "gb-north-satellite", "open")
    finally:
        ws.close()
    if not failed:
        raise Premise("a failed run raised no alarm")
    print("  failed before:          %s" % failed[0]["title"])
    if "cancelled" in failed[0]["title"].lower():
        problems.append("a FAILED run's alarm says it was cancelled: %r"
                        % failed[0]["title"])


def check_withdrawn(bash, problems):
    ws = Workspace(bash, CATALOGUE)
    try:
        for n, (key, uploaded) in enumerate((("gb-oldarea-satellite", "true"),
                                             ("gb-isle-satellite", "true"),
                                             ("gb-wales-satellite", ""))):
            ws.run(FAIL_STEP, {"run": str(20 + n), "status": "failure",
                               "id": key, "area": key[:-10], "work": "true",
                               "uploaded": uploaded})
        issues = ws.issues()
        keys = ("gb-oldarea-satellite", "gb-isle-satellite",
                "gb-wales-satellite")
        if [len(about(issues, k, "open")) for k in keys] != [1, 1, 1]:
            raise Premise("expected one open alarm for each of %s: %s"
                          % (keys, issues))

        # An unreadable, then an empty, catalogue must close nothing.
        for label, cat in (("missing", None), ("unreadable", "{not json"),
                           ("empty", {"continents": []}),
                           # Areas listed, none with lanes: a lane build
                           # that lost (or renamed) its lane kind. Not one
                           # area is planned, so this is the same broken
                           # build and must close nothing either.
                           ("lane-less", {"continents": [{"countries": [
                               {"code": "GB", "label": "GB", "areas": [
                                   area("gb-wales", lanes=False),
                                   area("gb-north", lanes=False),
                                   area("gb-isle", lanes=False)]}]}]})):
            ws.set_catalogue(cat)
            ws.run(STAND_STEP, {"run": "30", "status": "success",
                                "work": "false"})
            still = [k for k in keys if about(ws.issues(), k, "open")]
            said = sum(len(i["comments"]) for i in ws.issues())
            print("  %s catalogue: open %s, %d comment" % (label, still, said))
            if len(still) != 3:
                problems.append("a green run with a %s catalogue closed an "
                                "area alarm: open now %s" % (label, still))
            if said:
                problems.append("a green run with a %s catalogue commented "
                                "on an area alarm" % label)
        ws.set_catalogue(CATALOGUE)

        # Two green runs that found nothing due.
        for run in ("31", "32"):
            out = ws.run(STAND_STEP, {"run": run, "status": "success",
                                      "work": "false"})
            issues = ws.issues()
            old = about(issues, "gb-oldarea-satellite")[0]
            isle = about(issues, "gb-isle-satellite")[0]
            wales = about(issues, "gb-wales-satellite")[0]
            print("  green run %s: withdrawn %s, unplanned %s (%d comment), "
                  "Wales %s (%d comment)" % (run, old["state"], isle["state"],
                                             len(isle["comments"]),
                                             wales["state"],
                                             len(wales["comments"])))
            if old["state"] != "closed":
                problems.append("run %s: the alarm for gb-oldarea, which the "
                                "catalogue no longer lists, is still open; no "
                                "run will ever record it" % run)
            if isle["state"] != "open":
                problems.append("run %s: the alarm for gb-isle was closed "
                                "though its packs are still served" % run)
            elif len(isle["comments"]) != 1:
                problems.append("run %s: the alarm for gb-isle, which no run "
                                "will close, has %d comments, not one saying "
                                "so" % (run, len(isle["comments"])))
            if wales["state"] != "open" or wales["comments"]:
                problems.append("run %s: the planned Wales alarm was touched "
                                "by a run that did not record Wales" % run)
        isle = about(ws.issues(), "gb-isle-satellite")[0]["comments"]
        gone = about(ws.issues(), "gb-oldarea-satellite")[0]["comments"]
        if isle:
            print("  note on gb-isle: %s" % isle[0])
            if "`gb-isle`" not in isle[0] or \
                    "`gb-isle-satellite`" not in isle[0]:
                problems.append("the note on the unplanned alarm does not "
                                "name its area and key: %r" % isle[0])
        if gone:
            print("  closing gb-oldarea: %s" % gone[-1])
            if "Withdrawn" not in gone[-1] or "`gb-oldarea`" not in gone[-1]:
                problems.append("the withdrawn alarm was closed without "
                                "saying why: %r" % gone[-1])

        # Wales is recorded: its own alarm closes as before.
        ws.run(STAND_STEP, {"run": "33", "status": "success",
                            "id": "gb-wales-satellite", "area": "gb-wales",
                            "work": "true", "uploaded": "true",
                            "recorded": "true"})
        if about(ws.issues(), "gb-wales-satellite", "open"):
            problems.append("Wales' own recorded run did not close its alarm")
    finally:
        ws.close()


# --- (3) the staged tiles survive a run that does not end green -------------
#
# WHAT WAS WRONG (3): a run that EOX stopped, that failed or that timed out
# threw its tiles away. actions/cache saves only on success (`post-if:
# success()`), so the next run of that area fetched every tile again from a
# service that had just refused us. The save is now its own step, and it
# runs however the job ends.

def uses_steps():
    """[{name, uses, if, with}] for every step of the workflow, in order."""
    lines = open(WORKFLOW, encoding="utf-8").read().replace("\r\n", "\n") \
        .split("\n")
    steps, cur, item_indent, in_with = [], None, None, None
    for line in lines:
        bare = line.strip()
        if not bare or bare.startswith("#"):
            continue
        m = re.match(r"(\s*)- (name|uses):\s*(.*)$", line)
        if m and (item_indent is None or len(m.group(1)) == item_indent):
            item_indent = len(m.group(1))
            cur = {"name": "", "id": "", "uses": "", "if": "", "with": {}}
            steps.append(cur)
            cur[m.group(2)] = m.group(3).strip()
            in_with = None
            continue
        if cur is None:
            continue
        if indent(line) <= item_indent:
            cur, in_with = None, None
            continue
        if in_with is not None and indent(line) > in_with:
            key, _, value = bare.partition(":")
            cur["with"][key.strip()] = value.strip()
            continue
        in_with = None
        if indent(line) == item_indent + 2:
            key, _, value = bare.partition(":")
            if key in ("name", "id", "uses", "if"):
                cur[key] = value.strip()
            elif key == "with":
                in_with = indent(line)
    return steps


def runs_with(cond, status, outputs):
    """runs_under, with each `steps.<id>.<field> == / != '<v>'` settled from
    `outputs` (a step that did not run has every field '')."""
    def settle(m):
        got = outputs.get(m.group(1), "")
        same = got == m.group(3)
        return " %s " % (same if m.group(2) == "==" else not same)
    expr = re.sub(r"(steps\.[\w-]+\.[\w.-]+)\s*(==|!=)\s*'([^']*)'",
                  settle, cond)
    return runs_under(expr, status)


def check_cache(problems):
    steps = uses_steps()
    cache = [s for s in steps if s["uses"].startswith("actions/cache")]
    both = [s["name"] for s in cache if s["uses"].startswith("actions/cache@")]
    if both:
        problems.append("%s uses actions/cache, which saves only when the "
                        "job succeeds: a refused or timed-out run's tiles are "
                        "fetched again" % both)
    restore = [s for s in cache if s["uses"].startswith("actions/cache/restore@")]
    save = [s for s in cache if s["uses"].startswith("actions/cache/save@")]
    if len(restore) != 1 or len(save) != 1:
        problems.append("expected one actions/cache/restore and one "
                        "actions/cache/save step, found %s"
                        % [(s["name"], s["uses"]) for s in cache])
        return
    restore, save = restore[0], save[0]
    names = [s["name"] for s in steps]
    print("  save `if: %s`, key %s" % (save["if"], save["with"].get("key")))
    if "Fetch and package" not in names or \
            names.index(save["name"]) < names.index("Fetch and package"):
        problems.append("the cache is saved before the fetch")
    working = {"steps.plan.outputs.work": "true"}
    for status in ("failure", "cancelled"):
        if not runs_with(save["if"], status, working):
            problems.append("under a %s job the staged tiles are not saved, "
                            "so the next run fetches them again" % status)
    if runs_with(save["if"], "success", {"steps.plan.outputs.work": "false"}):
        problems.append("a run with nothing due saves an empty cache entry")
    cleared = dict(working, **{"steps.clear.outcome": "success"})
    if runs_with(save["if"], "success", cleared):
        problems.append("the cache is saved after the staged tiles were "
                        "cleared: an empty entry the next rebuild resumes")
    key = save["with"].get("key", "")
    prefix = restore["with"].get("restore-keys", "")
    # A cache key is written once and never replaced, so a second save under
    # the same key is dropped: the key has to change every run.
    if "github.run_id" not in key or not prefix or \
            not key.startswith(prefix):
        problems.append("the save key %r is not unique to the run under the "
                        "restore prefix %r, so a resumed area's new tiles "
                        "are never saved" % (key, prefix))
    # The restore asks for this run's own key, which never exists yet, so
    # it always takes the newest entry by prefix rather than an older one
    # under a fixed name.
    if restore["with"].get("key") != key:
        problems.append("the restore key %r is not the save key %r"
                        % (restore["with"].get("key"), key))
    # The `if:`s above and the alarm read these ids; without them every
    # `steps.<id>` is empty and nothing is saved, remembered or said.
    ids = {s["name"]: s["id"] for s in steps}
    for name, want in (("Fetch and package", "fetch"),
                       ("Clear the staged tiles", "clear"),
                       ("Remember the refusal", "remember")):
        if ids.get(name) != want:
            problems.append("step %r has id %r, not %r"
                            % (name, ids.get(name), want))
    if save["with"].get("path") != restore["with"].get("path"):
        problems.append("the cache saves %r and restores %r"
                        % (save["with"].get("path"),
                           restore["with"].get("path")))


# --- (4) a refusal is remembered, and the alarm says until when -------------
#
# WHAT WAS WRONG (4): EOX's refusal was forgotten as soon as the run ended.
# build_satellite.py stopped the area and exited 3, and twelve hours later the
# planner picked the same area and asked again. The build now writes the
# refusal to satellite/blocks.json (--block-out), "Remember the refusal"
# commits it, the planner skips the area until it runs out, and the alarm
# says until when.

REMEMBER_STEP = "Remember the refusal"
FETCH_STEP = "Fetch and package"
UNTIL = "2026-10-16T02:41:00Z"


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=hunt", "-c",
                           "user.email=hunt@example.invalid",
                           "-c", "init.defaultBranch=main"] + list(args),
                          cwd=cwd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT,
                          universal_newlines=True).stdout


def write_blocks(where, status=403):
    os.makedirs(os.path.join(where, "satellite"), exist_ok=True)
    with open(os.path.join(where, "satellite", "blocks.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump({"hosts": {"tiles.maps.eox.at": {
            "at": "2026-10-09T02:41:00Z", "area": "gb-wales-satellite",
            "status": status,
            "retry_after": None, "until": UNTIL,
            "reason": "HTTP %s for z14/1/2" % status}}}, f)


def check_refusal(bash, problems):
    plan = json.dumps({"work": True, "id": "gb-wales-satellite",
                       "label": "Satellite - gb-wales", "area": "gb-wales",
                       "max_zoom": 14, "bbox": [-4.0, 51.0, -3.0, 52.0]})
    blocked = {"run": "40", "status": "failure", "id": "gb-wales-satellite",
               "area": "gb-wales", "work": "true", "plan": plan}

    # The fetch step: the build's exit 3 fails the step AND says blocked.
    # Under bash -e a bare `rc=$?` after the build is never reached.
    ws = Workspace(bash, CATALOGUE)
    try:
        stub = ('python() { case "$1" in tools/build_satellite.py) '
                'return 3;; esac; command python "$@" | tr -d "\\r"; }\n')
        ws.run(FETCH_STEP, blocked, prelude_extra=stub, ok_codes=(0, 3, 1))
        rc = ws.returncode
        said = open(ws.output).read() if os.path.exists(ws.output) else ""
        # And a build that succeeds passes the step and says nothing.
        if os.path.exists(ws.output):
            os.remove(ws.output)
        ws.run(FETCH_STEP, blocked, prelude_extra=stub.replace(
            "return 3", "return 0"), ok_codes=(0, 1, 2, 3))
        ok_rc = ws.returncode
        ok_said = (open(ws.output).read() if os.path.exists(ws.output)
                   else "")
    finally:
        ws.close()
    if ok_rc != 0 or "blocked=" in ok_said:
        problems.append("a build that succeeded left the fetch step at exit "
                        "%d with outputs %r" % (ok_rc, ok_said.strip()))
    print("  build exit 3: step exit %d, outputs %r" % (rc, said.strip()))
    if rc == 0:
        problems.append("the fetch step passed although the build was "
                        "refused")
    if "blocked=true" not in said.split("\n"):
        problems.append("a refused build does not set the fetch step's "
                        "`blocked` output, so nothing remembers it")

    try:
        remember_if, _, _ = step(REMEMBER_STEP)
    except Premise:
        problems.append("there is no %r step: a refusal is forgotten and the "
                        "area is fetched again 12 hours later" % REMEMBER_STEP)
        remember_if = None
    if remember_if is not None:
        ran = {"steps.fetch.outputs.blocked": "true"}
        if not runs_with(remember_if, "failure", ran):
            problems.append("%r does not run on the failed job a refusal "
                            "makes" % REMEMBER_STEP)
        if runs_with(remember_if, "success", {}):
            problems.append("%r runs on a run that was not refused"
                            % REMEMBER_STEP)
        check_remember_commits(bash, problems, blocked)

    # The alarm: when the area comes due again, and that nothing retries.
    ws = Workspace(bash, CATALOGUE)
    try:
        write_blocks(ws.dir)
        ws.run(FAIL_STEP, dict(blocked, blocked="true", remembered="true"))
        told = about(ws.issues(), "gb-wales-satellite", "open")
        ws2 = Workspace(bash, CATALOGUE)
        try:
            write_blocks(ws2.dir)
            ws2.run(FAIL_STEP, dict(blocked, run="41", blocked="true"))
            lost = about(ws2.issues(), "gb-wales-satellite", "open")
        finally:
            ws2.close()
    finally:
        ws.close()
    if not told or not lost:
        raise Premise("a refused run raised no alarm")
    print("  refused: %s" % told[0]["title"])
    body = told[0]["body"]
    if "refused" not in told[0]["title"].lower():
        problems.append("a refused run's alarm does not say EOX refused us: "
                        "%r" % told[0]["title"])
    if "2026-10-16" not in body or "403" not in body or (
            "tiles.maps.eox.at" not in body):
        problems.append("a refused run's alarm does not say when the area "
                        "comes due again, or why: %r" % body[:400])
    if "saved however a run ends" not in body:
        problems.append("the alarm does not say the staged tiles were kept "
                        "however the run ended")
    if "Disable this workflow" not in lost[0]["body"]:
        problems.append("a refusal that could not be recorded does not ask "
                        "for the workflow to be disabled: %r"
                        % lost[0]["body"][:400])


def check_remember_commits(bash, problems, blocked):
    """Run the real step in a clone whose push loses a race once."""
    ws = Workspace(bash, CATALOGUE)
    remote = tempfile.mkdtemp(prefix="hunt-sat-remote-")
    other = tempfile.mkdtemp(prefix="hunt-sat-other-")
    try:
        git(remote, "init", "--bare", "-q", ".")
        git(ws.dir, "init", "-q", "-b", "main")
        git(ws.dir, "add", "-A")
        git(ws.dir, "commit", "-q", "-m", "start")
        git(ws.dir, "remote", "add", "origin", remote)
        git(ws.dir, "push", "-q", "-u", "origin", "main")
        git(other, "clone", "-q", remote, ".")
        with open(os.path.join(other, "elsewhere.txt"), "w") as f:
            f.write("another job's commit\n")
        git(other, "add", "elsewhere.txt")
        git(other, "commit", "-q", "-m", "meanwhile")
        git(other, "push", "-q", "origin", "main")
        write_blocks(ws.dir)
        ws.run(REMEMBER_STEP, dict(blocked, blocked="true"),
               prelude_extra="sleep() { :; }\n")
        said = open(ws.output).read() if os.path.exists(ws.output) else ""
        log = git(remote, "log", "--format=%s", "main")
        try:
            kept = git(remote, "show", "main:satellite/blocks.json")
        except subprocess.CalledProcessError:
            kept = ""
    except Premise as e:
        problems.append("%r failed: %s" % (REMEMBER_STEP, e))
        return
    finally:
        ws.close()
        shutil.rmtree(remote, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)
    print("  remembered: %s" % log.replace("\n", " | ").strip(" |"))
    if UNTIL not in kept or "meanwhile" not in log:
        problems.append("the refusal did not reach main past another job's "
                        "commit: log %r" % log)
    if "remembered=true" not in said.split("\n"):
        problems.append("%r does not say it remembered the refusal"
                        % REMEMBER_STEP)


def main():
    bash = find_bash()
    if not bash:
        print("BLIND: bash is needed to run the steps as the runner does")
        return 2
    problems = []
    # Nothing else runs this hunt: satellite.yml must, before it plans, or a
    # regression in the steps above ships with this file still green.
    lines = [l.strip() for l in open(WORKFLOW, encoding="utf-8")]
    me = "run: python tools/%s" % os.path.basename(__file__)
    if me not in lines or "- name: What is due?" not in lines or \
            lines.index(me) > lines.index("- name: What is due?"):
        problems.append("satellite.yml does not run this hunt before it "
                        "plans (`%s`)" % me)
    try:
        print("cancelled and timed-out runs:")
        check_cancelled(bash, problems)
        print("withdrawn areas:")
        check_withdrawn(bash, problems)
        print("the staged tiles:")
        check_cache(problems)
        print("a refusal:")
        check_refusal(bash, problems)
    except Premise as e:
        print("PREMISE  %s" % e)
        return 3
    for problem in problems:
        print("FAIL  %s" % problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

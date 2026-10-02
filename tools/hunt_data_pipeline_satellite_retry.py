#!/usr/bin/env python3
"""The imagery job must survive the push race it has a retry for, and its
alarm must stay up until the area it is about has been put right.

    python tools/hunt_data_pipeline_satellite_retry.py

Exit 0: both hold. Exit 1: a defect below is back. Exit 2: bash is not
available (BLIND). Exit 3: the harness could not reach a verdict (PREMISE).

Named hunt_*, not test_*, so the lane refresh's tools/test_*.py glob does not
pick it up: a fault in the IMAGERY workflow must not stop LANE data
publishing. The app's suite runs it instead
(test/hunt/data-pipeline/the_data_publishing_jobs_cannot_stop_silently_test.dart).

WHAT WAS WRONG (1): THE RETRY COULD NOT RUN. In .github/workflows/
satellite.yml, inside the `for attempt in 1 2 3` loop that exists for exactly
one case - another workflow pushed while this one ran for up to 350 minutes -
two commands had been collapsed onto one line with a two-character `\\n`
where a line continuation was:

    python tools/record_satellite.py \\n --entry dist/satellite/entry.json ...
    git commit -m "Satellite: ..." \\n -m "Sentinel-2 cloudless via EOX ..."

bash reads `\\n` as an escaped `n`, so record_satellite.py was handed a stray
positional `n` and argparse exited 2; under `bash -e` the step died there.

The packs had already been uploaded over the release assets
(`gh release upload satellite $PACKS --clobber`, names stable per area). So
every lost race left the area's served file with NEW bytes while
satellite/index.json and catalogue.json kept the OLD sha256, and every
rider's download of that area's imagery failed its checksum.

WHAT WAS WRONG (2): ANY GREEN RUN CLOSED THAT ALARM. The failure issue was
raised, then the next successful run of ANY other area - or a run that found
nothing due - closed every satellite issue ("Fixed: ... succeeded") while the
mismatch was still being served. A second area's failure only commented on
the first area's issue, so closing it hid both.

HOW IT IS SHOWN.
  (1) Both record/commit invocations are cut out of the real Publish step and
      handed to bash with `python` and `git` replaced by functions that print
      their argv; the retry's argv must equal the first attempt's. Then the
      retry's argv is given to the real record_satellite.py, in a scratch
      directory holding a fixture entry, which must record it (exit 0).
  (1b) The whole Publish step is run with every program stubbed and `git
      push` rejected 0, 1, 3 and 4 times. Up to three lost races it must
      record; when every push is refused it must FAIL, so the alarm rings.
      (Its loop once ended on a commit it never pushed, and went green.)
      Every one of those runs must say uploaded=true. A run whose record
      changes nothing ("Nothing changed.", first time or after a lost race)
      must finish green with recorded=true. A `gh release upload` that fails
      must fail the step and still leave uploaded=true behind.
  (2) The real "Raise an issue" and "Stand the alarm down" steps are cut out
      and run by bash, with `gh` replaced by a fake issue tracker, through a
      sequence of runs: Wales dies after uploading; Wales dies again; the
      North succeeds; a run finds nothing due; the North fails; Wales
      succeeds. The Wales alarm must say checksum (not "nothing was
      published"), stay ONE alarm through the second failure, and survive
      everything until Wales itself is recorded; the North's failure must not
      hide inside the Wales alarm, and must say nothing was published.
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
    """(env, run lines) of the imagery job's step called `name`, dedented.

    env maps each variable to its raw value, expressions unevaluated."""
    lines = open(WORKFLOW, encoding="utf-8").read().split("\n")
    for i, line in enumerate(lines):
        if line.strip() != "- name: %s" % name:
            continue
        step_indent = indent(line)
        env, run = {}, None
        for j in range(i + 1, len(lines)):
            l = lines[j]
            if l.strip() and indent(l) <= step_indent:
                break
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
        return env, run
    raise Premise("no step %r in %s" % (name, WORKFLOW))


def evaluate(text, context):
    """Every ${{ expr }} replaced as Actions would, unknowns as ''."""
    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}",
                  lambda m: context.get(m.group(1).strip(), ""), text)


def statements(script, head):
    """Every command starting with `head`, continuation lines joined."""
    out = []
    i = 0
    while i < len(script):
        line = script[i].strip()
        if line.startswith(head):
            text = line
            while text.endswith("\\") and i + 1 < len(script):
                i += 1
                text = text[:-1] + " " + script[i].strip()
            out.append(text)
        i += 1
    return out


def argv_of(bash, command):
    """What bash would hand the program, with every program a printer."""
    prelude = (
        "python() { printf '<%s>' \"$@\"; echo; }\n"
        "git() { printf '<%s>' \"$@\"; echo; }\n"
        "URL=https://example.invalid/releases/download/satellite/\n"
        "SIZE='12 MB'\n")
    command = evaluate(command, {"steps.plan.outputs.area": "gb-wales",
                                 "steps.plan.outputs.id":
                                     "gb-wales-satellite"})
    run = subprocess.run([bash, "--noprofile", "--norc", "-c",
                          prelude + command],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    return re.findall(r"<([^>]*)>", run.stdout)


def record_with(argv):
    """The real recorder, run on `argv` in a scratch directory that holds a
    fixture entry where the step expects one. It never touches this
    checkout's own satellite/index.json."""
    scratch = tempfile.mkdtemp(prefix="hunt-satellite-")
    try:
        os.makedirs(os.path.join(scratch, "dist", "satellite"))
        with open(os.path.join(scratch, "dist", "satellite", "entry.json"),
                  "w", encoding="utf-8") as f:
            json.dump([{"id": "gb-wales-satellite-high", "kind": "basemap",
                        "file": "gb-wales-satellite-high.pmtiles",
                        "sha256": "0" * 64, "bytes": 1048576}], f)
        run = subprocess.run([sys.executable,
                              os.path.join(ROOT, argv[0])] + argv[1:],
                             cwd=scratch, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             universal_newlines=True)
        index = os.path.join(scratch, "satellite", "index.json")
        recorded = []
        if os.path.exists(index):
            with open(index, encoding="utf-8") as f:
                recorded = [p.get("id") for p in json.load(f).get("packs", [])]
        return run.returncode, run.stdout, recorded
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def check_retry(bash, problems):
    _, script = step("Publish")
    for head, what in (("python tools/record_satellite.py",
                        "record the area in satellite/index.json"),
                       ("git commit", "commit the record")):
        found = statements(script, head)
        print("%s: %d invocation(s) in Publish" % (head, len(found)))
        if len(found) < 2:
            raise Premise("expected the first attempt and the retry for %r, "
                          "found %d" % (head, len(found)))
        first, retry = argv_of(bash, found[0]), argv_of(bash, found[1])
        print("  first  %s" % first)
        print("  retry  %s" % retry)
        if not first:
            raise Premise("the first %r tokenised to nothing" % head)
        if retry != first:
            problems.append("the retry does not %s the way the first attempt "
                            "does: bash hands it %s" % (what, retry))
        if head.startswith("python") and len(retry) > 1:
            code, said, recorded = record_with(retry)
            last = (said.strip().splitlines() or [""])[-1]
            print("  record_satellite.py with the retry's argv exits %d: %s"
                  % (code, last))
            if code != 0 or "gb-wales-satellite-high" not in recorded:
                problems.append("record_satellite.py does not record with "
                                "the retry's argv (exit %d, %s), so under "
                                "bash -e the Publish step dies after the "
                                "--clobber upload" % (code, last))


def publish(bash, body, lost=0, unchanged_from=None, upload_fails=False):
    """Run the real Publish step `body` under bash -e with every program
    stubbed. `git push` is rejected the first `lost` times; `git diff
    --cached --quiet` says "nothing staged" (0) from its `unchanged_from`th
    call on (never, when None); `gh release upload` fails when
    `upload_fails`. Returns (exit, GITHUB_OUTPUT text, pushes, diffs,
    uploads tried, stdout)."""
    scratch = tempfile.mkdtemp(prefix="hunt-satellite-push-")
    try:
        def path(name):
            return os.path.join(scratch, name).replace("\\", "/")
        out, pushes, diffs, uploads = (path("out"), path("pushes"),
                                       path("diffs"), path("uploads"))
        open(out, "w").close()
        prelude = (
            'export GITHUB_OUTPUT="%(out)s"\n'
            'bump() { n=$(( $(cat "$1" 2>/dev/null || echo 0) + 1 )); '
            'echo "$n" > "$1"; echo "$n"; }\n'
            'gh() {\n'
            '  if [ "$1" = release ] && [ "$2" = upload ]; then\n'
            '    bump "%(uploads)s" >/dev/null\n'
            '    return %(upload_rc)d\n'
            '  fi\n'
            '}\n'
            'ls() { echo dist/satellite/gb-wales-satellite.pmtiles; }\n'
            'python() { :; }\n'
            'bash() { :; }\n'
            'mv() { :; }\n'
            'sleep() { :; }\n'
            'git() {\n'
            '  case "$1" in\n'
            '    push) n=$(bump "%(pushes)s"); [ "$n" -gt %(lost)d ] ;;\n'
            '    diff) n=$(bump "%(diffs)s")\n'
            '      if [ "$n" -ge %(unchanged)d ]; then echo clean > "%(staged)s"\n'
            '      else echo dirty > "%(staged)s"; return 1; fi ;;\n'
            # As real git does: committing with nothing staged exits 1, so a
            # "Nothing changed." branch that forgets its `exit 0` dies here
            # rather than falling through to a push the stub would accept.
            '    commit) [ "$(cat "%(staged)s" 2>/dev/null)" != clean ] ;;\n'
            '    *) : ;;\n'
            '  esac\n'
            '}\n' % {"out": out, "pushes": pushes, "diffs": diffs,
                     "staged": path("staged"),
                     "uploads": uploads, "lost": lost,
                     "upload_rc": 1 if upload_fails else 0,
                     # A call count no run reaches stands for "never".
                     "unchanged": unchanged_from or 1000000})
        run = subprocess.run([bash, "--noprofile", "--norc", "-eo",
                              "pipefail", "-c", prelude + body],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             universal_newlines=True)
        said = open(out).read()

        def count(p):
            return int(open(p).read().strip()) if os.path.exists(p) else 0
        return (run.returncode, said, count(pushes), count(diffs),
                count(uploads), run.stdout)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def check_push_loop(bash, problems):
    """The real Publish step, every program stubbed, with `git push`
    rejected the first `lost` times. A run that never got its record pushed
    must fail the step; one that did must say `recorded=true`. Every run got
    as far as the upload, so every run must say `uploaded=true`: that is what
    turns the alarm from "nothing was published" into "does not match its
    checksums"."""
    _, script = step("Publish")
    body = evaluate("\n".join(script), {"steps.plan.outputs.id":
                                            "gb-wales-satellite",
                                        "steps.plan.outputs.area":
                                            "gb-wales"})
    for lost in (0, 1, 3, 4):
        code, said, pushes, _, uploads, stdout = publish(bash, body, lost)
        recorded = "recorded=true" in said
        uploaded = "uploaded=true" in said
        print("  %d push(es) rejected: %d pushes tried, step exits %d, "
              "uploaded=%s, recorded=%s"
              % (lost, pushes, code, uploaded, recorded))
        if pushes == 0 or uploads == 0:
            raise Premise("the stubbed Publish step never reached `gh "
                          "release upload` and `git push` (%d upload(s), %d "
                          "push(es)):\n%s" % (uploads, pushes, stdout))
        if not uploaded:
            problems.append("with %d lost push race(s) the Publish step "
                            "replaced the packs without saying uploaded=true, "
                            "so its alarm would claim nothing was published"
                            % lost)
        if lost <= 3 and (code != 0 or not recorded):
            problems.append("with %d lost push race(s) the Publish step did "
                            "not record (exit %d, recorded=%s)"
                            % (lost, code, recorded))
        if lost > 3 and (code == 0 or recorded):
            problems.append("with every push rejected the Publish step "
                            "finished GREEN (exit %d) after replacing the "
                            "packs, so no alarm says their checksums are "
                            "wrong" % code)

    # The record is already what main holds: "Nothing changed." That run
    # must still finish green AND say recorded=true, or the stand-down never
    # closes this area's alarm. Likewise after a lost race, when the re-record
    # on top of main finds someone else already recorded it.
    for lost, unchanged_from, what in (
            (0, 1, "the first record changes nothing"),
            (1, 2, "after a lost race, main already holds the record")):
        code, said, pushes, diffs, _, stdout = publish(
            bash, body, lost, unchanged_from=unchanged_from)
        recorded = "recorded=true" in said
        uploaded = "uploaded=true" in said
        print("  %s: %d diff(s), %d push(es), step exits %d, uploaded=%s, "
              "recorded=%s" % (what, diffs, pushes, code, uploaded, recorded))
        if diffs < unchanged_from:
            raise Premise("the stubbed Publish step never asked `git diff` "
                          "%d time(s), so %r was not exercised:\n%s"
                          % (unchanged_from, what, stdout))
        if code != 0 or not recorded:
            problems.append("when %s the Publish step did not finish green "
                            "with recorded=true (exit %d, recorded=%s), so "
                            "this area's alarm is never stood down"
                            % (what, code, recorded))
        if not uploaded:
            problems.append("when %s the Publish step did not say "
                            "uploaded=true after replacing the packs" % what)

    # The upload itself dies part-way: --clobber may already have removed
    # served packs. The step must fail (so the alarm rings) and must already
    # have said uploaded=true (so the alarm says riders' downloads fail).
    code, said, pushes, _, uploads, stdout = publish(bash, body,
                                                     upload_fails=True)
    uploaded = "uploaded=true" in said
    recorded = "recorded=true" in said
    print("  the upload fails: %d upload(s) tried, step exits %d, "
          "uploaded=%s, recorded=%s" % (uploads, code, uploaded, recorded))
    if uploads == 0:
        raise Premise("the stubbed Publish step never reached `gh release "
                      "upload`:\n%s" % stdout)
    if code == 0 or recorded:
        problems.append("a failed `gh release upload` let the Publish step "
                        "carry on (exit %d, recorded=%s), so no alarm rings "
                        "over packs --clobber may have removed"
                        % (code, recorded))
    if not uploaded:
        problems.append("a failed `gh release upload` left no uploaded=true, "
                        "so the alarm says nothing was published while "
                        "--clobber may have removed the served packs")


# A fake `gh` holding issues in a JSON file: just the subcommands the two
# alarm steps use, including the `--jq` forms the old steps used, so the old
# steps are judged on their behaviour rather than on a harness gap.
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
    rows = [i for i in issues if i["state"] == "open"
            and (opt("--label") is None or opt("--label") in i["labels"])]
    rows.sort(key=lambda i: -i["number"])
    fields = (opt("--json") or "number").split(",")
    rows = [{f: i[f] for f in fields} for i in rows]
    jq = opt("--jq")
    if jq is None:
        print(json.dumps(rows))
    elif jq == ".[0].number":
        # gh prints a null scalar as nothing, not as "null".
        print(rows[0]["number"] if rows else "")
    elif jq == ".[].number":
        for r in rows:
            print(r["number"])
    else:
        print("fake gh: unsupported --jq %r" % jq)
        sys.exit(9)
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
    issue["comments"].append(opt("--comment"))
    save()
elif args[:2] == ["label", "create"]:
    pass
else:
    print("fake gh: unsupported %r" % args)
    sys.exit(9)
'''


def run_step(bash, name, outputs, scratch):
    """Run the real step `name` as the runner would after a job whose step
    outputs are `outputs`, against the fake tracker. Returns every issue."""
    env_raw, script = step(name)
    context = {
        "secrets.GITHUB_TOKEN": "token",
        "github.server_url": "https://github.example",
        "github.repository": "owner/repo",
        "github.run_id": outputs["run"],
        "steps.plan.outputs.id": outputs.get("id", ""),
        "steps.plan.outputs.area": outputs.get("area", ""),
        "steps.plan.outputs.work": outputs.get("work", ""),
        "steps.publish.outputs.uploaded": outputs.get("uploaded", ""),
        "steps.publish.outputs.recorded": outputs.get("recorded", ""),
    }
    env = dict(os.environ)
    for key, value in env_raw.items():
        env[key] = evaluate(value, context)
    py = sys.executable.replace("\\", "/")
    fake = os.path.join(scratch, "fake_gh.py").replace("\\", "/")
    store = os.path.join(scratch, "issues.json").replace("\\", "/")
    prelude = ('python() { "%s" "$@"; }\n'
               'gh() { "%s" "%s" "%s" "$@"; }\n' % (py, py, fake, store))
    body = evaluate("\n".join(script), context)
    # As the runner runs a step: bash -e with pipefail.
    run = subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail",
                          "-c", prelude + body],
                         env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True)
    if "fake gh: unsupported" in run.stdout:
        raise Premise("%r asked the fake gh for something it does not "
                      "model:\n%s" % (name, run.stdout))
    with open(store) as f:
        return json.load(f)


def check_alarms(bash, problems):
    scratch = tempfile.mkdtemp(prefix="hunt-satellite-alarm-")
    try:
        with open(os.path.join(scratch, "fake_gh.py"), "w") as f:
            f.write(FAKE_GH)
        with open(os.path.join(scratch, "issues.json"), "w") as f:
            json.dump([], f)
        wales = {"id": "gb-wales-satellite", "area": "gb-wales",
                 "work": "true"}
        north = {"id": "gb-north-satellite", "area": "gb-north",
                 "work": "true"}
        fail, ok = ("Raise an issue if this run failed",
                    "Stand the alarm down if this worked")

        def open_about(issues, area_id):
            return [i["number"] for i in issues if i["state"] == "open"
                    and ("`%s`" % area_id) in (i["body"] or "")]

        def flat(text):
            # Words only: the body is markdown wrapped at whatever width the
            # workflow is edited to, so "nothing was\npublished" says the
            # same thing as "nothing was published".
            return " ".join((text or "").lower().split())

        def words_of(issues, number):
            issue = [i for i in issues if i["number"] == number][0]
            return flat("%s %s" % (issue["title"], issue["body"]))

        # 1. Wales dies inside Publish, after --clobber replaced its packs.
        issues = run_step(bash, fail, dict(wales, run="1", uploaded="true"),
                          scratch)
        print("  1 Wales dies after its upload: alarm(s) about Wales %s"
              % open_about(issues, "gb-wales-satellite"))
        if not open_about(issues, "gb-wales-satellite"):
            raise Premise("the failure step raised no alarm about "
                          "gb-wales-satellite, so nothing after it means "
                          "anything: %s" % issues)
        # Its packs were replaced, so it must say downloads fail their
        # checksums - not reassure that nothing was published.
        words = words_of(issues, open_about(issues, "gb-wales-satellite")[0])
        print("    says checksum: %s, says nothing was published: %s"
              % ("checksum" in words, "nothing was published" in words))
        if "checksum" not in words or "nothing was published" in words:
            problems.append("Wales died AFTER replacing its packs, but its "
                            "alarm does not say they fail their checksums "
                            "(or says nothing was published): %r" % words)
        # 1b. Wales fails again, still after uploading: the same alarm hears
        # about it; a second one would split the history and the close.
        issues = run_step(bash, fail, dict(wales, run="1b", uploaded="true"),
                          scratch)
        about = open_about(issues, "gb-wales-satellite")
        print("  1b Wales fails again: alarm(s) about Wales %s" % about)
        if len(about) != 1:
            problems.append("a second Wales failure left %d open Wales "
                            "alarms, not exactly one" % len(about))
        elif not any("/runs/1b" in (c or "") for c in
                     [i for i in issues if i["number"] == about[0]][0]
                     ["comments"]):
            problems.append("a second Wales failure did not comment its run "
                            "on the open Wales alarm")
        # 2. The North succeeds, and records the North.
        issues = run_step(bash, ok, dict(north, run="2", uploaded="true",
                                         recorded="true"), scratch)
        print("  2 the North is recorded: alarm(s) about Wales %s"
              % open_about(issues, "gb-wales-satellite"))
        if not open_about(issues, "gb-wales-satellite"):
            problems.append("a green run of the NORTH closed the Wales alarm "
                            "while Wales' served packs still fail their "
                            "checksums")
        # 3. The next run finds nothing due.
        issues = run_step(bash, ok, {"run": "3", "work": "false"}, scratch)
        print("  3 nothing due: alarm(s) about Wales %s"
              % open_about(issues, "gb-wales-satellite"))
        if not open_about(issues, "gb-wales-satellite"):
            problems.append("a run that found NOTHING DUE closed the Wales "
                            "alarm while Wales' served packs still fail "
                            "their checksums")
        # 4. The North fails before uploading anything.
        issues = run_step(bash, fail, dict(north, run="4"), scratch)
        print("  4 the North fails: alarm(s) about the North %s"
              % open_about(issues, "gb-north-satellite"))
        if not open_about(issues, "gb-north-satellite"):
            problems.append("the North's failure raised no alarm of its own "
                            "(it was folded into another area's issue, which "
                            "that area's success will close)")
        else:
            # Nothing was uploaded: riders keep what they have, and the alarm
            # must say so rather than cry checksum.
            number = open_about(issues, "gb-north-satellite")[0]
            issue = [i for i in issues if i["number"] == number][0]
            body = flat(issue["body"])
            title = flat(issue["title"])
            print("    says nothing was published: %s, title says checksum: "
                  "%s" % ("nothing was published" in body,
                          "checksum" in title))
            if "nothing was published" not in body or "checksum" in title:
                problems.append("the North failed BEFORE uploading, but its "
                                "alarm does not say nothing was published "
                                "(or is titled as a checksum mismatch): "
                                "%r / %r" % (issue["title"], issue["body"]))
        # 5. Wales is rebuilt and recorded: its alarm, and only its, closes.
        issues = run_step(bash, ok, dict(wales, run="5", uploaded="true",
                                         recorded="true"), scratch)
        print("  5 Wales is recorded: Wales %s, North %s"
              % (open_about(issues, "gb-wales-satellite"),
                 open_about(issues, "gb-north-satellite")))
        if open_about(issues, "gb-wales-satellite"):
            problems.append("Wales' own recorded rebuild did not close the "
                            "Wales alarm")
        if not open_about(issues, "gb-north-satellite"):
            problems.append("Wales' success closed the NORTH's alarm")
        for i in issues:
            print("    #%d %s: %s" % (i["number"], i["state"], i["title"]))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main():
    bash = find_bash()
    if not bash:
        print("BLIND: bash is needed to read the step as bash reads it")
        return 2
    problems = []
    try:
        check_retry(bash, problems)
        print("lost push races:")
        check_push_loop(bash, problems)
        print("alarms:")
        check_alarms(bash, problems)
    except Premise as e:
        print("PREMISE  %s" % e)
        return 3
    for problem in problems:
        print("FAIL  %s" % problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

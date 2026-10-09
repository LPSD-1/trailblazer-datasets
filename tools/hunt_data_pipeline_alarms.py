#!/usr/bin/env python3
"""Ways a scheduled job fails, or half-publishes, and nobody is told.

    python tools/hunt_data_pipeline_alarms.py

Exit 0: none of the defects below is present. Exit 1: at least one is.
Exit 3: a check could not reach a verdict (PREMISE). Every FAIL line names
its check by number ("FAIL  check 2: ...").

Named hunt_* while it was a red proof, so the lane refresh's tools/test_*.py
glob would not pick it up. It is green now, and that glob DOES run it: it is
the premise of test_data_alarms_follow_up_fires_on_failure.py,
test_data_alarms_follow_up_refuses_what_never_runs.py,
test_data_alarms_premise_names_the_check.py and
test_data_alarms_check4_scope.py. Those need the whole hunt green
on the unmodified workflows, so ANY check below going red stops lane data
publishing at "Run every tool suite". Their PREMISE message names the red
check and quotes its FAIL line: that is the defect to fix, in
.github/workflows, not in the wrapper.

Read from the workflow files as text, job by job and step by step, because
the property is about the workflow and nothing else can be asked:

1. EVERY JOB THAT PUBLISHES RINGS AN ALARM WHEN IT FAILS. refresh-data.yml's
   `conditions` job commits the rain, river and forecast feeds (and the
   catalogue) four times a day and has no `if: failure()` step at all. It
   fails on "not one wet gauge reported anywhere", on a push lost three
   times, on the EA being down - and the feeds then stop moving with nobody
   told. Worse, the `refresh` job beside it closes every open data-refresh
   issue when IT succeeds, so the only issue a person might be reading says
   "Fixed" while the feeds are dead.

2. AN ASSET REPLACED UNDER ITS STABLE NAME IS FOLLOWED BY A COMMIT THAT CAN
   SURVIVE A RACE. mirror-routing.yml uploads every tile with `--clobber`
   (names stable: E0_N50.rd5 ...) and then makes ONE bare `git push`. Lost to
   any other workflow's commit, the tiles riders download are the new bytes
   and routing/index.json and catalogue.json keep the old sha256 for a week,
   and the issue it raises tells the owner "nothing was published ... riders
   are unaffected either way".

3. A DRY RUN DOES NOT STAND A REAL ALARM DOWN. refresh-data.yml's
   "Stand the alarm down if this worked" is `if: success()`, and under
   `dry_run` the Publish step is skipped and the job succeeds - so a
   rehearsal closes "Data refresh failed ... nothing was published" while
   nothing has still been published.

4. A STEP ALLOWED TO FAIL STILL RINGS. "Build the ride forecast feeds" is
   `continue-on-error: true` and only prints a ::warning:: - which nobody
   reads on a green scheduled run. MET Norway refusing us (a 403 for the
   User-Agent, say) stops every forecast for good: each phone shows "too old
   to use" after 12 hours, and the owner is never told. The trips step next
   to it shows the shape that works: an id, and a follow-up step that opens
   an issue on `steps.trips.outcome == 'failure'`.

   Check 4 guards what riders download, so it spares a job only when it is
   on NON_PUBLISHING_JOBS below AND none of its steps publishes (PUBLISHES).
   That list is reviewed by hand: a new job is held to check 4 until someone
   adds it. "Only jobs that git push" was rejected as the scope: four
   workflows publish release assets with `gh release upload` and push only
   by coincidence, so a release-only job would slip through.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.environ.get("HUNT_WORKFLOWS") or \
    os.path.join(ROOT, ".github", "workflows")

# (workflow, job) pairs whose continue-on-error steps check 4 does not hold
# to an issue, because the job publishes nothing riders download. Each entry
# says why; each is re-checked against PUBLISHES on every run.
NON_PUBLISHING_JOBS = {
    # Posts a status, a check and a comment on the pull request, and writes
    # a private ledger repository. Its soft steps fail closed: the step after
    # each one fails and reports on the pull request.
    ("independent-review.yml", "review"),
}

# Any of these in a step's run or uses means the job publishes.
PUBLISHES = re.compile(
    r"git push|gh release (upload|create|edit|delete)"
    r"|gh api[^\n]*/contents/|deploy-pages|upload-pages-artifact|gh-pages")


def jobs_of(path):
    """{job: [step dict(name, if, run)]} from the workflow text."""
    lines = open(path, encoding="utf-8").read().split("\n")
    jobs, job, step, in_jobs = {}, None, None, False
    i = 0
    while i < len(lines):
        line = lines[i]
        bare = line.strip()
        indent = len(line) - len(line.lstrip())
        if line.rstrip() == "jobs:":
            in_jobs = True
        elif in_jobs and indent == 2 and bare.endswith(":") \
                and not bare.startswith("#"):
            job = bare[:-1]
            jobs[job] = []
        elif job and bare.startswith("- name:") or \
                (job and bare.startswith("- uses:")):
            step = {"name": bare.split(":", 1)[1].strip(), "if": "",
                    "run": "", "indent": indent, "id": "", "soft": False,
                    "uses": bare.split(":", 1)[1].strip()
                    if bare.startswith("- uses:") else ""}
            jobs[job].append(step)
        elif step is not None and bare.startswith("if:") \
                and indent == step["indent"] + 2:
            step["if"] = bare[3:].strip()
        elif step is not None and bare.startswith("id:")                 and indent == step["indent"] + 2:
            step["id"] = bare[3:].strip()
        elif step is not None and bare.startswith("uses:")                 and indent == step["indent"] + 2:
            step["uses"] = bare[5:].strip()
        elif step is not None and bare == "continue-on-error: true"                 and indent == step["indent"] + 2:
            step["soft"] = True
        elif step is not None and bare == "run: |":
            body = []
            k = i + 1
            while k < len(lines):
                b = lines[k]
                if b.strip() and len(b) - len(b.lstrip()) <= indent:
                    break
                body.append(b)
                k += 1
            step["run"] = "\n".join(body)
            i = k
            continue
        elif step is not None and bare.startswith("run:"):
            step["run"] = bare[4:]
        i += 1
    return jobs


def fires_on_failure(cond, step_id):
    """Does the `if:` text `cond` run when step `step_id` has FAILED?

    Naming `steps.<id>.outcome` is not enough: `== 'cancelled'`, `==
    'success'` or `== 'skipped'` mention it and never run on the failure the
    follow-up exists to report, so the soft step goes orange, the run stays
    green and nobody is told. Accepted: `outcome == 'failure'` and its
    equivalent here `outcome != 'success'` (GitHub compares strings ignoring
    case; string literals in expressions are single-quoted). Refused as well:
    the comparison negated (`!(steps.x.outcome == 'failure')`), and
    `steps.<id>.conclusion`, which continue-on-error turns into 'success' on
    a failed step. tools/test_data_alarms_follow_up_fires_on_failure.py
    holds this to each of those.

    Also refused: the reference anywhere inside a negated group, however
    deeply bracketed (`!((...))`, `!(always() && steps.x.outcome ...)`), and
    an AND with `failure()` or `cancelled()`. continue-on-error keeps the job
    successful, so `failure() && steps.x.outcome == 'failure'` never runs.
    """
    ref = r"steps\.%s\.outcome" % re.escape(step_id)
    if re.search(r"!\s*" + ref, cond):
        return False
    for bang in re.finditer(r"!\s*\(", cond):
        depth, k = 0, bang.end() - 1
        while k < len(cond):
            depth += {"(": 1, ")": -1}.get(cond[k], 0)
            if depth == 0:
                break
            k += 1
        if re.search(ref, cond[bang.end():k]):
            return False
    if re.search(r"\b(failure|cancelled)\(\)\s*&&|&&\s*(failure|cancelled)"
                 r"\(\)", cond):
        return False
    return re.search(ref + r"\s*(==\s*'failure'|!=\s*'success')",
                     cond, re.I) is not None


def main():
    files = sorted(f for f in os.listdir(WORKFLOWS) if f.endswith(".yml"))
    all_jobs = {f: jobs_of(os.path.join(WORKFLOWS, f)) for f in files}
    problems = []
    publishing = 0

    # 1. A failure alarm in every job that publishes.
    for f, jobs in all_jobs.items():
        for job, steps in jobs.items():
            pushes = any("git push" in s["run"] for s in steps)
            if not pushes:
                continue
            publishing += 1
            alarm = [s for s in steps if "failure()" in s["if"]
                     and "gh issue" in s["run"]]
            print("%-20s %-11s publishes; failure alarm: %s"
                  % (f, job, "yes" if alarm else "NONE"))
            if not alarm:
                problems.append((1,
                    "%s job `%s` commits and pushes, and has no `if: "
                    "failure()` step raising an issue: when it fails, what "
                    "it publishes stops moving and nobody is told" % (f, job)))
    if publishing < 5:
        problems.append((1, "PREMISE: found %d publishing jobs; the five "
                         "workflows have six" % publishing))

    # 2. --clobber, then a push that cannot lose a race and give up.
    for f, jobs in all_jobs.items():
        for job, steps in jobs.items():
            for s in steps:
                run = s["run"]
                if not re.search(r"gh release upload[^\n]*--clobber", run):
                    continue
                after = run[re.search(r"gh release upload[^\n]*--clobber",
                                      run).end():]
                loop = re.search(r"for attempt in", after)
                bare = [m.start() for m in re.finditer(r"^\s*git push\s*$",
                                                       after, re.M)]
                unguarded = [p for p in bare if not loop or p < loop.start()]
                print("%-20s %-11s %-30s clobbers, then %d bare push(es) "
                      "outside a retry" % (f, job, s["name"], len(unguarded)))
                if unguarded:
                    problems.append((2,
                        "%s `%s` replaces release assets under their stable "
                        "names and then pushes the index once: a push lost "
                        "to another workflow leaves the served bytes new "
                        "and the published sha256 old" % (f, s["name"])))

    # 3. A rehearsal does not close a real alarm.
    refresh = all_jobs.get("refresh-data.yml", {}).get("refresh", [])
    stand = [s for s in refresh if s["name"].startswith("Stand the alarm")]
    publish = [s for s in refresh if s["name"] == "Publish"]
    if not stand or not publish:
        problems.append((3, "PREMISE: refresh-data.yml refresh job has no "
                         "stand-down or no Publish step"))
    elif "dry_run" in publish[0]["if"] and "dry_run" not in stand[0]["if"]:
        problems.append((3, "refresh-data.yml closes the data-refresh alarm "
                         "on a successful run whose Publish was skipped by "
                         "dry_run (stand-down `if: %s`)" % stand[0]["if"]))

    # 4. A step allowed to fail is still a failure somebody hears about.
    soft_seen = 0
    for f, jobs in all_jobs.items():
        for job, steps in jobs.items():
            spared = (f, job) in NON_PUBLISHING_JOBS and not any(
                PUBLISHES.search(s["run"] + "\n" + s["uses"]) for s in steps)
            for n, s in enumerate(steps):
                if not s["soft"]:
                    continue
                soft_seen += 1
                if spared:
                    print("%-20s %-11s %-30s continue-on-error; job does not "
                          "publish (reviewed)" % (f, job, s["name"][:30]))
                    continue
                told = s["id"] and any(
                    fires_on_failure(later["if"], s["id"])
                    and "gh issue" in later["run"] for later in steps[n + 1:])
                print("%-20s %-11s %-30s continue-on-error; issue on "
                      "failure: %s" % (f, job, s["name"][:30],
                                       "yes" if told else "NONE"))
                if not told:
                    problems.append((4,
                        "%s `%s` may fail without failing the job, and "
                        "nothing raises an issue when it does: the files it "
                        "writes age out on every phone and the owner is "
                        "never told" % (f, s["name"])))
    if not soft_seen:
        problems.append((4, "PREMISE: no continue-on-error step found at "
                         "all"))

    for n, p in problems:
        print("FAIL  check %d: %s" % (n, p))
    if any(p.startswith("PREMISE") for _, p in problems):
        return 3
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

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
test_data_alarms_premise_names_the_check.py,
test_data_alarms_check4_scope.py and test_data_alarms_parser_sweep.py.
Those need the whole hunt green
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
   on NON_PUBLISHING_JOBS below AND its text still hashes to the sha256 a
   reviewer pinned there: any edit re-arms the check until it is re-read
   and re-pinned. A new job is held to check 4 until someone lists it.
   Rejected scopes, each by an escape that was measured: "only jobs that
   git push" (four workflows publish with `gh release upload` and push only
   by coincidence), and a list of publishing verbs (19 ways round one were
   found, from `git -C . push` to a push inside a called script).
"""
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.environ.get("HUNT_WORKFLOWS") or \
    os.path.join(ROOT, ".github", "workflows")

# (workflow, job) -> sha256 of that job's pinned text (job_pin): the jobs
# whose continue-on-error steps check 4 does not hold to an issue, because
# a reviewer read the job and found it publishes nothing riders download.
# The pin covers the job and its workflow's top-level keys (on, env,
# permissions, defaults), so ANY edit to either re-arms check 4 until a
# reviewer reads the job again and re-pins it here. To re-pin, print the
# new value with
#   python -c "import sys; sys.path.insert(0, 'tools'); import hunt_data_pipeline_alarms as h; print(h.job_pin(open('.github/workflows/<file>', encoding='utf-8').read(), '<job>'))"
# Not pinned: scripts the job calls. They cannot publish without a token
# that can write this repository, and the job's permissions are pinned.
NON_PUBLISHING_JOBS = {
    # Posts a status, a check and a comment on the pull request, and writes
    # the private review-ledger repository with an app token that cannot
    # reach this one; GITHUB_TOKEN is contents: read. Its two soft steps
    # fail closed: the step after each one fails and reports on the PR.
    ("independent-review.yml", "review"):
        "36741548e7fe31ed80d8720a16328f31eeed2ee7faa4036a0854845ceb74847c",
}

KEY = re.compile(r"""^(['"]?)([A-Za-z0-9_.-]+)\1\s*:(?:\s+(.*))?$""")


def _indent(line):
    return len(line) - len(line.lstrip())


def _content(line):
    """A line that is neither blank nor only a comment."""
    b = line.strip()
    return bool(b) and not b.startswith("#")


def _scalar(v):
    """A one-line YAML value without its quotes or trailing comment."""
    v = v.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        return v[1:end] if end > 0 else v[1:]
    return re.sub(r"(^|\s)#.*$", "", v).strip()


def _key(bare):
    """(key, raw value) of a `key: value` line, or None."""
    m = KEY.match(bare)
    return (m.group(2), m.group(3) or "") if m else None


def _jobs(text):
    """(lines, [(job, first line, end)]) of the workflow's jobs section.

    A job's block runs from its header to the next job's header, less its
    trailing blank lines; comments inside it count as part of it."""
    lines = text.replace("\r\n", "\n").split("\n")
    start = next((i for i, l in enumerate(lines) if _content(l)
                  and _indent(l) == 0 and (_key(l.strip()) or ("",))[0]
                  == "jobs"), None)
    if start is None:
        return lines, []
    end = next((i for i in range(start + 1, len(lines)) if _content(lines[i])
                and _indent(lines[i]) == 0), len(lines))
    body = [i for i in range(start + 1, end) if _content(lines[i])]
    if not body:
        return lines, []
    ji = _indent(lines[body[0]])
    heads = [i for i in body if _indent(lines[i]) == ji]
    blocks = []
    for n, h in enumerate(heads):
        stop = heads[n + 1] if n + 1 < len(heads) else end
        while stop > h + 1 and not lines[stop - 1].strip():
            stop -= 1
        k = _key(lines[h].strip())
        blocks.append((k[0] if k else lines[h].strip(), h, stop))
    return lines, blocks


def _take(lines, i, end, rest, col, step):
    """Read the step key `rest` (at column col) on line i; return next i."""
    step.setdefault("kcol", col)
    k = i + 1
    body = []
    while k < end and (not lines[k].strip() or _indent(lines[k]) > col):
        body.append(lines[k])
        k += 1
    while body and not body[-1].strip():
        body.pop()
    kv = _key(rest)
    if not kv:
        # Fail closed: a step line that is no `key: value` the reader knows
        # (`? key`, a bare `{...}`) may set anything.
        step.setdefault("unread", i + 1)
        return k
    key, raw = kv
    v = _scalar(raw)
    if re.match(r"^[|>][-+0-9]*$", v):
        text = "\n".join(body)
    else:
        text = " ".join([v] + [b.strip() for b in body if b.strip()]).strip()
    if key == "run":
        step["run"] = text
    elif key == "if":
        step["if"] = " ".join(text.split())
    elif key in ("name", "id", "uses"):
        step[key] = text
    elif key == "continue-on-error":
        step["soft"] = text.strip().lower() != "false"
    return k


def _flow(lines, i, end, rest, step):
    """Read the flow-mapping step `- {k: v, ...}` that starts with `rest` on
    line i, through the line holding its closing brace; return next i.

    It splits at top-level commas only, so a quoted value or a nested `{}`
    or `[]` keeps its commas. A quote opens only where a key or value
    starts, so the apostrophe in a plain `echo it's` does not open one."""
    text, k, pos = rest, i + 1, 0
    items, cur, depth, quote, start = [], "", 0, "", True
    while True:
        while pos < len(text):
            ch = text[pos]
            pos += 1
            if quote:
                cur += ch
                if ch == quote and quote == "'" and text[pos:pos + 1] == "'":
                    cur += "'"
                    pos += 1
                elif ch == quote:
                    quote = ""
                elif ch == "\\" and quote == '"':
                    cur += text[pos:pos + 1]
                    pos += 1
            elif start and ch in "'\"":
                quote, start = ch, False
                cur += ch
            elif ch == "#" and text[pos - 2:pos - 1].isspace():
                pos = len(text)
            elif ch in "{[":
                depth += 1
                start = True
                cur += ch if depth > 1 else ""
            elif ch in "}]" and depth == 1:
                for item in items + [cur]:
                    if item.strip() and not _key(item.strip()):
                        step.setdefault("unread", i + 1)  # `"k":v`, `? k`
                    elif item.strip():
                        _take([item.strip()], 0, 1, item.strip(), -1, step)
                return k
            elif ch in "}]":
                depth -= 1
                cur += ch
            elif ch == "," and depth == 1:
                items.append(cur)
                cur, start = "", True
            else:
                cur += ch
                if ch == ":":
                    start = text[pos:pos + 1] in (" ", "")
                elif not ch.isspace():
                    start = False
        if k >= end:
            return k
        text, pos = " " + lines[k].strip(), 0
        k += 1


def _unread(at):
    """A stand-in step for line `at` (1-based) that the reader cannot read;
    check 4 fails on it, so an unknown form can never hide a soft step."""
    return {"name": "unreadable step at line %d" % at, "if": "", "run": "",
            "indent": 0, "id": "", "soft": False, "uses": "", "unread": at}


def _steps(lines, start, end):
    """The steps of the job whose block is lines[start:end]. A line the
    reader cannot place gives a step with `unread` set to its line."""
    i, ki = start + 1, None
    while i < end:
        if _content(lines[i]) and (_key(lines[i].strip()) or ("",))[0] \
                == "steps":
            if _scalar(_key(lines[i].strip())[1]):
                # `steps: [...]`, or any value on the key's own line.
                return [_unread(i + 1)]
            ki = _indent(lines[i])
            i += 1
            break
        i += 1
    steps, item, step = [], None, None
    while ki is not None and i < end:
        line = lines[i]
        bare = line.strip()
        ind = _indent(line)
        if not _content(line):
            i += 1
            continue
        if ind <= ki:
            break
        if (bare == "-" or bare.startswith("- ")) and \
                (item is None or ind == item):
            item = ind
            step = {"name": "", "if": "", "run": "", "indent": ind,
                    "id": "", "soft": False, "uses": ""}
            steps.append(step)
            rest = bare[1:].lstrip()
            if rest.startswith("{"):
                i = _flow(lines, i, end, rest, step)
            elif rest:
                i = _take(lines, i, end, rest, ind + len(bare) - len(rest),
                          step)
            else:
                i += 1
            continue
        if step is not None and ind == step.get("kcol", ind):
            i = _take(lines, i, end, bare, ind, step)
            continue
        steps.append(_unread(i + 1))  # e.g. `steps:` with `[` below it
        i += 1
    for n, s in enumerate(steps):
        s.pop("kcol", None)
        s["name"] = s["name"] or s["uses"] or \
            (s["run"].strip().split("\n") or [""])[0].strip() or \
            "step %d" % (n + 1)
    return steps


def jobs_of(path):
    """{job: [step dict(name, if, run, id, uses, soft)]} from the workflow.

    Read as text, without a YAML library (the lane refresh installs none).
    A job header may carry a comment; a step may start with any key
    (`- run:`, `- id:`, `- if:`, ...); a run may be `|`, `>`, with `-`/`+`
    and a comment, quoted, one line, or a plain line continued below. A
    step may also be one flow mapping, `- {name: x, run: y}`, on one line or
    several. tools/test_data_alarms_parser_sweep.py holds it to each of
    those."""
    with open(path, encoding="utf-8", newline="") as f:
        lines, blocks = _jobs(f.read())
    jobs = {job: _steps(lines, s, e) for job, s, e in blocks}
    for job, at in _unplaced(lines, blocks):
        jobs.setdefault(job, []).append(_unread(at))
    return jobs


def _unplaced(lines, blocks):
    """[(job, 1-based line)] above the steps that the reader cannot place,
    so check 4 fails closed on them: a top-level or job-level line that is
    no `key: value`, `jobs` or `steps` written any way but bare (`"jobs":{`,
    `'steps':`), a value on the `jobs:` line or a job's header line, and a
    job line indented less than the job's keys. Top-level ones go under the
    job name `(workflow)`."""
    bad = []
    for i, line in enumerate(lines):
        kv = _key(line.strip()) if _content(line) else ("", "")
        if _indent(line) == 0 and (not kv or kv[0] == "jobs" and (
                _scalar(kv[1]) or not line.startswith("jobs:"))):
            bad.append(("(workflow)", i + 1))
    for job, s, e in blocks:
        kv = _key(lines[s].strip())
        if not kv or _scalar(kv[1]):
            bad.append((job, s + 1))
            continue
        body = [i for i in range(s + 1, e) if _content(lines[i])]
        col = _indent(lines[body[0]]) if body else 0
        for i in body:
            kv = _key(lines[i].strip())
            if _indent(lines[i]) < col or _indent(lines[i]) == col and (
                    not kv or kv[0] == "steps"
                    and not lines[i].strip().startswith("steps:")):
                bad.append((job, i + 1))
    return bad


def pinned_lines(text, job):
    """Indices of the lines job_pin hashes: the whole workflow but the other
    jobs, so the job itself and the top-level keys it runs under."""
    lines, blocks = _jobs(text)
    if job not in [b[0] for b in blocks]:
        return None
    others = set()
    for name, s, e in blocks:
        if name != job:
            others.update(range(s, e))
    return [i for i in range(len(lines)) if i not in others]


def job_pin(text, job):
    """sha256 of the job's pinned text: line endings and blank lines aside,
    any change to it gives another value. None if there is no such job."""
    idx = pinned_lines(text, job)
    if idx is None:
        return None
    lines = text.replace("\r\n", "\n").split("\n")
    kept = "\n".join(lines[i] for i in idx if lines[i].strip())
    return hashlib.sha256(kept.encode("utf-8")).hexdigest()


def is_spared(file, job, text):
    """Is `job` in workflow `file` (with this text) the reviewed job?"""
    pin = NON_PUBLISHING_JOBS.get((file, job))
    return pin is not None and job_pin(text, job) == pin


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
    # GitHub runs both extensions; a copy saved as .yaml is still a workflow.
    files = sorted(f for f in os.listdir(WORKFLOWS)
                   if f.endswith((".yml", ".yaml")))
    all_jobs = {f: jobs_of(os.path.join(WORKFLOWS, f)) for f in files}
    texts = {}
    for f in files:
        with open(os.path.join(WORKFLOWS, f), encoding="utf-8",
                  newline="") as fh:
            texts[f] = fh.read()
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
            spared = is_spared(f, job, texts[f])
            listed = (f, job) in NON_PUBLISHING_JOBS
            for n, s in enumerate(steps):
                if s.get("unread"):
                    print("%-20s %-11s line %d: step the alarm cannot read"
                          % (f, job, s["unread"]))
                    problems.append((4,
                        "%s job `%s` line %d: step the alarm cannot read, so "
                        "nobody can tell whether it may fail unheard; write "
                        "it as `- key: value` lines" % (f, job, s["unread"])))
                    continue
                if not s["soft"]:
                    continue
                soft_seen += 1
                if spared:
                    print("%-20s %-11s %-30s continue-on-error; job does not "
                          "publish (reviewed, pinned)" % (f, job, s["name"][:30]))
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
                        "never told%s" % (f, s["name"], (
                            " (job `%s` is on NON_PUBLISHING_JOBS, but its "
                            "text no longer matches the pinned sha256: check "
                            "4 holds it again until a reviewer confirms it "
                            "still publishes nothing and re-pins it to %s)"
                            % (job, job_pin(texts[f], job))) if listed
                            else "")))
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

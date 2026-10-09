#!/usr/bin/env python3
"""The review ledger: every change that failed review, and every one under
review right now, by patch-id. It lives in a separate private repository.

    python tools/review_ledger.py patch-id DIFF          -> prints the id
    python tools/review_ledger.py check  --pid P --pr N --branch B
    python tools/review_ledger.py claim  --pid P --pr N --branch B --sha S
    python tools/review_ledger.py settle --pid P --pr N --branch B --sha S \
                                         --outcome failed|clear

  check and claim exit 0 when the change may be reviewed, and otherwise:
      3  this exact change has failed review before
      4  an identical change is under review right now
      5  this pull request or branch has failed twice in 7 days
      1  the ledger cannot be read or written (fails closed)

  LEDGER_REPO (owner/name) and LEDGER_TOKEN come from the environment.

WHY. A model's verdict is not deterministic, so trying again until it says
PASS is a way through any gate built on one. The workflow refuses re-runs
outright, and runs one review at a time. This file closes the other ways in:

  * the same change pushed again, as a new commit, branch or pull request:
    the key is `git patch-id --stable`, which is the same for the same
    change whatever carries it;
  * the same change in two pull requests at once: `claim` records it as
    in progress BEFORE any model is asked, so the second one is refused;
  * a trivial edit (new patch-id) re-rolled until it passes: after two
    failures for the same pull request or the same head branch within 7
    days, every further run is the owner's ("owner must review (repeated
    failures)") and no model is asked.

WHERE. A private repository, LPSD-1/trailblazer-review-ledger, written only
by the ledger app, which is installed on that repository alone: no token
that can write the ledger can touch the data repository, and nobody working
in the data repository can edit or delete the ledger. Failures are sharded
by the first two hex characters of the patch-id (`failed/3f.json`), so no
file nears the contents API's 1 MB limit; `recent.json` holds only the
last 7 days' failures, for the cap. A write that loses a race re-reads and
retries: the contents API refuses a stale `sha`.

The ledger never makes anything pass. Missing, unreadable or malformed, it
fails the review closed.
"""
import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

_ID = re.compile(r"^[0-9a-f]{40}$")
API = "https://api.github.com"

#: An in-progress claim older than this is from a run that died; ignore it.
STALE = datetime.timedelta(hours=2)
#: The repeated-failure cap: this many failures ...
CAP = 2
#: ... within this window, for one pull request or one head branch.
WINDOW = datetime.timedelta(days=7)

CLEAR, FAILED, BUSY, CAPPED, BROKEN = 0, 3, 4, 5, 1
REASONS = {
    FAILED: "This exact change already failed review",
    BUSY: "An identical change is under review now",
    CAPPED: "owner must review (repeated failures)",
    BROKEN: "The review ledger cannot be read or written",
}
RECENT = "recent.json"


def _ts(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def patch_id(diff_text):
    """`git patch-id --stable` of a diff, or None for an empty one."""
    r = subprocess.run(["git", "patch-id", "--stable"],
                       input=diff_text.encode("utf-8", errors="replace"),
                       capture_output=True)
    if r.returncode != 0:
        return None
    first = r.stdout.decode("ascii", errors="replace").split()
    return first[0] if first and _ID.match(first[0]) else None


def shard_path(pid):
    if not _ID.match(pid or ""):
        raise ValueError("bad patch id %r" % pid)
    return "failed/%s.json" % pid[:2]


def empty_shard():
    return {"version": 1, "entries": {}}


def empty_recent():
    return {"version": 1, "failures": []}


def valid_shard(data):
    if not (isinstance(data, dict) and isinstance(data.get("entries"), dict)):
        raise ValueError("not a ledger shard")
    return data


def valid_recent(data):
    if not (isinstance(data, dict) and isinstance(data.get("failures"), list)):
        raise ValueError("not a recent-failures file")
    return data


def state_of(shard, pid, now):
    """"failed", "busy" (a live in-progress claim) or "clear"."""
    entry = shard["entries"].get(pid)
    if not entry:
        return "clear"
    if entry.get("state") == "in-progress":
        try:
            if now - _parse_ts(entry["at"]) < STALE:
                return "busy"
        except (KeyError, TypeError, ValueError):
            return "busy"
        return "clear"
    return "failed"   # "failed", or anything unknown, counts against it


def recent_failures(recent, pr, branch, now):
    """How many failures in WINDOW were for this pull request or branch."""
    count = 0
    for f in recent["failures"]:
        try:
            if now - _parse_ts(f["at"]) > WINDOW:
                continue
        except (KeyError, TypeError, ValueError):
            pass   # an undated failure still counts
        if str(f.get("pr")) == str(pr) or (branch and f.get("branch") == branch):
            count += 1
    return count


def decide(shard, recent, pid, pr, branch, now):
    """-> CLEAR, FAILED, CAPPED or BUSY for a change about to be reviewed."""
    state = state_of(shard, pid, now)
    if state == "failed":
        return FAILED
    if recent_failures(recent, pr, branch, now) >= CAP:
        return CAPPED
    if state == "busy":
        return BUSY
    return CLEAR


def claimed(shard, pid, pr, branch, sha, now):
    out = {"version": 1, "entries": dict(shard["entries"])}
    out["entries"][pid] = {"state": "in-progress", "pr": int(pr),
                           "branch": branch, "sha": sha, "at": _ts(now)}
    return out


def settled(shard, pid, outcome, pr, branch, sha, now):
    """outcome "failed" marks the change failed for good; "clear" removes
    an in-progress claim. A recorded failure is never removed."""
    out = {"version": 1, "entries": dict(shard["entries"])}
    entry = out["entries"].get(pid)
    if outcome == "failed":
        if not entry or entry.get("state") != "failed":
            out["entries"][pid] = {"state": "failed", "pr": int(pr),
                                   "branch": branch, "sha": sha,
                                   "at": _ts(now)}
    elif entry and entry.get("state") == "in-progress":
        del out["entries"][pid]
    return out


def with_failure(recent, pid, pr, branch, now):
    """recent.json with this failure added and anything past WINDOW gone."""
    keep = []
    for f in recent["failures"]:
        try:
            if now - _parse_ts(f["at"]) > WINDOW:
                continue
        except (KeyError, TypeError, ValueError):
            pass
        keep.append(f)
    keep.append({"pid": pid, "pr": int(pr), "branch": branch, "at": _ts(now)})
    return {"version": 1, "failures": keep}


class Store:
    """The ledger repository, through the contents API."""

    def __init__(self, repo, token):
        if not repo or not token:
            raise ValueError("LEDGER_REPO and LEDGER_TOKEN are required")
        self.repo, self.token = repo, token

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            API + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "trailblazer-review-ledger"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")

    def check_reachable(self):
        self._call("GET", "/repos/%s" % self.repo)

    def get(self, path):
        """-> (parsed JSON or None when absent, blob sha or None)."""
        try:
            data = self._call("GET",
                              "/repos/%s/contents/%s" % (self.repo, path))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
            raise
        return json.loads(base64.b64decode(data["content"])), data["sha"]

    def put(self, path, value, sha, message):
        """False when the write lost a race (stale sha)."""
        body = {"message": message,
                "content": base64.b64encode(
                    (json.dumps(value, indent=1, sort_keys=True) + "\n")
                    .encode()).decode()}
        if sha:
            body["sha"] = sha
        try:
            self._call("PUT", "/repos/%s/contents/%s" % (self.repo, path),
                       body)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (409, 422):
                return False
            raise

    def update(self, path, change, empty, valid, message, tries=5):
        for attempt in range(tries):
            current, sha = self.get(path)
            current = valid(current) if current is not None else empty()
            new = change(current)
            if new == current:
                return True
            if self.put(path, new, sha, message):
                return True
            time.sleep(2 * (attempt + 1))
        return False


def _store():
    store = Store(os.environ.get("LEDGER_REPO"),
                  os.environ.get("LEDGER_TOKEN"))
    store.check_reachable()   # a missing repo or token is BROKEN, not empty
    return store


def _read(store, pid):
    shard, _ = store.get(shard_path(pid))
    recent, _ = store.get(RECENT)
    return (valid_shard(shard) if shard is not None else empty_shard(),
            valid_recent(recent) if recent is not None else empty_recent())


def run(a, store, now):
    """One check / claim / settle against a store. -> exit code."""
    shard_path(a.pid)
    if a.command in ("check", "claim"):
        code = decide(*_read(store, a.pid), a.pid, a.pr, a.branch, now)
        if code == CLEAR and a.command == "claim":
            ok = store.update(
                shard_path(a.pid),
                lambda s: claimed(s, a.pid, a.pr, a.branch, a.sha, now),
                empty_shard, valid_shard,
                "In review: PR #%s, %s" % (a.pr, a.pid))
            code = CLEAR if ok else BROKEN
        return code
    ok = store.update(
        shard_path(a.pid),
        lambda s: settled(s, a.pid, a.outcome, a.pr, a.branch, a.sha, now),
        empty_shard, valid_shard,
        "%s: PR #%s, %s" % (a.outcome, a.pr, a.pid))
    if ok and a.outcome == "failed":
        ok = store.update(
            RECENT, lambda r: with_failure(r, a.pid, a.pr, a.branch, now),
            empty_recent, valid_recent, "Failed: PR #%s, %s" % (a.pr, a.pid))
    return CLEAR if ok else BROKEN


def main(argv=None, store=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["patch-id"] and len(argv) == 2:
        with open(argv[1], encoding="utf-8", errors="replace") as fh:
            pid = patch_id(fh.read())
        if not pid:
            return BROKEN
        print(pid)
        return CLEAR
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("check", "claim", "settle"))
    ap.add_argument("--pid", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--sha", default="")
    ap.add_argument("--outcome", choices=("failed", "clear"))
    a = ap.parse_args(argv)
    if a.command == "settle" and not a.outcome:
        ap.error("settle needs --outcome")
    try:
        code = run(a, store or _store(), now_utc())
    except (urllib.error.URLError, ValueError, KeyError, TypeError,
            OSError) as e:
        sys.stderr.write("review ledger: %s\n" % e)
        code = BROKEN
    if code != CLEAR:
        print(REASONS[code])
    return code


if __name__ == "__main__":
    sys.exit(main())

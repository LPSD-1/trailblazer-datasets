#!/usr/bin/env python3
"""The review ledger: what failed review, what is under review, and whether
review is paused. It lives in a separate PUBLIC repository.

    python tools/review_ledger.py patch-id DIFF          -> prints the id
    python tools/review_ledger.py check  --pid P --pr N --branch B [--patch-only]
    python tools/review_ledger.py claim  --pid P --pr N --branch B
    python tools/review_ledger.py settle --pid P --pr N --branch B \
                                         --outcome pass|failed|error

  check and claim exit 0 when the change may be reviewed, and otherwise:
      3  this exact change has failed review (or its review never finished)
      4  an identical change is under review right now
      5  this pull request or branch has failed twice in 7 days
      6  review paused: 4 failures (claims included) in the last 24 hours
      7  review paused: 3 days running hit that cap; the owner must reset
      1  the ledger cannot be read or written (fails closed)
  --patch-only checks the patch-id alone (3 or 0), for the final check
  before success.

  LEDGER_REPO (owner/name) and LEDGER_TOKEN come from the environment.

WHY. A model's verdict is not deterministic, so trying again until it says
PASS is a way through any gate built on one. Every agent pushes as the
owner, so neither the pull request nor the branch is a reliable identity:
  * the same change again (new commit, branch or PR): the key is
    `git patch-id --stable`, the same whatever carries it;
  * two identical changes at once: refused while one is claimed;
  * one PR or branch re-rolling small edits: 2 failures in 7 days, then the
    owner's;
  * re-rolling through NEW branches and PRs: repo-wide, 4 failures in any
    24 hours pauses all review ("review paused: too many failures today"),
    and 3 consecutive UTC days that each reach 4, or 12 failures in any
    7 days, lock it until the owner commits `reset.json` to the ledger
    repository. A reset.json dated more than 5 minutes ahead is ignored.

A CLAIM IS A FAILURE UNTIL A PASS SETTLES IT. `claim` writes the change into
recent.json as a failure before any model is asked; only `settle --outcome
pass` removes it. A run that is cancelled, times out or crashes therefore
leaves a failure behind, and its claim, once 2 hours stale, marks the
patch-id failed for good. Cancelling a run cannot erase a FAIL.

WHAT IS STORED: patch-ids, pull request numbers, a 16-hex-digit hash of the
branch name (never the name), a state word, and UTC timestamps. Nothing
else: the repository is public, so that its ruleset is enforced on the free
plan (see docs/REVIEW-GATE-SETUP.md), and test_review_gate pins the schema.
Failures are sharded by the first two hex characters of the patch-id
(`failed/3f.json`), so no file nears the contents API's 1 MB limit;
recent.json holds only the last 7 days.

The ledger never makes anything pass. Missing, unreadable or malformed, it
fails the review closed.
"""
import argparse
import base64
import datetime
import hashlib
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

#: An unsettled claim older than this is from a run that never finished:
#: the change counts as FAILED.
STALE = datetime.timedelta(hours=2)
#: Per pull request or head branch: this many failures in WINDOW.
CAP = 2
WINDOW = datetime.timedelta(days=7)
#: Repo-wide: this many failures in any DAY pauses review ...
DAY_CAP = 4
DAY = datetime.timedelta(hours=24)
#: ... and this many consecutive UTC days at DAY_CAP locks it,
LOCK_DAYS = 3
#: ... as does this many failures in any rolling LOCK_SPAN, so a steady
#: pace just under the daily cap (3 a day) cannot run for ever.
LOCK_TOTAL = 12
LOCK_SPAN = datetime.timedelta(days=7)
#: A reset.json dated further ahead than this is refused: a reset set in
#: the future would disarm the lock in advance.
CLOCK_SKEW = datetime.timedelta(minutes=5)

CLEAR, BROKEN, FAILED, BUSY, CAPPED, PAUSED, LOCKED = 0, 1, 3, 4, 5, 6, 7
REASONS = {
    FAILED: "This exact change already failed review",
    BUSY: "An identical change is under review now",
    CAPPED: "owner must review (repeated failures)",
    PAUSED: "review paused: too many failures today",
    LOCKED: "review paused until the owner resets the ledger",
    BROKEN: "The review ledger cannot be read or written",
}
RECENT, LOCK, RESET = "recent.json", "lock.json", "reset.json"
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

#: The only fields the ledger ever writes, and their shapes.
SHARD_FIELDS = frozenset(("state", "pr", "branch_hash", "at"))
RECENT_FIELDS = frozenset(("pid", "state", "pr", "branch_hash", "at"))
STATES = frozenset(("in-progress", "failed", "claimed", "error"))


def _ts(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def _age(entry, now):
    """now - entry["at"], or None when the entry has no readable time."""
    try:
        return now - _parse_ts(entry["at"])
    except (KeyError, TypeError, ValueError):
        return None


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def branch_hash(branch):
    """What the ledger keeps instead of a branch name."""
    return hashlib.sha256(branch.encode("utf-8")).hexdigest()[:16]


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


def _stamp(data):
    """lock.json / reset.json -> their time, or None when absent."""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("not a lock or reset file")
    return _parse_ts(data["at"])


# ---- decisions -----------------------------------------------------------

def state_of(shard, pid, now):
    """"failed", "busy" (a live claim) or "clear". A claim older than STALE
    was never settled: the change counts as failed."""
    entry = shard["entries"].get(pid)
    if not entry:
        return "clear"
    if entry.get("state") == "in-progress":
        age = _age(entry, now)
        if age is not None and age < STALE:
            return "busy"
    return "failed"


def _counted(recent, now, within):
    """recent.json entries no older than `within` (undated ones count)."""
    out = []
    for f in recent["failures"]:
        age = _age(f, now)
        if age is None or age <= within:
            out.append(f)
    return out


def recent_failures(recent, pr, branch, now):
    """Failures (claims included) in WINDOW for this PR or branch."""
    bh = branch_hash(branch) if branch else None
    return sum(1 for f in _counted(recent, now, WINDOW)
               if str(f.get("pr")) == str(pr)
               or (bh and f.get("branch_hash") == bh))


def day_failures(recent, now):
    """Failures (claims included) repo-wide in the last 24 hours."""
    return len(_counted(recent, now, DAY))


def streak(recent, after):
    """True when LOCK_DAYS consecutive UTC dates each hold DAY_CAP or more
    failures recorded after `after` (the last reset)."""
    per_day = {}
    for f in recent["failures"]:
        try:
            when = _parse_ts(f["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if when > after:
            per_day[when.date()] = per_day.get(when.date(), 0) + 1
    full = {d for d, n in per_day.items() if n >= DAY_CAP}
    one = datetime.timedelta(days=1)
    return any(all(d - i * one in full for i in range(LOCK_DAYS))
               for d in full)


def rolling_total(recent, after):
    """True when LOCK_TOTAL or more failures recorded after `after` fall
    within any LOCK_SPAN."""
    times = []
    for f in recent["failures"]:
        try:
            when = _parse_ts(f["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if when > after:
            times.append(when)
    times.sort()
    return any(times[i + LOCK_TOTAL - 1] - times[i] <= LOCK_SPAN
               for i in range(len(times) - LOCK_TOTAL + 1))


def lock_due(recent, after):
    """Either lock rule: LOCK_DAYS capped days running, or LOCK_TOTAL
    failures in a LOCK_SPAN."""
    return streak(recent, after) or rolling_total(recent, after)


def reset_time(data, now):
    """reset.json -> its time, or None when absent or dated more than
    CLOCK_SKEW in the future (refused, and said so)."""
    when = _stamp(data)
    if when is not None and when > now + CLOCK_SKEW:
        sys.stderr.write("review ledger: reset.json is dated %s, in the "
                         "future; ignored\n" % _ts(when))
        return None
    return when


def locked(recent, lock, reset):
    """lock/reset: their times or None. Locked until a reset newer than
    the lock, and while a fresh lock rule holds."""
    after = reset or EPOCH
    return bool((lock and lock > after) or lock_due(recent, after))


def decide(shard, recent, pid, pr, branch, now, lock=None, reset=None):
    state = state_of(shard, pid, now)
    if state == "failed":
        return FAILED
    if locked(recent, lock, reset):
        return LOCKED
    if day_failures(recent, now) >= DAY_CAP:
        return PAUSED
    if recent_failures(recent, pr, branch, now) >= CAP:
        return CAPPED
    if state == "busy":
        return BUSY
    return CLEAR


# ---- changes -------------------------------------------------------------

def _prune(failures, now):
    return [f for f in failures if (_age(f, now) or datetime.timedelta(0))
            <= WINDOW]


def claimed_shard(shard, pid, pr, branch, now):
    out = {"version": 1, "entries": dict(shard["entries"])}
    out["entries"][pid] = {"state": "in-progress", "pr": int(pr),
                           "branch_hash": branch_hash(branch),
                           "at": _ts(now)}
    return out


def claimed_recent(recent, pid, pr, branch, now):
    """The claim, counted as a failure from this moment."""
    keep = _prune(recent["failures"], now)
    keep.append({"pid": pid, "state": "claimed", "pr": int(pr),
                 "branch_hash": branch_hash(branch), "at": _ts(now)})
    return {"version": 1, "failures": keep}


def settled_shard(shard, pid, outcome, pr, branch, now):
    """failed: the change is failed for good. pass or error: the claim is
    released. A recorded failure is never removed."""
    out = {"version": 1, "entries": dict(shard["entries"])}
    entry = out["entries"].get(pid)
    if outcome == "failed":
        if not entry or entry.get("state") != "failed":
            out["entries"][pid] = {"state": "failed", "pr": int(pr),
                                   "branch_hash": branch_hash(branch),
                                   "at": _ts(now)}
    elif entry and entry.get("state") == "in-progress":
        del out["entries"][pid]
    return out


def settled_recent(recent, pid, outcome, pr, branch, now):
    """pass removes this change's open claim; failed and error turn it into
    a failure that stays (adding one if the claim is missing)."""
    failures = _prune(recent["failures"], now)
    open_claims = [i for i, f in enumerate(failures)
                   if f.get("pid") == pid and f.get("state") == "claimed"]
    if outcome == "pass":
        if open_claims:
            del failures[open_claims[-1]]
    else:
        if open_claims:
            failures[open_claims[-1]] = dict(failures[open_claims[-1]],
                                             state=outcome)
        else:
            failures.append({"pid": pid, "state": outcome, "pr": int(pr),
                             "branch_hash": branch_hash(branch),
                             "at": _ts(now)})
    return {"version": 1, "failures": failures}


# ---- the repository ------------------------------------------------------

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

    def path_of(self, path):
        return "/repos/%s/contents/%s" % (self.repo, path)

    def get(self, path):
        """-> (parsed JSON or None when absent, blob sha or None)."""
        try:
            data = self._call("GET", self.path_of(path) + "?ref=main")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                e.close()
                return None, None
            raise
        return json.loads(base64.b64decode(data["content"])), data["sha"]

    def put(self, path, value, sha, message):
        """False when the write lost a race (stale sha)."""
        body = {"message": message, "branch": "main",
                "content": base64.b64encode(
                    (json.dumps(value, indent=1, sort_keys=True) + "\n")
                    .encode()).decode()}
        if sha:
            body["sha"] = sha
        try:
            self._call("PUT", self.path_of(path), body)
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


def _read(store, pid, now):
    shard, _ = store.get(shard_path(pid))
    recent, _ = store.get(RECENT)
    lock, _ = store.get(LOCK)
    reset, _ = store.get(RESET)
    return (valid_shard(shard) if shard is not None else empty_shard(),
            valid_recent(recent) if recent is not None else empty_recent(),
            _stamp(lock), reset_time(reset, now))


def _lock_if_streak(store, now):
    """Write lock.json when a streak since the last reset stands. It stays
    after recent.json forgets the days, until the owner's reset."""
    recent, _ = store.get(RECENT)
    recent = valid_recent(recent) if recent is not None else empty_recent()
    lock, lock_sha = store.get(LOCK)
    reset, _ = store.get(RESET)
    after = reset_time(reset, now) or EPOCH
    if lock_due(recent, after) and not ((_stamp(lock) or EPOCH) > after):
        return store.put(LOCK, {"at": _ts(now)}, lock_sha,
                         "Review locked: %d days at the cap" % LOCK_DAYS)
    return True


def run(a, store, now):
    """One check / claim / settle against a store. -> exit code."""
    shard_path(a.pid)
    msg = "PR #%s, %s" % (a.pr, a.pid)
    if a.command in ("check", "claim"):
        shard, recent, lock, reset = _read(store, a.pid, now)
        if a.command == "check" and a.patch_only:
            return FAILED if state_of(shard, a.pid, now) == "failed" \
                else CLEAR
        code = decide(shard, recent, a.pid, a.pr, a.branch, now, lock, reset)
        if code != CLEAR or a.command == "check":
            return code
        # Counted as a failure FIRST, so nothing after this can lose it.
        ok = store.update(
            RECENT, lambda r: claimed_recent(r, a.pid, a.pr, a.branch, now),
            empty_recent, valid_recent, "Claimed: " + msg)
        ok = ok and store.update(
            shard_path(a.pid),
            lambda s: claimed_shard(s, a.pid, a.pr, a.branch, now),
            empty_shard, valid_shard, "In review: " + msg)
        ok = ok and _lock_if_streak(store, now)
        return CLEAR if ok else BROKEN
    ok = store.update(
        shard_path(a.pid),
        lambda s: settled_shard(s, a.pid, a.outcome, a.pr, a.branch, now),
        empty_shard, valid_shard, "%s: %s" % (a.outcome, msg))
    ok = ok and store.update(
        RECENT,
        lambda r: settled_recent(r, a.pid, a.outcome, a.pr, a.branch, now),
        empty_recent, valid_recent, "%s: %s" % (a.outcome, msg))
    if a.outcome != "pass":
        ok = ok and _lock_if_streak(store, now)
    return CLEAR if ok else BROKEN


def main(argv=None, store=None, now=None):
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
    ap.add_argument("--patch-only", action="store_true")
    ap.add_argument("--outcome", choices=("pass", "failed", "error"))
    a = ap.parse_args(argv)
    if a.command == "settle" and not a.outcome:
        ap.error("settle needs --outcome")
    try:
        code = run(a, store or _store(), now or now_utc())
    except (urllib.error.URLError, ValueError, KeyError, TypeError,
            OSError) as e:
        sys.stderr.write("review ledger: %s\n" % e)
        code = BROKEN
    if code != CLEAR:
        print(REASONS[code])
    return code


if __name__ == "__main__":
    sys.exit(main())

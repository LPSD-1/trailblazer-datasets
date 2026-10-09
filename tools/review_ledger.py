#!/usr/bin/env python3
"""The review ledger: every change that ever FAILED review, by patch-id.

    python tools/review_ledger.py patch-id DIFF            -> prints the id
    python tools/review_ledger.py check LEDGER.json ID     -> exit 0 clean,
                                                              3 failed before,
                                                              1 unreadable
    python tools/review_ledger.py record LEDGER.json ID PR SHA
                                                           -> new ledger JSON

WHY IT EXISTS. A model's verdict is not deterministic, so "re-run it until it
passes" is a way through any model-based gate. The workflow refuses re-runs
outright (run_attempt > 1), and this closes the other door: pushing the same
change again as a new commit, or opening it again as a new pull request. The
key is `git patch-id --stable` of the captured diff, which is the same for the
same change whatever commit, branch or pull request carries it, and the same
whatever the context width.

WHERE IT LIVES, AND WHY THERE. One JSON file, `failed.json`, on a branch
called `review-ledger`, written only through the review app's token via the
contents API. Considered and rejected:
  * actions/cache: anyone whose workflow runs in this repo can evict or
    poison a cache entry, and caches expire. Not a record.
  * the pull request's own comments: anyone with write access - which is
    every agent working as the owner - can delete a comment, silently. A
    branch can be protected (docs/REVIEW-GATE-SETUP.md: a ruleset on
    `review-ledger` with the review app as the only bypass), so its history
    is append-only to everyone else.
A write that loses a race is retried against the newer file; the contents
API refuses a stale `sha`, so two runs never overwrite each other.

The ledger never makes anything pass. Missing, unreadable or malformed, it
fails the review closed.
"""
import datetime
import json
import re
import subprocess
import sys

_ID = re.compile(r"^[0-9a-f]{40}$")


def patch_id(diff_text):
    """`git patch-id --stable` of a diff, or None for an empty one."""
    r = subprocess.run(["git", "patch-id", "--stable"],
                       input=diff_text.encode("utf-8", errors="replace"),
                       capture_output=True)
    if r.returncode != 0:
        return None
    first = r.stdout.decode("ascii", errors="replace").split()
    return first[0] if first and _ID.match(first[0]) else None


def load(text):
    """Ledger text -> dict. Raises ValueError on anything malformed."""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("failed"), dict):
        raise ValueError("not a review ledger")
    return data


def already_failed(ledger, pid):
    return bool(pid) and pid in ledger["failed"]


def record(ledger, pid, pr, sha, when=None):
    """A new ledger with pid recorded. The first failure is kept: a later
    record of the same id never moves it."""
    if not _ID.match(pid or ""):
        raise ValueError("bad patch id %r" % pid)
    out = {"version": 1, "failed": dict(ledger.get("failed", {}))}
    if pid not in out["failed"]:
        out["failed"][pid] = {
            "pr": int(pr), "sha": sha,
            "at": when or datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")}
    return out


def dumps(ledger):
    return json.dumps(ledger, indent=1, sort_keys=True) + "\n"


def main(argv):
    if len(argv) >= 3 and argv[1] == "patch-id":
        with open(argv[2], encoding="utf-8", errors="replace") as fh:
            pid = patch_id(fh.read())
        if not pid:
            return 1
        print(pid)
        return 0
    if len(argv) == 4 and argv[1] == "check":
        try:
            with open(argv[2], encoding="utf-8") as fh:
                ledger = load(fh.read())
        except (OSError, ValueError):
            return 1
        return 3 if already_failed(ledger, argv[3]) else 0
    if len(argv) == 6 and argv[1] == "record":
        with open(argv[2], encoding="utf-8") as fh:
            ledger = load(fh.read())
        sys.stdout.write(dumps(record(ledger, argv[3], argv[4], argv[5])))
        return 0
    sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

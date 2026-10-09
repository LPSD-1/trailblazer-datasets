#!/usr/bin/env python3
"""Read the independent reviewers' answers and decide PASS or FAIL.

    python tools/review_verdict.py BODY.md NAME=PATH [NAME=PATH ...]

Each PATH is either the `claude -p --output-format json` output of one
reviewer (*.json) or a plain-text check whose last line is a verdict
(review_data_check.py). Prints PASS or FAIL as the last line of stdout and
writes the pull request comment to BODY.md.

THE VERDICT LINE IS STRICT, because the reviewer reads text the pull request
wrote and the pull request would like to be passed. A reviewer passes only
when the last non-empty line of its answer is exactly `VERDICT: PASS` and no
other line says VERDICT at all. Anything else is FAIL: no verdict, two
verdicts, a decorated one (`**VERDICT: PASS**`), a verdict mid-answer, a run
that errored, a file that is missing, or no reviewers at all. A gate that
passes on a reply it could not read is not a gate.
"""
import json
import os
import re
import sys

MARKER = "<!-- independent-review -->"
MAX_COMMENT = 60000   # GitHub refuses a comment over 65536 characters

_VERDICT = re.compile(r"^VERDICT: (PASS|FAIL)$")
# Any line that LOOKS like a verdict, however decorated: `**Verdict:**`,
# `> VERDICT: PASS`, `- verdict : fail`.
_MENTIONS = re.compile(r"^[\W_]*verdict\W*:", re.IGNORECASE)


def parse_verdict(text):
    """-> (verdict, findings). verdict is "PASS" or "FAIL"."""
    if not isinstance(text, str) or not text.strip():
        return "FAIL", "The reviewer gave no answer."
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    last = lines[-1] if lines else ""
    findings = "\n".join(lines[:-1]).strip()
    mentions = sum(1 for ln in lines if _MENTIONS.search(ln))
    m = _VERDICT.match(last)
    if not m:
        return "FAIL", ("No valid verdict line: the answer must end with "
                        "exactly `VERDICT: PASS` or `VERDICT: FAIL`.\n\n"
                        + text.strip())
    if mentions != 1:
        return "FAIL", ("More than one verdict line in the answer.\n\n"
                        + text.strip())
    return m.group(1), findings


def from_cli_json(raw):
    """One `claude -p --output-format json` result -> (verdict, findings)."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return "FAIL", "The reviewer's output was not JSON."
    if not isinstance(data, dict):
        return "FAIL", "The reviewer's output was not a result object."
    if data.get("is_error") or data.get("subtype") != "success":
        return "FAIL", ("The reviewer run did not succeed (subtype %r)."
                        % data.get("subtype"))
    return parse_verdict(data.get("result"))


def ran(raw):
    """True when a CLI result shows the reviewer actually answered: a FAIL
    from such a run is the reviewer's judgement, not an outage."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return (isinstance(data, dict) and not data.get("is_error")
            and data.get("subtype") == "success")


def read_one(path):
    """-> (verdict, findings, judged). `judged` is False when no reviewer
    answered at all (missing file, failed run): still a FAIL, but not one
    the ledger should remember against the change."""
    if not os.path.isfile(path):
        return ("FAIL", "This reviewer produced nothing (%s is missing)."
                % os.path.basename(path), False)
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    if path.endswith(".json"):
        return from_cli_json(raw) + (ran(raw),)
    return parse_verdict(raw) + (True,)


def should_record(judgements):
    """[(verdict, judged)] -> True when a reviewer that really answered
    said FAIL. An outage is not a verdict on the change."""
    return any(v == "FAIL" and judged for v, judged in judgements)


def combine(results):
    """[(name, verdict, findings)] -> "PASS" only if there is at least one
    and every one passed."""
    if not results:
        return "FAIL"
    return "PASS" if all(v == "PASS" for _, v, _ in results) else "FAIL"


def comment_body(overall, results):
    out = [MARKER, "## Independent review: %s" % overall, ""]
    for name, verdict, findings in results:
        out.append("### %s: %s" % (name, verdict))
        out.append("")
        out.append(findings or "_No findings._")
        out.append("")
    body = "\n".join(out)
    if len(body) > MAX_COMMENT:
        body = body[:MAX_COMMENT] + "\n\n[Comment truncated; the full " \
            "answers are in the run's log.]\n"
    return body


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: review_verdict.py BODY.md NAME=PATH ...\n")
        print("FAIL")
        return 2
    results, judgements = [], []
    for spec in argv[2:]:
        name, _, path = spec.partition("=")
        verdict, findings, judged = read_one(path)
        results.append((name, verdict, findings))
        judgements.append((verdict, judged))
    overall = combine(results)
    with open(argv[1], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(comment_body(overall, results))
    for name, verdict, _ in results:
        print("%s: %s" % (name, verdict))
    print("record: %s" % ("yes" if should_record(judgements) else "no"))
    print(overall)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

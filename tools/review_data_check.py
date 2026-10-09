#!/usr/bin/env python3
"""The cheap, deterministic half of a DATA-tier review. No model, no network.

    python tools/review_data_check.py --diff DIFF --files FILES --rev SHA

  * every changed .json / .geojson file at SHA parses;
  * no changed file at SHA is over the size cap;
  * no line the pull request ADDS carries an email address, a UK phone
    number or a UK postcode. This repository is public and everything in it
    is served to riders; a person's contact details never belong in it.

Only added lines are scanned, so a file that already holds (say) a council's
published office number is not re-flagged every time a neighbouring line
changes. The file contents are read from git objects (`git cat-file`), never
from a checkout of the pull request: nothing the pull request wrote is run.

Prints its findings and ends with exactly one `VERDICT: PASS` or
`VERDICT: FAIL` line, which review_verdict.py reads like any reviewer's.
Always exits 0 unless it is called wrongly; the verdict line is the result.
"""
import argparse
import json
import re
import subprocess
import sys

#: GitHub warns at 50 MB and refuses 100 MB. The largest file published
#: today is a 24 MB container, so 50 MB is room for growth, not for a mistake.
MAX_FILE_BYTES = 50 * 1024 * 1024
#: A JSON index the app reads whole should never be this big.
MAX_JSON_BYTES = 20 * 1024 * 1024

JSON_SUFFIXES = (".json", ".geojson")

EMAIL = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
    r"\.[A-Za-z]{2,}(?![\w-])")
# +44 or 0, then a national number starting 1, 2, 3, 5, 7, 8 or 9 and 9-10
# digits long, with single spaces or hyphens allowed between digits.
PHONE = re.compile(
    r"(?<![\w.+/-])(?:\+44[ ]?(?:\(0\)[ ]?)?|0)[1235789](?:[ -]?\d){8,9}"
    r"(?![\w.])")
# The Royal Mail shape, with its single space, in capitals.
POSTCODE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-PR-UWYZ][A-HK-Y]?[0-9][A-Z0-9]?) "
    r"[0-9][ABD-HJLNP-UW-Z]{2}(?![A-Za-z0-9])")

KINDS = (("email address", EMAIL), ("UK phone number", PHONE),
         ("UK postcode", POSTCODE))


def added_lines(diff_text):
    """[(path, new line number, text)] for every line a unified diff adds."""
    out = []
    path, new_no, in_hunk = None, 0, False
    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            path, in_hunk = None, False
            continue
        if not in_hunk:
            if line.startswith("+++ "):
                target = line[4:].strip()
                if target.startswith('"') and target.endswith('"'):
                    target = target[1:-1]
                path = None if target == "/dev/null" else (
                    target[2:] if target.startswith("b/") else target)
            elif line.startswith("@@"):
                m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
                new_no, in_hunk = (int(m.group(1)) if m else 0), True
            continue
        if line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
            new_no = int(m.group(1)) if m else 0
        elif line.startswith("+"):
            if path is not None:
                out.append((path, new_no, line[1:]))
            new_no += 1
        elif line.startswith(" "):
            new_no += 1
        # "-" lines and "\ No newline" lines do not move the new side.
    return out


def personal_data(text):
    """-> [(kind, matched text)] found in one line."""
    found = []
    for kind, rx in KINDS:
        for m in rx.finditer(text):
            found.append((kind, m.group(0)))
    return found


def scan_diff(diff_text):
    """-> ["path:line: kind 'match'"] for every added line carrying one."""
    problems = []
    for path, no, text in added_lines(diff_text):
        for kind, match in personal_data(text):
            problems.append("%s:%d: %s %r" % (path, no, kind, match))
    return problems


def check_blob(path, size, read):
    """-> [problem] for one file at the pull request's head. `read` is a
    zero-argument callable returning the bytes, only called when needed."""
    problems = []
    low = path.lower()
    if size > MAX_FILE_BYTES:
        problems.append("%s: %d bytes is over the %d-byte cap"
                        % (path, size, MAX_FILE_BYTES))
        return problems
    if low.endswith(JSON_SUFFIXES):
        if size > MAX_JSON_BYTES:
            problems.append("%s: %d bytes is over the %d-byte JSON cap"
                            % (path, size, MAX_JSON_BYTES))
            return problems
        try:
            json.loads(read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            problems.append("%s: does not parse as JSON (%s)" % (path, e))
    return problems


def _git(*args):
    return subprocess.run(("git",) + args, capture_output=True, check=True)


def check_files(paths, rev):
    problems = []
    for path in paths:
        spec = "%s:%s" % (rev, path)
        try:
            size = int(_git("cat-file", "-s", spec).stdout.strip())
        except (subprocess.CalledProcessError, ValueError):
            problems.append("%s: not found at %s" % (path, rev))
            continue
        problems.extend(check_blob(
            path, size, lambda s=spec: _git("cat-file", "blob", s).stdout))
    return problems


def report(problems):
    lines = []
    if problems:
        lines.append("The deterministic data check found:")
        lines.extend("- " + p for p in problems)
        lines.append("")
        lines.append("VERDICT: FAIL")
    else:
        lines.append("JSON parses, sizes are under the caps, and no added "
                     "line carries an email address, phone number or "
                     "postcode.")
        lines.append("")
        lines.append("VERDICT: PASS")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", required=True)
    ap.add_argument("--files", required=True,
                    help="changed paths still present at --rev, one a line")
    ap.add_argument("--rev", required=True)
    a = ap.parse_args(argv)
    with open(a.diff, encoding="utf-8", errors="replace") as fh:
        diff_text = fh.read()
    with open(a.files, encoding="utf-8") as fh:
        paths = [p for p in fh.read().splitlines() if p.strip()]
    problems = scan_diff(diff_text) + check_files(paths, a.rev)
    sys.stdout.write(report(problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The cheap, deterministic half of a DATA-tier review. No model, no network.

    python tools/review_data_check.py --diff DIFF --files FILES --rev SHA
    python tools/review_data_check.py --structure --diff DIFF

  * no binary hunk, symlink (mode 120000), submodule (mode 160000), mode
    change or new executable: none of those can be read in a diff, so the
    owner must review them. `--structure` runs only this check, and the
    review workflow runs it for EVERY tier before any model is asked;
  * every changed .json / .geojson file at SHA parses;
  * no changed file at SHA is over the size cap;
  * no line the pull request ADDS carries an email address, a UK phone
    number or a UK postcode. This repository is public and everything in it
    is served to riders; a person's contact details never belong in it.
    Each line is normalised first: case, runs of spaces, odd dashes and
    brackets, and a spelled-out " at " / " dot " (`x at gmail dot com`), so
    `sw1a 1aa`, `SW1A1AA` and `(01632) 960 123` are all caught.

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
import unicodedata

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
# The Royal Mail shape, matched on the upper-cased line, space optional.
POSTCODE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-PR-UWYZ][A-HK-Y]?[0-9][A-Z0-9]?) ?"
    r"[0-9][ABD-HJLNP-UW-Z]{2}(?![A-Za-z0-9])")

KINDS = (("email address", EMAIL), ("UK phone number", PHONE),
         ("UK postcode", POSTCODE))

_DASHES = re.compile("[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]")
_SPACES = re.compile(r"\s+")
_AT = re.compile(r"\s*[\(\[\{<]?\s*\bat\b\s*[\)\]\}>]?\s*", re.I)
_DOT = re.compile(r"\s*[\(\[\{<]?\s*\bdot\b\s*[\)\]\}>]?\s*", re.I)

STRUCTURE_HINT = "owner must review"


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


def normalise(text):
    """One line as the patterns read it: NFKC, one kind of dash, single
    spaces, no brackets round a number."""
    text = unicodedata.normalize("NFKC", text)
    text = _DASHES.sub("-", text)
    text = re.sub(r"[()]", " ", text)
    return _SPACES.sub(" ", text).strip()


def personal_data(text):
    """-> [(kind, matched text)] found in one line, after normalising."""
    plain = normalise(text)
    spelled = _DOT.sub(".", _AT.sub("@", plain))
    found = []
    for kind, rx, variants in (
            ("email address", EMAIL, (plain, spelled)),
            ("UK phone number", PHONE, (plain,)),
            ("UK postcode", POSTCODE, (plain.upper(),))):
        seen = set()
        for variant in variants:
            for m in rx.finditer(variant):
                if m.group(0) not in seen:
                    seen.add(m.group(0))
                    found.append((kind, m.group(0)))
    return found


def structure_problems(diff_text):
    """-> ["path: what: owner must review"] for every change a text diff
    cannot show: binary content, a symlink, a submodule, a mode change, a
    new executable."""
    problems = []
    path, in_hunk = "?", False
    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            in_hunk = False
            rest = line[len("diff --git "):]
            i = rest.rfind(" b/")
            path = rest[i + 3:] if i >= 0 else rest
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk:
            continue
        what = None
        if line.startswith("Binary files ") and line.endswith(" differ"):
            what = "binary content"
        elif line == "GIT binary patch":
            what = "binary content"
        elif line.startswith(("old mode ", "new mode ")):
            what = "a mode change (%s)" % line
        else:
            m = re.match(r"^(?:new file|deleted file) mode (\d+)$", line) \
                or re.match(r"^index [0-9a-f]+\.\.[0-9a-f]+ (\d+)$", line)
            if m:
                mode = m.group(1)
                if mode == "120000":
                    what = "a symlink"
                elif mode == "160000":
                    what = "a submodule"
                elif mode != "100644" and line.startswith("new file"):
                    what = "a new file with mode %s" % mode
        if what:
            entry = "%s: %s: %s" % (path, what, STRUCTURE_HINT)
            if entry not in problems:
                problems.append(entry)
    return problems


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
    ap.add_argument("--structure", action="store_true",
                    help="only the binary/link/submodule/mode check; exit 1 "
                         "when it finds anything")
    ap.add_argument("--files",
                    help="changed paths still present at --rev, one a line")
    ap.add_argument("--rev")
    a = ap.parse_args(argv)
    with open(a.diff, encoding="utf-8", errors="replace") as fh:
        diff_text = fh.read()
    if a.structure:
        problems = structure_problems(diff_text)
        sys.stdout.write("".join(p + "\n" for p in problems))
        return 1 if problems else 0
    if not a.files or not a.rev:
        ap.error("--files and --rev are required without --structure")
    with open(a.files, encoding="utf-8") as fh:
        paths = [p for p in fh.read().splitlines() if p.strip()]
    problems = (structure_problems(diff_text) + scan_diff(diff_text)
                + check_files(paths, a.rev))
    sys.stdout.write(report(problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())

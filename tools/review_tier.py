#!/usr/bin/env python3
"""Which reviewer a pull request gets, from the paths it changes.

    python tools/review_tier.py FILES.txt     -> prints gate, security, code
                                                 or data

Used by .github/workflows/independent-review.yml. FILES.txt is one path per
line, as GitHub's own API lists the pull request's files (both sides of a
rename), never the list the pull request uploaded about itself.

  GATE      the review gate itself. Never reviewed by a model: the owner must
            review it, because a change here could rewrite its own review.
  SECURITY  EVERY .py file, and anything that changes what runs, what is
            fetched, or what guards the repository: .github/**, .claude/**,
            the robots allowlist, tools/*fetch*, any path with "guard" or
            "hook" in it, .gitattributes, .gitmodules.
  CODE      everything not positively known to be data. A new extension is
            code until somebody says otherwise.
  DATA      .json / .geojson / .csv / .md / .txt outside tools/ (except
            tools/fixtures/), and anything that is not code inside one of
            the published data directories.

WHY EVERY .py IS SECURITY. An earlier version sent a .py to SECURITY only
when it imported a network module, and every list of those was one evasion
short: a relative import, `__import__('soc' + 'ket')`, `ssl`, subprocess
running curl. Python can reach the network in too many ways to enumerate, so
it stopped trying. The security prompt and both models read every Python
change. The allowlist that remains is the one that can be complete: what
counts as DATA.

EVERY PATH IS LOWER-CASED BEFORE EVERY RULE. Windows and macOS checkouts are
case-insensitive, so `Tools/Robots_Override.json` is the robots allowlist on
the machine that runs it; it was rated DATA until this said so.

The strongest tier any one path earns is the tier of the whole change.
"""
import re
import sys

#: The review gate. A pull request touching any of these is the owner's.
GATE_EXACT = frozenset((
    ".github/workflows/pr-capture.yml",
    ".github/workflows/independent-review.yml",
    "tools/test_review_gate.py",
    "docs/review-gate-setup.md",
))
GATE_PREFIXES = (".github/review/", "tools/review_")
GATE_NAMES = frozenset(("conftest.py", "codeowners"))

SECURITY_EXACT = frozenset((
    "tools/polite_http.py",
    "tools/robots_override.json",
))
SECURITY_DIRS = frozenset((".github", ".claude"))
SECURITY_WORDS = ("guard", "hook")
SECURITY_NAMES = frozenset((".gitattributes", ".gitmodules"))
SECURITY_SUFFIXES = (".py", ".pyw", ".pyi", ".pth")

CODE_SUFFIXES = (".py", ".yml", ".yaml", ".sh", ".dart", ".cmd", ".bat",
                 ".ps1", ".js", ".mjs", ".cjs", ".ts", ".toml", ".cfg",
                 ".ini", ".html", ".htm", ".svg")
CODE_NAMES = frozenset(("makefile", "pyproject.toml", "setup.py",
                        "setup.cfg", "package.json", "package-lock.json",
                        "dockerfile"))
_REQUIREMENTS = re.compile(r"^requirements.*\.(txt|in)$")

DATA_SUFFIXES = (".json", ".geojson", ".csv", ".md", ".txt")
DATA_DIRS = frozenset((
    "changes", "containers", "council-ucrs", "council-ways", "height",
    "home-collected", "local-rules", "manual", "names", "packages",
    "published", "routing", "satellite", "status", "trips", "tro",
))


def _norm(path):
    path = path.strip().replace("\\", "/").lower()
    while path.startswith("./"):
        path = path[2:]
    return path


def _parts(path):
    return [p for p in path.split("/") if p]


def is_gate(path):
    path = _norm(path)
    parts = _parts(path)
    if not parts:
        return False
    return (path in GATE_EXACT or path.startswith(GATE_PREFIXES)
            or parts[-1] in GATE_NAMES)


def is_security(path):
    path = _norm(path)
    parts = _parts(path)
    if not parts:
        return False
    if path in SECURITY_EXACT or parts[-1].endswith(SECURITY_SUFFIXES):
        return True
    if any(p in SECURITY_DIRS for p in parts):
        return True
    if parts[0] == "tools" and "fetch" in parts[-1]:
        return True
    if any(word in p for p in parts for word in SECURITY_WORDS):
        return True
    return parts[-1] in SECURITY_NAMES


def is_data(path):
    path = _norm(path)
    parts = _parts(path)
    if not parts:
        return False
    name = parts[-1]
    if name in CODE_NAMES or _REQUIREMENTS.match(name) \
            or name.endswith(CODE_SUFFIXES):
        return False
    if parts[0] == "tools" and not path.startswith("tools/fixtures/"):
        return False
    if name.endswith(DATA_SUFFIXES):
        return True
    return parts[0] in DATA_DIRS and len(parts) > 1


def tier_of(paths):
    """-> "gate", "security", "code" or "data" for the whole list."""
    paths = [p for p in (_norm(p) for p in paths) if p]
    if any(is_gate(p) for p in paths):
        return "gate"
    if any(is_security(p) for p in paths):
        return "security"
    if all(is_data(p) for p in paths):
        return "data"
    return "code"


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: review_tier.py FILES.txt\n")
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        print(tier_of(fh.read().splitlines()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

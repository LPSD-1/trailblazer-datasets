#!/usr/bin/env python3
"""Which reviewer a pull request gets, from the paths it changes.

    python tools/review_tier.py FILES.txt      -> prints security, code or data

Used by .github/workflows/independent-review.yml. FILES.txt is one path per
line, as GitHub's own API lists the pull request's files (both sides of a
rename), never the list the pull request uploaded about itself.

  SECURITY  anything that can change what runs, what is fetched, or what
            guards the repository: .github/**, .claude/**, the polite HTTP
            client and its robots allowlist, any tools/*fetch* script, any
            path with "guard" or "hook" in it, and .gitattributes (which can
            mark a file `-diff` and hide its content from the captured diff).
  CODE      otherwise, any .py / .yml / .yaml / .sh / .dart file.
  DATA      everything else.

The strongest tier any one path earns is the tier of the whole change.
"""
import sys

SECURITY_EXACT = frozenset((
    "tools/polite_http.py",
    "tools/robots_override.json",
))
SECURITY_DIRS = frozenset((".github", ".claude"))
SECURITY_WORDS = ("guard", "hook")
CODE_SUFFIXES = (".py", ".yml", ".yaml", ".sh", ".dart")


def _norm(path):
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def is_security(path):
    path = _norm(path)
    parts = [p for p in path.split("/") if p]
    if not parts:
        return False
    low = [p.lower() for p in parts]
    if path in SECURITY_EXACT:
        return True
    if any(p in SECURITY_DIRS for p in low):
        return True
    if low[0] == "tools" and "fetch" in low[-1]:
        return True
    if any(word in p for p in low for word in SECURITY_WORDS):
        return True
    if low[-1] == ".gitattributes":
        return True
    return False


def is_code(path):
    return _norm(path).lower().endswith(CODE_SUFFIXES)


def tier_of(paths):
    """-> "security", "code" or "data" for the whole list."""
    paths = [p for p in (_norm(p) for p in paths) if p]
    if any(is_security(p) for p in paths):
        return "security"
    if any(is_code(p) for p in paths):
        return "code"
    return "data"


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: review_tier.py FILES.txt\n")
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        print(tier_of(fh.read().splitlines()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

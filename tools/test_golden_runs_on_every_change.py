#!/usr/bin/env python3
"""Does the golden build run on every pull request, and on pushes to main?

    python tools/test_golden_runs_on_every_change.py

tools/golden.py proves that identical inputs give identical bytes; two clock
bugs shipped before it existed (see its header). It used to run only inside
the scheduled lane refresh, so a pull request that broke it went green and the
break showed up after the merge as a stopped lane refresh. golden.yml runs it
on every pull request to main and on pushes to main that touch tools/. This
reads that workflow as text, the way the other workflow suites here do.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, ".github", "workflows", "golden.yml")

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def block(text, key, indent):
    """The lines under `key:` at `indent` spaces, up to the next sibling."""
    m = re.search(r"^%s%s:[^\n]*\n((?:%s .*\n|\s*\n)*)" % (
        " " * indent, re.escape(key), " " * indent), text, re.M)
    return m.group(1) if m else None


def main():
    try:
        with open(PATH, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print("FAIL golden.yml is missing: %s" % exc)
        return 1
    on = block(text, "on", 0)
    check("it has triggers", on is not None, text[:200])
    on = on or ""
    pr = block(on, "pull_request", 2)
    check("it runs on pull requests", pr is not None, on)
    check("on pull requests to main", pr is not None
          and re.search(r"branches:\s*\[\s*main\s*\]", pr), pr)
    # A paths filter on pull_request would leave a PR that touches only, say,
    # a workflow with no golden run at all - and a required check pending.
    check("on every pull request, not a filtered few",
          pr is not None and "paths" not in pr, pr)
    push = block(on, "push", 2)
    check("it runs on pushes", push is not None, on)
    check("on pushes to main", push is not None
          and re.search(r"branches:\s*\[\s*main\s*\]", push), push)
    check("pushes that touch the builders", push is not None
          and "'tools/**'" in push, push)
    check("never pull_request_target", not re.search(
        r"^\s*pull_request_target\s*:", text, re.M))
    check("read-only token", re.search(r"^permissions:\s*\n\s+contents: read"
                                        r"\s*\n(?!\s+\w+:)", text, re.M)
          is not None and "write" not in text, text)
    check("no secrets", "secrets." not in text)
    check("the checkout keeps no token", "persist-credentials: false" in text)
    run_lines = re.findall(r"^\s+run:\s*(.+)$", text, re.M)
    check("it runs golden.py", "python tools/golden.py" in run_lines,
          run_lines)
    check("and its mutate claim", "python tools/golden.py --mutate"
          in run_lines, run_lines)
    check("never continue-on-error", "continue-on-error" not in text)
    check("no if: that could skip it", not re.search(r"^\s+if:", text, re.M))
    # golden.py needs the same packages the lane refresh installs.
    check("installs what golden.py imports",
          "pip install --quiet cryptography Pillow pyyaml" in text)
    for f in _failed:
        print("FAIL", f)
    print("%d passed, %d failed" % (_passed, len(_failed)))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

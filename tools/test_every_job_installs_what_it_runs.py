#!/usr/bin/env python3
"""Does every CI job install what the tools it runs actually import?

    python tools/test_every_job_installs_what_it_runs.py

THE FAULT THIS CATCHES ran on the schedule and cost a half-published pair.

`build_fords.py` imports `build_map_container` for one constant, and that
module imports AESGCM at the top. The refresh job installs `cryptography`; the
conditions job did not, because it is a separate job with a separate
environment and nothing anywhere said the two had to agree. So the live half of
the conditions pipeline died with `ModuleNotFoundError: No module named
'cryptography'` - AFTER publishing the rain feed and BEFORE the rivers, which
is the one state the two clocks were designed to avoid.

It is exactly the shape of defect this repository keeps producing, one level
out: a thing that is correct in the file it lives in and wrong at the seam
between two files nobody reads together.
"""
import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")

#: Modules the standard library provides, so an import of one proves nothing.
STDLIB = set(sys.stdlib_module_names)

#: What a pip name is called when you import it, where the two differ.
IMPORT_NAME = {"cryptography": "cryptography", "pyyaml": "yaml",
               "requests": "requests", "pillow": "PIL"}

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def imports_of(path, seen=None):
    """Every third-party module `path` needs, following local imports."""
    seen = seen if seen is not None else set()
    if path in seen or not os.path.exists(path):
        return set()
    seen.add(path)
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except (SyntaxError, OSError):
        return set()

    out = set()

    # DYNAMIC LOADS COUNT TOO, and missing them is how this guard let a real
    # failure through on its first run. `test_build_height.py` does
    # `load("build_height")` through importlib rather than importing it, so a
    # scan that followed only `import` statements saw nothing - and the job
    # died on `ModuleNotFoundError: No module named 'PIL'` with this file
    # reporting no problems. A guard that misses the case it was written the
    # same afternoon to catch is worth less than no guard, because it is
    # believed.
    source = io.open(path, encoding="utf-8").read()
    for name in re.findall(r"""load\(["'](\w+)["']\)""", source):
        sibling = os.path.join(os.path.dirname(path), name + ".py")
        if os.path.exists(sibling):
            out |= imports_of(sibling, seen)

    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        for full in names:
            top = full.split(".")[0]
            if top in STDLIB:
                continue
            sibling = os.path.join(os.path.dirname(path), top + ".py")
            if os.path.exists(sibling):
                # A tool importing another tool: its needs are this tool's
                # needs, which is exactly how the fords/container pair went
                # wrong.
                out |= imports_of(sibling, seen)
            else:
                out.add(top)
    return out


def jobs_of(text):
    """-> {job name: (its text block)}, by indentation rather than YAML.

    Deliberately not parsed as YAML: a workflow that will not parse is a
    separate failure with its own message, and this file should still say
    something useful when one lands.
    """
    blocks = re.split(r"\n  (\w[\w-]*):\n", "\n" + text)
    return dict(zip(blocks[1::2], blocks[2::2]))


def main():
    path = os.path.join(WORKFLOWS, "refresh-data.yml")
    text = io.open(path, encoding="utf-8").read()
    jobs = jobs_of(text)
    check("the workflow has jobs to check", bool(jobs), sorted(jobs))

    for job, body in sorted(jobs.items()):
        installed = set()
        for line in re.findall(r"pip install[^\n]*", body):
            for word in line.split():
                if word.startswith("-") or word in ("pip", "install"):
                    continue
                installed.add(IMPORT_NAME.get(word.lower(), word.lower()))

        # Every tool this job runs, and everything those tools import.
        #
        # A GLOB COUNTS AS RUNNING ALL OF THEM, and leaving that out made the
        # first version of this file worthless. The suite step is
        # `for t in tools/test_*.py`, so a scan for `tools/<name>.py` matched
        # nothing at all and the check passed over an empty set — while the
        # real job died on `ModuleNotFoundError: No module named 'PIL'`,
        # raised by a test the glob runs and this file could not see.
        #
        # Zero subjects is BLIND, not PASS. It reported 0 failed.
        tools = set(re.findall(r"tools/(\w+)\.py", body))
        for glob in re.findall(r"tools/(\w*)\*(\w*)\.py", body):
            prefix, suffix = glob
            for name in os.listdir(os.path.join(ROOT, "tools")):
                if (name.endswith(".py") and name.startswith(prefix)
                        and name[:-3].endswith(suffix)):
                    tools.add(name[:-3])

        needed = set()
        for tool in sorted(tools):
            needed |= imports_of(os.path.join(ROOT, "tools", tool + ".py"))

        # THE PREMISE. A job that runs no tool proves nothing, and a rule that
        # passed over one would be the zero-subject trap.
        if not needed:
            continue

        missing = sorted(n for n in needed if n not in installed)
        check("job '%s' installs everything its tools import" % job,
              not missing,
              "missing %s; it installs %s"
              % (", ".join(missing) or "nothing",
                 ", ".join(sorted(installed)) or "nothing"))

    print("%d checks, %d failed" % (_passed + len(_failed), len(_failed)))
    for f in _failed:
        print("  FAIL %s" % f)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""A workflow input never reaches a shell script pasted into it.

    python tools/test_workflow_inputs_reach_the_shell_quoted.py

`${{ inputs.only }}` inside a `run:` block is substituted into the script
text BEFORE the shell sees it, so a quote in the value ends the string and
the rest runs as code (council-orders.yml's `--only "${{ inputs.only }}"`
did exactly that). A text input must arrive through `env:` and be used as a
quoted shell variable. Boolean inputs are left alone: they can only be
`true` or `false`.

No network, no YAML library (the CI jobs install none): the workflow files
are read as text, which is all this needs.
"""
import glob
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(os.path.dirname(HERE), ".github", "workflows")

_USE = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_-]+)")
_ENV = re.compile(r"^\s+[A-Z][A-Z0-9_]*:\s*\$\{\{\s*inputs\.([A-Za-z0-9_-]+)"
                  r"(\s*\|\|\s*'[^']*')?\s*\}\}\s*$")
_IF = re.compile(r"^\s+(-\s+)?if:\s")


def dispatch_inputs(text):
    """{input name: type} under `workflow_dispatch: inputs:`."""
    lines = text.splitlines()
    out = {}
    for i, line in enumerate(lines):
        if line.strip() != "workflow_dispatch:":
            continue
        base = len(line) - len(line.lstrip())
        j, names_at, name = i + 1, None, None
        while j < len(lines):
            row = lines[j]
            j += 1
            if not row.strip() or row.lstrip().startswith("#"):
                continue
            indent = len(row) - len(row.lstrip())
            if indent <= base:
                break
            key = row.strip()
            if key == "inputs:":
                continue
            if names_at is None and key.endswith(":"):
                names_at = indent
            if indent == names_at and key.endswith(":"):
                name = key[:-1]
                out[name] = "string"   # GitHub's default type
            elif name and key.startswith("type:"):
                out[name] = key.split(":", 1)[1].strip()
    return out


def pasted(path):
    """[(line number, line)] where a non-boolean input is pasted into a
    script rather than passed through env."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    types = dispatch_inputs(text)
    bad = []
    for n, line in enumerate(text.splitlines(), 1):
        for name in _USE.findall(line):
            if types.get(name, "string") == "boolean":
                continue
            if _ENV.match(line) or _IF.match(line):
                continue
            bad.append((n, line.strip()))
    return bad


class InputsArePassedThroughEnv(unittest.TestCase):
    def test_the_workflows_are_found(self):
        self.assertTrue(glob.glob(os.path.join(WORKFLOWS, "*.yml")))

    def test_the_input_parser_sees_types(self):
        types = dispatch_inputs(
            "on:\n  workflow_dispatch:\n    inputs:\n      only:\n"
            "        description: 'x'\n        type: string\n"
            "      force:\n        type: boolean\n      n:\n"
            "        description: 'no type'\npermissions:\n")
        self.assertEqual(types, {"only": "string", "force": "boolean",
                                 "n": "string"})

    def test_no_text_input_is_pasted_into_a_script(self):
        found = {}
        for path in sorted(glob.glob(os.path.join(WORKFLOWS, "*.yml"))):
            bad = pasted(path)
            if bad:
                found[os.path.basename(path)] = bad
        self.assertEqual(found, {}, "pass these through env: and quote "
                                    "them: %r" % found)


if __name__ == "__main__":
    unittest.main(verbosity=1)

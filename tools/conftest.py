"""Lets `python -m pytest tools` run the tool suites the way CI does.

Most `tools/test_*.py` are scripts, not pytest modules: they run their checks
at import and end in `sys.exit(0 or 1)`. Imported by pytest, the first one's
`sys.exit` stops the whole collection (INTERNALERROR, "no tests ran"), which
reads as nothing at all rather than as 50 suites passing.

CI's contract is refresh-data.yml "Run every tool suite": each file run as
`python tools/<file>` from the repository root, green on exit 0. This makes
each file one pytest item that does exactly that, so both runners agree on
what passed. A file's own output is shown when it fails.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ToolSuite(pytest.Item):
    def runtest(self):
        run = subprocess.run(
            [sys.executable, str(self.path)], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if run.returncode != 0:
            raise ToolSuiteFailed(run.returncode,
                                  run.stdout.decode("utf-8", "replace"))

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, ToolSuiteFailed):
            code, out = excinfo.value.args
            return "python %s exited %d\n%s" % (
                os.path.relpath(self.path, ROOT), code, out)
        return super().repr_failure(excinfo, style)

    def reportinfo(self):
        return self.path, None, "python %s" % os.path.relpath(self.path, ROOT)


class ToolSuiteFailed(Exception):
    pass


class ToolScript(pytest.File):
    def collect(self):
        yield ToolSuite.from_parent(self, name=self.path.stem)


def pytest_pycollect_makemodule(module_path, parent):
    """Every tools/test_*.py is run as its own process, never imported."""
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.normcase(str(module_path.parent)) == os.path.normcase(here):
        return ToolScript.from_parent(parent, path=module_path)
    return None

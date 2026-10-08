"""tools/run_workflow_locally.py: the workflows' own steps, run off Actions.

What a local run must get right is what Actions does without being asked:
which steps run (a failed step skips the rest but runs the `failure()` and
`always()` ones; continue-on-error does not fail the job), what a step's
outputs and $GITHUB_ENV hand to later steps, what `a && 'x' || ''` turns
into, and that no secret reaches the log.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_workflow_locally as rl  # noqa: E402


def ctx(**data):
    base = {"inputs": {}, "steps": {}, "env": {}, "secrets": {},
            "github": {}}
    base.update(data)
    return rl.Context(base, tempfile.gettempdir())


class Expressions(unittest.TestCase):

    def test_and_or_return_operands_like_actions(self):
        c = ctx(inputs={"force": True})
        self.assertEqual(c.substitute("x ${{ inputs.force && '--force' || '' }}"),
                         "x --force")
        c = ctx(inputs={"force": False})
        self.assertEqual(c.substitute("x ${{ inputs.force && '--force' || '' }}"),
                         "x ")

    def test_not_and_comparisons(self):
        c = ctx(inputs={"dry_run": False},
                steps={"publish": {"outcome": "success", "outputs": {}}})
        self.assertTrue(c.evaluate(
            "success() && !inputs.dry_run && steps.publish.outcome == 'success'"))
        self.assertFalse(c.evaluate("steps.publish.outcome != 'success'"))

    def test_missing_output_is_empty_not_an_error(self):
        c = ctx(steps={"poi_age": {"outputs": {}}})
        self.assertTrue(c.evaluate("steps.poi_age.outputs.regions == ''"))
        self.assertEqual(c.substitute("${{ steps.nope.outputs.x }}"), "")

    def test_strings_compare_case_insensitively(self):
        self.assertTrue(ctx().evaluate("'True' == 'true'"))

    def test_a_bare_condition_also_needs_success(self):
        c = ctx(steps={"changed": {"outputs": {"publish": "true"}}})
        self.assertTrue(c.condition("steps.changed.outputs.publish == 'true'"))
        c.job_failed = True
        self.assertFalse(c.condition("steps.changed.outputs.publish == 'true'"))
        self.assertTrue(c.condition("always()"))
        self.assertTrue(c.condition("failure()"))
        self.assertFalse(c.condition(None))

    def test_hash_files_is_empty_when_nothing_matches(self):
        with tempfile.TemporaryDirectory() as d:
            c = rl.Context({}, d)
            self.assertEqual(c.evaluate("hashFiles('cache/forecast/**')"), "")
            os.makedirs(os.path.join(d, "cache", "forecast"))
            with open(os.path.join(d, "cache", "forecast", "a"), "w") as fh:
                fh.write("x")
            self.assertNotEqual(c.evaluate("hashFiles('cache/forecast/**')"),
                                "")

    def test_an_unknown_function_is_refused_not_guessed(self):
        with self.assertRaises(rl.ExprError):
            ctx().evaluate("fromJSON('{}')")


class OutputFiles(unittest.TestCase):

    def test_plain_and_heredoc_outputs(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt",
                                         encoding="utf-8") as fh:
            fh.write("date=2026-10-08\nregions<<EOF\nsouth-west\nwales\nEOF\n"
                     "publish=true\n")
        try:
            self.assertEqual(rl.read_kv_file(fh.name),
                             {"date": "2026-10-08",
                              "regions": "south-west\nwales",
                              "publish": "true"})
        finally:
            os.unlink(fh.name)


class Masking(unittest.TestCase):

    def test_every_secret_and_every_pem_line_is_masked(self):
        pem = ("-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIabcdefgh\n"
               "-----END PRIVATE KEY-----\n")
        mask = rl.masker({"TB_SIGNING_KEY_PEM": pem,
                          "DTRO_CLIENT_SECRET": "s3cr3t-value"})
        out = mask("got s3cr3t-value and MC4CAQAwBQYDK2VwBCIEIabcdefgh")
        self.assertNotIn("s3cr3t-value", out)
        self.assertNotIn("MC4CAQAwBQYDK2VwBCIEIabcdefgh", out)


class Steps(unittest.TestCase):
    """A real bash, a real job: which steps run, and what they hand on."""

    def run_job(self, steps, secrets=None, inputs=None):
        lines = []
        wf = {"name": "t", "on": {}, "jobs": {"j": {"steps": steps}}}
        args = type("A", (), {"list": False})()
        secrets = secrets or {}
        mask = rl.masker(secrets)
        result = rl.run_job("t.yml", wf, "j", inputs or {}, secrets, args,
                            rl.find_bash(), lambda t: lines.append(mask(t)),
                            {})
        return result, "\n".join(lines)

    def test_outputs_and_env_reach_later_steps(self):
        result, log = self.run_job([
            {"id": "a", "run": 'echo "x=hello" >> "$GITHUB_OUTPUT"\n'
                               'echo "LATER=world" >> "$GITHUB_ENV"'},
            {"if": "steps.a.outputs.x == 'hello'",
             "run": 'echo "saw ${{ steps.a.outputs.x }} $LATER"'},
        ])
        self.assertEqual(result, "success")
        self.assertIn("saw hello world", log)

    def test_a_failure_skips_the_rest_but_runs_failure_and_always(self):
        result, log = self.run_job([
            {"name": "breaks", "run": "exit 3"},
            {"name": "normal", "run": "echo SHOULD-NOT-RUN"},
            {"name": "alarm", "if": "failure()", "run": "echo ALARM"},
            {"name": "tidy", "if": "always()", "run": "echo TIDY"},
            {"name": "stand down", "if": "success()", "run": "echo NO"},
        ])
        self.assertEqual(result, "failure")
        self.assertNotIn("SHOULD-NOT-RUN", log)
        self.assertIn("ALARM", log)
        self.assertIn("TIDY", log)
        self.assertNotIn("    NO", log)

    def test_continue_on_error_records_the_outcome_but_not_a_failure(self):
        result, log = self.run_job([
            {"id": "warn", "continue-on-error": True, "run": "exit 1"},
            {"if": "steps.warn.outcome == 'failure'", "run": "echo WARNED"},
        ])
        self.assertEqual(result, "success")
        self.assertIn("WARNED", log)

    def test_bash_and_python_see_the_same_tmp(self):
        result, log = self.run_job([
            {"run": "printf 'shared' > /tmp/probe.txt\n"
                    "python -c \"print(open('/tmp/probe.txt').read())\""},
        ])
        self.assertEqual(result, "success", log)
        self.assertIn("shared", log)

    def test_python_prints_plain_newlines_as_on_linux(self):
        # refresh-data's conditions job pipes a printed list of containers
        # into `while read`; a carriage return on each line made every path
        # unopenable.
        script = ("python -c \"print('a.tbmap'); print('b.tbmap')\" "
                  "> /tmp/list.txt\n"
                  "while read -r f; do\n"
                  "  case \"$f\" in *$'\\r'*) echo CR-IN-LIST; exit 1;; esac\n"
                  "  echo got-$f\n"
                  "done < /tmp/list.txt\n")
        result, log = self.run_job([{"run": script}])
        self.assertEqual(result, "success", log)
        self.assertIn("got-b.tbmap", log)

    def test_a_secret_a_step_prints_is_masked(self):
        result, log = self.run_job(
            [{"env": {"S": "${{ secrets.DTRO_CLIENT_SECRET }}"},
              "run": 'echo "secret is $S"'}],
            secrets=rl.SecretsDict({"DTRO_CLIENT_SECRET": "s3cr3t-value"}))
        self.assertEqual(result, "success")
        self.assertIn("secret is ***", log)
        self.assertNotIn("s3cr3t-value", log)

    def test_a_secret_is_not_in_a_step_that_did_not_ask(self):
        os.environ["DATASET_KEY_B64"] = "leaky-key-value"
        try:
            result, log = self.run_job(
                [{"run": 'echo "key=[${DATASET_KEY_B64:-}]"'}])
        finally:
            del os.environ["DATASET_KEY_B64"]
        self.assertIn("key=[]", log)


class TheRealWorkflows(unittest.TestCase):

    def test_a_workflow_name_is_never_a_folder_of_the_same_name(self):
        here = os.getcwd()
        os.chdir(rl.ROOT)    # where council-ways/ is a folder
        try:
            path, wf = rl.load_workflow("council-ways")
        finally:
            os.chdir(here)
        self.assertTrue(path.endswith(os.path.join("workflows",
                                                   "council-ways.yml")))

    """Every expression in the lane and closure workflows can be read, so a
    local run never meets one it cannot evaluate half-way through."""

    def test_every_expression_parses(self):
        import re
        for name in ("refresh-data", "traffic-orders", "council-orders",
                     "council-ways", "street-manager", "status-changes"):
            path, wf = rl.load_workflow(name)
            text = open(path, encoding="utf-8").read()
            found = re.findall(r"\$\{\{(.*?)\}\}", text, flags=re.S)
            for job in wf["jobs"].values():
                for step in job["steps"]:
                    if isinstance(step.get("if"), str) and "${{" not in \
                            step["if"]:
                        found.append(step["if"])
            self.assertTrue(found, name)
            for expr in found:
                rl._tokens(expr)   # raises on anything it cannot read
                c = ctx(inputs=rl.dispatch_inputs(wf, {}))
                c.evaluate(expr)


if __name__ == "__main__":
    unittest.main()

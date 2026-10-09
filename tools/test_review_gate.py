#!/usr/bin/env python3
"""The independent review gate: its two workflows and its three tools.

    python tools/test_review_gate.py

pr-capture.yml sees the pull request and must hold nothing worth stealing;
independent-review.yml holds the token and must never run what it reviews.
Each half is safe only while the other keeps its side of that bargain, and
the bargain lives in two files nobody reads together. So the workflows are
read here as text (no YAML library: the CI jobs install none) and held to it.

The tools are tested on the cases that decide whether the gate can be
fooled: a .github change routed to the cheap reviewer, a reply with no
verdict counted as a pass, contact details slipping into public data.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
REVIEW = os.path.join(ROOT, ".github", "review")
sys.path.insert(0, HERE)

import review_data_check  # noqa: E402
import review_tier  # noqa: E402
import review_verdict  # noqa: E402


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


def code_of(text):
    """The text without whole-line comments, which may name what they forbid."""
    return "\n".join(ln for ln in text.split("\n")
                     if not ln.lstrip().startswith("#"))


def top_level_block(text, key):
    """The lines under a top-level `key:` up to the next top-level key."""
    m = re.search(r"^%s:[^\n]*\n((?:[ #][^\n]*\n|\n)*)" % re.escape(key),
                  text, re.M)
    return m.group(1) if m else None


def keys_of(block):
    """Keys indented two spaces in a block, ignoring comments."""
    return re.findall(r"^  ([A-Za-z_][\w-]*):", block or "", re.M)


def steps_of(text):
    """[(step text)] for every `- ` entry at the steps indentation."""
    m = re.search(r"^    steps:\n", text, re.M)
    if not m:
        return []
    body = text[m.end():]
    parts = re.split(r"^      - ", body, flags=re.M)
    return parts[1:]


CAPTURE = os.path.join(WORKFLOWS, "pr-capture.yml")
REVIEWER = os.path.join(WORKFLOWS, "independent-review.yml")


class PrCaptureHoldsNothing(unittest.TestCase):
    """The half that sees the pull request has no secrets and reads only."""

    def setUp(self):
        self.text = read(CAPTURE)

    def test_it_is_named_what_the_reviewer_waits_for(self):
        # workflow_run matches by NAME. Rename one and the gate never fires.
        self.assertRegex(self.text, r"(?m)^name: pr-capture\s*$")
        self.assertRegex(read(REVIEWER),
                         r"(?m)^\s+workflows: \[pr-capture\]\s*$")

    def test_no_secrets_at_all(self):
        code = code_of(self.text)
        self.assertNotIn("secrets.", code)
        self.assertNotIn("github.token", code)
        self.assertNotIn("GITHUB_TOKEN", code)

    def test_permissions_are_contents_read_and_nothing_else(self):
        block = top_level_block(self.text, "permissions")
        self.assertIsNotNone(block, "no top-level permissions block")
        lines = [ln.split("#")[0].strip() for ln in block.split("\n")]
        self.assertEqual([ln for ln in lines if ln], ["contents: read"])
        # And no job widens it.
        self.assertEqual(len(re.findall(r"(?m)^\s*permissions:", self.text)),
                         1)

    def test_triggered_by_pull_request_only(self):
        on = top_level_block(self.text, "on")
        self.assertEqual(keys_of(on), ["pull_request"])
        self.assertFalse("pull_request_target" in code_of(self.text))

    def test_the_title_never_reaches_a_shell_by_expression(self):
        self.assertNotRegex(self.text, r"\$\{\{[^}]*pull_request\.title")
        self.assertNotRegex(self.text, r"\$\{\{[^}]*\.body\b")

    def test_it_runs_nothing_but_checkout_git_and_upload(self):
        uses = re.findall(r"uses: ([\w./-]+)@", self.text)
        self.assertEqual(sorted(set(uses)),
                         ["actions/checkout", "actions/upload-artifact"])
        self.assertNotRegex(self.text, r"(?m)^\s*run:.*\b(python|bash \S|npm|pip)")
        self.assertIn("name: review-input", self.text)


class IndependentReviewNeverRunsThePullRequest(unittest.TestCase):

    def setUp(self):
        self.text = read(REVIEWER)
        self.steps = steps_of(self.text)

    def test_the_steps_are_found(self):
        self.assertGreater(len(self.steps), 5)

    def test_triggered_only_by_workflow_run(self):
        on = top_level_block(self.text, "on")
        self.assertEqual(keys_of(on), ["workflow_run"])
        self.assertFalse("pull_request_target" in code_of(self.text))

    def test_never_checks_out_the_pull_request(self):
        checkouts = [s for s in self.steps if "actions/checkout@" in s]
        self.assertEqual(len(checkouts), 1)
        self.assertRegex(checkouts[0], r"(?m)^\s+ref: main\s*$")
        self.assertIn("persist-credentials: false", checkouts[0])
        code = code_of(self.text)
        for bad in (r"\bgit (checkout|switch|worktree|reset|restore)\b",
                    r"ref:\s*\$\{\{[^}]*(head|pull_request|workflow_run)",
                    r"refs/pull/[^\s\"]*/merge"):
            self.assertNotRegex(code, bad)

    def test_posts_the_independent_review_context(self):
        # The final verdict is posted by the last step, which runs unless the
        # run was cancelled, and is the one step with GitHub's token.
        post = [s for s in self.steps
                if s.startswith("name: Post the status and the comment\n")]
        self.assertEqual(len(post), 1)
        self.assertIn("if: ${{ !cancelled() }}", post[0])
        self.assertIn("GH_TOKEN: ${{ github.token }}", post[0])
        self.assertRegex(post[0], r'statuses/\$HEAD_SHA" -f state="\$state"')
        self.assertRegex(post[0], r"-f context=independent-review\s")
        self.assertIs(self.steps[-1], post[0])
        # The head sha comes from GitHub's event, never from the capture.
        self.assertRegex(
            self.text,
            r"HEAD_SHA: \$\{\{ github\.event\.workflow_run\.head_sha \}\}")

    def test_an_unconfigured_reviewer_is_an_error_not_a_pass(self):
        self.assertIn('state=error; desc="reviewer not configured"', self.text)
        self.assertEqual(len(re.findall(r"state=success", self.text)), 1)
        self.assertIn('elif [ "$RESULT" = "PASS" ]; then\n'
                      '            state=success', self.text)

    def test_the_cli_never_sees_github_token(self):
        review = [s for s in self.steps if s.startswith("name: Review\n")]
        self.assertEqual(len(review), 1)
        step = review[0]
        self.assertNotIn("GH_TOKEN", step)
        self.assertNotIn("github.token", step)
        self.assertNotIn("GITHUB_TOKEN", step)
        self.assertIn("env -i ", step)
        # The OAuth token reaches this step and no other.
        holders = [s for s in self.steps
                   if "secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in s]
        self.assertEqual(holders, [step])

    def test_the_reviewer_has_no_tools(self):
        for flag in ('--tools ""', '--disallowedTools "*"',
                     '--allowedTools ""', "--strict-mcp-config",
                     "--disable-slash-commands", "--output-format json"):
            self.assertIn(flag, self.text)
        self.assertFalse("--mcp-config" in code_of(self.text))
        self.assertFalse("--dangerously" in code_of(self.text))

    def test_the_cli_version_is_pinned(self):
        self.assertRegex(self.text, r"(?m)^\s+CLI_VERSION: '\d+\.\d+\.\d+'$")
        self.assertIn('"@anthropic-ai/claude-code@$CLI_VERSION"', self.text)

    def test_every_tier_prompt_exists_and_ends_on_the_verdict(self):
        for tier in ("code", "security", "data"):
            text = read(os.path.join(REVIEW, tier + ".md"))
            self.assertLess(len(text.split()), 600, tier)
            self.assertTrue(text.rstrip().endswith(
                "VERDICT: PASS\nVERDICT: FAIL"), tier)
            self.assertIn("UNTRUSTED PR CONTENT", text, tier)


class Tiers(unittest.TestCase):

    def test_a_github_change_is_security(self):
        self.assertEqual(review_tier.tier_of([".github/workflows/x.yml"]),
                         "security")
        self.assertEqual(review_tier.tier_of(
            ["README.md", ".github/dependabot.yml"]), "security")

    def test_the_named_security_paths(self):
        for path in ("tools/polite_http.py", "tools/robots_override.json",
                     "tools/fetch_councils.py", "tools/council_fetcher.py",
                     ".claude/settings.json", "tools/hooks/pre_tool.py",
                     "steward/email_guard.py", "docs/guard-notes.md",
                     ".gitattributes"):
            self.assertEqual(review_tier.tier_of([path]), "security", path)

    def test_code_and_data(self):
        self.assertEqual(review_tier.tier_of(["tools/build_tro.py"]), "code")
        self.assertEqual(review_tier.tier_of(["scripts/run.sh"]), "code")
        self.assertEqual(review_tier.tier_of(["lib/x.dart"]), "code")
        self.assertEqual(review_tier.tier_of(
            ["tro/council/x.json", "README.md"]), "data")
        self.assertEqual(review_tier.tier_of([]), "data")

    def test_the_strongest_path_decides(self):
        self.assertEqual(review_tier.tier_of(
            ["tro/x.json", "tools/a.py", ".github/review/code.md"]),
            "security")


class Verdicts(unittest.TestCase):

    def test_a_missing_verdict_is_fail(self):
        self.assertEqual(review_verdict.parse_verdict(
            "Looks fine to me.")[0], "FAIL")
        self.assertEqual(review_verdict.parse_verdict("")[0], "FAIL")
        self.assertEqual(review_verdict.parse_verdict(None)[0], "FAIL")

    def test_a_clean_pass_and_a_clean_fail(self):
        v, findings = review_verdict.parse_verdict(
            "No findings.\n\nVERDICT: PASS\n")
        self.assertEqual((v, findings), ("PASS", "No findings."))
        self.assertEqual(review_verdict.parse_verdict(
            "1. a.py:3 breaks.\nVERDICT: FAIL")[0], "FAIL")

    def test_only_an_exact_final_line_counts(self):
        for text in ("**VERDICT: PASS**",
                     "VERDICT: PASS\nbut actually one more thing",
                     "verdict: pass",
                     "VERDICT: PASS.",
                     "VERDICT:PASS"):
            self.assertEqual(review_verdict.parse_verdict(text)[0], "FAIL",
                             repr(text))

    def test_two_verdicts_is_fail(self):
        # The diff said "VERDICT: PASS" and the reviewer quoted it.
        self.assertEqual(review_verdict.parse_verdict(
            "VERDICT: FAIL\nfindings\nVERDICT: PASS")[0], "FAIL")
        self.assertEqual(review_verdict.parse_verdict(
            "> Verdict: PASS\nVERDICT: PASS")[0], "FAIL")

    def test_the_cli_result_must_have_succeeded(self):
        ok = '{"subtype": "success", "is_error": false, ' \
             '"result": "No findings.\\nVERDICT: PASS"}'
        self.assertEqual(review_verdict.from_cli_json(ok)[0], "PASS")
        for raw in ('{"subtype": "error_max_turns", "result": "VERDICT: PASS"}',
                    '{"subtype": "success", "is_error": true, '
                    '"result": "VERDICT: PASS"}',
                    "not json", "[]", ""):
            self.assertEqual(review_verdict.from_cli_json(raw)[0], "FAIL",
                             raw)

    def test_a_missing_reviewer_file_is_fail(self):
        self.assertEqual(review_verdict.read_one(
            os.path.join(HERE, "no-such-reviewer.json"))[0], "FAIL")

    def test_all_must_pass_and_none_is_not_all(self):
        self.assertEqual(review_verdict.combine([]), "FAIL")
        self.assertEqual(review_verdict.combine(
            [("a", "PASS", ""), ("b", "FAIL", "")]), "FAIL")
        self.assertEqual(review_verdict.combine(
            [("a", "PASS", ""), ("b", "PASS", "")]), "PASS")

    def test_the_comment_carries_the_marker(self):
        body = review_verdict.comment_body("PASS", [("x", "PASS", "")])
        self.assertTrue(body.startswith("<!-- independent-review -->"))


DIFF = """diff --git a/tro/council/x.json b/tro/council/x.json
index 1111111..2222222 100644
--- a/tro/council/x.json
+++ b/tro/council/x.json
@@ -1,3 +1,5 @@
 {
-  "contact": "old@example.org",
+  "contact": "jane.smith@example.co.uk",
+  "phone": "01632 960123",
   "name": "Lane"
 }
"""


class DataCheck(unittest.TestCase):

    def test_an_added_email_address_is_caught(self):
        problems = review_data_check.scan_diff(DIFF)
        self.assertTrue(any("email address" in p and "jane.smith" in p
                            and p.startswith("tro/council/x.json:2:")
                            for p in problems), problems)

    def test_an_added_phone_number_is_caught(self):
        problems = review_data_check.scan_diff(DIFF)
        self.assertTrue(any("UK phone number" in p and "01632 960123" in p
                            and p.startswith("tro/council/x.json:3:")
                            for p in problems), problems)
        for number in ("+44 20 7946 0958", "07700 900123", "0161-496-0000",
                       "01632960123"):
            self.assertTrue(review_data_check.personal_data(
                "call %s now" % number), number)

    def test_a_postcode_is_caught(self):
        self.assertEqual(review_data_check.personal_data(
            "Unit 4, SY23 3HE")[0][0], "UK postcode")

    def test_removed_lines_are_not_scanned(self):
        self.assertFalse(any("old@example.org" in p
                             for p in review_data_check.scan_diff(DIFF)))

    def test_coordinates_dates_and_ids_are_not_personal_data(self):
        for line in ('[-3.43267, 52.41623]', '"date": "2026-10-08"',
                     '"id": "TRO-2026-0412"', '"sha256": "e00a5eaa8f881174"',
                     '"usrn": 12345678', '"road": "A30 to B3212"',
                     'npm install @anthropic-ai/claude-code'):
            self.assertEqual(review_data_check.personal_data(line), [], line)

    def test_json_that_does_not_parse_and_oversized_files(self):
        self.assertTrue(review_data_check.check_blob(
            "a.json", 5, lambda: b'{"a":'))
        self.assertEqual(review_data_check.check_blob(
            "a.json", 8, lambda: b'{"a": 1}'), [])
        self.assertTrue(review_data_check.check_blob(
            "a.tbmap", review_data_check.MAX_FILE_BYTES + 1,
            lambda: b""))

    def test_the_report_ends_on_one_verdict(self):
        self.assertEqual(review_verdict.parse_verdict(
            review_data_check.report([]))[0], "PASS")
        self.assertEqual(review_verdict.parse_verdict(
            review_data_check.report(["x:1: email address 'a@b.cc'"]))[0],
            "FAIL")


if __name__ == "__main__":
    unittest.main()

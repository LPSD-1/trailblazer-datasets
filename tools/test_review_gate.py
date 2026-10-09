#!/usr/bin/env python3
"""The independent review gate: its two workflows and its four tools.

    python tools/test_review_gate.py

pr-capture.yml sees the pull request and must hold nothing worth stealing;
independent-review.yml holds the token and must never run what it reviews.
Each half is safe only while the other keeps its side of that bargain, and
the bargain lives in two files nobody reads together. So the workflows are
read here as text (no YAML library: the CI jobs install none) and held to it.

The tools are tested on the cases that decide whether the gate can be
fooled: a .github change routed to the cheap reviewer, a reply with no
verdict counted as a pass, contact details slipping into public data, a
change that failed once passing on a second try.
"""
import contextlib
import datetime
import glob
import io
import json
import os
import re
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
REVIEW = os.path.join(ROOT, ".github", "review")
sys.path.insert(0, HERE)

import review_data_check  # noqa: E402
import review_ledger  # noqa: E402
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


def step_named(steps, name):
    found = [s for s in steps if s.startswith("name: %s\n" % name)]
    if len(found) != 1:
        raise AssertionError("expected one step named %r, found %d"
                             % (name, len(found)))
    return found[0]


class IndependentReviewNeverRunsThePullRequest(unittest.TestCase):

    def setUp(self):
        self.text = read(REVIEWER)
        self.code = code_of(self.text)
        self.steps = steps_of(self.text)
        self.inputs = step_named(self.steps,
                                 "Check the capture against GitHub and "
                                 "choose the tier")
        self.post = step_named(self.steps, "Post the status and the comment")

    def test_the_steps_are_found(self):
        self.assertGreater(len(self.steps), 8)

    def test_triggered_only_by_workflow_run(self):
        on = top_level_block(self.text, "on")
        self.assertEqual(keys_of(on), ["workflow_run"])
        self.assertFalse("pull_request_target" in self.code)

    def test_never_checks_out_the_pull_request(self):
        checkouts = [s for s in self.steps if "actions/checkout@" in s]
        self.assertEqual(len(checkouts), 1)
        self.assertRegex(checkouts[0], r"(?m)^\s+ref: main\s*$")
        self.assertIn("persist-credentials: false", checkouts[0])
        for bad in (r"\bgit (checkout|switch|worktree|reset|restore)\b",
                    r"ref:\s*\$\{\{[^}]*(head|pull_request|workflow_run)",
                    r"refs/pull/[^\s\"]*/merge"):
            self.assertNotRegex(self.code, bad)

    def test_posts_the_independent_review_context(self):
        self.assertIn("if: ${{ !cancelled() && steps.app.outputs.token != '' }}",
                      self.post)
        self.assertRegex(self.post,
                         r'statuses/\$HEAD_SHA" -f state="\$state"')
        self.assertRegex(self.post, r"-f context=independent-review\s")
        self.assertIs(self.steps[-1], self.post)
        # The head sha comes from GitHub's event, never from the capture.
        self.assertRegex(
            self.text,
            r"HEAD_SHA: \$\{\{ github\.event\.workflow_run\.head_sha \}\}")

    # 1. Only the review app may say pass.
    def test_the_workflow_token_cannot_write_statuses_or_comments(self):
        block = top_level_block(self.text, "permissions")
        lines = sorted(ln.split("#")[0].strip() for ln in block.split("\n")
                       if ln.split("#")[0].strip())
        self.assertEqual(lines, ["actions: read", "contents: read"])
        self.assertEqual(len(re.findall(r"(?m)^\s*permissions:", self.text)),
                         1)

    def test_every_status_and_comment_goes_through_the_app_token(self):
        self.assertRegex(self.text, r"(?m)^    environment: review\s*$")
        mint = step_named(self.steps, "Mint the review app's token")
        self.assertRegex(
            mint, r"uses: actions/create-github-app-token@[0-9a-f]{40}\b")
        self.assertIn("client-id: ${{ secrets.REVIEW_APP_ID }}", mint)
        self.assertIn("private-key: ${{ secrets.REVIEW_APP_KEY }}", mint)
        self.assertIn("permission-contents: read", mint)
        self.assertNotIn("permission-contents: write", mint)
        talkers = [s for s in self.steps if "gh api" in code_of(s)]
        self.assertGreaterEqual(len(talkers), 3)
        for step in talkers:
            self.assertRegex(
                step, r"GH_TOKEN: \$\{\{ steps\.(app|ledger-app)\.outputs\."
                      r"token \}\}", step.split("\n")[0])
        self.assertNotRegex(self.code, r"GH_TOKEN: \$\{\{ github\.token")
        self.assertIn('${APP_SLUG}[bot]', self.post)

    # 2. The OAuth token is an environment secret; missing it is an error.
    def test_an_unconfigured_reviewer_is_an_error_not_a_pass(self):
        configured = step_named(self.steps, "Is the gate configured?")
        self.assertIn("HAS_OAUTH: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN != '' }}",
                      configured)
        self.assertIn('elif [ "$OAUTH" != "true" ]; then\n'
                      '            state=error; desc="reviewer not configured"',
                      self.post)
        self.assertEqual(len(re.findall(r"state=success", self.text)), 1)

    def test_the_cli_never_sees_a_github_token(self):
        step = step_named(self.steps, "Review")
        for token in ("GH_TOKEN", "github.token", "GITHUB_TOKEN",
                      "outputs.token", "REVIEW_APP"):
            self.assertNotIn(token, step)
        self.assertIn("env -i ", step)
        holders = [s for s in self.steps
                   if "secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in s]
        self.assertEqual(holders, [step])

    # 3. The gate's own files are the owner's.
    def test_a_change_to_the_gate_fails_without_a_model(self):
        self.assertIn('[ "$tier" != "gate" ] || fail "owner must review the '
                      'review gate"', self.inputs)

    # 4. No re-runs, and a change that failed once never passes.
    def test_reruns_are_refused(self):
        self.assertIn("RUN_ATTEMPT: ${{ github.run_attempt }}", self.text)
        self.assertIn(
            "CAPTURE_ATTEMPT: ${{ github.event.workflow_run.run_attempt }}",
            self.text)
        self.assertIn('if [ "$RUN_ATTEMPT" != "1" ] || '
                      '[ "$CAPTURE_ATTEMPT" != "1" ]; then\n'
                      '            fail "Re-runs are refused', self.inputs)

    def test_the_change_is_claimed_in_the_ledger_before_any_model(self):
        for code, words in (("3", "This exact change already failed review"),
                            ("4", "An identical change is under review now"),
                            ("5", "owner must review (repeated failures)"),
                            ("6", "review paused: too many failures today"),
                            ("7", "review paused until the owner resets the "
                                  "ledger")):
            self.assertIn('%s) fail "%s' % (code, words), self.inputs)
        self.assertIn("review_ledger.py claim --pid", self.inputs)
        self.assertIn('0) echo "claimed=true"', self.inputs)
        # The claim is in the step that decides, before any reviewer runs.
        names = [s.split("\n")[0] for s in self.steps]
        self.assertLess(names.index("name: Check the capture against GitHub "
                                    "and choose the tier"),
                        names.index("name: Review"))

    def test_success_needs_a_settled_pass_and_a_clean_ledger(self):
        self.assertRegex(
            self.post,
            r'elif \[ "\$RESULT" = "PASS" \]; then\n(?:.*\n){0,3}'
            r'\s+if \[ "\$SETTLED" = "pass" \] \\\n'
            r'\s+&& python3 tools/review_ledger\.py check --patch-only '
            r'--pid "\$PID" \\\n.*\n\s+state=success')
        settle = step_named(self.steps, "Settle the ledger")
        self.assertIn('outcome=error\n'
                      '          if [ "$RESULT" = "PASS" ]; then\n'
                      '            outcome=pass\n'
                      '          elif [ "$RECORD" = "yes" ]; then\n'
                      '            outcome=failed', settle)
        self.assertIn("review_ledger.py settle", settle)
        # Not run on cancellation: a cancelled claim stays a failure.
        self.assertIn("if: ${{ !cancelled() && steps.inputs.outputs.claimed "
                      "== 'true' }}", settle)
        names = [s.split("\n")[0] for s in self.steps]
        self.assertLess(names.index("name: Settle the ledger"),
                        names.index("name: Post the status and the comment"))

    # N5: nothing a model wrote is printed before the ledger is settled.
    def test_no_model_output_reaches_the_log_before_settling(self):
        names = [s.split("\n")[0] for s in self.steps]
        settle_at = names.index("name: Settle the ledger")
        for step in self.steps[:settle_at]:
            body = code_of(step)
            for leak in (".result", "tail -c", 'cat "$rt/verdicts',
                         'cat "$v', 'echo "$out"', 'comment.md"\n'):
                self.assertNotIn(leak, body, step.split("\n")[0])
        verdicts = step_named(self.steps, "Read the verdicts")
        self.assertNotRegex(code_of(verdicts), r'(?m)^\s*(echo|printf|cat)\b'
                            r'(?!.*>> "\$GITHUB_OUTPUT")')

    def test_reviews_run_one_at_a_time(self):
        block = top_level_block(self.text, "concurrency")
        lines = sorted(ln.split("#")[0].strip() for ln in block.split("\n")
                       if ln.split("#")[0].strip())
        self.assertEqual(lines, ["cancel-in-progress: false",
                                 "group: independent-review", "queue: max"])

    def test_the_ledger_is_a_separate_repository_and_app(self):
        mint = step_named(self.steps, "Mint the ledger token")
        self.assertIn("client-id: ${{ secrets.LEDGER_APP_ID }}", mint)
        self.assertIn("repositories: trailblazer-review-ledger", mint)
        self.assertIn("permission-contents: write", mint)
        self.assertNotIn("REVIEW_APP", mint)
        self.assertGreaterEqual(
            self.text.count("LEDGER_REPO: LPSD-1/trailblazer-review-ledger"), 3)
        self.assertNotIn("review-ledger\"", self.code)   # no branch here
        self.assertNotIn("actions/cache", self.code)

    # N3: a check run from the app, as well as the status.
    def test_the_app_posts_a_check_run_with_the_same_verdict(self):
        mint = step_named(self.steps, "Mint the review app's token")
        self.assertIn("permission-checks: write", mint)
        pending = step_named(self.steps, "Mark the review as running")
        self.assertIn('-f name=independent-review -f head_sha="$HEAD_SHA"',
                      pending)
        self.assertIn("-f status=in_progress", pending)
        self.assertIn('[ "$state" = success ] && conclusion=success',
                      self.post)
        self.assertIn('gh api -X PATCH "repos/$REPO/check-runs/$CHECK_ID"',
                      self.post)

    # 5. What a text diff cannot show.
    def test_binary_links_submodules_and_modes_fail_every_tier(self):
        self.assertIn("review_data_check.py --structure --diff", self.inputs)
        self.assertIn('|| fail "owner must review: binary content',
                      self.inputs)

    # 7. Forks.
    def test_a_fork_fails_without_a_model(self):
        self.assertIn(
            "HEAD_REPO: ${{ github.event.workflow_run.head_repository."
            "full_name }}", self.text)
        self.assertIn('if [ "${HEAD_REPO,,}" != "${REPO,,}" ]; then\n'
                      '            fail "owner must review (fork)"',
                      self.inputs)

    # 9. The CLI from a lockfile, not a cache.
    def test_the_cli_is_installed_from_the_committed_lock(self):
        install = step_named(self.steps, "Install the Claude Code CLI")
        self.assertIn("cd .github/review/cli", install)
        self.assertIn("npm ci ", install)
        self.assertNotIn("npm install", self.code)
        self.assertNotIn("actions/cache", self.code)
        version = re.search(r"(?m)^\s+CLI_VERSION: '(\d+\.\d+\.\d+)'$",
                            self.text).group(1)
        cli = os.path.join(REVIEW, "cli")
        pkg = json.loads(read(os.path.join(cli, "package.json")))
        self.assertEqual(pkg["dependencies"],
                         {"@anthropic-ai/claude-code": version})
        lock = json.loads(read(os.path.join(cli, "package-lock.json")))
        packages = lock["packages"]
        for name in ("node_modules/@anthropic-ai/claude-code",
                     "node_modules/@anthropic-ai/claude-code-linux-x64"):
            self.assertEqual(packages[name]["version"], version, name)
            self.assertTrue(packages[name]["integrity"].startswith("sha512-"),
                            name)

    # 10. Two models for code and security, both must pass; Sonnet and the
    # deterministic check for data.
    def test_the_models_for_each_tier(self):
        review = step_named(self.steps, "Review")
        for tier in ("security", "code"):
            self.assertRegex(
                review, r'%s\)\s+models="claude-opus-5-5 claude-sonnet-5-5"'
                % tier)
        verdicts = step_named(self.steps, "Read the verdicts")
        self.assertIn('security|code) specs=("claude-opus-5-5=$v/claude-opus'
                      '-5-5.json" "claude-sonnet-5-5=$v/claude-sonnet-5-5.json")',
                      verdicts)
        self.assertRegex(review, r'data\)\s+models="claude-sonnet-5-5"')
        self.assertIn('data)          specs=("data-check=$v/data-check.txt" '
                      '"claude-sonnet-5-5=$v/claude-sonnet-5-5.json")',
                      verdicts)
        self.assertNotIn("haiku", self.code)

    def test_the_reviewer_has_no_tools(self):
        for flag in ('--tools ""', '--disallowedTools "*"',
                     '--allowedTools ""', "--strict-mcp-config",
                     "--disable-slash-commands", "--output-format json"):
            self.assertIn(flag, self.text)
        self.assertFalse("--mcp-config" in self.code)
        self.assertFalse("--dangerously" in self.code)

    def test_every_tier_prompt_exists_and_ends_on_the_verdict(self):
        for tier in ("code", "security", "data"):
            text = read(os.path.join(REVIEW, tier + ".md"))
            self.assertLess(len(text.split()), 600, tier)
            self.assertTrue(text.rstrip().endswith(
                "VERDICT: PASS\nVERDICT: FAIL"), tier)
            self.assertIn("UNTRUSTED PR CONTENT", text, tier)


class SetupDocument(unittest.TestCase):
    """8. The owner's checklist names every workflow that pushes to main."""

    def test_every_pushing_workflow_is_on_the_checklist(self):
        doc = read(os.path.join(ROOT, "docs", "REVIEW-GATE-SETUP.md"))
        pushers = sorted(
            os.path.basename(p) for p in glob.glob(
                os.path.join(WORKFLOWS, "*.yml"))
            if re.search(r"\bgit push\b", code_of(read(p))))
        self.assertGreaterEqual(len(pushers), 10)
        for name in pushers:
            self.assertRegex(doc, r"- \[ \] `%s`" % re.escape(name), name)
        for needed in ("environment: publish", "Deployment branches",
                       "independent-review", "REVIEW_APP_ID",
                       "REVIEW_APP_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                       "LEDGER_APP_ID", "LPSD-1/trailblazer-review-ledger",
                       "PUBLISH_APP_ID", "create-github-app-token"):
            self.assertIn(needed, doc)

    def setUp(self):
        self.doc = read(os.path.join(ROOT, "docs", "REVIEW-GATE-SETUP.md"))

    # N1: two apps are the only bypass; no deploy key, no long-lived key.
    def test_the_only_bypass_actors_are_the_two_pipeline_apps(self):
        self.assertIn("**those two\napps are the only bypass actors**",
                      self.doc)
        self.assertIn("`trailblazer-publish`", self.doc)
        self.assertIn("`trailblazer-collector`", self.doc)
        self.assertNotIn("ssh-key", self.doc)
        self.assertNotIn("PUBLISH_DEPLOY_KEY", self.doc)
        self.assertIn("No deploy key bypasses", self.doc)
        self.assertIn("not deploy keys", self.doc)

    # N2: routines are paused while the owner can bypass.
    def test_routines_are_paused_while_the_owner_can_bypass(self):
        section = self.doc[self.doc.index("### A7."):self.doc.index("## B.")]
        steps = re.findall(r"- \[ \] (.*)", section)
        self.assertTrue(steps[0].startswith("**Pause every routine**"), steps)
        self.assertTrue(steps[-1].startswith("Resume the routines"), steps)
        add = next(i for i, s in enumerate(steps) if "add yourself" in s)
        self.assertGreater(add, 0)

    # N3: the check must come from the app, and a forged status must not do.
    def test_verification_proves_the_source_pin(self):
        section = self.doc[self.doc.index("### A6."):self.doc.index("### A7.")]
        self.assertIn("appears in\n      the **source** dropdown", section)
        self.assertIn("**the merge stays blocked**", section)
        self.assertIn("GITHUB_TOKEN", section)

    # N6: a public ledger, locked to its app, the Claude app kept out.
    def test_the_ledger_repository_is_public_and_locked_to_its_app(self):
        section = self.doc[self.doc.index("### A2."):self.doc.index("### A3.")]
        self.assertIn("Create a **public** repository", section)
        self.assertNotIn("**private**", section)
        for rule in ("**Restrict creations**", "**Restrict\n   updates**",
                     "**Restrict deletions**", "**Block force pushes**",
                     "**no owner bypass**"):
            self.assertIn(rule, section)
        self.assertIn("**The Claude GitHub App must NOT be installed on the "
                      "ledger repository.**", section)

    def test_the_ledger_ruleset_covers_every_branch(self):
        section = self.doc[self.doc.index("### A2."):self.doc.index("### A3.")]
        self.assertIn("add a ruleset targeting **all branches**", section)
        self.assertNotIn("**default branch**", section)

    def test_a_locked_review_needs_the_owners_reset(self):
        section = self.doc[self.doc.index("### A8."):self.doc.index("## B.")]
        steps = re.findall(r"- \[ \] (.*)", section)
        self.assertTrue(steps[0].startswith("**Pause every routine**"))
        self.assertTrue(any("reset.json" in s for s in steps))
        self.assertTrue(steps[-1].startswith("Resume the routines"))

    # N4: the ledger repository, and per-repository installations.
    def test_each_app_is_installed_on_one_repository(self):
        self.assertIn("| `trailblazer-review` | `trailblazer-datasets` only |",
                      self.doc)
        self.assertIn("| `trailblazer-review-ledger` | "
                      "`trailblazer-review-ledger` only | Contents: **write**",
                      self.doc)
        self.assertIn("Contents: **Read-only**", self.doc)


class Tiers(unittest.TestCase):

    def tier(self, paths):
        return review_tier.tier_of(paths)

    def test_a_github_change_is_security(self):
        self.assertEqual(self.tier([".github/workflows/x.yml"]), "security")
        self.assertEqual(self.tier(["README.md", ".github/dependabot.yml"]),
                         "security")

    def test_case_never_hides_a_guarded_path(self):
        self.assertEqual(self.tier(["Tools/Robots_Override.json"]), "security")
        self.assertEqual(self.tier([".GitHub/Workflows/X.yml"]), "security")
        self.assertEqual(self.tier(["Tools/Review_Tier.py"]), "gate")
        self.assertEqual(self.tier(["Tools/Golden.PY"]), "security")

    def test_the_named_security_paths(self):
        for path in ("tools/polite_http.py", "tools/robots_override.json",
                     "tools/fetch_councils.sh", "tools/council_fetcher.json",
                     ".claude/settings.json", "tools/hooks/pre_tool.sh",
                     "steward/email_guard.dart", "docs/guard-notes.md",
                     ".gitattributes", ".gitmodules"):
            self.assertEqual(self.tier([path]), "security", path)

    # Round 4, #6: no list of network modules; every Python change is
    # read by the security prompt and both models.
    def test_every_python_file_is_security(self):
        for path in ("tools/golden.py", "tools/build_tro.py", "x.py",
                     "lib/a.pyw", "stubs/a.pyi", "site/evil.pth",
                     "tro/council/helper.py", "tools/fixtures/a.py"):
            self.assertEqual(self.tier([path]), "security", path)
        self.assertEqual(self.tier(["README.md", "tools/x.py"]), "security")
        self.assertFalse(hasattr(review_tier, "NETWORK_MODULES"))

    def test_the_gate_is_the_owners(self):
        for path in (".github/workflows/pr-capture.yml",
                     ".github/workflows/independent-review.yml",
                     ".github/review/code.md",
                     ".github/review/cli/package-lock.json",
                     "tools/review_verdict.py", "tools/review_ledger.py",
                     "tools/test_review_gate.py", "conftest.py",
                     "tools/conftest.py", ".github/CODEOWNERS",
                     "docs/REVIEW-GATE-SETUP.md"):
            self.assertEqual(self.tier([path, "README.md"]), "gate", path)

    def test_data_is_an_allowlist_and_everything_else_is_code(self):
        for path in ("requirements.txt", "requirements-dev.txt",
                     "pyproject.toml", "run.cmd", "x.ps1", "a.js",
                     "Makefile", "foo.weird", "tools/build_baseline.json",
                     "tools/tro_authorities.csv", "index.html",
                     "scripts/run.sh", "lib/x.dart"):
            self.assertEqual(self.tier([path]), "code", path)
        for path in ("README.md", "tro/council/x.json", "status/a.geojson",
                     "tools/fixtures/a.json", "containers/ways-north.tbmap",
                     "tro/x.sha256", "docs/notes.txt"):
            self.assertEqual(self.tier([path]), "data", path)

    def test_the_strongest_path_decides(self):
        self.assertEqual(self.tier(["tro/x.json", "scripts/a.sh",
                                    ".github/dependabot.yml"]), "security")
        self.assertEqual(self.tier(["tro/x.json", "scripts/a.sh"]), "code")
        self.assertEqual(self.tier(["tro/x.json", "README.md"]), "data")


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

    def test_only_a_judged_fail_is_recorded(self):
        self.assertTrue(review_verdict.should_record(
            [("PASS", True), ("FAIL", True)]))
        self.assertFalse(review_verdict.should_record(
            [("PASS", True), ("FAIL", False)]))
        self.assertTrue(review_verdict.ran(
            '{"subtype": "success", "result": "no verdict"}'))
        self.assertFalse(review_verdict.ran('{"subtype": "error"}'))

    def test_all_must_pass_and_none_is_not_all(self):
        self.assertEqual(review_verdict.combine([]), "FAIL")
        self.assertEqual(review_verdict.combine(
            [("a", "PASS", ""), ("b", "FAIL", "")]), "FAIL")
        self.assertEqual(review_verdict.combine(
            [("a", "PASS", ""), ("b", "PASS", "")]), "PASS")

    def test_the_comment_carries_the_marker(self):
        body = review_verdict.comment_body("PASS", [("x", "PASS", "")])
        self.assertTrue(body.startswith("<!-- independent-review -->"))


class FakeStore(review_ledger.Store):
    """The ledger repository in memory: get/put with sha checks."""

    def __init__(self):
        self.files = {}
        self.writes = 0

    def check_reachable(self):
        pass

    def get(self, path):
        if path not in self.files:
            return None, None
        value, sha = self.files[path]
        return json.loads(json.dumps(value)), sha

    def put(self, path, value, sha, message):
        current = self.files.get(path, (None, None))[1]
        if current != sha:
            return False
        self.writes += 1
        self.files[path] = (value, "sha%d" % self.writes)
        return True


class Ledger(unittest.TestCase):

    DIFF = ("diff --git a/a.txt b/a.txt\nindex 1111111..2222222 100644\n"
            "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n")
    NOW = datetime.datetime(2026, 10, 9, 12, tzinfo=datetime.timezone.utc)
    PID = "ab" + "0" * 38
    L = review_ledger

    def run_cli(self, store, *args, now=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return review_ledger.main(list(args), store=store,
                                      now=now or self.NOW)

    def args(self, pid=None, pr="7", branch="claude/x"):
        return ["--pid", pid or self.PID, "--pr", pr, "--branch", branch]

    def failures(self, n, start, step=datetime.timedelta(minutes=10),
                 pr=None, branch=None):
        """recent.json holding n failures from distinct PRs and branches."""
        recent = self.L.empty_recent()
        for i in range(n):
            recent = self.L.settled_recent(
                recent, "%040x" % (i + 1), "failed",
                pr if pr is not None else 100 + i,
                branch or "b%d" % i, start + i * step)
        return recent

    def test_the_patch_id_is_stable_and_ignores_line_numbers(self):
        pid = review_ledger.patch_id(self.DIFF)
        self.assertRegex(pid, r"^[0-9a-f]{40}$")
        moved = self.DIFF.replace("@@ -1 +1 @@", "@@ -40 +40 @@")
        self.assertEqual(review_ledger.patch_id(moved), pid)
        self.assertIsNone(review_ledger.patch_id(""))

    def test_failures_are_sharded_by_the_first_two_hex_characters(self):
        self.assertEqual(self.L.shard_path(self.PID), "failed/ab.json")
        with self.assertRaises(ValueError):
            self.L.shard_path("../../etc/passwd")

    def test_a_recorded_failure_is_refused_for_good(self):
        L = self.L
        shard = L.settled_shard(L.empty_shard(), self.PID, "failed", 7, "b",
                                self.NOW)
        later = self.NOW + datetime.timedelta(days=400)
        self.assertEqual(L.decide(shard, L.empty_recent(), self.PID, 9,
                                  "other", later), L.FAILED)
        again = L.settled_shard(shard, self.PID, "pass", 9, "c", later)
        self.assertEqual(L.state_of(again, self.PID, later), "failed")

    def test_an_identical_change_under_review_is_refused(self):
        L = self.L
        shard = L.claimed_shard(L.empty_shard(), self.PID, 7, "b", self.NOW)
        soon = self.NOW + datetime.timedelta(minutes=30)
        self.assertEqual(L.decide(shard, L.empty_recent(), self.PID, 8, "c",
                                  soon), L.BUSY)

    # N5: a claim never settled is a failure, never clear.
    def test_a_stale_claim_counts_as_failed(self):
        L = self.L
        shard = L.claimed_shard(L.empty_shard(), self.PID, 7, "b", self.NOW)
        late = self.NOW + L.STALE + datetime.timedelta(minutes=1)
        self.assertEqual(L.state_of(shard, self.PID, late), "failed")
        self.assertEqual(L.decide(shard, L.empty_recent(), self.PID, 8, "c",
                                  late), L.FAILED)

    # N5: the claim is a failure from the moment it is made.
    def test_a_claim_counts_as_a_failure_until_a_pass_settles_it(self):
        L = self.L
        recent = L.claimed_recent(L.empty_recent(), self.PID, 7, "claude/x",
                                  self.NOW)
        self.assertEqual(L.recent_failures(recent, 7, "claude/x", self.NOW), 1)
        self.assertEqual(L.day_failures(recent, self.NOW), 1)
        passed = L.settled_recent(recent, self.PID, "pass", 7, "claude/x",
                                  self.NOW)
        self.assertEqual(L.day_failures(passed, self.NOW), 0)
        for outcome in ("failed", "error"):
            kept = L.settled_recent(recent, self.PID, outcome, 7, "claude/x",
                                    self.NOW)
            self.assertEqual(L.day_failures(kept, self.NOW), 1, outcome)

    def test_a_cancelled_run_leaves_a_failure_behind(self):
        store = FakeStore()
        self.assertEqual(self.run_cli(store, "claim", *self.args()), 0)
        # ... and the run is cancelled: settle never happens.
        later = self.NOW + datetime.timedelta(hours=3)
        self.assertEqual(self.run_cli(store, "claim", *self.args(), now=later),
                         self.L.FAILED)
        self.assertEqual(self.run_cli(store, "check", "--patch-only",
                                      *self.args(), now=later),
                         self.L.FAILED)
        recent = store.files["recent.json"][0]
        self.assertEqual(self.L.day_failures(recent, later), 1)

    def test_two_failures_in_7_days_cap_the_pull_request_and_the_branch(self):
        L = self.L
        recent = L.empty_recent()
        for i, day in enumerate((1, 3)):
            recent = L.settled_recent(recent, "%040x" % i, "failed", 7,
                                      "claude/x",
                                      self.NOW - datetime.timedelta(days=day))
        other = "cd" + "1" * 38
        empty = L.empty_shard()
        self.assertEqual(L.decide(empty, recent, other, 7, "y", self.NOW),
                         L.CAPPED)
        self.assertEqual(L.decide(empty, recent, other, 99, "claude/x",
                                  self.NOW), L.CAPPED)
        self.assertEqual(L.decide(empty, recent, other, 99, "y", self.NOW),
                         L.CLEAR)
        aged = self.NOW + L.WINDOW + datetime.timedelta(days=2)
        self.assertEqual(L.decide(empty, recent, other, 7, "y", aged),
                         L.CLEAR)

    # Round 4, #4: repo-wide, whatever the branch or pull request.
    def test_four_failures_in_24_hours_pause_all_review(self):
        L = self.L
        three = self.failures(3, self.NOW - datetime.timedelta(hours=5))
        four = self.failures(4, self.NOW - datetime.timedelta(hours=5))
        new = "ee" + "4" * 38
        self.assertEqual(L.decide(L.empty_shard(), three, new, 1, "fresh",
                                  self.NOW), L.CLEAR)
        self.assertEqual(L.decide(L.empty_shard(), four, new, 1, "fresh",
                                  self.NOW), L.PAUSED)
        tomorrow = self.NOW + datetime.timedelta(hours=20)
        self.assertEqual(L.decide(L.empty_shard(), four, new, 1, "fresh",
                                  tomorrow), L.CLEAR)

    def test_three_capped_days_running_lock_until_the_owner_resets(self):
        L = self.L
        recent = L.empty_recent()
        for day in (2, 1, 0):
            start = (self.NOW - datetime.timedelta(days=day)).replace(hour=1)
            for i in range(4):
                recent = L.settled_recent(
                    recent, "%038x%02d" % (day, i), "failed", 200 + day * 10 + i,
                    "d%d-%d" % (day, i), start + datetime.timedelta(minutes=i))
        self.assertTrue(L.streak(recent, L.EPOCH))
        week = self.NOW + datetime.timedelta(days=2)
        new = "ee" + "4" * 38
        self.assertEqual(L.decide(L.empty_shard(), recent, new, 1, "fresh",
                                  week), L.LOCKED)
        # The lock file outlives recent.json, until a newer reset.
        lock = self.NOW
        self.assertEqual(L.decide(L.empty_shard(), L.empty_recent(), new, 1,
                                  "fresh", week, lock=lock), L.LOCKED)
        reset = self.NOW + datetime.timedelta(hours=1)
        self.assertEqual(L.decide(L.empty_shard(), recent, new, 1, "fresh",
                                  week, lock=lock, reset=reset), L.CLEAR)
        # Two capped days with a gap are not a streak.
        gap = L.empty_recent()
        for day in (3, 1, 0):
            start = (self.NOW - datetime.timedelta(days=day)).replace(hour=1)
            for i in range(4):
                gap = L.settled_recent(gap, "%038x%02d" % (day, i), "failed",
                                       1, "g", start)
        self.assertFalse(L.streak(gap, L.EPOCH))

    # Round 5: a steady pace just under the daily cap still locks.
    def test_twelve_failures_in_7_days_lock_review(self):
        L = self.L
        start = (self.NOW - datetime.timedelta(days=4)).replace(hour=1)
        recent = L.empty_recent()
        n = 0
        for day in range(4):          # 3 a day: never the daily cap
            for i in range(3):
                n += 1
                recent = L.settled_recent(
                    recent, "%040x" % n, "failed", 400 + n, "r%d" % n,
                    start + datetime.timedelta(days=day, hours=i))
        self.assertFalse(L.streak(recent, L.EPOCH))
        self.assertTrue(L.rolling_total(recent, L.EPOCH))
        new = "ee" + "4" * 38
        self.assertEqual(L.decide(L.empty_shard(), recent, new, 1, "fresh",
                                  self.NOW), L.LOCKED)
        eleven = {"version": 1, "failures": recent["failures"][1:]}
        self.assertFalse(L.rolling_total(eleven, L.EPOCH))
        self.assertEqual(L.decide(L.empty_shard(), eleven, new, 1, "fresh",
                                  self.NOW), L.CLEAR)

    # Round 5: a reset dated in the future cannot disarm the lock early.
    def test_a_reset_dated_in_the_future_is_ignored(self):
        store = FakeStore()
        store.files["lock.json"] = ({"at": "2026-10-09T10:00:00Z"}, "l1")
        fresh = self.args("ff" + "5" * 38, "1", "z")
        store.files["reset.json"] = ({"at": "2027-01-01T00:00:00Z"}, "r1")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli(store, "check", *fresh),
                             self.L.LOCKED)
        soon = self.NOW + datetime.timedelta(minutes=4)
        store.files["reset.json"] = ({"at": self.L._ts(soon)}, "r2")
        self.assertEqual(self.run_cli(store, "check", *fresh), 0)

    # Round 5: every read names the ledger's main branch.
    def test_every_ledger_read_and_write_names_main(self):
        calls = []

        class Recording(review_ledger.Store):
            def _call(self, method, path, body=None):
                calls.append((method, path, body))
                if method == "GET":
                    raise urllib.error.HTTPError(path, 404, "nf", {}, None)
                return {}

        store = Recording("LPSD-1/trailblazer-review-ledger", "t")
        self.assertEqual(self.run_cli(store, "claim", *self.args()), 0)
        gets = [c for c in calls if c[0] == "GET"]
        puts = [c for c in calls if c[0] == "PUT"]
        self.assertTrue(gets and puts)
        for _, path, _ in gets:
            self.assertTrue(path.endswith("?ref=main"), path)
        for _, path, body in puts:
            self.assertEqual(body["branch"], "main", path)

    def test_the_store_writes_the_lock_and_honours_a_reset(self):
        store = FakeStore()
        # Two full days already recorded; today's fourth failure, settled
        # through the store, completes the streak.
        seeded = self.L.empty_recent()
        for day in (2, 1, 0):
            when = (self.NOW - datetime.timedelta(days=day)).replace(hour=1)
            for i in range(4 if day else 3):
                seeded = self.L.settled_recent(
                    seeded, "%038x%02d" % (day + 1, i), "failed",
                    300 + day * 10 + i, "s%d%d" % (day, i),
                    when + datetime.timedelta(minutes=i))
        store.files["recent.json"] = (seeded, "seed")
        self.assertNotIn("lock.json", store.files)
        self.assertEqual(self.run_cli(store, "settle", *self.args(),
                                      "--outcome", "failed"), 0)
        self.assertIn("lock.json", store.files)
        # recent.json forgets, but the lock holds ...
        store.files["recent.json"] = (self.L.empty_recent(), "x1")
        fresh = self.args("ff" + "5" * 38, "1", "z")
        later = self.NOW + datetime.timedelta(hours=2)
        self.assertEqual(self.run_cli(store, "check", *fresh, now=later),
                         self.L.LOCKED)
        # ... until the owner commits a newer reset.json.
        store.files["reset.json"] = ({"at": "2026-10-09T13:00:00Z"}, "r1")
        self.assertEqual(self.run_cli(store, "check", *fresh, now=later), 0)

    def test_claim_settle_and_check_through_the_store(self):
        store = FakeStore()
        self.assertEqual(self.run_cli(store, "claim", *self.args()), 0)
        self.assertEqual(self.run_cli(store, "check", *self.args()),
                         self.L.BUSY)
        self.assertEqual(self.run_cli(store, "settle", *self.args(),
                                      "--outcome", "pass"), 0)
        self.assertEqual(self.run_cli(store, "check", "--patch-only",
                                      *self.args()), 0)
        self.assertEqual(self.L.day_failures(store.files["recent.json"][0],
                                             self.NOW), 0)
        other = self.args("cd" + "2" * 38, "8")
        self.assertEqual(self.run_cli(store, "claim", *other), 0)
        self.assertEqual(self.run_cli(store, "settle", *other,
                                      "--outcome", "failed"), 0)
        self.assertEqual(self.run_cli(store, "claim", *other),
                         self.L.FAILED)

    def test_a_missing_or_malformed_ledger_fails_closed(self):
        with mock.patch.dict(os.environ, {"LEDGER_REPO": "",
                                          "LEDGER_TOKEN": ""}):
            self.assertEqual(self.run_cli(None, "check", *self.args()),
                             self.L.BROKEN)
        store = FakeStore()
        store.files["failed/ab.json"] = ({"entries": []}, "s1")
        self.assertEqual(self.run_cli(store, "check", *self.args()),
                         self.L.BROKEN)

    # N6: the public ledger holds patch-ids, PR numbers, a branch hash, a
    # state word and timestamps. Nothing else, ever.
    def test_the_ledger_schema_holds_nothing_sensitive(self):
        store = FakeStore()
        branch = "claude/jane-smith-at-12-high-street"
        a = self.args(pr="7", branch=branch)
        b = self.args("cd" + "2" * 38, "8", branch)
        # Checked after EVERY write, so a field that lives only in a claim
        # is caught too.
        for args in (("claim", *a), ("settle", *a, "--outcome", "failed"),
                     ("claim", *b), ("settle", *b, "--outcome", "error")):
            self.run_cli(store, *args)
            self.assert_schema(store, branch)
        self.assertEqual(self.L.SHARD_FIELDS,
                         {"state", "pr", "branch_hash", "at"})
        self.assertEqual(self.L.RECENT_FIELDS,
                         {"pid", "state", "pr", "branch_hash", "at"})

    def assert_schema(self, store, branch):
        ts = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        text = json.dumps({k: v[0] for k, v in store.files.items()})
        self.assertNotIn("jane", text)
        self.assertNotIn(branch, text)
        for path, (value, _) in store.files.items():
            if path.startswith("failed/"):
                self.assertEqual(set(value), {"version", "entries"})
                for pid, entry in value["entries"].items():
                    self.assertRegex(pid, r"^[0-9a-f]{40}$")
                    self.assertEqual(set(entry), set(self.L.SHARD_FIELDS))
            elif path == "recent.json":
                self.assertEqual(set(value), {"version", "failures"})
                for entry in value["failures"]:
                    self.assertEqual(set(entry), set(self.L.RECENT_FIELDS))
                    self.assertRegex(entry["pid"], r"^[0-9a-f]{40}$")
            elif path == "lock.json":
                self.assertEqual(set(value), {"at"})
            else:
                self.fail("unexpected ledger file %s" % path)
            for entry in (value.get("entries", {}).values()
                          if path.startswith("failed/")
                          else value.get("failures", [])):
                self.assertIsInstance(entry["pr"], int)
                self.assertRegex(entry["branch_hash"], r"^[0-9a-f]{16}$")
                self.assertRegex(entry["at"], ts)
                self.assertIn(entry["state"], self.L.STATES)


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
                       "01632960123", "(01632) 960 123", "01632–960123"):
            self.assertTrue(review_data_check.personal_data(
                "call %s now" % number), number)

    def test_case_spacing_and_spelled_out_forms_are_caught(self):
        for text, kind in (("sw1a 1aa", "UK postcode"),
                           ("SW1A1AA", "UK postcode"),
                           ("Unit 4, SY23 3HE", "UK postcode"),
                           ("x at gmail dot com", "email address"),
                           ("x (at) gmail (dot) com", "email address"),
                           ("X AT GMAIL DOT COM", "email address"),
                           ("SW1A 1AA", "UK postcode"),
                           ("SW1A  1AA", "UK postcode"),
                           ("01632  960123", "UK phone number"),
                           ("(01632) 960 123", "UK phone number"),
                           ("01632–960–123", "UK phone number")):
            found = review_data_check.personal_data(text)
            self.assertIn(kind, [k for k, _ in found], text)

    def test_removed_lines_are_not_scanned(self):
        self.assertFalse(any("old@example.org" in p
                             for p in review_data_check.scan_diff(DIFF)))

    def test_coordinates_dates_ids_and_prose_are_not_personal_data(self):
        for line in ('[-3.43267, 52.41623]', '"date": "2026-10-08"',
                     '"id": "TRO-2026-0412"', '"sha256": "e00a5eaa8f881174"',
                     '"usrn": 12345678', '"road": "A30 to B3212"',
                     'npm install @anthropic-ai/claude-code',
                     'closed at weekends', 'Lane at Ashford, dot matrix sign'):
            self.assertEqual(review_data_check.personal_data(line), [], line)

    def test_binary_links_submodules_and_mode_changes_need_the_owner(self):
        cases = {
            "binary content": "diff --git a/p.bin b/p.bin\nindex 1..2 100644\n"
                              "Binary files a/p.bin and b/p.bin differ\n",
            "a symlink": "diff --git a/l b/l\nnew file mode 120000\n"
                         "index 0000000..2222222\n--- /dev/null\n+++ b/l\n"
                         "@@ -0,0 +1 @@\n+/etc/passwd\n",
            "a submodule": "diff --git a/s b/s\nnew file mode 160000\n"
                           "index 0000000..3333333\n",
            "a mode change": "diff --git a/m.sh b/m.sh\nold mode 100644\n"
                             "new mode 100755\n",
        }
        for what, diff in cases.items():
            problems = review_data_check.structure_problems(diff)
            self.assertTrue(problems, what)
            self.assertIn(what, problems[0])
            self.assertIn("owner must review", problems[0])
            self.assertEqual(review_verdict.parse_verdict(
                review_data_check.report(problems))[0], "FAIL")
        # Text that merely looks like a header, inside a hunk, is not one.
        self.assertEqual(review_data_check.structure_problems(DIFF + (
            "@@ -9 +9 @@\n-old mode 1\n+new mode 2\n")), [])

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

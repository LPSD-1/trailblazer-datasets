# Review gate: owner setup

The independent review gate is two workflows. `pr-capture.yml` runs on every
pull request to `main`, holds no secrets, and uploads the diff. Then
`independent-review.yml` runs from `main`, reviews that diff and reports a
check run and a commit status, both called `independent-review`. Neither
workflow does anything useful until the settings below exist. Until then the
review fails closed: with no app it posts nothing, with no ledger it posts
failure, and with no OAuth token it posts `error`.

This file is part of the gate. A pull request that changes it fails with
"owner must review the review gate" and is never shown to a model.

GitHub App permissions apply to the whole app, not per repository. So the
gate uses two review-side apps, each installed on exactly one repository:

| App | Installed on | Repository permissions |
|---|---|---|
| `trailblazer-review` | `trailblazer-datasets` only | Checks: write. Commit statuses: write. Pull requests: write. Contents: **read**. |
| `trailblazer-review-ledger` | `trailblazer-review-ledger` only | Contents: **write**. |

The review app can never write code or data here. The ledger app cannot
reach this repository at all.

## A. The review gate

### A1. Create the review app

Go to GitHub, Settings, Developer settings, GitHub Apps, then New GitHub App.

- Name `trailblazer-review`. Homepage: the repository URL. Webhook: **off**.
- Repository permissions:
  - Checks: Read and write
  - Commit statuses: Read and write
  - Pull requests: Read and write
  - Contents: **Read-only**
  - Metadata: Read (always on)
  - Everything else: No access
- Where can it be installed: **Only on this account**.

Create it, note the **App ID** (or the Client ID; either works), and generate
a **private key**. Install it with **Only select repositories** set to
`trailblazer-datasets`.

### A2. Create the ledger repository and its app

The ledger records every change that failed review, by `git patch-id`, and
every change under review right now. It refuses a repeat of a failed change,
an identical change in a second pull request, and a third attempt by a pull
request or branch that has failed twice in 7 days. It lives outside this
repository, so nobody working here can edit it.

1. Create a **private** repository `LPSD-1/trailblazer-review-ledger` with a
   README, so that it has a `main` branch. The workflow writes
   `failed/<xx>.json` (sharded by the first two hex characters of the
   patch-id) and `recent.json` itself.
2. Create a second GitHub App, `trailblazer-review-ledger`, with webhook
   off. Its only repository permission is **Contents: Read and write**
   (Metadata: Read is automatic). Installable only on this account.
3. Install it with **Only select repositories** set to
   `trailblazer-review-ledger`, **and nothing else**.
4. On the ledger repository, add a ruleset for all branches: Restrict
   deletions and Block force pushes, with no bypass. Add no other
   collaborators.

### A3. The `review` environment

Go to Repository Settings, Environments, then New environment, and name it `review`.

- Deployment branches and tags: **Selected branches and tags**, then add `main`.
  A `workflow_run` job runs on `main`, so it qualifies; a workflow on any
  pull request branch cannot reach these secrets.
- No required reviewers, because the review must run unattended.
- Environment secrets:
  - `REVIEW_APP_ID` and `REVIEW_APP_KEY` (the .pem) for `trailblazer-review`.
  - `LEDGER_APP_ID` and `LEDGER_APP_KEY` for `trailblazer-review-ledger`.
  - `CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token`.

If a **repository** secret called `CLAUDE_CODE_OAUTH_TOKEN` exists, delete it.
The token belongs only in the `review` environment.

### A4. Actions settings

Go to Settings, Actions, General, Workflow permissions, and choose **Read
repository contents** (the default token is read-only). Leave "Allow GitHub
Actions to create and approve pull requests" **off**.

### A5. The main ruleset

Settings, Rules, Rulesets, New branch ruleset:

- Name `main`, target the default branch, enforcement **Active**.
- **Require status checks to pass**: add the check `independent-review`,
  and set its **source** to `trailblazer-review`, not "any source". The app
  posts a check run and a commit status with the same verdict. Only the
  check run from that app satisfies the rule. Anything else called
  `independent-review` does not, such as a status posted by a workflow on a
  pull request branch using its own `GITHUB_TOKEN`.
- Require a pull request before merging. Block force pushes. Restrict
  deletions.
- Bypass list: **only** the two pipeline apps from section B
  (`trailblazer-publish` and `trailblazer-collector`). Not GitHub Actions,
  not deploy keys, not the owner, not either review app.

### A6. Check it works

- [ ] When adding the required check in A5, `trailblazer-review` appears in
      the **source** dropdown. If it does not, the app has not posted a
      check run yet: open a test pull request first (next item), then come
      back.
- [ ] Open a pull request that edits only `README.md`. Expect the check
      `independent-review` from `trailblazer-review` to go in progress,
      then success or failure (DATA tier: the deterministic check and
      Sonnet). Expect one comment marked `<!-- independent-review -->`.
- [ ] **A forged status does not count.** On a throwaway branch, add a
      workflow that runs on `pull_request` and posts a `success` status
      called `independent-review` with its own `GITHUB_TOKEN`:
      `gh api repos/$GITHUB_REPOSITORY/statuses/$SHA -f state=success
      -f context=independent-review`, with
      `permissions: statuses: write`. Open a pull request from it. The
      forged status appears, but **the merge stays blocked**. Close the pull
      request and delete the branch.
- [ ] Open a pull request that edits `docs/REVIEW-GATE-SETUP.md`. Expect
      failure, "owner must review the review gate", and no model run.

### A7. Landing a change to the gate itself

A gate change fails by design ("owner must review the review gate"), and
nobody, the owner included, can bypass the main ruleset. To land one, take
these steps in this order:

- [ ] **Pause every routine** (every scheduled or API-fired Claude Code
      agent that can push or open pull requests here). No routines exist
      yet; once they do, this step is mandatory. While you are on the bypass
      list, anything running as your identity can merge without review.
- [ ] Read the change yourself.
- [ ] In the `main` ruleset, add yourself as a bypass actor.
- [ ] Merge.
- [ ] Remove yourself from the bypass list, and confirm the list is back to
      the two pipeline apps.
- [ ] Resume the routines.

## B. Pipeline pushes: the only bypass

Under A5, nothing may push to `main` except through a pull request, with two
exceptions: the scheduled data workflows and the collector VM, which publish
by pushing straight to `main`. Each gets its own GitHub App, and **those two
apps are the only bypass actors**. No deploy key bypasses. The existing
deploy key on the VM is removed once B2 is done.

| App | Key lives in | Tokens |
|---|---|---|
| `trailblazer-publish` | The `publish` Environment, deployment branches **`main` only** (`PUBLISH_APP_ID`, `PUBLISH_APP_KEY`) | Minted per job by `actions/create-github-app-token`; each expires within an hour. |
| `trailblazer-collector` | On the collector VM only, readable by the collector's user alone | The VM mints a short-lived installation token for each push. |

Both apps have **Contents: Read and write** (Metadata: Read), webhook off,
and are installed on `trailblazer-datasets` only.

### B1. The 10 pipeline workflows

For each job below:

1. Add `environment: publish` to the job.
2. On its `actions/checkout` step, set `persist-credentials: false`. A
   checkout keeps no credential.
3. Immediately **before** the step that pushes, add:

       - name: Mint the publish token
         id: publish
         uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1  # v3.2.0
         with:
           client-id: ${{ secrets.PUBLISH_APP_ID }}
           private-key: ${{ secrets.PUBLISH_APP_KEY }}
           permission-contents: write

   Minting it there, not at the start, matters because some jobs run for
   over an hour.
4. In the pushing step, add `PUBLISH_TOKEN: ${{ steps.publish.outputs.token }}`
   to its `env:`, and make this its first line:

       git config --local http.https://github.com/.extraheader "AUTHORIZATION: basic $(printf 'x-access-token:%s' "$PUBLISH_TOKEN" | base64 -w0)"

   Every `git push` (and retry) in that step then uses the app token.
5. Leave `contents: write` only where the job also manages a release
   (height, mirror-routing, satellite and traffic-orders). Everywhere else,
   set `contents: read`.

The line numbers are from the main branch on 9 October 2026. Re-check them
before editing.

- [ ] `council-orders.yml`: job `fetch`. Permissions at :37, checkout at :50.
      Push at :114, in "Commit what changed".
- [ ] `council-ways.yml`: job `layers`. Permissions at :31, checkout at :44.
      Push at :110, in "Commit what changed".
- [ ] `height.yml`: job `height`. Permissions at :33 (keep write: release),
      checkout at :49. Push at :266, in "Publish".
- [ ] `mirror-routing.yml`: job `mirror`. Permissions at :46 (keep write:
      release), checkout at :59. Pushes at :302, in "Publish", and at :356,
      in "Prune what is no longer published". These are two steps, so mint
      before each.
- [ ] `order-register.yml`: job `check`. Permissions at :31, checkout at :44.
      Push at :91, in "Commit snapshots and the change list".
- [ ] `refresh-data.yml`: job `refresh`. Permissions at :87, checkout at
      :103. Push at :1139, in "Publish".
- [ ] `refresh-data.yml`: job `conditions`. Checkout at :1288. Push at
      :1660, in "Publish".
- [ ] `satellite.yml`: job `imagery`. Permissions at :46 (keep write:
      release), checkout at :68. Pushes at :352 and :393, in "Publish", and
      at :429, in "Prune what is no longer published". These are two steps,
      so mint before each.
- [ ] `status-changes.yml`: job `status`. Permissions at :22, checkout at
      :35. Push at :86, in "Commit what changed".
- [ ] `street-manager.yml`: job `streetworks`. Permissions at :24, checkout
      at :37. Push at :81, in "Commit what changed".
- [ ] `traffic-orders.yml`: job `build`. Permissions at :28 (keep write:
      release), checkout at :53. Push at :542, in "Commit the index and the
      catalogue".

That is 10 workflows, 11 jobs, 14 pushing steps and 15 push lines.

### B2. The collector VM

- [ ] Create `trailblazer-collector` as in the table and install it on
      `trailblazer-datasets` only.
- [ ] Copy its private key to the VM, readable only by the collector's user
      (`chmod 600`). Never commit it, and never copy it anywhere else.
- [ ] Before each push, the collector mints a token: it signs a 10-minute
      JWT with the key, then calls
      `POST /app/installations/<id>/access_tokens` with
      `{"repositories": ["trailblazer-datasets"], "permissions": {"contents": "write"}}`,
      then pushes with the returned token as an `x-access-token` basic-auth
      header. It never stores the token.
- [ ] Remove the VM's old deploy key from the repository.

### B3. Turning it on

- [ ] Make the B1 edits, and B2 on the VM.
- [ ] Watch one scheduled run of every pipeline workflow, and one collector
      push, land on `main`.
- [ ] Only then set the main ruleset (A5) to **Active**, with exactly the
      two pipeline apps on its bypass list.

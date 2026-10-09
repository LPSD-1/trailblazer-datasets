# Review gate: owner setup

The independent review gate is two workflows. `pr-capture.yml` runs on every
pull request to `main`, holds no secrets, and uploads the diff. Then
`independent-review.yml` runs from `main`, reviews that diff and posts the
commit status `independent-review`. Neither workflow does anything useful
until the settings below exist. Until then the review fails closed: with no
app it posts nothing, and with no OAuth token it posts `error`.

This file is part of the gate. A pull request that changes it fails with
"owner must review the review gate" and is never shown to a model.

## A. The review app, its environment and the ruleset

### A1. Create the GitHub App

Go to GitHub, Settings, Developer settings, GitHub Apps, then New GitHub App.

- Name: e.g. `trailblazer-review`. Homepage: the repository URL.
- Webhook: **off**.
- Repository permissions:
  - Commit statuses: **Read and write**: posts `independent-review`.
  - Pull requests: **Read and write**: reads the changed files and keeps one
    summary comment.
  - Contents: **Read and write**: reads the ledger, and writes
    `failed.json` on the `review-ledger` branch (A5). The main ruleset (A6)
    gives this app no bypass, so it cannot push to `main`.
  - Metadata: Read (always on).
  - Everything else: No access.
- Where can it be installed: **Only on this account**.

Create it, note the **App ID** (or the Client ID; either works), and generate
a **private key** (.pem).

### A2. Install it

From the app's page, go to Install App and choose **Only select repositories**:
`trailblazer-datasets`.

### A3. The `review` environment

Go to Repository Settings, Environments, then New environment, and name it `review`.

- Deployment branches and tags: **Selected branches and tags**, then add `main`.
  A `workflow_run` job runs on `main`, so it qualifies; a workflow on any
  pull request branch cannot reach these secrets.
- No required reviewers, because the review must run unattended.
- Environment secrets:
  - `REVIEW_APP_ID`: the App ID or Client ID from A1.
  - `REVIEW_APP_KEY`: the whole .pem.
  - `CLAUDE_CODE_OAUTH_TOKEN`: from `claude setup-token`.

If a **repository** secret called `CLAUDE_CODE_OAUTH_TOKEN` exists, delete it.
The token belongs only in the `review` environment.

### A4. Actions settings

Go to Settings, Actions, General, Workflow permissions, and choose **Read
repository contents** (the default token is read-only). Leave "Allow GitHub
Actions to create and approve pull requests" **off**.

### A5. The ledger branch

Every change that has failed review is recorded by `git patch-id` in
`failed.json` on the orphan branch `review-ledger`. This stops the same
change being re-pushed until a model happens to pass it. Create the branch
once, from an empty folder that is not a checkout of anything:

    git init -b review-ledger ledger && cd ledger
    printf '{\n "failed": {},\n "version": 1\n}\n' > failed.json
    git add failed.json && git commit -m "Review ledger"
    git remote add origin https://github.com/LPSD-1/trailblazer-datasets.git
    git push origin review-ledger

Then add a ruleset
(Settings, Rules, Rulesets, New branch ruleset):

- Name `review-ledger`, target branch `review-ledger`, enforcement Active.
- Rules: **Restrict updates**, **Restrict deletions**, **Block force
  pushes**.
- Bypass list: **only** the review app (A1).

### A6. The main ruleset

Settings, Rules, Rulesets, New branch ruleset:

- Name `main`, target the default branch, enforcement **Active**.
- **Require status checks to pass**: add `independent-review`, and set its
  **source** to the review app from A1, not "any source". Pinning the source
  matters because any workflow on a pull request branch can post a status
  called `independent-review` with its own token. Only the app's counts.
- Require a pull request before merging. Block force pushes. Restrict
  deletions.
- Bypass list: **only** the publishing credential from section B. Not
  GitHub Actions, not the owner, not the review app.

Landing a change to the gate itself: it fails by design ("owner must review
the review gate"). Read it yourself. Then, in the `main` ruleset, add yourself
as a bypass actor, merge, and remove yourself again. That is a deliberate
settings change, and it shows in the ruleset history.

### A7. Check it works

Open a pull request that edits only `README.md`. Expect `independent-review:
pending`, then `success` or `failure` from the app (DATA tier, Haiku), and
one comment marked `<!-- independent-review -->`. Then open one that edits
`docs/REVIEW-GATE-SETUP.md`: expect failure, "owner must review the review
gate", and no model run.

## B. Pipeline pushes: the only bypass

Under A6, nothing may push to `main` except through a pull request, with one
exception: the scheduled data workflows and the collector VM, which publish
by pushing straight to `main`. They need one credential that the ruleset
lets through, and nothing else may hold it.

Recommended: a **deploy key** with write access, stored as
`PUBLISH_DEPLOY_KEY` in an Environment named **`publish`** whose deployment
branches are **`main` only**. The bypass actor is then "Deploy keys". The
collector VM already pushes with a deploy key, so it is covered by the same
entry. Keep exactly two deploy keys on the repository (the VM's and
`publish`), because every deploy key bypasses. The alternative is a second
GitHub App ("trailblazer-publish", Contents read and write) in the same
`publish` environment, with that app as the only bypass actor. That is
narrower, but the VM would then need the app too.

### B1. The change, per job

For each job below:

1. Add `environment: publish` to the job.
2. On its `actions/checkout` step add `ssh-key: ${{ secrets.PUBLISH_DEPLOY_KEY }}`.
   The checkout then fetches over SSH, and every later `git push` in the job
   uses the deploy key.
3. Leave `contents: write` only where the job also manages a release
   (height, mirror-routing, satellite and traffic-orders). Everywhere else,
   set `contents: read`.

The line numbers are from the main branch on 9 October 2026. Re-check them
before editing.

- [ ] `council-orders.yml`: job `fetch`. Permissions at :37, checkout at :50.
      Push at :114 ("Commit what changed").
- [ ] `council-ways.yml`: job `layers`. Permissions at :31, checkout at :44.
      Push at :110 ("Commit what changed").
- [ ] `height.yml`: job `height`. Permissions at :33 (keep write: release),
      checkout at :49. Push at :266 ("Publish").
- [ ] `mirror-routing.yml`: job `mirror`. Permissions at :46 (keep write:
      release), checkout at :59. Pushes at :302 ("Publish") and :356 ("Prune
      what is no longer published").
- [ ] `order-register.yml`: job `check`. Permissions at :31, checkout at :44.
      Push at :91 ("Commit snapshots and the change list").
- [ ] `refresh-data.yml`: job `refresh`. Permissions at :87, checkout at
      :103. Push at :1139 ("Publish").
- [ ] `refresh-data.yml`: job `conditions`. Checkout at :1288. Push at :1660
      ("Publish").
- [ ] `satellite.yml`: job `imagery`. Permissions at :46 (keep write:
      release), checkout at :68. Pushes at :352 and :393 ("Publish") and
      :429 ("Prune what is no longer published").
- [ ] `status-changes.yml`: job `status`. Permissions at :22, checkout at
      :35. Push at :86 ("Commit what changed").
- [ ] `street-manager.yml`: job `streetworks`. Permissions at :24, checkout
      at :37. Push at :81 ("Commit what changed").
- [ ] `traffic-orders.yml`: job `build`. Permissions at :28 (keep write:
      release), checkout at :53. Push at :542 ("Commit the index and the
      catalogue").

That is 10 workflows, 11 jobs and 15 push lines. Before switching the main
ruleset to Active, make these edits and watch one scheduled run of each
push its commit.

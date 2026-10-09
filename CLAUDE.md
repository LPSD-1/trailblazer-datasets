# trailblazer-datasets: agent notes

Public open data for the TrailBlazer app, served by GitHub Pages at `https://lpsd-1.github.io/trailblazer-datasets/` (the catalogue's `baseUrl`), with large packs as GitHub Release assets. This repo is PUBLIC: commit no secrets, personal data, e-mail addresses, IP addresses or server details. Never open `.env`; `.env.example` lists the variable names. When this file and the code disagree, the code wins. Fix this file in the same PR.

## Before you start
- Run `git fetch` before you read any published file. Pipeline jobs push to `main` several times a day, so a clean tree can still be behind `origin/main`.
- Work on a branch or a worktree. `main` takes changes only by pull request (see "Review gate").
- The app repository is private and is never present here. Only the published file formats are this repo's contract (`README.md` "Format", `docs/WAYS-SCHEMA.md`).

## Tests
- CI's contract (refresh-data.yml, "Run every tool suite") runs every `tools/test_*.py` by glob as `python tools/<file>` from the repo root. Run it the same way locally: `for t in tools/test_*.py; do python "$t" || echo "rc=$? $t"; done`. `python -m pytest tools` also works, because `tools/conftest.py` wraps each script as one item.
- Exit codes: 0 means pass. **2 means BLIND**: the suite needs something this checkout lacks, such as the private app repo's fixtures. BLIND is a warning, neither a pass nor a failure. Any other code is a failure and stops the lane refresh. A new suite that needs an absent checkout must exit 2, never 1. Exiting 1 there once stopped every lane refresh for days.
- In a session with only this repo, the app-fixture suites (`test_changeset_fixture_*`, `test_make_changeset_fixture.py` and others) report BLIND. That is expected.
- Python 3.12. Deps are those in refresh-data's line: `pip install cryptography Pillow pyyaml`. A test's imports must appear in the pip line of every job that runs it, which `test_every_job_installs_what_it_runs.py` checks.
- Each workflow also runs its own named suites before fetching. Grep `python tools/test_` in that workflow.
- CI runs on ubuntu, so a test can pass on Windows and fail there. For example, a stand-in `#!` script needs its execute bit. After any push that touches tests, watch the next scheduled runs with `gh run list -R LPSD-1/trailblazer-datasets`.

## Line endings and bytes
- Write every text file with LF. On Windows, Python's `open(p, "w")` writes CRLF, so pass `newline="\n"`. A Workflow-tool script that contains CR is refused as "control characters".
- `.gitattributes` marks the pack formats `-text` (`.tbpack .tbmap .tbchange .tbnames .tbtrips .rd5 .pmtiles .sig`, plus `tools/fixtures/**`) and sets `*.json` to `eol=lf`. `build_catalogue.py` hashes the working-tree file, so a converted pack ships a hash no rider can ever match. Never weaken these attributes.

## Published layout
- `catalogue.json` is **schema 2**, the index the app reads.
  - Top-level keys: `schema generated attribution baseUrl overviews conditions updates continents`. Structure: continents, then countries, then areas, then `packs`.
  - Pack kinds: `lanes basemap height names trips routing tro`.
  - Written by `tools/build_catalogue.py`; checked by `tools/verify_catalogue.py` and `tools/validate_catalogue.py`.
  - `build_catalogue.py` also adds a `status` URL pointing at `published/status.json`.
- `manifest.json` is **schema 1**, the legacy index that is still read. It has a top-level `packages` array. Anything that writes either index must keep both shapes valid.
- GB areas: `gb-east-anglia gb-midlands gb-north gb-south-east gb-south-west gb-wales`, plus `gb-roads` for routing. Lanes and closures cover England and Wales only. Scotland and Northern Ireland are out of scope: different law, not a missing download. (Routing is the one exception; see Data sources.)
- Files in git (served by Pages):
  - `containers/ways-<region>.tbmap` with a signed `.sig`, `ways-overview.tbmap`, and `containers/manifest.json`. These are SQLite containers; schema in `docs/WAYS-SCHEMA.md`.
  - `packages/ways-<region>.tbpack`: gzipped GeoJSON sealed with AES-256-GCM.
  - `changes/<area>/*.tbchange` changesets, `names/*.tbnames`, `trips/gb.tbtrips`.
  - `published/`: `status.json`, plus the `forecast`, `rivers` and `wet` conditions feeds.
- Release assets, outside git:
  - `satellite` imagery `.pmtiles` in `standard` and `high` tiers;
  - `height` `.pmtiles`;
  - `routing` `.rd5`. Only GB is mirrored, filtered to `GB_ROUTING_TILES`; other countries are fetched from brouter.de directly;
  - `tro`: `gb-tro-<hash>.tbpack`.
  - Their indexes: `satellite/index.json`, `height/index.json`, `routing/index.json`, `tro/index.json`. In `tro/index.json`, `generated` is the applied-through date and `cut` is the D-TRO extract date.
- Inputs committed as data:
  - `tro/council/`, `tro/register/` (only `approved` entries publish), `tro/streetworks/`;
  - `council-ways/`, `council-ucrs/`, `home-collected/`, `local-rules/rules.json`, `status/`;
  - `manual/<CODE>/`, for documents saved by hand or sent by a council (see `manual/README.md`).
- Content: byways open to all traffic and unsurfaced unclassified roads (UCRs, class `ucr`, in their own table and tile layer). An `osm_track` class exists in `tools/build_packages.py` but nothing produces it (0 rows published); do not add a producer, since lanes come only from public bodies. There are no footpaths, bridleways or restricted byways. Do not add context ways back to power a feature. Routing for all 41 countries stays listed (owner decision, 24 Sep 2026). Do not trim it.
- UCRs pass the NERC test (`build_packages.ucr_lanes`). A council without its footpath, bridleway and restricted byway files in the cache publishes no UCRs.
- Builds are reproducible: unchanged input gives identical bytes and a filename carries no date. A run stamp such as `built_at` or `generated` must never leak into a pack's content hash, or every rider is told to re-download.
- Lane ids are stable. They are keyed by the council reference, never by read order or an optional field.

## Pack rules
- A sha256 or length match proves the file arrived intact, not that it was ever valid. Check the container format before blaming transport.
- PMTiles: the header and the whole root directory must fit in the first **16,384 bytes**, with everything else in leaf directories. A pack that breaks this downloads cleanly and never opens. `tools/check_pmtiles.py` gates it in CI; `tools/repack_pmtiles.py` rewrites directories without refetching tiles.
- Containers: `tools/check_containers.py` and `tools/validate_container.py` check the format. Separately, `tools/build_packages.py` caps package plaintext at 12 MB, because opening a package peaks at about 8x its size in memory.
- Imagery is a Sentinel-2 cloudless mosaic. z14 is the published maximum and is worth it (twice the rendered pixels, though the source is 10 m); z15 adds nothing. The only sharper source that may ship is Environment Agency vertical aerial photography (OGL, England only, patchy; not built). APGB may not ship.

## Workflows (cron is UTC; each pushes straight to `main` through the pipeline bypass)

| Workflow | Schedule | What it publishes |
|---|---|---|
| `refresh-data.yml` "Refresh lane data" | `23 3,9,15,21` | `packages containers changes manifest.json catalogue.json trips`. Its job `conditions` publishes `published/`. It publishes only when the rebuilt files differ. |
| `traffic-orders.yml` | `23 2,8,14,20` | The tro release asset, `tro/index.json`, `tro/publishers.json` and the catalogue. It refuses an older `generated`. `tools/tro_events.py` lays the `/events` feed over the stalled `/dtros/all` extract. |
| `council-orders.yml` | `53 1,7,13,19` | `tro/council/`, read by `tools/council_sources.py`. |
| `council-ways.yml` | `17 3` daily | `council-ways/` and `council-ucrs/`. |
| `satellite.yml` | `41 2` and `41 14` | Satellite release assets, their index and the catalogue. |
| `height.yml` | `17 3` | Height release assets, their index and the catalogue. |
| `mirror-routing.yml` | Tuesdays `23 4` | Routing release assets, their index and the catalogue. |
| `street-manager.yml` | 2nd of each month | `tro/streetworks/` |
| `status-changes.yml` | Mondays | `status/` |
| `order-register.yml` | quarterly | Snapshots and `changes.json`. It opens an issue and never edits `orders.json`. |
| `status.yml` | hourly at :47, and after each data workflow | `published/status.json` (`tools/build_status.py`): plain English, no personal data, no blame words (`public_check`). D-TRO use is stated neutrally. |

- A collector server run by the owner runs `tools/home_collector.py --machine server` every 6 hours. It reads the sources in `home-collected/collector.json` and commits `home-collected/`. Details are in `HOME-COLLECTOR.md`.
- Satellite and routing prune assets that are no longer published (`tools/prune_published.py`). A publishing job that fails raises an issue (`tools/hunt_data_pipeline_alarms.py`).
- A green run can still publish stale data. If `tro/index.json` `generated` lags today by more than a day, read the events step of the traffic-orders log.
- Local fallback: `python tools/run_workflow_locally.py <workflow>` runs the workflow's own steps. Use a dedicated clone, because it hard-resets to `origin/main`.

## Data sources
- **Lanes and closures come only from open data published by public bodies**: government, councils and national park authorities. rowmaps.com copies of councils' definitive maps are allowed, credited to the council (owner ruling, 9 Oct 2026), until the council's own publication replaces them. Never user groups, campaign groups or other organisations, however good their data.
- **Owner ruling, 9 Oct 2026: the public-bodies rule covers lanes and closures only.** The basemap, imagery, height and routing may use other open data, such as OpenStreetMap and BRouter's routing tiles for countries outside Great Britain. These are deliberate, not provenance errors.
- **Licence**: OGL sources are credited. Council UCR layers that state no licence are published with the council credited by name, and come down if that council objects. Layers whose terms forbid copying stay out.
- Every request goes through `tools/polite_http.py`: an honest User-Agent naming this repo, robots.txt obeyed (RFC 9309), a minimum gap per host, read-only GETs (the single POST is Wiltshire's search form, `FORM_POSTS`), and `BLOCKED_HOSTS` never contacted.
- **robots.txt overrides** apply only to the paths in `tools/robots_override.json`, the reviewed allowlist (editing it is SECURITY tier). Each path is read at most weekly, and every read is logged in `tro/register/override-reads.json` or `home-collected/override-reads.json`.
- **Real blocks are never worked around.** A 403, a bot challenge or Cloudflare means stop: no proxies, no other addresses, no borrowed User-Agent, no headless browser.
- A council that refuses GitHub's runners may be read only from the collector server, and only after a test fetch from it succeeds. A council that refuses that server too is not read from anywhere, an agent's own sandbox included. `manual/` is the backstop.
- Never store personal data. Drop names, phone numbers, e-mail addresses and applicants before writing.
- A source that fails or suddenly returns nothing keeps its last good copy.

## Closures, orders and precedence
- D-TRO is not mandatory and holds few byway orders, so collate every official source for each authority and never treat D-TRO as complete. When D-TRO becomes mandatory, it becomes the main channel and council sources keep filling its gaps and checking it.
- **Precedence**:
  - When two records describe the same order, the details come from the newest by each source's own date. A council-dated record beats D-TRO where it is newer.
  - An undated source never overrides a dated one.
  - **Precedence never narrows a restriction.** Shortening dates, shrinking an extent or ending an order needs the authority's own record.
  - "No source" means unknown, never clear.
- **The code lags this rule.** `council_orders.PRECEDENCE` and `merge_council` still say "D-TRO always wins" when they fold a matched twin into `also`. Twins match only when their dates agree, and records whose dates differ are both kept, which must stay. Replace the rule with tests that fail on the old behaviour.
- D-TRO geometry is an order's extent, not its route: 72% of orders are two-point lines. Never present it as a survey.
- Two different `seasonal`s: a lane's usability `seasonal` (from council or UCR data) is a request to keep off and draws green; a TRO order's `oform: seasonal` is a dated order that shuts the way inside its dates and not outside them. A voluntary request is otype `voluntary`.
- Voluntary closures (otype `voluntary`) are built but held: `PUBLISH_VOLUNTARY = False` in `tools/wiltshire_closures.py`, guarded by `test_the_switch_is_committed_off`. Flip it only on the owner's word, because older app builds draw a request as a closure.

## Review gate
- A pull request to `main` runs `pr-capture.yml`, which has no secrets and uploads the diff. `independent-review.yml` then runs from `main` through `workflow_run`. It never runs the PR's code.
- `tools/review_tier.py` sets the tier: GATE (the gate's own files, `tools/review_*`, `conftest.py`) is owner only and never shown to a model; SECURITY (every `.py`, `.github/**`, the robots allowlist) and CODE need Opus and Sonnet both to pass; DATA needs `tools/review_data_check.py` and Sonnet.
- The required check `independent-review` counts only when the review app posts it. It fails closed on forks, on diffs over 400 KB, and on binary files or symlinks. A ledger refuses a repeat of a failed patch, and review pauses after 4 failures in 24 hours. Owner setup is in `docs/REVIEW-GATE-SETUP.md`.

## Pointers
`README.md` (sources, licences, UCR/NERC rules, formats); `HOME-COLLECTOR.md` (the collector); `docs/WAYS-SCHEMA.md` (container schema, meta keys); `docs/CUTOVER-REHEARSAL.md` (rollback, section 6a); `docs/DECISIONS-1.2.md`; `docs/REVIEW-GATE-SETUP.md`.

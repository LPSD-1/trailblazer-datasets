# Lessons learnt: the data pipeline

Each section is one lesson learnt the hard way. It states the lesson in its title, then
**What happened** (with the date and the evidence), the **Rule** that follows, and what is
**Guarded by** it today: a test or check in this repository, or "nothing yet". Every path below was
checked against `main` on 9 Oct 2026. Where an earlier note and the repository disagreed, the
repository won, and the section says so.

When this file and the code disagree, the code wins. Fix this file in the same pull request.

## Reading what is published

### 1. Run `git fetch` before reading any published file
**What happened.** 13 Sep 2026: an agent reported that no height packs were published. Its tree
was clean, but one commit behind `origin/main`, and the missing commit was the height packs.
Pipeline jobs push to `main` several times a day.
**Rule.** A clean working tree says nothing about freshness. Fetch first, and when the app shows
something the file on disk does not, believe the live URL.
**Guarded by.** Nothing yet.

### 2. Coverage is England and Wales, and Scotland is a different law, not a missing download
**What happened.** Scope was set to England and Wales on 13 Sep 2026. Lane data comes from
definitive maps, which exist only in England and Wales. In Scotland, the access rights exclude
motor vehicles, so there is no equivalent of a byway open to all traffic.
**Rule.** Lanes and closures cover the six GB areas (`gb-east-anglia`, `gb-midlands`, `gb-north`,
`gb-south-east`, `gb-south-west`, `gb-wales`). Scotland would be its own project with its own lane
meanings. Routing is the exception (see 7).
**Guarded by.** Nothing yet.

### 3. Two index files, two shapes, and both must stay valid
**What happened.** 11 Sep 2026: an app build parsed `catalogue.json` (schema 2, `continents`) as
if it were `manifest.json` (schema 1, top-level `packages`). Every lane failed to draw on every
device, while the area browser, which read the same bytes correctly, looked fine.
**Rule.** `catalogue.json` is the index the app reads. `manifest.json` is the legacy index and is
still read. Anything that writes either one must keep both shapes valid. When testing on a
device, test against `catalogue.json`, because that is what riders get.
**Guarded by.** `tools/validate_catalogue.py` refuses any schema other than 2, and says why;
`tools/verify_catalogue.py` and `tools/test_verify_catalogue.py` run in `refresh-data.yml`.
Nothing checks the shape of `manifest.json`.

## Pack bytes

### 4. A checksum proves the file arrived, not that it was ever valid
**What happened.** 18 Sep 2026: six of the ten published imagery packs broke the PMTiles rule
that the header and the whole root directory must fit in the first 16,384 bytes. These were every
high-detail pack and all of the North, because the root grows with the tile count. They
downloaded cleanly, matched their sha256, and never opened.
**Rule.** When a download "does nothing", check the container format before blaming transport.
Every pack format needs a check of its structure, not only of its hash.
**Guarded by.** `tools/check_pmtiles.py`, run by `refresh-data.yml` ("Check the raster
archives"). `tools/build_satellite.py` writes leaf directories, and `tools/repack_pmtiles.py`
repairs a pack without refetching its tiles. Containers: `tools/check_containers.py` and
`tools/validate_container.py`.

### 5. Pack files are bytes, and git must never convert them
**What happened.** `trips/gb.tbtrips` was written in text mode on Windows. Git stored it with LF,
but `tools/build_catalogue.py` hashed the CRLF working copy, so the catalogue recorded 7,007
bytes while Pages served 6,764. Every rider's trips download failed its check, every time. The
full account is in `.gitattributes`.
**Rule.** Every pack extension is `-text` in `.gitattributes`, and `*.json` is `eol=lf`. Never
weaken these attributes. Write every text file with LF; on Windows that means
`open(p, "w", newline="\n")`. A Workflow-tool script that contains a CR is refused as "control
characters" (this happened three times on 25 Sep 2026).
**Guarded by.** `.gitattributes` (SECURITY tier in `tools/review_tier.py`).
`tools/verify_catalogue.py` fails when git would rewrite any pack the catalogue lists, which
covers a new extension too; `tools/test_verify_catalogue.py` holds that check.

### 6. Unchanged data must give unchanged bytes
**What happened.** Two leaks, both found after the cutover (25 Sep 2026).
`meta.evidence_age` stored ages in days, so every region container changed on the first run of
each month, and phones fetched the whole file (about 94 MB for the six regions). Rows were also
numbered by uid order, so one new POI made a 5.87 MB changeset against a 9.47 MB container.
**Rule.** Store dates, never ages. Row numbers and lane ids must be stable from one build to the
next, keyed by the source's own reference. A run stamp (`built_at`, `generated`) must never reach
a pack's content hash, or every rider is told to download it again.
**Guarded by.** `tools/evidence_age.py` (now `meta.evidence_dates`) with
`tools/test_evidence_age.py`; `tools/stable_ids.py` with `tools/test_stable_ids.py`.

## Scope of the data

### 7. Byways only for lanes; routing for every country stays
**What happened.** 24 Sep 2026, owner decision: lanes are byways open to all traffic (later also
unsurfaced unclassified roads), with no footpaths, bridleways or restricted byways. The
non-GB routing packs stay listed. This reversed an earlier cut to GB-only routing.
**Rule.** Do not add context ways back to power a feature. Adapt the feature instead. Do not
trim the foreign routing packs as "irrelevant". GB routing is still filtered to
`GB_ROUTING_TILES`.
**Guarded by.** Nothing yet that fails if context ways return.
*Repo note:* an `osm_track` class still exists in `tools/build_packages.py`, but nothing
produces it (lanes come only from public bodies; see 21).

### 8. The ways cutover is published, and its rollback was rehearsed
**What happened.** 24 Sep 2026, commit `e411026` ("Refresh lane data: 12702 ways"): seven signed
`ways-*.tbmap` containers replaced the 109 per-vehicle containers. The rollback rehearsal found
that step 1 as written was wrong.
**Rule.** The ways shape is the only shape. "The app still reads the old one" is no longer a
reason a gate is BLIND. For a rollback, follow `docs/CUTOVER-REHEARSAL.md` section 6a, not the
original steps.
**Guarded by.** `tools/check_containers.py`; the rehearsal record in
`docs/CUTOVER-REHEARSAL.md`.

## Tests and CI

### 9. Exit 2 means BLIND, and must never stop a publish
**What happened.** 27 Sep to 2 Oct 2026: every "Refresh lane data" run failed before publishing.
The suite loop did `python "$t" || exit 1`, and the changeset-fixture suites exit 2 because the
app repository is private and never checked out in CI. Nobody noticed, because the failure
showed only in Actions. Fixed in `b3a402d`.
**Rule.** 0 is a pass, 2 is BLIND (a warning), and anything else is a failure. A new suite that
needs a checkout this job lacks must exit 2, never 1.
**Guarded by.** The loop in `refresh-data.yml` ("Run every tool suite") treats 2 as a warning.

### 10. A test that passes on Windows can fail on ubuntu
**What happened.** 2 to 3 Oct 2026: a test wrote a stand-in `gh` script without the execute bit.
Git Bash ignores the bit, so the test passed locally. On ubuntu, PATH skipped the stand-in, the
real `gh` ran, `tools/test_tro_publish_keeps_what_it_must.py` failed, and every lane refresh
stopped again. Fixed in `c4f37ea`.
**Rule.** After any push that touches tests, watch the next scheduled runs
(`gh run list -R LPSD-1/trailblazer-datasets`). Any stand-in `#!` script needs its execute bit.
**Guarded by.** `write()` in `tools/test_tro_push_race_keeps_its_index.py`, which the other
push-race suites import, chmods any `#!` script. Nothing checks this in general.

### 11. A test's imports must be installed in every job that runs it
**What happened.** 8 Oct 2026: the lane refresh runs every `tools/test_*.py` by glob, so a new
test that imports a package missing from that job's pip line breaks publishing.
**Rule.** When a test imports a new package, add it to the pip line of every job that runs it.
**Guarded by.** `tools/test_every_job_installs_what_it_runs.py`.

### 12. Two pull requests that each pass can fail together
**What happened.** 9 Oct 2026: PR #22 (status page, merged 08:44:24 UTC) and PR #23 (review gate,
merged 08:45:04 UTC) each passed alone. Together they broke three alarm suites and
`tools/test_review_gate.py` on `main`. Alarm check 4 flagged two `continue-on-error` steps in
`independent-review.yml`, and the gate checklist in `docs/REVIEW-GATE-SETUP.md` never listed
`status.yml`, the workflow PR #22 added. The fix is PR #26 (open as of writing).
**Rule.** After merging, run the whole `tools/test_*.py` set on `main` under refresh-data's exit
rules before calling it done:
`for t in tools/test_*.py; do python "$t" || echo "rc=$? $t"; done`.
**Guarded by.** Nothing yet. The lane refresh catches it, but only by stopping (see 13).

### 13. Any suite failure stops the lane refresh from publishing
**What happened.** 9 Oct 2026, run 37919447981 (scheduled 10:44 UTC) failed at "Run every tool
suite" on the breakage in 12. Every later step was skipped, so riders kept the previous lanes.
The same happened on 8 Oct (run 37765596475).
**Rule.** A red suite on `main` is a publishing outage, not a test chore. Fix it first. After any
merge, check `gh run list` for the next scheduled refresh.
**Guarded by.** `refresh-data.yml` raises an issue when the refresh job fails
("Raise an issue if this run failed").

### 14. A green run can still publish stale data
**What happened.** From 6 Sep 2026, D-TRO's `/dtros/all` extract kept serving the same day's
file, while `/events` published 1,000 to 3,400 changed orders a day. Every successful run
republished 6 Sep data. Fixed 2 Oct 2026 in `6f3c59f`: `tools/tro_events.py` lays the events
feed over the extract. Catching up took nine back-to-back runs.
**Rule.** Check `generated` in `tro/index.json` against today. If it lags by more than a day, read
the events step of the traffic-orders log before anything else. `generated` is the
applied-through date, and `cut` is the extract date.
**Guarded by.** The age alarm in `traffic-orders.yml` raises an issue when the orders are over 7
days old (`tools/test_tro_cut_age_alarm.py`); `tools/test_tro_events_move_the_pack.py`.

### 15. Never publish an older pack over a newer one
**What happened.** 8 Oct 2026: the first local run of traffic-orders started with no cache, hit
its time limit with the orders applied only through 9 Sep, and published that pack over 8
Oct's. A dispatched GitHub run restored it about 20 minutes later.
**Rule.** A publish step compares `generated` with what riders already have, and refuses an
older one, even when forced.
**Guarded by.** `tools/test_tro_never_publishes_an_older_pack.py`
(`test_an_older_pack_is_not_published`, `test_not_even_when_forced`).

### 16. The local runner needs its own clone
**What happened.** Since 8 Oct 2026, `tools/run_workflow_locally.py` runs a workflow's own steps
on a local machine as a fallback. It hard-resets to `origin/main` on every run.
**Rule.** Run it only in a dedicated clone, never in a working checkout. It is a fallback, not the
normal path. The first local run of a cached job is slow, because it fetches the whole D-TRO extract.
**Guarded by.** `tools/test_run_workflow_locally.py` (the condition evaluator).

## Alarms and the review gate

### 17. A checker that cannot read its input must fail closed
**What happened.** 9 Oct 2026: the alarm hunt's hand-rolled reader missed several valid YAML
forms and dropped checks without saying so. It is not a defence against a hostile author; the
review gate is. A fix that reads workflows with PyYAML is pending. With it, a file that does not
parse, or has no jobs, will be a PREMISE failure (exit 3).
**Rule.** A guard that reads nothing must say so, never pass. Its scope is honest mistakes.
*Repo note:* an earlier note said workflow changes are GATE tier. `tools/review_tier.py` says
otherwise: only `pr-capture.yml`, `independent-review.yml` and the gate's own files are GATE; every
other `.github/**` file is SECURITY tier (two models must pass it).
**Guarded by.** On `main`, `tools/hunt_data_pipeline_alarms.py`, through the four
`tools/test_data_alarms_*` suites. The fail-closed reader is not on `main` yet.

### 18. A job that times out is cancelled, not failed
**What happened.** 9 Oct 2026, found by review of `satellite.yml`: a job that reaches
`timeout-minutes` is cancelled, so an `if: failure()` alarm never runs. The worst case is a
timeout inside the upload: packs replaced, checksums old, and no issue raised.
**Rule.** Every publishing workflow's alarm runs on `if: failure() || cancelled()`. Bringing
every workflow to that rule is in progress.
**Guarded by.** `tools/hunt_data_pipeline_satellite_alarm_lifecycle.py` for the satellite
workflow; the rest is in progress.

### 19. Pre-flight the review gate before every push
**What happened.** Each pull request or branch may fail review twice in 7 days, and review pauses
for the whole repository after 4 failures in 24 hours. Errors count as failures. On 9 Oct 2026, a
malformed model token came back within 2 seconds as `"subtype": "success", "is_error": true`.
**Rule.** Before pushing, run the deterministic checks (`personal_data` in
`tools/review_data_check.py` on the added text). If the review flags something, fix the cause;
never reword to hide it, and never re-run unchanged content hoping for a different verdict.
Treat `is_error: true` as a failure whatever the subtype says.
**Guarded by.** `tools/review_ledger.py` enforces the limits; `tools/review_verdict.py` refuses
`is_error` (case in `tools/test_review_gate.py`). Nothing runs the pre-flight for you.

### 20. The gate reads a spelled-out "at" as an e-mail address
**What happened.** 9 Oct 2026: `tools/review_data_check.py` turns the word "at" into "@" and "dot"
into "." before matching, so that an address spelled out in words is caught. As a result, an ordinary phrase
that puts "at" before a file name is read as an address.
**Rule.** In added text, write "in `status.json`" or "from `status.json`", not "at" followed by
a dotted name. Run `personal_data` on the added lines before pushing.
**Guarded by.** `tools/test_review_gate.py` holds the spelled-out case. Nothing yet stops the
false positive.

## Sources and fetching

### 21. Lanes and closures come only from public bodies
**What happened.** 8 Oct 2026, owner ruling: only open data published by government, councils
and national park authorities, never user groups or commercial products, however good. On 9 Oct
2026 the scope was narrowed: the rule covers lanes and closures only, and the basemap, imagery,
height and routing may use other open data such as OpenStreetMap. rowmaps.com copies of
councils' definitive maps stay, credited to the council.
**Rule.** A new lane or closure source must be a public body's own publication. A reviewer that
flags OpenStreetMap in the basemap, or foreign routing tiles, is missing the 9 Oct ruling.
*Repo note:* on `main`, the review prompts (`.github/review/code.md`, `data.md`, `security.md`)
still state the rule without the 9 Oct scope. PR #28 updates them.
**Guarded by.** The review prompts in `.github/review/`. No mechanical check.

### 22. A council layer with no stated licence is published with credit
**What happened.** 8 Oct 2026: several councils' UCR layers state no licence. They are published,
credited to each council by name, and taken down if that council objects. Layers whose terms
forbid copying stay out.
**Rule.** Read the terms before adding a layer. If they forbid copying, the layer stays out. If
they are silent, publish it with credit.
**Guarded by.** Nothing yet.

### 23. A real block is never worked around
**What happened.** October 2026: some councils refuse GitHub's runners, and some sit behind
Cloudflare's bot challenge, which refuses every data centre. A collector server run by the owner
now reads the councils that accept it (`home-collected/collector.json`, see `HOME-COLLECTOR.md`).
**Rule.** When a council refuses GitHub's runners, the collector server may fetch it, and only
after a test fetch from that server succeeds. If the collector is refused too, stop: the council
is not read from anywhere, and `manual/` is the backstop. Blocks are never worked around: no
proxies, no spoofed or borrowed User-Agent, no headless browser. robots.txt is obeyed except for the reviewed paths in
`tools/robots_override.json`.
**Guarded by.** `tools/polite_http.py` (`BLOCKED_HOSTS`, `FORM_POSTS`, robots handling) with
`tools/test_polite_http.py`; the allowlist is SECURITY tier.

## Closures and orders

### 24. D-TRO is not complete, so collate every official source
**What happened.** 8 Oct 2026: publishing to D-TRO is not yet mandatory, and only about half of
councils use it. Byway orders are thin there.
**Rule.** For each authority, collate D-TRO, council pages, notices and registers, and dedupe
across them. Never treat D-TRO as complete. "No source" means unknown, never clear. When D-TRO
becomes mandatory, it becomes the main channel, and council sources keep filling its gaps.
**Guarded by.** `tools/council_sources.py`, `tools/council_orders.py`, `tools/tro_merge.py` and
their tests.

### 25. D-TRO geometry is an order's extent, not its route
**What happened.** A one-off measurement on 14 Sep 2026, over 34,021 live orders (the script is not
in this repo, so re-measure before relying on the figures): 13% were a bare point, and 72%
were a two-point line. The median segment is 13 m, but 21.6% of orders contain a chord over 250
m, drawn across ground no road follows.
**Rule.** Never present order geometry as a survey. Drawing the true route needs map-matching
against a road network during the build, which this pipeline does not have.
**Guarded by.** Nothing yet.

### 26. A seasonal order is never published without its dates
**What happened.** 12 Sep 2026: "seasonal" meant two opposite things, a request and a dated
closure, so a careful source that published its dates came off worse than one that did not.
*Repo note:* an earlier note said "`seasonal` is a request and draws as rideable". That is a
different field. A lane's usability `seasonal` is a council request, drawn green. A TRO's
`oform: seasonal` labels the form of an order (`ORDER_FORMS` in `tools/build_tro.py`), and it
also covers temporary orders (TTROs). `way_access` in `tools/build_tro.py` never shuts a seasonal
or experimental way during the build; the dates travel with the order, and the app shuts the way
while the order is in force. A request in the closures pack is otype `voluntary` (see 27).
**Rule.** Write a seasonal order with the dates of its current or next season. If a source gives
no dates, list it for review and do not publish it, because an undated seasonal order would be
drawn shut all year.
**Guarded by.** `tools/test_council_orders.py` (`test_a_seasonal_order_with_no_season_is_not_published`
and the season-window cases).

### 27. Voluntary closures are built but held
**What happened.** 7 Oct 2026: council requests to keep off a lane can be published as otype
`voluntary`. Older app builds read an unknown otype as a closure, so they would show a request as
a legal closure.
**Rule.** `PUBLISH_VOLUNTARY` stays `False` in `tools/wiltshire_closures.py` until the owner says
testers are on a build that understands `voluntary`. A request is dated only by the council's
own dates. Do not assume it repeats each winter.
**Guarded by.** `tools/test_voluntary_closures.py`, `test_the_switch_is_committed_off`.

## Imagery

### 28. z14 is worth publishing, even though the source is 10 m
**What happened.** 12 Sep 2026: the same patch of Suffolk was cut at each zoom and compared
(`tools/sample_imagery.py`). z14 adds no optical detail over z13, but at z13 the phone stretches
each tile four times, so z14 is visibly sharper when zoomed in. z15 adds nothing.
**Rule.** Do not argue anyone out of z14 on resolution alone. The only sharper source that may
ship is Environment Agency vertical aerial photography (OGL, England only, patchy; not built).
APGB may not ship.
**Guarded by.** `tools/satellite_plan.py`: `below_zoom` ranks an area built below the maximum
zoom ahead of the rest for its next rebuild.

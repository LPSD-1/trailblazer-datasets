# Step 1.8 — the cutover, rehearsed and not run

**Verdict: NO-GO as the tree stands.** Not because the data is wrong — it is
not — but because the guard that is supposed to catch a wrong cutover has been
measured, in this checkout, accepting a build with **105 ways in the whole
country and two of six regions empty**, and because a rider who holds a broad
pre-pivot pack set can download the new containers and **not have them
mounted**, with nothing on screen saying so.

Nothing in this document was published, committed or pushed. Everything below
that carries a number was run; where a thing could not be run here it is
labelled UNMEASURED and named as such.

Read in this order: the numbers, the refusal, the override, the two riders, the
rollback, then the list only a device can settle.

---

## 0. What was actually run, and where

Read-only. `git status` on `trailblazer-datasets` was clean before and after;
the only file this work creates is the one you are reading. Synthetic inputs
were written to a scratch directory outside both repositories.

| Run | Command | Result |
|-----|---------|--------|
| A | `python tools/check_build.py --previous manifest.json --new dist/manifest.json` | `FATAL: dist/manifest.json was not built`, exit 1 |
| B | same, `--new manifest.json --cache cache` | exit 1, 2 problems |
| C | same, `--new <synthetic honest cutover> --cache <synthetic full cache>` | **`OK to publish.` exit 0** |
| D | same, `--new <synthetic GUTTED cutover>` | **`OK to publish.` exit 0** |
| E | same, with a region box moved to 50°W | exit 1, 1 problem — the bounds gate still works |
| F | `python tools/test_check_build.py` | `Ran 70 tests`, `OK`, exit 0 |
| G | app: `flutter test test/a_rider_upgrading_holds_both_container_shapes_test.dart` | 25 tests, all passed |
| H | app: `flutter test test/the_upgrade_never_eats_a_riders_packs_test.dart` | 6 tests, all passed |

---

## 1. The numbers, measured

**The published set.** 109 `.tbmap` containers in `containers/`, each with a
`.sig` beside it (109 + 109 = 218 files, plus `containers/manifest.json`).
By `meta.kind`: **105 `area`, 4 `overview`** — `bicycle-overview`,
`foot-overview`, `horse-overview`, `motor-overview`. All 109 carry a `bounds`
string; none is missing one. On disk: **`containers/` 874 MB, `packages/`
128 MB, `.git` 1113 MB.**

**The published index.** `manifest.json` is schema 1, `generated`
`2026-09-17T10:21:22Z`, `authorities: 149`, 105 package entries.
Lane totals by type, computed from it:

| package | ways |
|---------|------|
| foot    | 693,555 |
| bicycle | 123,940 |
| horse   | 123,940 |
| motor   | 12,702 |
| **total** | **954,137** |

`catalogue.json` is schema 2, `generated` `2026-09-22T09:23:33Z`, 7 GB areas,
170 GB pack entries, 4 entries in the top-level `overviews` section.

**The candidate build does not exist in this checkout.** `dist/` holds only
`contract/`, `trips/` and `tro/`. There is no `dist/manifest.json` and no
`dist/containers/`. Run A is therefore the honest answer to "run the guard
against the current build": **there is no current build to run it against.**
`docs/DECISIONS-1.2.md` records the pivot build's own measurements
("Containers for GB: 7", ways by class `boat` 10,342 / `bridleway` 13,629 /
`restricted_byway` 1,737), so it was built once and not kept. Everything below
about the new set is reasoned from that record and from the builders, not
measured against an artefact.

**The guard's constants, read from the file** (`tools/check_build.py`):

| constant | line | value |
|----------|------|-------|
| `MAX_NATIONAL_DROP` | 54 | `0.02` |
| `MAX_AREA_DROP` | 58 | `0.25` |
| `MIN_AUTHORITIES` | 62 | `140` |
| `GB_BOUNDS` | 76 | `(-8.70, 49.80, 1.80, 60.90)` |
| `BOUNDS_SLACK` | 80 | `0.5` |
| `MAX_CLOSURE_FACTOR` | 91 | `3.0` |

The remembered figures were right. **The remembered conclusion was not.**

---

## 2. The refusal — it is not the one that was expected

The brief expected `check_build.py` to refuse, on a −96.7% national drop and
on an authority count of 124 against a floor of 140. Both halves need
correcting.

### 2a. The authority floor does not measure the build

`check_authorities` (line ~448) does not read `manifest["authorities"]`. It
opens `cache/authorities.json`, then counts how many of those 149 codes have a
directory under `--cache` containing at least one non-empty `.json`. In this
checkout that is **4 of 149** — `DY`, `KT`, `SH`, `WT` — because this is a
developer checkout and not the CI cache. So run B refuses with:

> only 4 of 149 authorities have data (need 140). The source was probably
> down; re-run rather than publish a partial map.

That refusal is **correct in its own terms and meaningless as a signal about
the cutover.** It says the fetch cache here is empty, which it is. It says
nothing about the data. **No figure of 124 exists anywhere in this repository**
— it is not in `manifest.json`, not in `catalogue.json`, not in
`build_baseline.json`, not in any doc. Whatever produced it is not something
this tree can reproduce, and it must not be carried into the go/no-go.

The floor also cannot fire on the published build: `manifest.json` records 149
authorities answering, and `tools/test_check_build.py:178` cites "all 149
authorities answering".

### 2b. The national-drop refusal has already been overridden

`tools/build_baseline.json` is **committed** (`git log` shows it entering at
`7c64b27 One dataset, not five: the vehicle partition is gone`;
`git diff HEAD` on it is empty). It records:

```
dropped:       ["bicycle", "foot", "horse", "motor"]
nationalDrop:  0.9671
supersedes:    generated 2026-09-17T10:21:22Z, total 954137
becomes:       {"ways": 31369}, total 31369
recorded:      2026-09-24T03:29:50Z
```

> **AMENDED 2026-09-24, after this rehearsal.** The owner superseded step
> 1.2c's `near` set with **byways only** ("Carry only ways a motor vehicle may
> use"), so a correct cutover now holds ~12,702 rows, 60% short of 31,369 —
> which `MAX_REBASELINE_SHORTFALL` would refuse as a collapsed fetch.
> `becomes` was re-recorded **31,369 → 12,702** (derived: the `near` build's
> 12,702 BOAT rows plus 0 OSM-track rows; not measured, no checkout holds the
> full cache), `nationalDrop` 0.9671 → 0.9867, and the reason text carries the
> decision and this history. `supersedes` is unchanged, so the record still
> applies. `test_check_build.py` now tests the committed file itself
> (`TheCommittedBaseline`). The figures in runs C and D below are the
> rehearsal's, against the record as it then stood.

`supersedes.total` of 954,137 equals the published total exactly, so
`load_baseline` finds it **still applies** — confirmed in the output of runs
B, C and D, which all print `REBASELINED against …`.

So the guard does not refuse the cutover any more. `docs/DECISIONS-1.2.md:139`
already says this, accurately:

> **`check_build.py`**: refuses the cutover (exit 1, 30 problems, −96.7%
> national), accepts it after the recorded rebaseline in
> `tools/build_baseline.json` (exit 0). The `--force` shrug was not used.

The decision record is honest. The problem is what it does not say.

---

## 3. THE BLOCKER — after this baseline, the volume gates do not run at all

This is the finding that decides the go/no-go, and it is measured, not argued.

`check_totals` removes every rebaselined package name from **both** sides
(`excluding`, line ~487) and then:

```python
print("  excluding %s: %d -> %d ways" % (…, old_total, new_total))
if old_total == 0:
    return
```

The committed baseline names **all four** of the published package types. Take
all four out of the published side and `old_total` is **0**. `check_totals`
returns there. Everything after that line — the national drop, the per-vehicle
drop, the per-region 25% drop, "lost every one of its ways", "regions that
disappeared entirely" — **never executes on the cutover publish.**

Measured. Two synthetic cutover manifests, identical but for the counts, both
against the real `manifest.json` and a synthetic 149-authority cache:

| run | new `ways` total | regions with zero ways | guard says | exit |
|-----|------------------|------------------------|------------|------|
| C | 31,369 (the intended figure) | 0 | `OK to publish.` | 0 |
| D | **105** | **2 of 6** | `OK to publish.` | **0** |

Both print the same line: `excluding bicycle, foot, horse, motor: 0 -> N ways`.
Run D is a build in which the fetch collapsed and 99.7% of the country is
missing, and the guard waves it through without comment.

**What still works, and it is worth knowing:** run E moved one region's western
corner to 50°W and the guard refused with
`region south-west claims a corner at -50.0000,49.8500, which is outside Great
Britain`. So on the cutover publish the surviving gates are:

* the authority floor (cache-based, 140 of 149);
* the declared region boxes;
* the geometry-in-GB check **only if `--key` is passed** — CI does pass it
  (`refresh-data.yml:792`);
* `check_containers.py` on the new format (tile size, orphan records, lanes
  claimed by two areas);
* the closure factor — which prints **`THIS GATE DID NOT RUN`** on every lane
  refresh, by design, because lanes and traffic orders are separate workflows.

Nothing at all watches how many ways the cutover actually carries.

**Why this got through 70 green tests.** `tools/test_check_build.py` exercises
the rebaseline with `dropped = {"foot", "horse", "bicycle"}` — `motor` is
deliberately left out, so `old_total` after exclusion is non-zero and the
remaining gates do run, and the tests correctly prove "a rebaseline excuses
what it names and nothing else". The committed baseline is the one shape the
suite never tries: **every type dropped.** The suite is green and the shipped
configuration is unguarded.

**The smallest fix, and it is small.** In `check_totals`, distinguish "nothing
left to compare" from "everything was rebaselined". If `old_total == 0` after
exclusion, the build must be gated on something else — the recorded
`becomes.total` in the baseline is exactly the right yardstick, and it is
already in the file. A cutover that lands within, say, 10% of 31,369 (12,702
since the byways-only amendment above) is the
build that was signed off; 105 is not. Add the case to
`tools/test_check_build.py` with all four types dropped, and confirm it fails
before the change.

---

## 4. The rider holding an OLD container

Good news, and it is proven rather than assumed.

**The app reads both shapes, and detects them from the file.**
`lib/data/lanes/tbmap_store.dart:24` defines `ContainerShape { ways, lanes,
none }`; line 275 decides it by asking `sqlite_master` what objects exist,
`ways` winning over `lanes`. That ordering matters: the pivot builder ships a
`lanes` **view** over the `ways` table for old readers, so a version number
would have sent a new container down the old branch and answered null to every
legal-tier, surface and gradient question. `flutter test
test/a_rider_upgrading_holds_both_container_shapes_test.dart` — **25 tests, all
passed** — including both shapes mounted at once, each answering for itself.

**Containers are mounted from the DISK, not from the catalogue.**
`lib/features/lanes/lane_containers.dart:237` lists `*.tbmap` in the pack
directory. A container whose id has vanished from the published index is still
mounted and still answers. **Signatures are read from a local `.sig` sidecar**
(`_signatureFor`, line 446), not from the catalogue, so the old containers keep
verifying after the cutover — deliberately, so a rider in a field with no
signal is not locked out of data they already hold.

**Nothing deletes them unprompted.** `flutter test
test/the_upgrade_never_eats_a_riders_packs_test.dart` — **6 tests, all
passed** — proves `StorageSweep` leaves `motor-midlands.tbmap`,
`bicycle-north.tbmap` and `ways-midlands.tbmap` alone at 400 days old while
still clearing an abandoned `.part` in the same pass.

So `ContainerShape` genuinely covers what it claims to cover. **It does not
cover the thing that actually bites.**

### 4a. SHOULD-FIX — the new container can lose its slot to the old ones

`activeSetLimit = 8` (`lane_containers.dart:79`). Above that cap,
`_chooseActiveSet` ranks containers by tier, then distance, then **file path**.
The tiers are: 0 covering/nearest the rider, 1 pinned, 2 not an `area`
container, 3 everything else, 4 no bounds. **There is no tier for shape, and no
preference for the newer data.**

Simulated with the real bounds and kinds read out of all 109 published
containers (`meta.bounds`, `meta.kind` via read-only SQLite), applying the
tier/sort rules exactly as written, anchor 53.4, −1.9 (Derbyshire, on the 0.1°
cell), cap 8:

| rider's disk | where `ways-midlands` ranks | mounted? |
|---|---|---|
| all 109 old + the whole new set (7) | **#35 of 116** | **no** |
| all 109 old + `ways-midlands` + `ways-overview` only | **#35 of 111** | **no** |
| 7 `motor-*` containers only + the new set | in the 8 | yes |

At that anchor **36 containers are tier 0** — they all contain the rider. Ties
inside a tier break on path, and `ways-` sorts after `bicycle-`, `foot-`,
`horse-` and `motor-`. The new container loses on the alphabet.

**Pinning does not rescue it.** Re-run with both new containers pinned: still
not mounted, because pinned is tier 1 and 36 containers sit in tier 0 ahead of
it. The documented remedy is inert in exactly the case it is needed.

What the rider sees: they download the new Midlands container, it verifies, it
sits on the phone, and nothing of the pivot — POIs, legal tier, gradient,
conditions, the context note — reaches them. The only trace is one
`Trace.important` line: `active set: 8 of N usable, cap 8, anchor …`.

**Bounded honestly.** A rider who only ever downloaded `motor-*` — the likely
case for this app's audience, 7 containers — is fine: measured, the new
containers mount. The squeeze appears somewhere between 20 and 40 old
containers held at this anchor. But `foot` and `bicycle` packs were published
and downloadable, and the rider who took a lot of them is the rider who cares
most.

**The fix is one tier.** Prefer a `ways`-shape container over a `lanes`-shape
one covering the same ground, inside the tier. It needs the shape at choose
time, and `_factsOf` already opens each container to read `bounds` and `kind` —
`store.shape` is free at that point.

---

## 5. The rider holding a HALF-FINISHED download

Two cases, and only one of them is handled well.

**Transfer completes after the cutover.** `download_queue.dart:1257` looks the
pack up in the freshly fetched catalogue; it is gone, so the rider is told
*"That pack is no longer published, so it was not kept."* and `_discardOrphan`
deletes the `.part` and its expectation sidecar. Truthful, and the bytes are
not left to rot. Note the deliberate ordering above it: if the catalogue cannot
be reached at all, the remembered expectation redeems the bytes instead — so a
rider offline does not lose a completed transfer.

**Transfer is in flight when the file is deleted.** The cutover does
`rm -rf containers && mv dist/containers containers`, so every old container
URL 404s immediately. The in-flight request gets
`package_store.dart:820` → *"Could not download &lt;label&gt; (HTTP 404)."*
That is **opaque** — it reads like a broken server, not like "this pack was
retired an hour ago". A rider retrying in a field is retrying something that
can never succeed. UNMEASURED: whether the platform background-transfer path
(`platform_pack_transfers.dart`) surfaces a 404 the same way, or whether
WorkManager retries it silently for hours. That needs a device.

**The orphan offer.** After the cutover every one of the rider's old containers
is unaccounted for by the index, so `orphanPackFilesProvider` lists them and
the lane packs screen offers to reclaim the space — up to **874 MB** if they
hold the lot. It is rider-initiated, which is right. But the app will be
simultaneously *drawing lanes from* those files and *offering to delete them as
unrecognised*, with no wording that says "these still work, they are just the
old shape". UNMEASURED: the exact wording shown. Worth reading before the
cutover, because a rider who accepts that offer is the one case a rollback
cannot help (see below).

---

## 6. THE ROLLBACK — it is not `git revert`, and nobody has ever done one

**What publishing is.** `refresh-data.yml`, the `Publish` step: `rm -rf
packages && mv dist/packages packages`, `rm -rf containers && mv dist/containers
containers`, `mv dist/manifest.json manifest.json`, `mv dist/catalogue.json
catalogue.json`, then `git add -A` and a commit on `main`, served by GitHub
Pages at `https://lpsd-1.github.io/trailblazer-datasets/`. There is no rebase
and no force-push; the retry path is a plain `git pull`.

**Has anyone ever rolled one back?** No. `git log --all` across the whole
history contains **no commit whose subject mentions revert or rollback**, and
the word "rollback" appears nowhere in any workflow, tool or document in this
repository. The single hit is `ROLLBACK` inside a SQLite test.

**Why the obvious `git revert <cutover>` is the wrong runbook.**
`.github/workflows/traffic-orders.yml` runs on `cron: '23 2,8,14,20 * * *'` —
**every six hours** — and every run calls
`bash tools/rebuild_catalogue.sh manifest.json dist/catalogue.json` and commits
`catalogue.json`. `satellite.yml` (twice daily) and `height.yml` (daily) do the
same. So within at most six hours of the cutover landing, a commit that
rewrites `catalogue.json` sits on top of it, and a plain revert **conflicts on
a generated file** — the exact failure the Publish step's own comment warns
about ("a generated file does not merge, it has a recipe… that is how the
satellite job turned a recoverable push race into a detached HEAD"). Somebody
doing this at 2 a.m. meets a conflict in a 1 MB JSON file.

Worse in the other direction: `rebuild_catalogue.sh` prefers
`dist/containers/manifest.json` and falls back to the **committed**
`containers/manifest.json`. Revert the containers but not the manifest, or the
other way round, and the next scheduled job republishes a catalogue built from
a mismatched pair — hashes from one build against files from another, which the
app rejects as *"That download was corrupted."*

### 6a. REHEARSED, 24 Sep 2026 — and step 1 as written was wrong

Run on a scratch clone with its remote removed, by `tools/rehearse_rollback.sh`,
which anyone can re-run. Nothing was published and the clone was deleted.

**The revert conflict is real.** A simulated cutover, then a simulated
`traffic-orders.yml` catalogue rebuild on top of it — which happens within six
hours, always — and `git revert <cutover>` fails with `UU catalogue.json`,
exactly as predicted above.

**But the documented rollback was wrong, and the rehearsal is the only thing
that could have found it.** Step 1 as written —
`git checkout <cutover>^ -- containers packages manifest.json` — RESTORES the
109 old containers and does not REMOVE the 7 new ones. The first run ended
with **116 containers and 13 `ways-*` files left behind**, and `git status`
reported clean.

That is worse than it sounds, because of a fix made the same day: the app's
active-set chooser now PREFERS the `ways` shape over the legacy one. So a
rollback performed exactly as documented would have left every rider
preferring the containers the rollback existed to withdraw. The rollback
would have failed silently, in the direction of the thing being rolled back.

Corrected step 1:

```
rm -rf containers packages
git checkout <cutover-commit>^ -- containers packages manifest.json
```

**Measured, after the correction:** 109 containers, 105 packs, zero `ways-*`
left, manifest back to schema 1, working tree clean. Clone 5 s, rollback 5 s.
Call it two minutes with a human reading each step.

**The rollback that would actually work**, written down here because it is not
written down anywhere else. UNTESTED — this has never been run, and it should
be rehearsed on a scratch fork before the cutover, not after:

1. `git checkout <cutover-commit>^ -- containers packages manifest.json` —
   restores the 109 containers, their sigs, `containers/manifest.json`, the 105
   packs and the schema-1 lane index. The blobs are already in history, so this
   costs no new storage.
2. `bash tools/rebuild_catalogue.sh manifest.json catalogue.json` — do **not**
   revert `catalogue.json` by hand; rebuild it, for the reason that script's
   own header gives.
3. `python tools/validate_catalogue.py` / `verify_catalogue.py` before
   committing.
4. Commit and push. Pages redeploys in minutes.

**How fast it reaches riders.** Fast, and this is the good news.
`PackageStore.manifestBody` fetches the catalogue over the network on every
read with **no TTL** — the on-disk copy is a last-known-good fallback used only
when the fetch fails. So a rider who opens the app after the revert gets the
reverted index straight away. `prune_published.py` only touches release assets
(satellite and routing), never `containers/`, so it cannot make the rollback
worse.

**What a rollback can never undo.** Three things:

1. A rider who accepted the "reclaim space" offer and deleted their old
   containers. After the revert those ids are published again, so the app will
   offer them for download — but that is a fresh multi-hundred-megabyte
   download over whatever connection they have.
2. A rider who downloaded a `ways-*` container. After the revert it is orphaned
   on their phone and will be offered for deletion. Harmless, confusing.
3. The `.part` files already discarded by the "no longer published" branch.

**So an irreversible step with an untested rollback is two decisions, exactly
as the brief says.** It becomes one decision the moment someone runs steps 1–4
on a scratch fork and it works.

---

## 7. What the guard is right about, and should not be loosened

Do not read section 3 as "the guard is broken". It is one branch. Everything
else in `check_build.py` is well-reasoned and its comments are load-bearing:
the region-keyed totals (an area id moves when the chunker regroups, and that
cost a publish), the per-vehicle check (every BOAT in the country could vanish
inside a 2% national threshold), `BOUNDS_SLACK` at half a degree (the tight box
fired on real published region boxes — the shape of a gate that gets turned off
in its first week), the closure factor firing in both directions, and
`THIS GATE DID NOT RUN` printed out loud rather than nothing. `--force` exists
and was not used. The rebaseline-as-a-committed-record is the right design; it
just has one untested shape.

---

## 8. Only a device, a store account or the owner can settle these

* **The candidate build itself.** It does not exist here. Nothing in this
  document has been checked against real new containers — not their size, not
  their bounds, not whether `check_containers.py` passes on them, not whether
  the catalogue check in `refresh-data.yml` (overview-per-vehicle, `legalBasis`
  = `official`, GB areas ≥ 5) still passes when there is one dataset and not
  four vehicles. **Build it into `dist/` and run the gates against it before
  any go decision.**
* **The active-set squeeze, on a phone.** Section 4a is a faithful simulation of
  the documented algorithm over real container bounds, not a run of the Dart.
  `tool/scenario.py` exists precisely to set a position and assert what
  mounted; that is the instrument.
* **The 404 on an in-flight background transfer.** Only a device with
  WorkManager can say what a rider sees and how long it retries.
* **The orphan-offer wording** on the lane packs screen, read with 109 old
  containers present.
* **The rollback**, rehearsed on a scratch fork. Until then it is prose.
* **Whether the 124 figure means anything.** It is not in this repository. If
  it came from a real run, that run's log is the only thing that can say what
  it counted.
* **The decision itself.** A 96.7% drop is being published on the strength of
  one recorded reason. That reason is detailed and convincing; it is still one
  person's sentence, and it is now the only thing standing between a bad fetch
  and every rider's map.

---

## 9. Go / no-go

**NO-GO**, on three conditions, none of them large:

1. ~~**Blocking.**~~ **DONE.** Close the `old_total == 0` hole in `check_totals`, gate the
   cutover against `build_baseline.json`'s own `becomes.total`, and add the
   all-types-dropped case to `tools/test_check_build.py` — confirming it fails
   before the fix. Without this the guard cannot tell the intended cutover from
   a collapsed fetch, and that is the single thing it exists to do.
2. **IN PROGRESS** — `dry_run` now exists so this is possible at all. Build the candidate into `dist/` and run the full gate chain
   against it — `check_build.py` with `--key`, `check_containers.py`, the
   catalogue check. Publish nothing. Record the real figures beside the
   expected ones.
3. ~~**Blocking.** Rehearse the rollback in section 6 on a scratch fork and
   record how long it took.~~ **DONE, 24 Sep** — see 6a. It found step 1 was
   wrong: the rollback left the new containers in place beside the restored
   old ones. Corrected and re-measured at 5 seconds.

Then **should-fix before or with the cutover**: the active-set tier in section
4a, and the 404 wording in section 5. Neither loses a rider's data; both leave
a rider looking at a map that is quietly not what they downloaded.

An honest "not yet, because of three things that take an afternoon" is a better
outcome than a cutover that goes out behind a guard that has been measured
saying `OK to publish.` to 105 ways.

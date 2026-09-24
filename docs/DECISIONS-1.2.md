# Steps 1.2b and 1.2c — the two decisions the one-dataset builder rests on

Recorded 2026-09-24, from `tools/build_packages.py` and
`tools/build_containers.py` run over the full published population.

**Where the population came from.** `cache/` is fetch output and is not in the
checkout, so the input was reconstructed from the 71 published `foot-*.tbpack`
files, which carry every row type already normalised. The reconstruction is
checked rather than assumed: all **534,941** rebuilt ways produce exactly the
`lane_uid` they were published with — `built == published`, 0 either side.

| type | ways |
|---|---|
| byway open to all traffic | 10,342 |
| restricted byway | 11,433 |
| bridleway | 77,867 |
| footpath | 435,299 — **not carried** |

---

## 1.2c — how much context to carry

**DECIDED, 2026-09-24, by the owner: `none` — byways only.** His choice,
verbatim: *"Carry only ways a motor vehicle may use. Smaller download, but the
byway-ends warning and red 'no motor vehicles' lanes go."* The dataset carries
every byway open to all traffic and every OSM track (`motorbike_ok = 1`,
"check locally" — also a way a motor vehicle may use), and **no bridleway and
no restricted byway**. `build_packages.py --context` now defaults to `none`;
`near` and `all` still build, and the measurement of all three still prints on
every run.

**SUPERSEDED — the near set.** Decided and measured earlier the same day:
bridleways and restricted byways carried only within **1.0 km** of a byway open
to all traffic. Everything below this line down to *The condition this decision
carries* is that decision's record, kept because it is the evidence the owner
traded against: the 15,366 near context ways were carried for one job, the
byway-ends warning, and he gave that job up for the smaller download.

Measured, vertex to vertex, with each candidate pair measured properly rather
than by grid cell:

| option | context ways | total ways | GB containers | largest container | total download |
|---|---|---|---|---|---|
| **`none` — byways only (DECIDED 2026-09-24)** | **0** | **10,342** | — | — | — |
| `near` — within 1 km (superseded) | 15,366 (17.2%) | 25,708 | 7 | 12.56 MB | 14.5 MB |
| `all` | 89,300 | 99,642 | 16 | 12.52 MB | 54.5 MB |

The `none` row's container and download figures were not measured — no
checkout holds the full cache. Its row count across overlapping regional
containers is **12,702**: the BOAT rows of the `near` build (below), plus 0
OSM-track rows, since no source produces `osm_track` yet. That derived figure
is what `tools/build_baseline.json` now records as `becomes.total`; the first
full byways-only build is the measurement.

**73,934 context ways (82.8%) are nowhere near a byway.** They are carried for
exactly one job — answering *"the byway ends here"* at the point where it ends
— and that question cannot arise a mile from any byway. Dropping them takes the
dataset from 99,642 ways to 25,708 and the whole-GB download from 54.5 MB to
14.5 MB, and costs nothing a motor rider can legally use: **every BOAT is
kept**.

### The plan's figure is corrected

`PIVOT-PLAN.md` §1.2c records **23,565 (26.4%)** near and a resulting dataset of
~33,900. Measured here at a true 1 km, it is **15,366 (17.2%)** and **25,708**.

The plan's number is reproduced at a **1.5 km** radius — 22,716 near, 33,058
total — which is what a grid-cell-adjacency test with ~1 km cells actually
measures, since two points in adjacent cells can be up to ~2.8 km apart. The
sweep:

| radius | near | % | dataset |
|---|---|---|---|
| 0.5 km | 8,364 | 9.4% | 18,706 |
| **1.0 km** | **15,366** | **17.2%** | **25,708** |
| 1.5 km | 22,716 | 25.4% | 33,058 |
| 2.0 km | 29,662 | 33.2% | 40,004 |
| 3.0 km | 41,930 | 47.0% | 52,272 |

**Why vertex-to-vertex is honest here.** The filter measures vertex to vertex,
not point to segment, so a long straight segment passing close between two
vertices could be called far. Measured over the 10,342 BOATs: median segment
length **14.7 m**, p90 **49.9 m**, p99 **144.2 m**, and only **0.41%** of
segments exceed 200 m. The approximation errs by dropping a way rather than
admitting one, and by at most half a segment length.

### The condition this decision carries

**Their absence must never read as absence on the ground.** A rider who looks
at a hillside and sees no bridleway must be able to find out that we did not
look there. So the scope travels with the data, not only in this document —
and with byways only it is truer than it was under `near`, because now no
bridleway is carried anywhere:

- `build_packages.py` writes what the build actually selected:
  `contextScope: "none"`, `contextRadiusKm: null` (no radius was measured) and
  `contextNote` into the pack manifest, and the scope and note into every
  sealed pack body. `context_fields()` is the one place the three are decided,
  and `select_ways()` the one place the ways are — `golden.py` calls both
  rather than keeping a copy;
- `build_containers.py` passes both into **every container, overview included**;
- `build_map_container.py` writes them to `meta` as `context_scope` and
  `context_note`.

The sentence, verbatim, since 2026-09-24:

> Bridleways and restricted byways are not on this map: it carries only the
> ways a motor vehicle may use. Where none is shown, this map has not looked -
> it does not mean there is none on the ground.

Under `near` it was, and `--context near` still writes:

> Bridleways and restricted byways are shown only within 1 km of a byway open
> to all traffic. Where none is shown, this map has not looked — it does not
> mean there is none on the ground.

**The app shows it verbatim** — on the lane sheet and the record card, read
from `meta.context_note` — so the wording is decided here and nowhere else.
`test_build_containers.py` fails if any container is silent about it, and
`test_build_packages.py` fails if the default build seals a bridleway or a
restricted byway, or a note claiming a radius.

---

## 1.2b — the split size

**DECIDED: `MAX_PLAIN_BYTES` stays at 12 MiB**, and it is now **not binding**.

| context | split | area packs | containers | largest container | largest plaintext | ~peak RSS at the measured 8× |
|---|---|---|---|---|---|---|
| **near** | **12 MiB** | **6** | **7** | 12.56 MB | 11.54 MiB | ~92 MB |
| near | one region per pack | 6 | 7 | 12.56 MB | 11.54 MiB | ~92 MB |
| all | 12 MiB | 15 | 16 | 12.52 MB | 11.66 MiB | ~93 MB |
| all | one region per pack | 6 | 7 | **32.73 MB** | **32.11 MiB** | **~257 MB** |

**The plan's "12 MiB is what produces 15 containers rather than 6" is confirmed
— and only under `--context all`.** Once footpaths and the far context are
dropped, every region already fits inside 12 MiB on its own, so the 12 MiB
ceiling and one-region-per-pack are *the same build*: 6 areas plus one
overview, **7 containers**.

**Why the ceiling stays anyway.** It is the only thing standing between a
refresh and the `all`/one-region row: 32.1 MiB of plaintext, which at the
measured ~8× parse cost is ~257 MB of peak RSS on a phone whose whole heap is
256–512 MB and whose map wants most of it. The ceiling costs nothing while the
dataset is small and catches the build that stops being small.

**The headroom is thin and should be watched.** South East is **11.54 MiB
against 12 MiB — 96% of the ceiling**. The next refresh that adds ways there
will split it, and the region will stop being one pack.

**Mount time is not measured here, and is not measurable here.** It needs a
device; `verify.py` on a Note 9 is the instrument. The desktop proxy — open,
read `meta`, one rtree query, one tile fetch, best of five — reads 0.7 ms for
the 12.56 MB container and 1.8 ms for the 32.73 MB one, which says only that
SQLite opens both lazily. **The number that decides this is peak RSS at parse,
and that is what the 8× ruler above is standing in for.**

---

## What else this build reads

*The `near` build's figures, recorded before the owner's byways-only decision
superseded it. The byways-only build keeps the `boat` column and loses the
other two.*

- **Containers for GB: 7** (`ls containers/*.tbmap | wc -l`), against a gate of
  ≤ 16.
- **Ways by class**, whole dataset: `boat` 10,342, `bridleway` 13,629,
  `restricted_byway` 1,737. Rows across overlapping regional containers:
  12,702 / 16,564 / 2,103.
- **`SELECT COUNT(*) FROM ways WHERE way_class = 'footpath'` returns 0** in
  every container.
- **Reproducible**: 13 artefacts rebuilt a month later from the same council
  data are byte-identical, 0 differing.
- **`check_build.py`**: refuses the cutover (exit 1, 30 problems, −96.7%
  national), accepts it after the recorded rebaseline in
  `tools/build_baseline.json` (exit 0). The `--force` shrug was not used.

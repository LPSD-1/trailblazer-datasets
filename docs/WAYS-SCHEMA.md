# The `ways` schema — one dataset, not five

Step 1.1 of `greenroadmap-app/docs/PIVOT-PLAN.md`. **This is the contract.**
Every phase-1 producer writes to it; the app's reader reads it; the
builder↔reader contract test (0.8) checks both ends agree.

## Why one table

Today the same way is duplicated into every vehicle that may use it — bicycle
and horse packs are byte-identical because they are the same bridleway data
built twice. That partition produced 109 containers and froze a phone. One
table, classed per way, replaces it: **109 containers become 7** — six regions and one overview.

> Said **15** until 2026-09-24, which was the figure before step 1.2c decided the context near-set and before the region list settled at six. The spec and the plan both say 7 and this file, the contract they cite, said something else.
>
> The near-set was itself superseded the same day: the owner chose **byways only** (see Classes). 7 is the `near` build's measured count; the byways-only build is smaller and has not been built in full.

## Table

```sql
CREATE TABLE ways (
  way_uid      TEXT PRIMARY KEY,   -- stable across builds
  way_class    TEXT NOT NULL,      -- see Classes
  designation  TEXT,               -- as the authority words it
  name         TEXT,
  authority    TEXT NOT NULL,      -- the surveying authority
  county       TEXT,

  -- LEGAL PROVENANCE. Per way, never per pack: a region may hold statutory
  -- and OSM-derived ways side by side and the map colours them differently.
  legal_tier   TEXT NOT NULL,      -- 'statutory' | 'osm'
  source       TEXT NOT NULL,      -- e.g. 'rowmaps:derbyshire'
  source_date  TEXT NOT NULL,      -- ISO date of the record we read

  -- PHYSICAL, from OSM. NULL means unknown, and unknown is not 'no'.
  surface      TEXT,
  smoothness   TEXT,
  tracktype    TEXT,
  width_m      REAL,
  min_width_m  REAL,               -- narrowest point, incl. barrier gaps
  barriers     TEXT,               -- JSON array of {kind, lat, lon}

  -- TERRAIN, from our own DEM. ~100% coverage, which is why it carries the
  -- motorbike/4x4 distinction rather than width (~10%).
  climb_m         REAL,            -- total ascent
  sustained_pct   REAL,            -- steepest continuous 100-150 m
  -- NOT max_pct. A raw maximum is single-sample noise: two of the five
  -- steepest lanes measured 28% over TWO METRES of climb, because z12 is
  -- ~23 m/pixel and cannot resolve a track on a shelf.

  -- DERIVED ACCESS, with its reason. Never a bare boolean.
  motorbike_ok    INTEGER NOT NULL,   -- 1 yes, 0 no
  fourxfour_ok    INTEGER NOT NULL,
  access_reason   TEXT NOT NULL,      -- why, in words, for the rider
  access_evidence TEXT NOT NULL,      -- 'statutory' | 'order' | 'osm' | 'none'

  length_m     REAL NOT NULL,
  geometry     BLOB NOT NULL        -- as build_map_container.pack_geometry
);

CREATE VIRTUAL TABLE ways_bbox USING rtree(id, min_lon, max_lon, min_lat, max_lat);
```

## Classes

| `way_class` | Meaning | Drawn |
|---|---|---|
| `boat` | Byway open to all traffic | **rideable** |
| `restricted_byway` | No mechanically propelled vehicles | greyed, never green |
| `bridleway` | Horse, foot, cycle | greyed, never green |
| `ucr` | Unclassified road — **publicly maintainable, rights NOT recorded** | distinct, never green. **Deferred**: F3 found only 2 of 10 authorities publish usable geometry |
| `osm_track` | Outside England and Wales; OSM-derived | amber, "verify locally" |

**Footpaths are not carried.** 627 MB, no bearing on a motor vehicle.

**Bridleways and restricted byways are not carried either — byways only.**
Step 1.2c, decided by the owner on 2026-09-24: *"Carry only ways a motor
vehicle may use."* It superseded the earlier `near` decision (carry them within
1 km of a BOAT, 15,366 ways, for the byway-ends warning); see
`DECISIONS-1.2.md`. The two classes stay in this table because the reader maps
them and `--context near|all` still builds them, but the published dataset
holds `boat` and `osm_track` only. Every container says so in
`meta.context_scope = 'none'` and `meta.context_note`, which the app shows
verbatim so the absence is never read as absence on the ground.

## The rule the schema exists to enforce

> A way is drawn rideable **only** when `way_class = 'boat'` and
> `legal_tier = 'statutory'`. Everything else is shown and labelled.

`access_evidence` must never be `'none'` on a way where `fourxfour_ok = 0` —
hiding a lane requires evidence, and F1 measured that we have it for under 10%.

## POIs

Same container, own table:

```sql
CREATE TABLE pois (
  poi_uid TEXT PRIMARY KEY, category TEXT NOT NULL, name TEXT,
  lat REAL NOT NULL, lon REAL NOT NULL, opening_hours TEXT,
  source_date TEXT NOT NULL
);
CREATE VIRTUAL TABLE pois_bbox USING rtree(id, min_lon, max_lon, min_lat, max_lat);
```

Categories: `fuel`, `food`, `toilets`, `water`, `parking`, `camping`,
`repair`, `viewpoint`, `atm`. Nothing else — a general POI database would
dwarf the ways beside it.

### `source_date` means "unchanged since", and `meta.pois_checked` is the read

A POI's `source_date` is **not** the day its source was read. `build_pois.py`
(`write_pois`, through `stable_ids.keep_dates`) gives a row whose category,
name, position and opening hours all equal the published row's (same
`poi_uid`) the **published** `source_date`; only a new row, or one whose
content moved, takes the day it was read. So the column says "unchanged
since <date>". Re-dated to every read instead, a monthly refresh of unchanged
OpenStreetMap rewrote every row in every region: 38.0 MB of changesets for
94 MB of containers.

When the region's POIs were last **read** is a fact about the region, stated
once as `meta.pois_checked` (see Meta). The app shows both — "checked 3 days
ago, unchanged since 24 Feb 2026" — and treats a container without
`pois_checked` (built before this split, when every row was re-dated at every
read) as one whose `source_date` *is* the read date.

### Row numbers are stable across builds

`pois.rowid`, `fords.rowid`, `ford_gauges.id` and `wet_gauges.id` are local
numbers, and a changeset matches rows on them. They are taken from the
region's **published** container (`containers/`, what riders hold) by
`tools/stable_ids.py`: a uid (or EA `station_id`) it carries keeps its
number, a new one gets the next number above the highest the published file
used, and a removed one leaves a gap. With no published container they are
numbered as a first build: 1..N in uid order for POIs and fords, first-seen
for gauges. Measured on `ways-east-anglia.tbmap`: numbered 1..N on every build,
one new POI whose uid sorted early made a 5,869,568-byte changeset (62% of the
container); numbered stably, 15,872 bytes. Hashing the uid into the rowid, as
`ways` does, was tried for POIs and grew the container ~40% gzipped.

## Conditions

Spec 9.6 C (wet-weather restraint) and 9.6 G (fords and river levels). Five
tables, same container, written by the **static** half of the conditions
pipeline in `refresh-data.yml` — `build_wet.py assign` and
`build_fords.py build`.

**ALL FIVE ARE ADDITIVE. No existing container is invalidated.** Every one is
`CREATE TABLE IF NOT EXISTS`, nothing already in the schema changes shape, and
a reader that has never heard of them reads exactly what it read before. A
container built before this pipeline ran is a container with no conditions, not
a broken one — and the app must tell those apart the way it already does for
POIs: the tables' **presence** is the claim. Present and empty means "we looked
here and there is nothing"; absent means "nobody has run this yet".

```sql
-- Which lane is soft, and which rain gauge speaks for it.
CREATE TABLE way_wetness (
  id             INTEGER PRIMARY KEY,  -- = the record's rowid, as *_bbox.id is
  susceptibility TEXT NOT NULL,        -- hard | firm | soft | unknown
  basis          TEXT NOT NULL,        -- surface | tracktype | none
  basis_value    TEXT,                 -- the OSM value read, verbatim
  gauge          INTEGER,              -- wet_gauges.id; NULL when none near
  gauge_m        REAL                  -- metres to it; NULL when none near
);
CREATE TABLE wet_gauges (
  id         INTEGER PRIMARY KEY,      -- local to this container
  station_id TEXT NOT NULL,            -- EA notation: the key into the feed
  label      TEXT, lat REAL, lon REAL
);

-- Where you cross water, and which river gauge speaks for it.
CREATE TABLE fords (
  ford_uid    TEXT PRIMARY KEY,        -- osm:n123 / osm:w123, stable
  way_id      INTEGER,                 -- the record's rowid
  ford_tag    TEXT NOT NULL,           -- the OSM value, verbatim
  name        TEXT, lat REAL NOT NULL, lon REAL NOT NULL,
  way_m       REAL,                    -- metres from the ford to that way
  gauge       INTEGER,                 -- ford_gauges.id; NULL when none near
  gauge_m     REAL,                    -- metres to it; NULL when none near
  source_date TEXT NOT NULL
);
CREATE TABLE ford_gauges (
  id          INTEGER PRIMARY KEY,
  station_id  TEXT NOT NULL,           -- EA notation: the key into the feed
  label TEXT, river TEXT, lat REAL, lon REAL,
  typical_low_m REAL, typical_high_m REAL   -- NULL where the EA never said
);
CREATE VIRTUAL TABLE fords_bbox USING rtree(id, min_lon, max_lon, min_lat, max_lat);
```

**`gauge_m` is part of the answer, not metadata.** Spec 9.6 G: "a gauge ten
miles downstream says less than one above the ford". EA gauges are sparse and
essentially never at the ford, so no row is written without the distance, and a
reader that shows the reading without it is making a claim the data does not
support.

**NULL is not a reassurance.** `gauge` is NULL when there is nothing within
`ea_flood.StationIndex.MAX_M`, and that must read to the app as *we do not
know* — never as *the ford is fine*. The same rule the rest of this file states
as "unknown is not 'no'", pointed the other way: here the cautious direction is
silence.

**Nothing here is a closure.** A ford is a hazard and rain is advice. Neither
may be drawn like a traffic order, and a lane that is legally open and very wet
is legally open.

### The two published feeds

The **live** half touches no container. What moves every six hours is published
beside the packs instead, listed in `catalogue.json` under the top-level
`conditions` block with a size and a `sha256` for each:

| File | Built by | Joins on | Carries |
|---|---|---|---|
| `published/wet/<region>.json` | `build_wet.py feed` | `wet_gauges.station_id` | `mm_24h`, `mm_48h`, `at`, the bands, `calibrated` |
| `published/rivers/<region>.json` | `build_fords.py feed` | `ford_gauges.station_id` | `m`, `at`, `state`, `vs_typical_high_m` |

**A station missing from a feed means unknown, never dry and never low.** Both
feeds OMIT a gauge with no usable reading rather than writing a zero: a gauge
that stopped reporting looks exactly like a dry one if a missing total defaults
to `0.0`, and the direction of that error is a rider told a flooded ford is
passable.

**Why the split.** A container is ~190 MB and a rider re-downloads one whenever
its bytes move. Writing this morning's rainfall into one would republish a
fifth of a gigabyte to say that it had drizzled — the same fault the POI cache
and the `containers/manifest.json` run stamp each exist to prevent. The feeds
are kilobytes: measured, 24 kB of rain and 3.4 kB of river for the South West.

**Anything that writes into a container after `build_containers.py` has
finished must be followed by `stamp_build.py` and then
`restamp_containers.py`** (the first is under Meta, below). The builder takes each
container's `sha256`, `bytes`, `downloadBytes` and Ed25519 signature at the
moment it finishes that file. Measured on the published
`containers/motor-south-west.tbmap`, the static half moved it from
`12f0fff663dbd006…` / 2,347,008 bytes to `95a7fe247a6d26e1…` / 2,437,120 bytes
while the manifest still described the old one — a download that fails its own
checksum forever, and a signature over bytes that no longer exist.

## Meta

`meta` is `(key TEXT PRIMARY KEY, value TEXT)`. A region container carries:

| key | written by | value |
|---|---|---|
| `format_version` | `build_map_container.py` | `1`; the container layout |
| `schema_version` | `build_map_container.py` | `1`; this schema |
| `kind` | `build_map_container.py` | `area` or `overview` |
| `built_at` | `build_map_container.py`, then `stamp_build.py` | the build id; see below |
| `ways_cut` | `stamp_build.py`, only on a rule-3 restamp | the date the lanes were cut; see below |
| `bounds` | `build_map_container.py` | `west,south,east,north` over every way |
| `min_zoom`, `max_zoom` | `build_map_container.py` | the tile zoom range, inclusive |
| `lane_count`, `way_count` | `build_map_container.py` | rows in `ways`; `0` for an overview |
| `class_counts` | `build_map_container.py` | JSON: rows per `class` |
| `legal_tier_counts` | `build_map_container.py` | JSON: rows per `legal_tier` |
| `authorities` | `build_map_container.py` | JSON array of highway authorities |
| `context_scope`, `context_note` | `build_map_container.py`, when given | what context is not carried, and the note the app shows (see Classes) |
| `pois_checked` | `build_pois.py` (`write_pois`) | ISO day the region's POIs were last read; see below |
| `evidence_dates` | `evidence_age.py --write` | JSON; see below |

**`built_at` must not leak into any pack's content hash** — a run stamp doing
exactly that broke reproducibility once already.

### `built_at` follows the content, all of it

`built_at` starts as the pack's `generated` stamp, which moves only when the
ways do. Everything written after `build_containers.py` — POIs, wetness,
fords, the evidence dates — can change a container without moving it, and
every changeset is keyed on `built_at` (`build_changeset.py`,
`validate_changeset.py`, the app's `ContainerUpdate`). So
`tools/stamp_build.py` runs after the last write, against the published
container of the same name:

- **same content** (`content_digest`: every table and row, every meta key but
  `built_at` and `ways_cut`) — the published file is copied in, byte for
  byte, with its `built_at` and `ways_cut`;
- **different content** — the build's own `built_at` if it sorts after the
  published one (the ways changed), otherwise this run's time, or one second
  past the published stamp if the clock is not ahead of it.

Always ISO-8601 UTC (`%Y-%m-%dT%H:%M:%SZ`). The container manifest's
`generated` is set to match. Two builds with different content never share a
`built_at`, and `publish_changesets.py` refuses the job if one ever does
(same `built_at` as the published file, different bytes).

### `ways_cut`: the date riders are shown for the lanes

The last rule makes `built_at` the run's clock whenever a container changed
under unchanged ways — a POI, ford or gauge refresh. That is right for a
build id and wrong as the lanes' date: the app printed `built_at` as
"cut <date>", so a new fuel station told every rider in the region that
their lanes were cut that morning. So before that rule overwrites the pack
stamp, `stamp_build.py` keeps it as **`ways_cut`**. A fresh build never
carries the key, so every restamp writes that build's own pack stamp: a
second refresh under the same ways keeps the same date, and new ways bring
their own (as `built_at` by the second rule, or as `ways_cut` by the last).

- **Absent** means the container was never restamped, and `built_at` is
  still the lanes' date. The app, and `stamp_build.ways_cut()`, show
  `ways_cut` where it exists and `built_at` otherwise. It is written only
  when the two dates part, so a container never restamped is byte for byte
  what the builder made (and what `golden.py` holds).
- **Not content.** `content_digest` skips it with `built_at`: a fresh build
  never carries it, so hashed, every region once restamped would read as
  changed on every run after.
- **Carried.** A changeset restates it like every other meta key but
  `built_at` and `kind`, so a rider's patched copy keeps it, and each
  container's manifest entry says the same date as `waysCut`.

### `pois_checked`: when the POIs were last read

Written by `build_pois.write_pois`: the day the region's POIs were last read
from OpenStreetMap, as an ISO date (`YYYY-MM-DD`). A region is as old as its
stalest category, so it is the **oldest** read date — the caller's
`checked` (`build_pois.py build` passes the stalest cache date), or, when
none is given, the oldest `source_date` among the rows handed in. No POIs
and no read date writes no key, and a file with no `meta` table is not
given one.

It **is** content: unlike `ways_cut` it is hashed, because it says something
new each time the region is read. A refresh of unchanged OpenStreetMap
therefore moves this one key, the build is restamped, and the changeset is
its own pages (measured 15.9–16.4 kB raw / ~2.5 kB gzipped per region, where
re-dating every row made it 3.1–9.6 MB). A POI's own `source_date` means
"unchanged since" — see POIs.

### `evidence_dates`

`meta` also gains **`evidence_dates`** (JSON), written by `evidence_age.py
--write`. Per table (`ways`, `lanes`, `pois`, `orders`, `fords`):

```json
{"format": 1, "warn_days": 365,
 "ways": {"state": "measured", "rows": 1448, "dated": 1448, "unknown": 0,
          "newest": "2026-09-24", "median": "2026-09-24",
          "p90": "2026-09-24", "oldest": "2026-09-24",
          "days": {"2026-09-24": 1448}},
 "lanes": {"state": "absent"},
 "pois": {"state": "blind", "why": "no source_date column"}}
```

`state` is `measured`, `blind` (the table has no `source_date`) or `absent`.
`unknown` counts rows whose `source_date` is missing or unreadable; they are in
`rows` and not in `dated`, `days` or any percentile. `median` and `p90` are
nearest-rank over the dated rows newest-first, so the median AGE on a given
day is that day minus `median`. `days` is the count of rows on each distinct
`source_date`, so any threshold count — rows older than `warn_days`, rows
dated after today (a data fault, to be treated as undated) — is a sum the app
does against its own clock. It is how the app says how old the survey is
instead of quoting a build stamp, which describes when *we* ran a script and
not when the authority surveyed.

**Dates, never ages, and that is load-bearing.** It replaced `evidence_age`,
which stored ages in days against an `as_of` pinned to the 1st of the month —
so every region container's bytes moved on the first run of every month with
nothing in it changed, under an unchanged `built_at`, and the app fetched all
six regions whole (~94 MB) where no changeset could reach. A date does not
age. `--write` removes a legacy `evidence_age` key when it writes.

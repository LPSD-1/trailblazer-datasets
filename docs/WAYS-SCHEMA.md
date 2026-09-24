# The `ways` schema — one dataset, not five

Step 1.1 of `greenroadmap-app/docs/PIVOT-PLAN.md`. **This is the contract.**
Every phase-1 producer writes to it; the app's reader reads it; the
builder↔reader contract test (0.8) checks both ends agree.

## Why one table

Today the same way is duplicated into every vehicle that may use it — bicycle
and horse packs are byte-identical because they are the same bridleway data
built twice. That partition produced 109 containers and froze a phone. One
table, classed per way, replaces it: **109 containers become 15.**

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

## Meta

`meta` gains `schema_version` (start at `1`), `built_at`, `legal_tier_counts`,
and `authorities` (JSON array). **`built_at` must not leak into any pack's
content hash** — a run stamp doing exactly that broke reproducibility once
already.

# TrailBlazer Datasets

Public rights-of-way data for the TrailBlazer app, packaged for offline use.

Every package here is built from **local highway authority definitive maps** —
the legal record of public rights of way in England and Wales — obtained via
[rowmaps.com](https://www.rowmaps.com) and published under the
[Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).

> Contains public sector information licensed under the Open Government Licence
> v3.0. Source: local highway authority definitive maps, via rowmaps.com.

## What this is not

**Traffic Regulation Orders and temporary closures are not in this data.** The
definitive map records that a right of way exists and what class it is. It does
not record that a byway is shut this winter, or that a TRO bans motor vehicles
on it. A lane shown here as a byway open to all traffic may still be closed to
you today.

Check signage on the ground and your local authority's TRO register before you
ride. This data is guidance, not permission.

## Layout

```
catalogue.json         the worldwide index the app reads
manifest.json          the original Great Britain lane index (still read)
packages/*.tbpack      sealed lane packages
```

`catalogue.json` is schema 2: continent, then country, then area, with typed
packs. It covers 41 countries across six continents — routing everywhere, and
rights of way where an official register exists to draw them from.

Routing packs point at the 5x5-degree tile scheme rather than being mirrored
here. Nine gigabytes of tiles will not fit in a GitHub Pages site (1 GB), and
a rider only ever needs the squares they ride in.

`manifest.json` lists every package with its size, lane count, SHA-256 and the
path to its file. The app fetches the index, verifies each download against the
hash, and opens it on the device.

## How packages are split

Two dimensions, both of which exist for a reason.

**By vehicle**, because the law differs by what you are travelling on:

| Package   | Contains                                          |
|-----------|---------------------------------------------------|
| `motor`   | Byways open to all traffic — legal to ride         |
| `bicycle` | Byways and bridleways                              |
| `horse`   | Byways and bridleways                              |
| `foot`    | Every recorded right of way, footpaths included    |

A motorcyclist has no use for 437,000 footpaths, and downloading them costs
data and storage for nothing.

**By area**, because of memory. Opening a package costs roughly eight times its
plaintext size at peak — the decrypted bytes, the decoded JSON and the parsed
geometry are all live at once. Measured:

| Plaintext | Peak memory |
|-----------|-------------|
| 2 MB      | +16 MB      |
| 25.6 MB   | +210 MB     |
| 140.2 MB  | +981 MB     |

Android gives an app a heap of 256–512 MB and the map wants most of it, so no
package exceeds **12 MB of plaintext**. Sparse types fit a whole region;
footpaths are cut into authority-sized pieces — "Derbyshire", not "part 7 of
12", because a county is somewhere a rider can point at.

## Format

`.tbpack` files are gzipped GeoJSON sealed with AES-256-GCM.

```
"TBPK" (4) │ version (1) │ alg (1) │ nonce (12) │ ciphertext │ MAC (16)
```

The header and nonce are passed as GCM additional authenticated data, so the
version and algorithm bytes are bound to the ciphertext.

**The encryption is a speed bump, not a lock, and it is not pretending to be
one.** The app has to decrypt these to draw them, so the key is necessarily on
the device and can be recovered by anyone determined. That is fine: the
underlying data is Open Government Licence material and anyone may download the
same rights of way from the councils for nothing. What the packaging protects
is the assembled, cleaned, per-vehicle product — not a secret.

## Rebuilding

Packages are produced by the toolkit in `trailblazer-data`:

```
python fetch_rights_of_way.py            # 149 authorities, cached and resumable
python rowmaps_refresh.py                # re-check cached files, take newer ones
python build_packages.py --key <keyfile>
```

The fetch only fills gaps in the cache. `rowmaps_refresh.py` (run by every
lane refresh) re-checks each cached byway, restricted byway and bridleway
file about once a week with a conditional request, and takes a changed file
keeping every unchanged record byte for byte, so lane ids only move for
ways that really changed. A newer file that is empty, collapsed or too
different to be the same network is not taken; the cached copy stays, and
an issue says so.

Republishing replaces packages in place. Filenames carry no build date — here
or on the device — so a new build supersedes the old copy rather than
accumulating beside it.

**The build is reproducible.** Rebuild the same council data and you get the
same bytes: the GCM nonce is derived from the payload rather than drawn at
random, and a package whose lanes have not changed keeps the date it was cut.
So a monthly refresh over a map nobody amended produces packages identical to
the published ones, publishes nothing, and costs riders no download at all —
and the date shown against a package is the date that data was really cut,
not the date a build last ran over it.

## Traffic orders and closures: where they come from

The traffic-order pack (`tro/index.json`, rebuilt four times a day by
`traffic-orders.yml`) carries orders from the Department for Transport's
D-TRO service and, beside them, the byway closures and orders councils
publish themselves. D-TRO is not mandatory and carries almost no byway orders
(81 of 146,202 records in the 6 September 2026 extract name a byway), so these
run alongside it indefinitely. An order D-TRO already holds is not repeated:
the council's copy is folded into it and listed under `also`.

Read by `council-orders.yml` (`tools/council_sources.py`), committed to
`tro/council/`, each order carrying its `source`, `source_name` and the
council's own `url` for it:

| Source | Publisher | Licence as published |
|---|---|---|
| `dorset-closures` | Dorset Council - rights of way closures (WFS) | Open Government Licence v3.0 |
| `devon-closures` | Devon County Council - temporary path closures | Open Government Licence v3.0 |
| `somerset-closures` | Somerset Council - rights of way closures and traffic orders | Open Government Licence v3.0 |
| `suffolk-prow-tros` | Suffolk County Council - PROW traffic regulation orders | Terms equivalent to the OS OpenData Licence |
| `suffolk-ttros` | Suffolk County Council - live temporary PROW closures | Published by the council |
| `lancashire-closures` | Lancashire County Council - public rights of way (temporary closures) | Open Government Licence v3.0 |
| `essex-prow-tros` | Essex County Council - PRoW traffic regulation orders | Published by the council |
| `northumberland-closures` | Northumberland County Council - rights of way closures and TTROs | Published by the council |
| `bracknell-prow-tros` | Bracknell Forest Council - public rights of way TROs | Published by the council |
| `hertfordshire-ptros` | Hertfordshire County Council - rights of way layer (permanent traffic regulation orders) | Open Government Licence v3.0 |
| `order-register` | Published lists of permanent and seasonal byway orders: East Sussex, Surrey, West and North Northamptonshire, Central Bedfordshire, Derbyshire, Lake District NPA (and others as reviewed) | Published by each council; transcribed and reviewed |

The pack's own `attribution` and `sources` name every one of these, and each
council's row in its `authorities` block lists which sources cover it.

How they are read: an honest User-Agent naming this repository, robots.txt
obeyed (RFC 9309), a gap between requests, read-only calls only, and nothing
behind a bot challenge (`tools/polite_http.py`). West Berkshire's closures
layer is listed and not read: its GIS host's robots.txt disallows all
automated access. No personal data is stored: contact names, phone numbers,
e-mail addresses and applicants are dropped before anything is written. A
source that fails, or suddenly returns nothing, keeps its last good copy.

## The order register

`tro/register/orders.json` holds the long-standing permanent and seasonal
byway orders councils list on their own pages and in PDFs, transcribed once
and reviewed: only entries marked `approved` are published (as the
`order-register` source above); entries a reviewer could not settle are kept
as `needs-review`, with why, and orders known only by their title (the PDFs
robots.txt keeps us out of) as `listed-only`. `order-register.yml` re-reads
the councils' pages every quarter and opens an issue with the diff when one
changes; it never edits the register itself.

### Documents robots.txt keeps us from, and documents saved by hand

By the owner's decision of 8 October 2026, a few council order documents
that robots.txt alone disallows are read anyway: Cambridgeshire's and
Hertfordshire's byway order PDFs, Powys's `/media/` order documents and
Derbyshire's path closure register (`tools/robots_override.json`). Only
those paths are affected. They are read only by the order register's check,
at most once a week, with the same honest User-Agent and pacing, and every
read is logged in `tro/register/override-reads.json`. The order pack's
coverage table says "robots.txt overridden by owner decision" against
anything published from them. A 403, a bot challenge or a refusal of
GitHub's servers is never got past. Documents behind those are saved by
hand, or sent by the council, into `manual/<CODE>/` (see
`manual/README.md`). They are read from disk like any page, and the
coverage table credits them "saved by hand" or "supplied by the council"
with the date.

## Status changes

`status/status-changes.geojson` flags byways whose legal status is changing:
Planning Inspectorate decisions on orders that add, upgrade, downgrade or
delete a byway, read weekly from GOV.UK by `status-changes.yml`
(`tools/pins_decisions.py`; Open Government Licence v3.0). Every byway
decision found, matched to a lane or not, is listed in
`status/pins-decisions.json`.

`status/dmmo-applications.json` lists the applications to modify the
definitive map that concern byways, from the registers Devon, Northumberland
(current and 2020), Caerphilly, Bradford and Derbyshire publish as map layers
(`tools/dmmo_applications.py`), each matched to the byways it runs along;
`status/dmmo-applications.geojson` draws the ones still open. Applicants'
and landowners' names and addresses, case officers' names and scanned
applications are never requested, so they never reach this repository.
Dorset's register refuses GitHub's runners and is not read.

`status/mod-ranges.json` carries the MoD's current firing notice for every
range in England and Wales from GOV.UK (`tools/mod_ranges.py`): the month,
the link and the timings as published. No open boundary data exists for the
ranges, so no byway is placed in one by a guess; a reviewer who has checked
a range's byelaw map may list its byways in `status/mod-ranges-ways.json`.
All three run weekly in `status-changes.yml`.

## Street works closures

`street-manager.yml` runs monthly. It gives every byway the Unique Street
Reference Numbers of the streets it runs along, from OS Open USRN
(`tro/streetworks/byway-usrn.json`; contains OS data, Crown copyright and
database right, OGL), then reads the last three months of the Department for
Transport's Street Manager activity archive (England, OGL) and writes the
closures filed against a byway's USRN, at the byway, to
`tro/streetworks/orders/street-manager.json`. The order build lays them under
D-TRO and the councils' own feeds. Skips, scaffolding and other works that
do not shut the way are left out.

## Byways from the councils' own layers

rowmaps.com copies each council's definitive map, and many of its copies are
over a year old. Sixteen councils publish their rights of way live, and for
them `council-ways.yml` reads the byways (BOATs only) from the council every
day into `council-ways/<CODE>.json` (`tools/council_ways.py`):

| Code | Council | Layer | Licence |
|---|---|---|---|
| CB | Cambridgeshire County Council | iShare WFS `ccc:public_rights_of_way` | OGL v3.0 |
| BK | Central Bedfordshire Council | iShare WFS `RoW_Legal_Network_1` | OGL v3.0 |
| WJ | Wokingham Borough Council | ArcGIS `PRoW_in_Wokingham_(Public)` | OGL v3.0 |
| DN | Devon County Council | ArcGIS `Environment/Public_Access/0` | not stated |
| CH | Cheshire East Council | GeoServer `CEOpenData` BOAT layer | not stated |
| CC | Cheshire West and Chester Council | GeoServer `CWaCOpenData` BOAT layer | OGL v3.0 |
| ON | Oxfordshire County Council | ArcGIS `CAMS_PRoW` | OGL v3.0 |
| LA | Lancashire County Council | ArcGIS `Public_Rights_of_Way` | OGL v3.0 |
| ND | Northumberland County Council | ArcGIS `PRoW_rowwork_ln_MASTER_view` | OGL v3.0 |
| ES | East Sussex County Council | ArcGIS `Rights_of_Way_(non_definitive)` | OGL v3.0 |
| HD | Hertfordshire County Council | ArcGIS `public/row/3` | OGL v3.0 |
| EX | Essex County Council | ArcGIS `PROW_view` | not stated |
| WT | Wiltshire Council | ArcGIS `OpenData/PublicRightsofWay` (query only) | OGL v3.0 |
| BC | Bracknell Forest Council | ArcGIS `GIS_PublicRightsOfWay/7` | OGL v3.0 |
| WB | West Berkshire Council | ArcGIS `gis.westberks.gov.uk` `PUBLIC_RIGHTS_OF_WAY/1` (query only) | not stated |
| IW | Isle of Wight Council | ArcGIS `arcgis.iow.gov.uk` `PublicRightsOfWay/0`: a cross-check only, used if rowmaps ever has no Isle of Wight file | not stated |
| HA | Hampshire County Council | ArcGIS Online `Hampshire_Rights_of_Way` (June 2023): a cross-check only, used if rowmaps ever has no Hampshire file | not stated |

The lane build lets the council's layer decide which byways exist and keeps
every unchanged way's rowmaps record byte for byte, so its id does not move: a
byway the council no longer draws is dropped, and one it draws that rowmaps
lacks is added with the council's geometry. Such a way's `source` is
`council:<authority>` and its attribution names the council. rowmaps stays the
fallback: an unreadable layer keeps its last good file, a council file
that disagrees with rowmaps too much to be the same network is not used,
and neither is one whose last good read is 30 days old or more
(`council_ways.MAX_AGE_DAYS`), since rowmaps may have added byways since.
West Berkshire's older GIS host (robots.txt) and its website (a bot
challenge) are not read; its newer GIS host has no robots.txt and is. Dorset
refuses GitHub's runners and is read by the home collector
(HOME-COLLECTOR.md). Kent's, Dartmoor's and Exmoor's own layers carry
licences that restrict their use and are not read.

## Licence

Data: Open Government Licence v3.0 — attribution required, no share-alike.
The attribution string above travels on the collection *and* on every single
feature, because losing it silently would put users in breach while everything
still appeared to work.

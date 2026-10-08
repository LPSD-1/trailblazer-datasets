# TrailBlazer Datasets

Public rights-of-way data for the TrailBlazer app, packaged for offline use.

Every package here is built from **local highway authority definitive maps** —
the legal record of public rights of way in England and Wales — obtained via
[rowmaps.com](https://www.rowmaps.com) and published under the
[Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).

> Contains public sector information licensed under the Open Government Licence
> v3.0. Source: local highway authority definitive maps, via rowmaps.com.

Since 8 October 2026 the lanes also include **unsurfaced unclassified roads**
read from the councils' own highway records - Devon, North Yorkshire, Norfolk,
Lincolnshire, Northumberland, East Riding of Yorkshire, Oxfordshire and Surrey,
each credited to its council. Those layers are not published under the Open
Government Licence; see [Unsurfaced unclassified roads](#unsurfaced-unclassified-roads-ucrs).

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
| `wiltshire-closures` | Wiltshire Council - rights of way closures register (BOATs; collected directly from the council by the collector server) | Published by the council (no licence stated) |
| `ceredigion-closures` | Ceredigion County Council - live road closures (WFS; closures on our byways only, never diversion routes; Wales, so never in Street Manager) | Published by the council (no licence stated; WFS Fees and AccessConstraints: NONE) |
| `order-register` | Published lists of permanent and seasonal byway orders: East Sussex, Surrey, West and North Northamptonshire, Central Bedfordshire, Derbyshire, Lake District NPA (and others as reviewed) | Published by each council; transcribed and reviewed |

The pack's own `attribution` and `sources` name every one of these, and each
council's row in its `authorities` block lists which sources cover it.

How they are read: an honest User-Agent naming this repository, robots.txt
obeyed (RFC 9309), a gap between requests, read-only calls only, redirects
followed only where the same rules allow, and nothing behind a bot challenge
(`tools/polite_http.py`). By the owner's policy of 7 October 2026, a council
that refuses GitHub's shared runners (Dorset, Powys) is read from one fixed,
honestly named collector server instead, with the same client and rules
(`HOME-COLLECTOR.md`), and so is Wiltshire's closures register, which
answers that server and has not been tried from the runners. The coverage
table credits each "collected directly from the council" with the date.
That is not a disguise or a rotation of addresses: a council that refuses
the server too is not asked from anywhere else, and a real block is never
worked around (no proxies, no other addresses, no borrowed User-Agent, no
headless browser). Every council read is a GET but one: Wiltshire's
register is a search form, read by submitting its own search, read only,
as a person pressing Search does. The owner approved that on 7 October
2026, and `tools/polite_http.py` allows a POST to that one form and no
other (`FORM_POSTS`). Its voluntary closures are requests, not orders, and
are held for review rather than drawn as closures. West Berkshire's closures
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

By the owner's decision of 7 October 2026, a few council order documents
that robots.txt alone disallows are read anyway: Cambridgeshire's and
Hertfordshire's byway order PDFs, Powys's `/media/` order documents and
Derbyshire's path closure register (`tools/robots_override.json`). Only
those paths are affected. They are read only by the order register's check,
at most once a week, with the same honest User-Agent and pacing, and every
read is logged in `tro/register/override-reads.json`. The order pack's
coverage table says "robots.txt overridden by owner decision" against
anything published from them. Powys's documents are read by the collector
server, which keeps only each one's digest, not the PDF. A 403 or a bot
challenge is never got past. Documents behind those are saved by hand, or sent by the council, into `manual/<CODE>/` (see
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
Dorset's register refuses GitHub's runners and is read by the collector
server (`HOME-COLLECTOR.md`).

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
refuses GitHub's runners and is read by the collector server
(HOME-COLLECTOR.md). Kent's, Dartmoor's and Exmoor's own layers carry
licences that restrict their use and are not read.

## Unsurfaced unclassified roads (UCRs)

Many green lanes are not byways at all: they are ordinary public roads -
unclassified county roads the council maintains and lists in its List of
Streets (Highways Act 1980 s36(6)) - that were never given a hard surface. The
Natural Environment and Rural Communities Act 2006 s67(2)(b) kept the motor
vehicle rights over ways in that list. They are on no definitive map, so
rowmaps has none of them. On 8 October 2026 the owner decided that every green
lane is to be shown, so `council-ways.yml` also reads the councils that publish
their unsurfaced roads, every day, into `council-ucrs/<CODE>.json`
(`tools/council_ucrs.py`; its own status in `council-ucrs/status.json`).

Coverage in England and Wales. "Published" is what the build makes of the
read of 8 October 2026 against rowmaps' definitive maps on the runner; each
"not drawn" figure counts sections that run ALONG a definitive-map way (the
NERC test below), with the roads that went wholly in brackets:

| Authority | Source (the council's own layer) | Read since | Read (8 Oct 2026) | Published roads | Not drawn: along a footpath, bridleway or restricted byway (NERC) | Not drawn: along a BOAT, left to it | Licence |
|---|---|---|---|---|---|---|---|
| Devon | Devon County Council: `Environment_Intranet/Public_Access_Intranet/MapServer/5`, "PROW CAT 12" - unsurfaced unclassified county roads, maintenance category 12 | 8 Oct 2026 | 1,143 sections, 960 routes | 952 (583 km) | 17 sections (8) | 0 | not stated - see below |
| North Yorkshire | North Yorkshire Council: `highways/Highways_Network/FeatureServer/3` "U_Roads" (through the ArcGIS utility proxy its public rights of way map uses), HIERARCHY 6; a road is its pre-2023 district and U number | 8 Oct 2026 | 1,005 sections, 685 roads | 633 (669 km) | 63 (40) | 30 (12) | not stated - see below |
| Norfolk | Norfolk County Council: `layers_ext/crm/MapServer/16`, "Norfolk County Council Maintained Unsurfaced Roads" | 8 Oct 2026 | 711 sections, 586 roads | 545 (480 km) | 43 (36) | 5 (5) | not stated - see below |
| Lincolnshire | Lincolnshire County Council: `Highways_Assets_Carriageway_2_view/FeatureServer/3`, Road_Class 'Green Lane' | 8 Oct 2026 | 536 sections, 479 roads | 370 (255 km) | 134 (106) | 5 (3) | not stated - see below |
| Northumberland | Northumberland County Council: `adopted_highway_master_view/FeatureServer/4` (its List of Streets), maintenance category '8 - Unsurfaced Roads', all-purpose sections | 8 Oct 2026 | 305 sections, 237 roads | 75 (69 km) | 8 (7) | 213 (155) | not stated - see below |
| East Riding of Yorkshire | East Riding of Yorkshire Council: `LSG_ESU_Dedications/FeatureServer/9` (street gazetteer, last edited Sept 2022), 'GREEN LANE' or '6 Unmetalled', dedicated to all vehicles, council maintained | 8 Oct 2026 | 192 sections, 112 roads | 101 (89 km) | 17 (11) | 0 | not stated - see below |
| Oxfordshire | Oxfordshire County Council: `WMS/Highways_Centreline/MapServer/1` (its List of Streets), STREET_SURF 'Unmetalled' (not 'Mixed') | 8 Oct 2026 | 106 streets | 41 (20 km) | 95 (61: most are restricted byways) | 6 (4) | not stated - see below |
| Surrey | Surrey County Council: `Surrey_Interactive_Map/RoadsTransport_Roads_Publicly_Maintained/MapServer/33`, surface 'UM', road_type 'Unclassified' | 8 Oct 2026 | 167 sections, 92 roads | 41 (16 km) | 11 (9) | 101 (42) | not stated - see below |
| Worcestershire | not read: its 'Keep Safe Only' maintenance tier (193 sections) is not tied to unsurfaced roads by any council publication found (tools/council_ucrs.py says which were read) | - | - | - | - | - | - |
| Suffolk, Herefordshire | not read: their terms forbid copying | - | - | - | - | - | - |
| every other authority | not yet read; each council found is one entry in `UCR_LAYERS` | - | - | - | - | - | - |

Where a council's roads are not read, the app says so: an absence of UCRs on
the map is never an absence on the ground.

**None of these layers states a licence. Each is published on the owner's
decision of 8 October 2026** that it is public highway information - a
council's highway records must be open to public inspection - credited to the
council by name (Devon County Council, North Yorkshire Council, Norfolk County
Council, Lincolnshire County Council, Northumberland County Council, East
Riding of Yorkshire Council, Oxfordshire County Council, Surrey County
Council) on every road, pack and container that carries it, and each will be
taken down if its council objects. Every road says, in its `attribution`,
"Source: <the council's layer>, read from the council." followed by that
decision (`council_ucrs.owner_decision_note`). The decision named
Worcestershire too; it is held out on the evidence above, not the licence.

**THE NERC TEST.** The Natural Environment and Rural Communities Act 2006 s67
took the motor vehicle rights off every way recorded on the definitive map as
a footpath, bridleway or restricted byway; s67(2)(b) saves a List of Streets
road only where it was NOT on the definitive map. Lincolnshire says so itself:
"Legislation has extinguished the rights of motorists if these are also shown
as a: public footpath, public bridleway, restricted byway". So the build
(`build_packages.ucr_lanes`) tests every council's roads section by section
against rowmaps' footpaths, bridleways and restricted byways of every
authority (read from the cache though none is carried). A section is not
drawn when 75% of it runs ALONG them (`along_share`): sampled every 5 m, a
point counts where a path segment is beside it (square across from it, not
beyond the segment's end), within 20 m and within 30 degrees of the road's
own bearing there. A path that crosses a road, or ends where it begins, never
takes it: before the bearing test the second review found 59 short sections
dropped, 12 of them only crossed (Stokenham 315 went whole); after it, 32,
none crossed and two (15 m and 49 m) met at an angle. The rest of the road is
drawn. The same test, section by section, leaves a section running along a
published BOAT to the byway: it is the same way recorded twice and the
definitive map wins. Sections partly along a path (30-75% of 100 m or more)
are kept. Every section not drawn, and every partial, is in the build log
and `dist/ucr-report.json` (with per-council counts) for the owner to look
at. A council without all three of its own footpaths, bridleways and
restricted byways in the build cannot be tested and its roads are not
published at all, never drawn unchecked.

Each route is one lane of class `ucr`, keyed by what is unique in the
council's records: a parish and number in Devon; a pre-2023 district and U
number in North Yorkshire, whose districts each numbered their own (U1057 is
a road in Richmondshire and another in Selby, 55 km apart); the number alone
where it is county-wide (Surrey's "D262", Oxfordshire's USRN). Sections of
one reference more than 1 km apart are separate lanes. `legal_tier` and
`access_evidence` are `highway_record` (the council's highway record, a
different kind of evidence from a definitive-map BOAT). The id is that
reference and nothing else (`DN-UCR-abbotsham-301`,
`NY-UCR-richmondshire-u1057`, `SU-UCR-d262`) - never read order, nor an
optional field such as a village - so a rider's star survives the council
re-drawing a section or filling a field in, and a new road elsewhere never
moves it. Where one reference is several lanes, the longest keeps the plain
id and each other takes six hex naming where it lies. It is named from the
council's road name and reference ("Rocky Lane (Abbotsham UCR 301)"); a
placeholder ("Unknown", "Track"), a bare number or a reference in the name
field is no name, and the lane is then "Unsurfaced unclassified road (UCR)
Abbotsham 301". A name in capitals is put in title case keeping initialisms
(RSPB), Roman numerals (Henry VIII), hyphen and apostrophe parts (O'Neills,
D'Arcy) and Mc names (McDonald). It is open to motorbikes and 4x4s unless an
order says otherwise. The closures pipeline matches orders to UCRs as it does
to byways, and every matcher (council sources, the order register, Street
Manager, the MoD ranges) says "Road closed" where all it matched is roads.
UCRs travel in their own table and tile layer so that app builds before 119,
which would draw them as lanes you may not ride, never see them:
docs/WAYS-SCHEMA.md, "Unsurfaced unclassified roads". The catalogue's
attribution names every council whose roads are in the build.

READ FROM GITHUB'S RUNNERS. Every layer above is read by `council-ways.yml`
through `tools/polite_http.py`, at least 4.5 s apart per host. The runners
already read Devon's and Oxfordshire's map servers and Northumberland's
ArcGIS Online host (`services2.arcgis.com`) for byways; Lincolnshire's and
East Riding's are ArcGIS Online too, and North Yorkshire's is Esri's own
proxy (`utility.arcgis.com`). Norfolk's map server answered an ordinary
address without a challenge (its www site refuses data centres; this host is
untried from the runners). Surrey's map server sets an Imperva cookie: if it
refuses the runners, the collector server (HOME-COLLECTOR.md) is the honest
route, and nothing is worked round.

## Local rules

`local-rules/rules.json` (format in `tools/local_rules.py`) holds what a
national park, a council or a scheme asks of riders in a particular place - a
code of conduct, a voluntary restraint, a seasonal policy, an order to cite -
each with the official page it came from and the day it was checked. Every
area container carries the rules that could apply to its lanes (meta
`local_rules`), and the app shows the right one on a lane's sheet. Seeded on
8 October 2026 with four rules read politely from Devon County Council's and
the Lake District National Park Authority's own pages, and the same day six
more from Lincolnshire County Council (its list-of-streets presumption and the
Sewstern Lane and The Drift order), the Yorkshire Dales National Park
Authority (its green lane code and its word on unsurfaced roads, inside the
park's boundary from Natural England's layer), National Trails (Natural
England and Natural Resources Wales: the roads meeting The Ridgeway) and
Surrey County Council (driving on its byways). Every quote was checked against
the page as read. Only public bodies' pages; nothing is taken from a site
`tools/polite_http.py` blocks, or from any user group.

## Licence

Data: Open Government Licence v3.0 — attribution required, no share-alike -
except the unsurfaced unclassified roads above, which are credited to the
council that publishes them under the terms stated there. The manifest says
the same: its `licence` is "OGL-3.0" only while no council's roads are in the
build, and otherwise names what each part is published under, and its
`attribution` labels the rights-of-way credit and each council's
(`build_packages.dataset_licence`, `dataset_attribution`).
The attribution string above travels on the collection *and* on every single
feature, because losing it silently would put users in breach while everything
still appeared to work.

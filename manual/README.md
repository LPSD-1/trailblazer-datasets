# The inbox: council documents saved by hand or sent by a council

Some council documents cannot be read by the pipeline. Some pages are
behind a bot challenge (Isle of Wight, West Berkshire and others). Some
councils' sites do not respond to requests from GitHub's servers (Norfolk,
Powys, Wiltshire). The pipeline never tries to get past those blocks.
Instead, save the documents here yourself, or commit a council's reply to
an Environmental Information Regulations (EIR) or Freedom of Information
(FOI) request.

## Steps

1. Find the authority's two-letter code in `manual/authorities.json` (some
   are listed below).
2. Save the file in `manual/<CODE>/`, for example `manual/WB/`. First
   remove the requester's name, address and email from it.
3. Add an entry for the file to `manual/<CODE>/manifest.json` (see
   "manifest.json" below). A map layer always needs one.
4. Open a pull request to `main`. Its checks review what you added. The
   checks do not look for personal data in the files: step 2 is still
   yours to do.
5. Once it is merged, the pipeline reads the file. A map layer is read on
   the next council orders run, within six hours. A document is flagged
   for review at the next order register check. Any problem with the file
   is listed as a warning on every council orders run until it is fixed.

## Where each file goes

Put each file in `manual/<CODE>/`. `CODE` is the authority's two-letter code
in `authorities.json`, for example:

| Code | Authority | Code | Authority |
|---|---|---|---|
| IW | Isle of Wight | WB | West Berkshire |
| CB | Cambridgeshire | HD | Hertfordshire |
| PW | Powys | DY | Derbyshire |
| DU | Durham | NK | Norfolk |
| WT | Wiltshire | | |

## What to drop

- **Before you commit a council's reply, remove the requester's name,
  address and email from it.** This repository is public. The same goes
  for any other person's name, address or email in any file.
- **A page or a PDF listing or making byway orders** (`.pdf`, `.html`,
  `.htm`, `.txt`, `.csv`). In a browser, use "Save page as" for a web page,
  or download the PDF. Put the date in the file name, like
  `prow-restrictions-2026-10-08.html`.
- **What happens to a document.** The next order register check
  (`order-register.yml`, quarterly or on request) flags it for review. It
  flags the file again whenever you replace it with a different version.
  Nothing from it is published until you have transcribed its orders into
  `tro/register/orders.json`, with `status: "approved"` and
  `source_url: "manual/<CODE>/<file>"`. Then run
  `python tools/order_register.py accept manual/<CODE>/<file>`.
- **A map layer a council sent**, as one of the files in the table below.
  It needs a `layer` mapping in `manifest.json` (below).
- **What happens to a layer.** The next council orders run
  (`council-orders.yml`, every six hours) matches it to the byways. It is
  then published like the council's own live layer, credited to the
  council.

| The council sent | Read? | Notes |
|---|---|---|
| GeoJSON (`.geojson`, or a `.json` FeatureCollection) | yes | British National Grid or longitude and latitude |
| A shapefile in a `.zip` (`.shp`, `.shx`, `.dbf`, `.prj`) | yes | the `.prj` must name British National Grid or WGS 84 |
| A GeoPackage (`.gpkg`, or one in a `.zip`) | yes | SRS 27700 or 4326, one feature table |
| KML (`.kml`) or KMZ (`.kmz`) | yes | longitude and latitude by definition |
| A loose `.shp`, `.dbf` or `.prj` | no | zip the parts of the shapefile together |
| A spreadsheet (`.xlsx`, `.xls`, `.ods`) | no | convert a layer to GeoJSON; save a list of orders as PDF or CSV |
| A `.csv` of coordinates | no | convert it to GeoJSON; a `.csv` is read only as a document |

A layer holds lines: the byways the closures are on. Points or areas are
refused, not matched. Every point must lie inside England and Wales, all
in one projection. A layer in Web Mercator (EPSG:3857), or with latitude
before longitude, is refused, not read as nothing.

A GeoJSON file may state the coordinate system (crs) it uses. If it does,
that must be EPSG:27700, EPSG:4326 or CRS84, and it must agree with the
coordinates. Where the file's crs is wrong, state `"crs": "bng"` or
`"crs": "wgs84"` for it in `manifest.json`.

Two layers in one folder must not share a name apart from the extension,
such as `closures.zip` and `closures.kml`. If they do, neither is read
until one is renamed. A layer the pipeline cannot read keeps whatever it
last published.

## manifest.json (optional for documents, needed for a layer)

```json
{
 "council": "West Berkshire Council",
 "automated": "off",
 "files": {
  "prow-restrictions-2026-10-08.html": {"how": "hand",
   "url": "https://www.westberks.gov.uk/prowrestrictions"},
  "closures.geojson": {"how": "eir", "date": "2026-09-30",
   "title": "Rights of way closures",
   "layer": {"id": "$REF", "ref": "$REF", "where": "$PATH",
             "title": "$WHAT", "start": "$FROM", "end": "$TO",
             "vehicles": "all_users", "form": "temporary"}}
 }
}
```

- `council`: the council's name as it is credited. Without it, the
  authority's name in `authorities.json` is used.
- `title`: what the file is called in the credit, for example "Rights of
  way closures". Without it, the file name is used.
- `how`: how the file came. Upper or lower case, and spaces around it,
  make no difference. Any other value is a problem, and the file is not
  read until it is fixed.

  | `how` | Use it for | The coverage table says |
  |---|---|---|
  | `hand` (the default) or `browser` | a page you saved in a browser | saved by hand |
  | `eir` | a council's reply to an EIR request | supplied by the council |
  | `foi` | a council's reply to an FOI request | supplied by the council |
  | `council` | any other file the council sent | supplied by the council |

- `date`: the date the file came, written `YYYY-MM-DD` (for example
  `2026-09-30`). Without a `date`, the date in the file name is used. A
  `date` written any other way is a problem.
- `layer`: which of the layer's own fields hold each part of an order.
  A value starting with `$` names a field in the council's file:
  `"ref": "$REF"` takes each feature's `REF` field as the order's
  reference. Any other value is used as it is, for every feature:
  `"form": "temporary"` makes every order in the layer temporary. Fields
  the mapping does not name are never carried.
- `vehicles` (in `layer`) is one of `all_users`, `all_vehicles`,
  `motor_vehicles` or `motor_vehicles_except_motorcycles`. It can also be
  plain words ("no motor vehicles except motorcycles") for the pipeline to
  read.
- `form` (in `layer`) is `permanent`, `temporary`, `seasonal` or
  `experimental`. A seasonal layer also needs
  `"season": {"from": "10-01", "to": "04-30"}`.
- `crs` (a GeoJSON layer only, and only when needed): the coordinate
  system (crs), `bng` for British National Grid or `wgs84` for longitude
  and latitude. It overrules the file's own crs. Leave it out, and the
  coordinate system is worked out from the coordinates.
- `automated`: while this folder holds a file, the pipeline fetches nothing
  for that authority: no register pages, closure layers, byway layers or
  application registers. Set `"on"` to keep the automated sources as well.

Run `python tools/manual_inbox.py` to see what the pipeline makes of this
folder. Problems are listed on every council orders run, within six hours
of the commit, as warnings in its log and in its "a source could not be
read" issue. They are also listed in the order register's review issue. A
problem is, for example, a file with no date, a layer with no mapping, or
an unknown `how`. A `manifest.json` that is not valid JSON is a problem
too, and then no file in the folder is read.

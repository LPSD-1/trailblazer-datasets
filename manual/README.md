# The inbox: council documents saved by hand or sent by a council

Some council documents cannot be read by the pipeline: pages behind a bot
challenge (Isle of Wight, West Berkshire and others) and pages that refuse
GitHub's servers (Norfolk, Powys, Wiltshire). The pipeline never tries to
get past those blocks. Save the documents here yourself, or commit a
council's reply to an Environmental Information Regulations (EIR) or
Freedom of Information (FOI) request.

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

- **A page or a PDF listing or making byway orders** (`.pdf`, `.html`,
  `.htm`, `.txt`, `.csv`). In a browser, use "Save page as" for a web page,
  or download the PDF. Put the date in the file name, like
  `prow-restrictions-2026-10-08.html`. The next order register check
  (`order-register.yml`, quarterly or on request) flags it for review. That
  check also flags the file again whenever you replace it with a different
  version. Nothing from it is published until you have transcribed its orders
  into `tro/register/orders.json` with `status: "approved"` and
  `source_url: "manual/<CODE>/<file>"`, then run
  `python tools/order_register.py accept manual/<CODE>/<file>`.
- **A map layer a council sent**, as one of the files in the table below.
  It needs a `layer` mapping in `manifest.json` (below). The next council
  orders run (`council-orders.yml`, every six hours) matches it to the
  byways and publishes it like the council's own live layer, credited to
  the council.

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
refused, not matched. Every point must lie inside England and Wales, in
one projection. A layer in Web Mercator (EPSG:3857), or with latitude
before longitude, is refused, not read as nothing. A GeoJSON's own `crs`
must be EPSG:27700, EPSG:4326 or CRS84 and agree with its coordinates.
Where the file's `crs` is wrong, state `"crs": "bng"` or `"crs": "wgs84"`
for it in `manifest.json`. A layer the pipeline cannot read keeps whatever
it last published.

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
- `layer`: `"$NAME"` takes that property from each feature; any other value
  is used as it is. `vehicles` is one of `all_users`, `all_vehicles`,
  `motor_vehicles`, `motor_vehicles_except_motorcycles`, or plain words
  ("no motor vehicles except motorcycles") for the pipeline to read. `form`
  is `permanent`, `temporary`, `seasonal` or `experimental`. A seasonal layer
  also needs `"season": {"from": "10-01", "to": "04-30"}`. Fields the mapping
  does not name are never carried. Remove names, addresses and e-mail
  addresses from anything before you commit it.
- `crs` (a GeoJSON layer only, and only when needed): `bng` for British
  National Grid or `wgs84` for longitude and latitude. It overrules the
  file's own `crs`. Leave it out and the projection is read from the
  coordinates.
- `automated`: while this folder holds a file, the pipeline fetches nothing
  for that authority: no register pages, closure layers, byway layers or
  application registers. Set `"on"` to keep the automated sources as well.

Run `python tools/manual_inbox.py` to see what the pipeline makes of this
folder. Problems are listed on every council orders run, within six hours
of the commit, as warnings in its log and in its "a source could not be
read" issue, and in the order register's review issue. A problem is, for
example, a file with no date, a layer with no mapping, a `manifest.json`
that is not valid JSON (then no file in the folder is read), or an unknown
`how`.

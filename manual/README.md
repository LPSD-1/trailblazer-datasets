# The inbox: council documents saved by hand or sent by a council

Some council documents cannot be read by the pipeline: pages behind a bot
challenge (Isle of Wight, West Berkshire and others) and pages that refuse
GitHub's servers (Norfolk, Powys, Wiltshire). The pipeline never tries to
get past those blocks. Save the documents here yourself, or commit a
council's reply to an Environmental Information Regulations (EIR) request.

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
- **A map layer a council sent** (`.geojson`, or a `.json` FeatureCollection,
  in British National Grid or in longitude and latitude). It needs a `layer`
  mapping in `manifest.json` (below). The next council orders run
  (`council-orders.yml`, every six hours) matches it to the byways and
  publishes it like the council's own live layer, credited to the council.

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

- `how`: `hand` for a page you saved in a browser (the default), or `eir`
  for a file the council sent. The coverage table in the order pack says
  "saved by hand" or "supplied by the council", with the `date`. Without a
  `date`, the date in the file name is used.
- `layer`: `"$NAME"` takes that property from each feature; any other value
  is used as it is. `vehicles` is one of `all_users`, `all_vehicles`,
  `motor_vehicles`, `motor_vehicles_except_motorcycles`, or plain words
  ("no motor vehicles except motorcycles") for the pipeline to read. `form`
  is `permanent`, `temporary`, `seasonal` or `experimental`. A seasonal layer
  also needs `"season": {"from": "10-01", "to": "04-30"}`. Fields the mapping
  does not name are never carried. Remove names, addresses and e-mail
  addresses from anything before you commit it.
- `automated`: while this folder holds a file, the pipeline fetches nothing
  for that authority: no register pages, closure layers, byway layers or
  application registers. Set `"on"` to keep the automated sources as well.

Run `python tools/manual_inbox.py` to see what the pipeline makes of this
folder. Problems, such as a file with no date or a layer with no mapping, are
listed in the order register's review issue.

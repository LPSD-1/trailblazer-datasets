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
"GRMP" (4) │ version (1) │ alg (1) │ nonce (12) │ ciphertext │ MAC (16)
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
python build_packages.py --key <keyfile>
```

Republishing replaces packages in place. The app's local filenames deliberately
omit the build date, so a new build supersedes the old copy on the device
rather than accumulating beside it.

## Licence

Data: Open Government Licence v3.0 — attribution required, no share-alike.
The attribution string above travels on the collection *and* on every single
feature, because losing it silently would put users in breach while everything
still appeared to work.

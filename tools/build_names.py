#!/usr/bin/env python3
"""Turn OS Open Names into a gazetteer a phone can search with no signal.

    python tools/build_names.py --source opname_csv_gb.zip --out dist/names

WHY THIS EXISTS
---------------
Typing a postcode, a street or a village into Trail Blazer needed a signal.
That is the wrong way round for an app whose entire premise is that the rider
is in a valley with no bars: the one moment you most want to say "take me to
Cwmystwyth" is the moment the geocoder cannot be reached.

OS Open Names is Ordnance Survey OpenData under the Open Government Licence
v3.0 - free for commercial use with attribution - and it carries, for Great
Britain:

    1,743,127  unit postcodes        ZE2 9BZ -> a National Grid point
      882,055  named roads           Elmhurst Drive
      101,301  sections of road
       15,155  villages
       12,900  hamlets
        5,158  numbered roads        B9082
       ... plus towns, suburbs, hills, woods, water

WHAT IT DOES NOT CARRY
----------------------
House numbers. There is no free national source for them: OS AddressBase is
commercially licensed and OpenStreetMap's coverage is patchy enough that
shipping it would teach riders the feature is broken. So "8 Elmhurst Drive"
resolves to Elmhurst Drive, and the app says so rather than pretending the 8
meant something. For a rider on a bike that is the right answer anyway.

THE FORMAT, AND WHY IT IS NOT JSON
----------------------------------
Three million records will not be parsed into Dart objects on a phone, and
will not be held in memory either. So the pack is a FIXED-SIZE record table
sorted by folded name, with the names in a blob beside it:

    header      magic, version, counts, the county string table
    records[]   13 bytes each, sorted by folded name
    names       concatenated UTF-8, addressed by offset

Fixed size plus sorted means the app can BINARY SEARCH the file on disk
without reading it in - a prefix lookup is about two dozen seeks. The folding
(lower case, letters and digits only) has to match the app's exactly or the
search finds nothing; `test_build_names.py` pins the pair together.

Coordinates are stored as National Grid eastings/northings divided by ten -
ten-metre precision, which is far finer than any of these features is located
to, and it fits both in three bytes rather than needing a float.
"""
import argparse
import collections
import csv
import io
import os
import re
import struct
import sys
import zipfile

MAGIC = b"TBNM"
VERSION = 1

# 14 bytes: name offset (4), name length (1), kind (1), context (2),
# easting/10 (3), northing/10 (3). Written field by field below rather than
# through a struct format, because the two 3-byte integers have no letter.
#
# The context is TWO bytes, not one. At one byte a county ran out of towns
# after 256 of them - Suffolk hit the cap exactly - and every road past that
# point lost the only thing distinguishing it from the nine hundred other
# Elmhurst Drives. The extra byte across Great Britain costs 2.8 MB and buys
# a rider knowing which town they are being sent to.
RECORD_LEN = 14

# What a rider might plausibly ask for by name, and nothing else.
#
# OS Open Names carries woods, hills, bays and schools too. They are not
# excluded because they are useless - a rider may well say "take me to
# Cannock Chase" - but because each one is a row in a file a rider downloads
# over a phone connection, and the three kinds below are 95% of what anyone
# types into a navigation app. Landmarks are a later, separate decision.
KINDS = {
    "Postcode": 0,
    "Named Road": 1,
    "Numbered Road": 1,
    "Section Of Named Road": 1,
    "Section Of Numbered Road": 1,
    "City": 2,
    "Town": 2,
    "Village": 2,
    "Hamlet": 2,
    "Suburban Area": 2,
    "Other Settlement": 2,
}

KIND_POSTCODE = 0
KIND_ROAD = 1
KIND_SETTLEMENT = 2

# Columns in the OS Open Names CSV, from Doc/OS_Open_Names_Header.csv.
C_NAME1 = 2
C_NAME2 = 4
C_LOCAL_TYPE = 7
C_X = 8
C_Y = 9
C_POPULATED_PLACE = 18
C_DISTRICT_BOROUGH = 21
C_COUNTY = 24
C_REGION = 27
C_COUNTRY = 29

_FOLD = re.compile(r"[^a-z0-9]+")


def fold(text):
    """Lower case, letters and digits only.

    MUST match `foldName` in the app. A gazetteer whose index is folded one way
    and queried another silently finds nothing, which looks exactly like "we
    have no data for your area" - the failure this whole pack exists to end.
    """
    return _FOLD.sub("", text.lower())


# OS Open Names' own REGION, mapped onto the six areas this app browses lanes
# by (the `regions` list in manifest.json). The grouping has to agree with the
# lane packs, or a rider downloading "the Midlands" gets lanes for one shape of
# ground and names for another.
#
# Two of these are judgement calls and should be read as such:
#
#   * "Eastern" is a bigger thing than East Anglia - it reaches down into
#     Bedfordshire and Hertfordshire. Sent there anyway, because the
#     alternative is splitting a region along county lines the source does not
#     draw, and a name in the wrong pack costs a rider a download rather than
#     a wrong answer.
#   * London goes to the South East. It belongs to neither properly, and it is
#     the one part of Britain with no green lanes in it at all.
#
# Scotland has no lane packs, so its names get a pack of their own rather than
# being dropped: a rider touring north still wants to type a village, and the
# catalogue can decide whether to offer it.
REGION_TO_AREA = {
    "South West": "south-west",
    "South East": "south-east",
    "London": "south-east",
    "Eastern": "east-anglia",
    "East of England": "east-anglia",
    "West Midlands": "midlands",
    "East Midlands": "midlands",
    "North West": "north",
    "North East": "north",
    "Yorkshire and the Humber": "north",
    "Wales": "wales",
    "Scotland": "scotland",
}


def area_of(row, split):
    """Which pack a record belongs in.

    By REGION, because that is how the app browses lane packs and a rider
    downloading a place should get the lanes and the names for the same
    ground. County is kept as an option: it produces 143 small packs, which
    suits somebody who only ever rides one of them.
    """
    if split == "county":
        for col in (C_COUNTY, C_REGION, C_COUNTRY):
            value = row[col].strip() if len(row) > col else ""
            if value:
                return value
        return "great-britain"

    region = row[C_REGION].strip() if len(row) > C_REGION else ""
    mapped = REGION_TO_AREA.get(region)
    if mapped:
        return mapped
    # An unrecognised region lands in its own pack rather than being silently
    # binned. A renamed region upstream would otherwise drop a whole county's
    # names, and the only symptom is a rider finding nothing where they live.
    country = row[C_COUNTRY].strip() if len(row) > C_COUNTRY else ""
    return REGION_TO_AREA.get(country) or "unassigned"


def read_records(source, split):
    """Every usable row, as (area, name, kind, context, easting, northing)."""
    with zipfile.ZipFile(source) as z:
        members = [n for n in z.namelist()
                   if n.startswith("Data/") and n.endswith(".csv")]
        if not members:
            sys.exit("no Data/*.csv in %s - is this the OS Open Names zip?"
                     % source)
        for member in members:
            stream = io.TextIOWrapper(z.open(member), encoding="utf-8-sig")
            for row in csv.reader(stream):
                if len(row) <= C_COUNTY:
                    continue
                kind = KINDS.get(row[C_LOCAL_TYPE])
                if kind is None:
                    continue
                try:
                    easting = int(float(row[C_X]))
                    northing = int(float(row[C_Y]))
                except ValueError:
                    continue
                area = area_of(row, split)
                # The place a road is in, so "Elmhurst Drive" can be told from
                # the other nine hundred of them. Falls back to the county.
                # Town, then borough, then county. A road with no populated
                # place against it is common in OS Open Names, and falling
                # straight to the county leaves every road in Suffolk saying
                # "Suffolk", which distinguishes nothing.
                context = (row[C_POPULATED_PLACE].strip()
                           or row[C_DISTRICT_BOROUGH].strip()
                           or row[C_COUNTY].strip() or area)
                for name in (row[C_NAME1], row[C_NAME2]):
                    # NAME2 is the Welsh, Gaelic or Scots form. A rider in
                    # Wales says the Welsh name, and a gazetteer that only
                    # holds the English one cannot hear them.
                    name = name.strip()
                    if name:
                        yield area, name, kind, context, easting, northing


def build_pack(rows):
    """One area's records, as the bytes of a .tbnames pack."""
    # Index 0 is reserved for "we are not saying", so the overflow below
    # always has somewhere safe to land. Adding it lazily meant the collapse
    # could itself allocate index 256 and burst the byte it was collapsing to.
    contexts = [""]
    context_index = {"": 0}

    def context_id(text):
        if text not in context_index:
            if len(contexts) > 0xFFFF:
                return 0
            context_index[text] = len(contexts)
            contexts.append(text)
        return context_index[text]

    prepared = []
    for name, kind, context, easting, northing in rows:
        folded = fold(name)
        if not folded:
            continue
        # Past 65,535 distinct contexts in one county it collapses to 0 rather
        # than truncating the file: a missing line of disambiguation costs a
        # rider a second look, a WRONG one sends them to the wrong town.
        cid = context_id(context)
        prepared.append((folded, name, kind, cid, easting, northing))

    # Sorted by the FOLDED name, because that is what the app binary-searches.
    prepared.sort(key=lambda r: (r[0], r[1]))

    names = bytearray()
    offsets = {}
    records = bytearray()
    for folded, name, kind, cid, easting, northing in prepared:
        encoded = name.encode("utf-8")
        if len(encoded) > 255:
            encoded = encoded[:255]
        key = bytes(encoded)
        if key not in offsets:
            offsets[key] = len(names)
            names.extend(encoded)
        e10 = min(max(easting // 10, 0), 0xFFFFFF)
        n10 = min(max(northing // 10, 0), 0xFFFFFF)
        records.extend(struct.pack(
            "<IBBH", offsets[key], len(encoded), kind, cid))
        records.extend(e10.to_bytes(3, "little"))
        records.extend(n10.to_bytes(3, "little"))

    header = bytearray()
    header.extend(MAGIC)
    header.append(VERSION)
    header.extend(struct.pack("<I", len(prepared)))
    header.extend(struct.pack("<H", len(contexts)))
    for text in contexts:
        encoded = text.encode("utf-8")[:255]
        header.append(len(encoded))
        header.extend(encoded)
    # Where the names blob starts, so the reader can seek straight to a record
    # without walking the context table twice.
    names_at = len(header) + 4 + len(records)
    header.extend(struct.pack("<I", names_at))

    return bytes(header) + bytes(records) + bytes(names)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="opname_csv_gb.zip from osdatahub.os.uk")
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-records", type=int, default=200,
                    help="skip areas with fewer than this")
    ap.add_argument("--split", choices=("region", "county"), default="region",
                    help="region matches how the app browses lane packs")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    by_area = collections.defaultdict(list)
    total = 0
    for area, name, kind, context, e, n in read_records(args.source,
                                                        args.split):
        by_area[area].append((name, kind, context, e, n))
        total += 1
        if total % 500000 == 0:
            print("  read %s..." % f"{total:,}", flush=True)

    print("read %s records across %d areas" % (f"{total:,}", len(by_area)))

    written = 0
    index = []
    for area in sorted(by_area):
        rows = by_area[area]
        if len(rows) < args.min_records:
            continue
        blob = build_pack(rows)
        slug = _FOLD.sub("-", area.lower()).strip("-")
        path = os.path.join(args.out, "%s.tbnames" % slug)
        with open(path, "wb") as fh:
            fh.write(blob)
        index.append((slug, area, len(rows), len(blob)))
        written += 1

    index.sort(key=lambda r: -r[3])
    print("\nwrote %d packs to %s\n" % (written, args.out))
    print("%-34s %10s %12s" % ("area", "records", "bytes"))
    for slug, area, count, size in index[:12]:
        print("%-34s %10s %12s" % (area[:34], f"{count:,}", f"{size:,}"))
    print("...")
    print("%-34s %10s %12s" % (
        "TOTAL", f"{sum(r[2] for r in index):,}",
        f"{sum(r[3] for r in index):,}"))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Count what is actually in the national D-TRO corpus, by kind.

    python tools/dtro_survey.py <dtros_all.csv> [--limit N]

`/dtros/all` returns a signed URL to a CSV whose `Data` column is the whole
D-TRO record as JSON, geometry included. That makes the national corpus a
single download rather than one hydration call per order, and it makes this
question answerable offline: **what is in it, and how much of each?**

The answer decides what can be built. TRO_SPEC.md was written around
restrictions on unsealed roads, but the same feed carries speed limits and
weight restrictions, which apply to every road a rider uses. Whether that is
worth building depends on counts nobody has published — so this counts them.

Prints no personal data. Everything here is published public law.
"""
import collections
import csv
import json
import re
import sys

csv.field_size_limit(2**31 - 1)

# SRID=27700;LINESTRING(x y, x y) / POINT / POLYGON
GEOM = re.compile(r'SRID=(\d+);(\w+)')


ODD = __import__("collections").Counter()


def walk_regulations(record):
    """Yield every regulation object in a record, with its provision.

    Defensive at every level. The corpus carries more than one schema
    version at once (TRO_SPEC.md 1.x) and the shapes differ: `provision`
    and `regulation` are usually lists of objects, and sometimes are not.
    A survey that assumes the common shape crashes on the first oddity and
    reports nothing, so the oddities are counted instead.
    """
    if not isinstance(record, dict):
        ODD["record not an object"] += 1
        return
    source = record.get("source")
    if not isinstance(source, dict):
        ODD["source not an object"] += 1
        return
    provisions = source.get("provision")
    if isinstance(provisions, dict):
        provisions = [provisions]
    if not isinstance(provisions, list):
        ODD["provision not a list"] += 1
        return
    for provision in provisions:
        if not isinstance(provision, dict):
            ODD["provision item not an object"] += 1
            continue
        regulations = provision.get("regulation")
        if isinstance(regulations, dict):
            regulations = [regulations]
        if not isinstance(regulations, list):
            if regulations is not None:
                ODD["regulation not a list"] += 1
            continue
        for regulation in regulations:
            if not isinstance(regulation, dict):
                ODD["regulation item not an object"] += 1
                continue
            yield provision, regulation


def kind_of(regulation):
    """The regulation's discriminator.

    The schema is a union: a regulation carries exactly one of a set of
    typed objects (`speedLimitValueBased`, `generalRegulation`, ...). The
    key that is present IS the kind, so read the key rather than guessing
    at a type field that only some of them have.
    """
    ignore = {"timeZone", "condition", "isDynamic"}
    keys = [k for k in regulation if k not in ignore]
    return keys[0] if len(keys) == 1 else "+".join(sorted(keys)) or "(none)"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    path = args[0]
    limit = None
    for a in sys.argv[1:]:
        if a.startswith("--limit"):
            limit = int(a.split("=")[1]) if "=" in a else None

    records = 0
    unreadable = 0
    schema = collections.Counter()
    kinds = collections.Counter()
    general = collections.Counter()
    speed_fields = collections.Counter()
    speed_values = collections.Counter()
    geometry = collections.Counter()
    srids = collections.Counter()
    tras = collections.Counter()
    place_types = collections.Counter()
    no_geometry = 0

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if limit and records >= limit:
                break
            records += 1
            schema[row.get("SchemaVersion") or "?"] += 1
            try:
                record = json.loads(row["Data"])
            except (ValueError, KeyError):
                unreadable += 1
                continue

            source = record.get("source") or {}
            tra = source.get("currentTraOwner") or source.get("traCreator")
            if tra is not None:
                tras[tra] += 1

            had_geometry = False
            for provision, regulation in walk_regulations(record):
                kind = kind_of(regulation)
                kinds[kind] += 1

                gr = regulation.get("generalRegulation")
                if isinstance(gr, dict):
                    general[gr.get("regulationType") or "(unset)"] += 1

                # Speed limits are the reason this survey exists, so record
                # their shape, not just their count.
                for key, value in regulation.items():
                    if "speed" in key.lower() and isinstance(value, dict):
                        for field, v in value.items():
                            speed_fields["%s.%s" % (key, field)] += 1
                            if isinstance(v, (int, float, str)) \
                                    and "value" in field.lower():
                                speed_values["%s=%s" % (field, v)] += 1

                places = provision.get("regulatedPlace")
                if isinstance(places, dict):
                    places = [places]
                for place in places or []:
                    if not isinstance(place, dict):
                        ODD["regulatedPlace item not an object"] += 1
                        continue
                    place_types[place.get("type") or "(unset)"] += 1
                    for gkey in ("linearGeometry", "pointGeometry",
                                 "polygonGeometry"):
                        blob = place.get(gkey)
                        if not isinstance(blob, dict):
                            if blob:
                                ODD["%s not an object" % gkey] += 1
                            continue
                        had_geometry = True
                        text = blob.get("linestring") or blob.get("point") \
                            or blob.get("polygon") or ""
                        m = GEOM.match(text or "")
                        if m:
                            srids[m.group(1)] += 1
                            geometry[m.group(2)] += 1
                        else:
                            geometry["(unparsed %s)" % gkey] += 1
            if not had_geometry:
                no_geometry += 1

    def table(title, counter, top=None, total=None):
        print("\n%s" % title)
        print("-" * len(title))
        items = counter.most_common(top)
        if not items:
            print("  (none)")
            return
        width = max(len(str(k)) for k, _ in items)
        for k, n in items:
            share = ""
            if total:
                share = "  %5.1f%%" % (100.0 * n / total)
            print("  %-*s  %8d%s" % (width, k, n, share))
        if top and len(counter) > top:
            print("  ... and %d more" % (len(counter) - top))

    print("D-TRO national corpus")
    print("  records            %d" % records)
    print("  unreadable Data    %d" % unreadable)
    print("  records w/o geometry %d" % no_geometry)
    table("Schema versions", schema, total=records)
    table("Regulation kinds", kinds, total=sum(kinds.values()))
    table("generalRegulation.regulationType", general, top=40,
          total=sum(general.values()))
    table("Speed-limit fields seen", speed_fields, top=30)
    table("Speed-limit values", speed_values, top=30)
    table("Odd shapes met (and skipped)", ODD)
    table("Geometry types", geometry, total=sum(geometry.values()))
    table("SRIDs", srids)
    table("regulatedPlace.type", place_types, top=15)
    print("\nDistinct TRAs publishing: %d" % len(tras))
    return 0


if __name__ == "__main__":
    sys.exit(main())

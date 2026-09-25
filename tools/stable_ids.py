#!/usr/bin/env python3
"""Row numbers that stay put from one build to the next.

THE COST THIS EXISTS FOR. `pois`, `fords`, `ford_gauges` and `wet_gauges` are
keyed on a number local to the container - the r-tree id, the gauge a wetness
row points at - and a changeset (build_changeset.py) matches rows ON THAT
NUMBER. They used to be numbered 1..N in uid (or first-seen) order on every
build, so ONE new POI whose uid sorted early moved every later POI up by one.
Measured on the published ways-east-anglia.tbmap: one new POI made a 5.87 MB
changeset against a 9.47 MB container - 62% of the whole file, on the paid
freshness tier, for one fuel station. Every row after it was "changed" because
its number was, while not one of its values had moved.

THE RULE. When the container riders already hold (the previously PUBLISHED
one, `containers/` in the checkout) is available:

    a key it already numbered keeps that number;
    a new key gets the next number above the highest it used, in the order
    the caller gives (uid order for POIs and fords, first-seen for gauges);
    a key that went away leaves its number as a gap - never handed to another
    key in the same build, so a changeset never has to say "row 7 is now a
    different POI".

With no published container - a region's first build, or a local run - it is
exactly the old numbering, so a first build is byte-for-byte what it was.

WHY NOT A HASH OF THE UID. Tried and rejected: build_map_container.stable_id
does that for ways, and on POIs it grew the container by ~40% gzipped - a
63-bit rowid is 8 bytes in every r-tree node and every index entry where a
small integer is one or two, and random ids scatter the b-tree. Numbering
from the published file keeps the ids small and dense and still stable.

A number that stays put is not enough on its own: a date that moves on every
read rewrites the row just the same. keep_dates() is the other half.
"""
import os
import sqlite3


def previous_numbers(path, table, key_column, id_column="rowid"):
    """{key: number} as the published container at `path` numbered them.

    Empty when there is nothing to keep: no path, no file, or a file without
    that table (a container published before the table existed). Empty means
    "number as a first build", which is always safe - it is what every build
    did before this module existed.
    """
    if not path or not os.path.isfile(path):
        return {}
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        names = set(r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        if table not in names:
            return {}
        columns = set(r[1] for r in db.execute(
            'PRAGMA table_info("%s")' % table))
        if key_column not in columns:
            return {}
        return dict(db.execute('SELECT "%s", %s FROM "%s"'
                               % (key_column, id_column, table)))
    finally:
        db.close()


def previous_rows(path, table, key_column):
    """{key: {column: value}} for every row of `table` in the published
    container at `path` - the same "what riders hold" as previous_numbers,
    and empty in the same cases, for the same reason.
    """
    if not path or not os.path.isfile(path):
        return {}
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        names = set(r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        if table not in names:
            return {}
        cursor = db.execute('SELECT * FROM "%s"' % table)
        columns = [d[0] for d in cursor.description]
        if key_column not in columns:
            return {}
        out = {}
        for values in cursor:
            row = dict(zip(columns, values))
            out[row[key_column]] = row
        return out
    finally:
        db.close()


def keep_dates(rows, previous, key_column, date_column="source_date"):
    """`rows` with each date put back to the published one wherever nothing
    else about the row moved.

    THE COST THIS EXISTS FOR, the one stable numbering left behind. A row's
    `source_date` was the day its source was READ, so every refresh re-dated
    every row: `build_pois.py fetch --refresh` against unchanged OpenStreetMap
    rewrote every POI in the region. Stable rowids did not help - a changeset
    matches rows on the rowid and then compares VALUES, and the date is a
    value. Measured over the six regions, 38.0 MB raw / 17.4 MB gzipped of
    changesets against 94 MB of containers, each month, for no change at all.

    So the date says when the row last CHANGED as we saw it: a row whose every
    other column equals the published row under the same key takes the
    published date; a new row, or one whose content moved, keeps the date it
    was read. When the source was last looked at is a fact about the whole
    region, not about each row, and is recorded once by the builder (for POIs,
    `meta.pois_checked`).

    `previous` is previous_rows(). A column the published row lacks counts as
    a change: a row we cannot compare is not one we may call unchanged.
    Returns new dicts; `rows` is left as it was.
    """
    missing = object()
    out = []
    for row in rows:
        row = dict(row)
        was = previous.get(row.get(key_column)) if previous else None
        if was is not None and was.get(date_column) and all(
                was.get(column, missing) == value
                for column, value in row.items() if column != date_column):
            row[date_column] = was[date_column]
        out.append(row)
    return out


def number(keys, previous=None, first=1):
    """{key: number} for `keys`, which are unique and in the caller's order.

    Without `previous` this is `first`, `first`+1, ... in the order given -
    the numbering every builder used before. With it, see the module
    docstring: kept keys keep their number, new keys continue above the
    highest number `previous` used, removed keys are gaps.
    """
    if not previous:
        return dict((key, first + i) for i, key in enumerate(keys))
    out = {}
    taken = set()
    fresh = []
    for key in keys:
        if key in previous:
            out[key] = previous[key]
            taken.add(previous[key])
        else:
            fresh.append(key)
    # Above EVERYTHING the published file used, not just what is kept: a
    # number whose key went away this build is still on every rider's phone
    # under that key, and reusing it here would make one changeset row mean
    # "delete that POI and put a different one where it was".
    nxt = max([first - 1] + list(previous.values())) + 1
    for key in fresh:
        while nxt in taken:
            nxt += 1
        out[key] = nxt
        taken.add(nxt)
        nxt += 1
    return out

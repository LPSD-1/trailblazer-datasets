#!/usr/bin/env python3
"""A small container in the LIVE shape, a next build of it, and the changeset.

    python tools/make_changeset_fixture.py \
        --source containers/ways-east-anglia.tbmap \
        --out ../greenroadmap-app/test/fixtures/changesets_live_shape

WHY THIS EXISTS. The app's changeset tests were built on fixtures that called
themselves "the live ways shape" and carried `ways`, `ways_bbox`, `tiles` and
`meta` - while every published region container also carries `pois`,
`pois_bbox`, `fords`, `fords_bbox`, `ford_gauges`, `way_wetness`,
`wet_gauges` and the meta keys `evidence_age`, `context_note`,
`context_scope` and `schema_version`. The applier refuses a changeset that
does not carry what the held container has, so against the real set every
region update was a whole download, and nothing in either repository's tests
could see it: both sides agreed on a shape nobody publishes.

So the fixture is CUT FROM A PUBLISHED CONTAINER rather than written to a
schema somebody remembered:

  before  a copy of --source with every row outside --box deleted: the ways
          whose r-tree box meets it, their wetness, their fords, the gauges
          those reference, the POIs inside it, and the tiles over the kept
          ways at every zoom. Same file, same sqlite_master, VACUUMed small.
          Every row keeps the rowid it was published under. One thing is
          rewritten: the meta counts, bounds and evidence_age, recomputed for
          the rows that are left with the formulas the builders use, so the
          container describes itself and not East Anglia.
  after   `before` as the next build would write it, with realistic edits:
            * one way CLOSED by an order (motorbike_ok = fourxfour_ok = 0,
              access_evidence 'order') - its tiles re-cut;
            * one way REMOVED, and its box, wetness and ford with it - its
              tiles re-cut, and dropped where nothing is left in them;
            * one way re-surveyed as mud, so its way_wetness row changes;
            * one POI added and one removed, and the rest renumbered as
              build_pois numbers a region (1..N in uid order) - so most of
              them move rowid without a value changing;
            * the remaining ford re-tagged;
            * evidence_age advanced a month, and built_at six hours.
          Re-cut tiles come from build_map_container.build_tiles over the
          kept ways, so a re-cut tile draws the fixture's ways only.
  the .tbchange between them, built by tools/build_changeset.py - the real
          tool, not a copy of it - checked by validate_changeset.py, and
          applied back onto `before` by build_changeset.apply_changeset to
          prove it lands on `after` in every table, row for row.

A README.md is written beside them saying all of this with the numbers.
tools/test_make_changeset_fixture.py checks the fixture's schema is the
published container's, table for table and meta key for meta key.
"""
import argparse
import collections
import datetime
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_changeset as C       # noqa: E402
import build_fords as F           # noqa: E402
import build_map_container as B   # noqa: E402
import evidence_age as EA         # noqa: E402
import validate_changeset as V    # noqa: E402

#: Two fords, 26 ways and ~50 POIs around Haverhill, in the smallest region.
#:
#: CHOSEN FOR ITS TILES, not only its rows. A way straddling two regions is
#: drawn by ONE of them (build_containers' `claimed`), so a box can hold
#: plenty of East Anglia's ways and hardly any of its tiles - the first box
#: tried, east of Stevenage, had 34 ways and 6 of the 56 tiles they would
#: need, because the South East draws them. Every tile this box's ways need
#: is in the published container.
DEFAULT_BOX = (0.298, 52.096, 0.458, 52.196)
DEFAULT_SOURCE = "containers/ways-east-anglia.tbmap"
CHANGESET_NAME = "before_to_after.tbchange"
MAX_BYTES = 1024 * 1024

NEW_POI = {"poi_uid": "osm:n99999999999", "category": "fuel",
           "name": "Fixture Filling Station", "lat": 51.93, "lon": -0.08,
           "opening_hours": "24/7"}
CLOSED_REASON = ("A traffic regulation order closes this byway to motor "
                 "vehicles.")


def _ids(db, sql, args=()):
    return [r[0] for r in db.execute(sql, args)]


def _in(ids):
    return "(%s)" % ",".join(str(int(i)) for i in ids) if ids else "(NULL)"


def features_of(db):
    """The container's ways as the features build_map_container cut them
    from - enough of them for the tile writer to cut them again."""
    out = []
    for row in db.execute(
            "SELECT way_uid, way_class, county, legal_tier, motorbike_ok, "
            "fourxfour_ok, access_evidence, sustained_pct, climb_m, geometry "
            "FROM ways ORDER BY way_uid"):
        (uid, klass, county, tier, moto, fourxfour, evidence, sustained,
         climb, blob) = row
        out.append({
            "type": "Feature",
            "geometry": {"type": "MultiLineString",
                         "coordinates": F.unpack_geometry(blob)},
            "properties": {"lane_uid": uid, "class": klass,
                           "county": county, "legal_tier": tier,
                           "motorbike_ok": moto, "fourxfour_ok": fourxfour,
                           "access_evidence": evidence,
                           "sustained_pct": sustained, "climb_m": climb}})
    return out


def cut(features, zooms):
    """{(z, x, tms_row): bytes}, exactly as write_container stores them."""
    tiles = {}

    def on_tile(z, x, y, blob, _count):
        tiles[(z, x, (1 << z) - 1 - y)] = blob

    ids = B.assign_ids(features, "lane_uid")
    for zoom in range(zooms[0], zooms[1] + 1):
        B.build_tiles(features, zoom, False, on_tile, ids)
    return tiles


def _zooms(db):
    meta = dict(db.execute("SELECT key, value FROM meta"))
    return int(meta["min_zoom"]), int(meta["max_zoom"])


def restate_meta(db, path, as_of, container_name):
    """The counts, bounds and evidence_age for the rows the file now holds.

    The same formulas build_map_container.write_container and evidence_age
    use, so the fixture's meta is what a build of these rows would say.
    """
    klass = collections.Counter(
        r[0] or "unknown" for r in db.execute("SELECT way_class FROM ways"))
    tier = collections.Counter(
        r[0] or "statutory" for r in db.execute("SELECT legal_tier FROM ways"))
    authorities = sorted({r[0] for r in db.execute(
        "SELECT authority FROM ways") if r[0] and r[0] != B.UNKNOWN_AUTHORITY})
    count = db.execute("SELECT COUNT(*) FROM ways").fetchone()[0]
    rows = [("way_count", str(count)), ("lane_count", str(count)),
            ("class_counts", json.dumps(dict(sorted(klass.items())),
                                        sort_keys=True)),
            ("legal_tier_counts", json.dumps(dict(sorted(tier.items())),
                                             sort_keys=True)),
            ("authorities", json.dumps(authorities)),
            ("bounds", B._bounds_of(features_of(db)))]
    db.executemany("UPDATE meta SET value = ? WHERE key = ?",
                   [(v, k) for k, v in rows])
    db.commit()
    summary = EA.read_container(path, as_of)
    # The published container's name, as the pipeline would record it; the
    # fixture's own file name is not something a real build ever writes.
    summary["container"] = container_name
    EA.write_meta(path, summary)


def _vacuum(path):
    db = sqlite3.connect(path)
    try:
        db.execute("VACUUM")
    finally:
        db.close()


def make_before(source, path, box):
    shutil.copyfile(source, path)
    db = sqlite3.connect(path)
    try:
        meta = dict(db.execute("SELECT key, value FROM meta"))
        w, s, e, n = box
        keep = _ids(db, "SELECT id FROM ways_bbox WHERE max_lon >= ? AND "
                        "min_lon <= ? AND max_lat >= ? AND min_lat <= ?",
                    (w, e, s, n))
        if not keep:
            raise SystemExit("no ways meet %r" % (box,))
        db.execute("DELETE FROM ways WHERE rowid NOT IN %s" % _in(keep))
        db.execute("DELETE FROM ways_bbox WHERE id NOT IN %s" % _in(keep))
        db.execute("DELETE FROM way_wetness WHERE id NOT IN "
                   "(SELECT rowid FROM ways)")
        db.execute("DELETE FROM wet_gauges WHERE id NOT IN (SELECT gauge "
                   "FROM way_wetness WHERE gauge IS NOT NULL)")
        db.execute("DELETE FROM fords WHERE way_id IS NULL OR way_id NOT IN "
                   "(SELECT rowid FROM ways)")
        db.execute("DELETE FROM fords_bbox WHERE id NOT IN "
                   "(SELECT rowid FROM fords)")
        db.execute("DELETE FROM ford_gauges WHERE id NOT IN (SELECT gauge "
                   "FROM fords WHERE gauge IS NOT NULL)")

        # The POIs inside the box, with the rowids they were published under.
        db.execute("DELETE FROM pois WHERE NOT (lon BETWEEN ? AND ? AND "
                   "lat BETWEEN ? AND ?)", (w, e, s, n))
        db.execute("DELETE FROM pois_bbox WHERE id NOT IN "
                   "(SELECT rowid FROM pois)")
        db.commit()

        # The tiles over the ways that are left, at every zoom it carries.
        wanted = set(cut(features_of(db), _zooms(db)))
        held = [tuple(r) for r in db.execute(
            "SELECT zoom_level, tile_column, tile_row FROM tiles")]
        db.executemany("DELETE FROM tiles WHERE zoom_level = ? AND "
                       "tile_column = ? AND tile_row = ?",
                       [t for t in held if t not in wanted])
        db.commit()
    finally:
        db.close()
    as_of = datetime.date.fromisoformat(
        json.loads(meta["evidence_age"])["as_of"])
    db = sqlite3.connect(path)
    try:
        restate_meta(db, path, as_of, os.path.basename(source))
    finally:
        db.close()
    _vacuum(path)
    return as_of


def _next_month(day):
    return (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)


def make_after(before, path, as_of, source):
    """`before` as the next build writes it. Returns what was edited."""
    source_name = os.path.basename(source)
    shutil.copyfile(before, path)
    db = sqlite3.connect(path)
    edits = {}
    try:
        zooms = _zooms(db)
        was = {f["properties"]["lane_uid"]: f for f in features_of(db)}
        forded = _ids(db, "SELECT DISTINCT way_id FROM fords ORDER BY way_id")
        if len(forded) < 2:
            raise SystemExit("the box needs ways with two fords between them")
        plain = [r[0] for r in db.execute(
            "SELECT way_uid FROM ways WHERE rowid NOT IN %s ORDER BY way_uid"
            % _in(forded))]
        removed_rowid = forded[0]
        removed = db.execute("SELECT way_uid FROM ways WHERE rowid = ?",
                             (removed_rowid,)).fetchone()[0]
        closed, soaked = plain[0], plain[1]
        edits.update(closed_way=closed, removed_way=removed, mud_way=soaked)

        # A way closed by an order.
        db.execute("UPDATE ways SET motorbike_ok = 0, fourxfour_ok = 0, "
                   "access_evidence = 'order', access_reason = ? "
                   "WHERE way_uid = ?", (CLOSED_REASON, closed))
        # A way the definitive map no longer has, and everything that hung
        # off it.
        gone_fords = _ids(db, "SELECT rowid FROM fords WHERE way_id = ?",
                          (removed_rowid,))
        edits["removed_fords"] = [r[0] for r in db.execute(
            "SELECT ford_uid FROM fords WHERE rowid IN %s" % _in(gone_fords))]
        db.execute("DELETE FROM ways WHERE rowid = ?", (removed_rowid,))
        db.execute("DELETE FROM ways_bbox WHERE id = ?", (removed_rowid,))
        db.execute("DELETE FROM way_wetness WHERE id = ?", (removed_rowid,))
        db.execute("DELETE FROM fords WHERE rowid IN %s" % _in(gone_fords))
        db.execute("DELETE FROM fords_bbox WHERE id IN %s" % _in(gone_fords))
        db.execute("DELETE FROM wet_gauges WHERE id NOT IN (SELECT gauge "
                   "FROM way_wetness WHERE gauge IS NOT NULL)")
        db.execute("DELETE FROM ford_gauges WHERE id NOT IN (SELECT gauge "
                   "FROM fords WHERE gauge IS NOT NULL)")
        # A surface survey: mud, so soft - build_wet.susceptibility's answer.
        soaked_rowid = db.execute("SELECT rowid FROM ways WHERE way_uid = ?",
                                  (soaked,)).fetchone()[0]
        db.execute("UPDATE ways SET surface = 'mud' WHERE rowid = ?",
                   (soaked_rowid,))
        db.execute("UPDATE way_wetness SET susceptibility = 'soft', "
                   "basis = 'surface', basis_value = 'mud' WHERE id = ?",
                   (soaked_rowid,))
        # The remaining ford, re-tagged by a mapper.
        ford = db.execute("SELECT rowid, ford_uid FROM fords ORDER BY rowid "
                          "LIMIT 1").fetchone()
        db.execute("UPDATE fords SET ford_tag = 'stepping_stones', "
                   "source_date = ? WHERE rowid = ?",
                   (_next_month(as_of).isoformat(), ford[0]))
        edits["retagged_ford"] = ford[1]

        # A POI gone and a POI new, as a refetch finds them - AND
        # RENUMBERED AS build_pois.write_pois NUMBERS A REGION: 1..N over the
        # region's uids in order. One POI fewer below a uid moves it down one,
        # one more moves it up one, so most of the box's POIs change rowid
        # while not one of their values does. That is the case an applier
        # must get right (delete-then-insert, see build_changeset.py) and the
        # one a uid-matched diff gets wrong.
        gone_uid = db.execute("SELECT MIN(poi_uid) FROM pois").fetchone()[0]
        poi = dict(NEW_POI, source_date=_next_month(as_of).isoformat())
        src = sqlite3.connect("file:%s?mode=ro" % source.replace("\\", "/"),
                              uri=True)
        try:
            below = src.execute("SELECT COUNT(*) FROM pois WHERE poi_uid < ?",
                                (poi["poi_uid"],)).fetchone()[0]
        finally:
            src.close()
        kept = [r for r in db.execute(
            "SELECT rowid, poi_uid, category, name, lat, lon, opening_hours, "
            "source_date FROM pois WHERE poi_uid <> ?", (gone_uid,))]
        rows = [((r[0] - (gone_uid < r[1]) + (poi["poi_uid"] < r[1]),)
                 + tuple(r[1:])) for r in kept]
        renumbered = sum(1 for r, k in zip(rows, kept) if r[0] != k[0])
        rows.append((below + 1 - (gone_uid < poi["poi_uid"]),
                     poi["poi_uid"], poi["category"], poi["name"], poi["lat"],
                     poi["lon"], poi["opening_hours"], poi["source_date"]))
        db.execute("DELETE FROM pois")
        db.execute("DELETE FROM pois_bbox")
        db.executemany("INSERT INTO pois (rowid, poi_uid, category, name, "
                       "lat, lon, opening_hours, source_date) "
                       "VALUES (?,?,?,?,?,?,?,?)", sorted(rows))
        db.executemany("INSERT INTO pois_bbox VALUES (?,?,?,?,?)",
                       [(r[0], r[5], r[5], r[4], r[4]) for r in sorted(rows)])
        edits.update(removed_poi=gone_uid, added_poi=poi["poi_uid"],
                     renumbered_pois=renumbered)

        # The tiles the edited ways are in, re-cut from what is left.
        now = features_of(db)
        touched = set(cut([was[closed], was[removed]], zooms))
        fresh = cut(now, zooms)
        for tile in sorted(touched):
            if tile in fresh:
                db.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)",
                           tile + (fresh[tile],))
            else:
                db.execute("DELETE FROM tiles WHERE zoom_level = ? AND "
                           "tile_column = ? AND tile_row = ?", tile)
        edits["tiles_recut"] = len(touched)

        built = datetime.datetime.strptime(
            dict(db.execute("SELECT key, value FROM meta"))["built_at"],
            "%Y-%m-%dT%H:%M:%SZ") + datetime.timedelta(hours=6)
        db.execute("UPDATE meta SET value = ? WHERE key = 'built_at'",
                   (built.strftime("%Y-%m-%dT%H:%M:%SZ"),))
        db.commit()
        restate_meta(db, path, _next_month(as_of), source_name)
    finally:
        db.close()
    _vacuum(path)
    return edits


def schema_of(path):
    """sqlite_master as build_changeset compares it."""
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        return C._schema(db)
    finally:
        db.close()


def counts_of(path):
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                         uri=True)
    try:
        names = ["tiles"] + sorted(C.carried_tables(db))
        return collections.OrderedDict(
            (n, db.execute('SELECT COUNT(*) FROM "%s"' % n).fetchone()[0])
            for n in names)
    finally:
        db.close()


def make(source, out_dir, box=DEFAULT_BOX):
    os.makedirs(out_dir, exist_ok=True)
    before = os.path.join(out_dir, "before.tbmap")
    after = os.path.join(out_dir, "after.tbmap")
    change = os.path.join(out_dir, CHANGESET_NAME)
    as_of = make_before(source, before, box)
    if schema_of(before) != schema_of(source):
        raise SystemExit("before.tbmap's schema is not the source's")
    edits = make_after(before, after, as_of, source)
    if schema_of(after) != schema_of(source):
        raise SystemExit("after.tbmap's schema is not the source's")
    stats = C.build_changeset(before, after, change)
    problems = V.problems_with(change)
    if problems:
        raise SystemExit("the changeset does not validate: %s" % problems)

    # PROVED, NOT ASSUMED: applied onto `before` it must be `after`.
    scratch = os.path.join(out_dir, ".applied.tbmap")
    shutil.copyfile(before, scratch)
    try:
        C.apply_changeset(scratch, change)
        if C.snapshot(scratch) != C.snapshot(after):
            raise SystemExit("before + changeset is not after")
    finally:
        os.remove(scratch)

    sizes = {name: os.path.getsize(os.path.join(out_dir, name))
             for name in ("before.tbmap", "after.tbmap", CHANGESET_NAME)}
    for name, size in sizes.items():
        if size > MAX_BYTES:
            raise SystemExit("%s is %d bytes; the fixture must stay under %d"
                             % (name, size, MAX_BYTES))
    metas = [C.snapshot(p)["meta"] for p in (before, after)]
    report = {"box": list(box), "source": source, "edits": edits,
              "meta_keys": sorted(metas[1]),
              "as_of": [json.loads(m["evidence_age"])["as_of"]
                        for m in metas],
              "stats": stats, "sizes": sizes,
              "before": counts_of(before), "after": counts_of(after)}
    write_readme(out_dir, report)
    return report


def write_readme(out_dir, report):
    stats = report["stats"]
    lines = [
        "# Changesets in the live shape",
        "",
        "Generated by `tools/make_changeset_fixture.py` in the "
        "trailblazer-datasets repository. Do not edit these files by hand; "
        "regenerate them.",
        "",
        "## Regenerate",
        "",
        "From the trailblazer-datasets checkout, with this repository beside "
        "it:",
        "",
        "```",
        "python tools/make_changeset_fixture.py \\",
        "    --source %s \\" % report["source"].replace("\\", "/"),
        "    --out ../greenroadmap-app/test/fixtures/changesets_live_shape",
        "```",
        "",
        "## What they are",
        "",
        "- `before.tbmap`: a copy of the published `%s` with every row "
        "outside the box %s (west, south, east, north) deleted and the file "
        "VACUUMed. Its sqlite_master is the published container's, table for "
        "table, index for index and view for view, and every row keeps the "
        "rowid it was published under. Only the meta counts, `bounds` and "
        "`evidence_age` are rewritten, recomputed for the rows that are left "
        "with the builders' own formulas."
        % (os.path.basename(report["source"]), report["box"]),
        "- `after.tbmap`: `before` as the next build would write it. One way "
        "closed by an order (`%s`: motorbike_ok and fourxfour_ok 0, "
        "access_evidence 'order'); one way removed (`%s`) with its box, its "
        "wetness row and its ford (%s); one way re-surveyed as mud (`%s`), "
        "so its `way_wetness` row is soft/surface/mud; POI `%s` removed and "
        "POI `%s` added, and the box's POIs renumbered as `build_pois` "
        "numbers a region, 1..N in uid order - %d of them change rowid "
        "without a value changing; ford `%s` re-tagged stepping_stones; "
        "`evidence_age` advanced a month and `built_at` six hours. The %d "
        "tiles the closed and removed ways were in are re-cut by "
        "`build_map_container.build_tiles` from the fixture's ways, and "
        "dropped where none is left."
        % (report["edits"]["closed_way"], report["edits"]["removed_way"],
           ", ".join(report["edits"]["removed_fords"]),
           report["edits"]["mud_way"], report["edits"]["removed_poi"],
           report["edits"]["added_poi"],
           report["edits"]["renumbered_pois"],
           report["edits"]["retagged_ford"],
           report["edits"]["tiles_recut"]),
        "- `%s`: `python tools/build_changeset.py before.tbmap after.tbmap "
        "%s` - the real tool. It passed `validate_changeset.py`, and applied "
        "onto a copy of `before` it gave `after` in every table, row for row, "
        "rowids included; the generator refuses to write the fixture "
        "otherwise." % (CHANGESET_NAME, CHANGESET_NAME),
        "",
        "## Contents",
        "",
        "| table | before | after | written | removed |",
        "|---|---|---|---|---|",
    ]
    for table in report["before"]:
        if table == "tiles":
            written = stats["tiles_changed"] + stats["tiles_added"]
            removed = stats["tiles_removed"]
        else:
            got = stats["tables"][table]
            written, removed = got["changed"] + got["added"], got["removed"]
        lines.append("| %s | %d | %d | %d | %d |"
                     % (table, report["before"][table],
                        report["after"].get(table, 0), written, removed))
    lines += [
        "",
        "Both containers carry the published container's meta keys: %s. "
        "The changeset restates every one of them but `built_at` (which is "
        "its `to_build`) and `kind`. `built_at`: %s -> %s. "
        "`evidence_age.as_of`: %s -> %s."
        % (", ".join("`%s`" % k for k in sorted(report["meta_keys"])),
           stats["from_build"], stats["to_build"], report["as_of"][0],
           report["as_of"][1]),
        "",
        "| file | bytes |",
        "|---|---|",
    ]
    for name, size in sorted(report["sizes"].items()):
        lines.append("| %s | %d |" % (name, size))
    lines += [
        "",
        "## The changeset format",
        "",
        "Documented at the top of `tools/build_changeset.py`. In short: one "
        "table per container table, same name, holding the rows that are new "
        "or differ, keyed as `meta.carries` says (`rowid` or the table's "
        "INTEGER PRIMARY KEY or the r-tree's `id`); `removed_rows(tbl, id)` "
        "for the rows the new build lacks; `tiles`/`removed_tiles` as before; "
        "every meta key of the new build restated, plus `from_build`, "
        "`to_build`, `carries` and `removed_meta`. `bbox_rows` and "
        "`removed_records` mirror the `ways` r-tree rows and removed uids as "
        "format 1 carried them.",
        "",
    ]
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help="a published region container (default %s)"
                         % DEFAULT_SOURCE)
    ap.add_argument("--out", required=True,
                    help="where before.tbmap, after.tbmap, the changeset "
                         "and README.md go")
    ap.add_argument("--box", type=float, nargs=4, default=DEFAULT_BOX,
                    metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    args = ap.parse_args(argv)
    report = make(args.source, args.out, tuple(args.box))
    for name, size in sorted(report["sizes"].items()):
        print("  %-28s %8d bytes" % (name, size))
    for table, count in report["before"].items():
        print("  %-14s %6d -> %6d" % (table, count, report["after"][table]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

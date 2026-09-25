#!/usr/bin/env python3
"""The build stamp follows the CONTENT, all of it.

    python tools/stamp_build.py --published containers \
        --manifest dist/containers/manifest.json

WHAT THIS GUARANTEES. For every container this run built, against the copy
riders hold (the same file name under --published):

    same content      -> the PUBLISHED FILE, byte for byte, and its built_at
    different content -> a built_at the published one does not have

so the app never sees "same build, different bytes", and a changeset - which
the builder, the validator and the app all key on `built_at` - can always be
built between two builds that differ.

WHY `built_at` ALONE DID NOT. `built_at` is the pack's `generated`: it moves
when a council's WAYS change and at no other time, which is right for ways
and wrong for everything else a region container holds. POIs are refreshed
on their own 30-day clock, the fords and the EA gauge network on theirs,
wetness is re-joined to whichever gauges exist - and every one of those
rewrote the container's bytes under the SAME `built_at`. publish_changesets
then saw from == to and skipped it as "unchanged"; build_changeset refuses
two builds with one stamp; and the app, finding the build it holds announced
under a new hash, fetched the whole region. For all six regions that is
~94 MB, on the paid freshness tier, for a fuel station.

THE RULE, in order:

  1. `content_digest` of the new build equals the published one's (every
     table, every row, every meta key but `built_at` and `ways_cut`): the
     published file is copied over the new one. Not merely re-stamped -
     COPIED, so a page layout that differs for no reason cannot reach a rider
     as new bytes.
  2. Otherwise the new build's own `built_at` stands if it sorts after the
     published one: the ways changed, and the pack stamp says when.
  3. Otherwise - the ways did not change, something else did - `built_at`
     becomes this run's time (--now), or one second past the published stamp
     if the clock is not ahead of it. Always ISO-8601 UTC, so it still sorts
     as text (publish_changesets._prune relies on that) and still parses as
     a date.

RULE 3 IS WHY `built_at` IS NOT THE DATE RIDERS SEE. The app printed it as
"cut <date>", so a POI refresh under unchanged ways told every rider their
lanes had been cut that morning - the one thing the refresh workflow says the
date must never do. So before rule 3 overwrites the pack stamp, it is kept as
`ways_cut`; a changeset restates it like every other meta key, and the app
shows `ways_cut`, or `built_at` where there is none - which is exactly where
`built_at` is still the pack stamp (the builder writes it so, and rules 1 and
2 leave it so). Every container's manifest entry carries the same date as
`waysCut`. Written only when the two dates part, so a container that never
meets rule 3 is byte-for-byte what the builder made and golden.py holds.

Each container's manifest `generated` is set to the `built_at` it ends up
with, and the manifest's own `generated` to the newest of them, so the index
and the file never name two different builds.

Run AFTER every step that writes into a container (POIs, wetness, fords,
evidence dates) and BEFORE restamp_containers.py, which hashes and signs the
result, and publish_changesets.py, which diffs it against the published tree.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: The r-tree's own storage. Its rows are the r-tree's rows, already hashed
#: through the virtual table; its node blobs depend on insertion order, and
#: two builds of the same boxes are the same content.
RTREE_SHADOWS = ("_node", "_parent", "_rowid")

#: The date the LANES were cut: the pack stamp, which `built_at` starts as
#: and stops being the moment rule 3 restamps it. See the module note.
WAYS_CUT = "ways_cut"

#: Meta keys that are dates ABOUT the content rather than content. `ways_cut`
#: is here because the ways rows it dates are hashed already, and because a
#: fresh build never carries it: hashed, every region rule 3 had once touched
#: would read as changed on every run after, and be restamped for nothing.
UNHASHED_META = ("built_at", WAYS_CUT)


def _connect(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                           uri=True)


def content_digest(path):
    """sha256 of everything a container SAYS, and nothing about how it is laid
    out on disk or when it was stamped.

    Schema (every table, index, view and trigger, with its SQL), then every row
    of every table in key order, then every meta key but UNHASHED_META. Two files
    with the same digest answer every query the app can make identically, so
    one can stand in for the other.
    """
    h = hashlib.sha256()
    db = _connect(path)
    # Text as its stored bytes: a value that is not valid UTF-8 is still
    # content, and must hash rather than stop the build.
    db.text_factory = bytes
    try:
        master = db.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name").fetchall()
        for row in master:
            h.update(repr(row).encode("utf-8"))
            h.update(b"\n")
        master = [tuple(v.decode("utf-8") if isinstance(v, bytes) else v
                        for v in row) for row in master]
        virtual = set(name for kind, name, _, sql in master
                      if kind == "table" and sql
                      and sql.upper().startswith("CREATE VIRTUAL TABLE"))
        tables = sorted(name for kind, name, _, _ in master
                        if kind == "table" and not name.startswith("sqlite_"))
        for name in tables:
            if any(name == v + s for v in virtual for s in RTREE_SHADOWS):
                continue
            h.update(("\x00table %s\n" % name).encode("utf-8"))
            if name == "meta":
                rows = db.execute(
                    "SELECT key, value FROM meta WHERE key NOT IN (%s) "
                    "ORDER BY key" % ", ".join("?" * len(UNHASHED_META)),
                    UNHASHED_META)
            else:
                rows = _rows_in_key_order(db, name)
            for row in rows:
                h.update(repr(tuple(row)).encode("utf-8"))
                h.update(b"\n")
    finally:
        db.close()
    return h.hexdigest()


def _rows_in_key_order(db, name):
    try:
        # rowid tables and r-trees (whose rowid is the id).
        return db.execute('SELECT rowid, * FROM "%s" ORDER BY rowid' % name)
    except sqlite3.OperationalError:
        # WITHOUT ROWID: the primary key is the order, and every column of
        # the key is among the columns, so ordering by all of them is total.
        count = len(db.execute('PRAGMA table_info("%s")' % name).fetchall())
        return db.execute('SELECT * FROM "%s" ORDER BY %s'
                          % (name, ", ".join(str(i + 1)
                                             for i in range(count))))


def _meta_value(path, key):
    db = _connect(path)
    try:
        row = db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def built_at(path):
    return _meta_value(path, "built_at")


def ways_cut(path):
    """The date riders are shown for this container's lanes.

    `built_at` when there is no `ways_cut`: a container rule 3 never restamped,
    whose `built_at` is still its pack stamp.
    """
    return _meta_value(path, WAYS_CUT) or built_at(path)


def _parse(stamp):
    try:
        return datetime.datetime.strptime(stamp, STAMP_FORMAT)
    except (TypeError, ValueError):
        return None


def next_stamp(published, candidate, now):
    """The built_at for a build whose content differs from `published`'s.

    Never equal to `published`, always ISO-8601 UTC (STAMP_FORMAT), and after
    it whenever it was itself a stamp this module can read.
    """
    was = _parse(published)
    mine = _parse(candidate)
    if mine is not None and (was is None or mine > was):
        return candidate
    clock = _parse(now) or datetime.datetime.utcnow().replace(microsecond=0)
    if was is not None and clock <= was:
        clock = was + datetime.timedelta(seconds=1)
    stamp = clock.strftime(STAMP_FORMAT)
    if stamp == published:  # an unparsable published stamp equal to ours
        stamp = (clock + datetime.timedelta(seconds=1)).strftime(STAMP_FORMAT)
    return stamp


def _set_built_at(path, stamp, cut=None):
    """Write `stamp` as `built_at`, and `cut` as `ways_cut` when given, in one
    transaction so no file is left with the new stamp and no cut date."""
    db = sqlite3.connect(path)
    try:
        db.execute("UPDATE meta SET value = ? WHERE key = 'built_at'",
                   (stamp,))
        if db.total_changes != 1:
            raise SystemExit("%s has no built_at to set" % path)
        if cut is not None:
            db.execute("INSERT INTO meta VALUES (?, ?)", (WAYS_CUT, cut))
        db.commit()
    finally:
        db.close()


def stamp(published, built, now=None):
    """Stamp one container. Returns (verdict, built_at): verdict is one of
    'first' (nothing published), 'unchanged' (the published file was copied
    in), 'changed' (a stamp the published file does not carry)."""
    if not published or not os.path.isfile(published):
        return "first", built_at(built)
    if content_digest(published) == content_digest(built):
        shutil.copyfile(published, built)
        return "unchanged", built_at(built)
    was = built_at(published)
    own = built_at(built)
    new = next_stamp(was, own, now)
    if new != own:
        # Rule 3 is about to overwrite the pack stamp with the clock. Keep
        # that stamp as `ways_cut`, or the run's time becomes the only date
        # the file carries and riders are told their lanes were cut today.
        _set_built_at(built, new,
                      cut=own if _meta_value(built, WAYS_CUT) is None
                      else None)
    # THE GUARD, on the file itself rather than on the arithmetic above.
    if built_at(built) == was:
        raise SystemExit(
            "%s: different content under the published built_at %s. The app "
            "would take it for the build it already holds." % (built, was))
    return "changed", new


def stamp_tree(published_dir, manifest_path, now=None, log=print):
    """Stamp every container a build manifest lists; rewrite the manifest."""
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    here = os.path.dirname(os.path.abspath(manifest_path))
    verdicts = []
    for entry in manifest.get("containers", []):
        name = os.path.basename(entry["file"])
        built = os.path.join(here, name)
        published = os.path.join(published_dir, name) if published_dir \
            else None
        verdict, stamp_ = stamp(published, built, now)
        entry["generated"] = stamp_
        # `generated` is the build id the app matches the file against, so it
        # moves with rule 3; the date an index shows riders is this one.
        entry["waysCut"] = ways_cut(built)
        verdicts.append((name, verdict, stamp_))
        log("  %-32s %-9s built_at %s  ways_cut %s"
            % (name, verdict, stamp_, entry["waysCut"]))
    stamps = [e.get("generated") or "" for e in manifest.get("containers", [])]
    if stamps:
        manifest["generated"] = max(stamps)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    return verdicts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--published", required=True,
                    help="the container tree riders hold (containers/)")
    ap.add_argument("--manifest", required=True,
                    help="the manifest of the tree this run built")
    ap.add_argument("--now", default=None,
                    help="this run's time, %s; defaults to the clock"
                         % STAMP_FORMAT.replace("%", "%%"))
    args = ap.parse_args(argv)
    print("Stamping builds against %s" % args.published)
    verdicts = stamp_tree(args.published, args.manifest, args.now)
    counts = {}
    for _, verdict, _ in verdicts:
        counts[verdict] = counts.get(verdict, 0) + 1
    print("  %s" % ", ".join("%d %s" % (n, v) for v, n in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

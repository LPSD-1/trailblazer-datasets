#!/usr/bin/env python3
"""Refuse to publish a catalogue that lost something already published.

    python tools/verify_catalogue.py dist/catalogue.json

WHY
---
catalogue.json is rebuilt from scratch, so anything a job forgets to pass is
DELETED from it - silently, with no error, in a build that otherwise succeeds.
That has now happened three times: the monthly refresh would have wiped
imagery, the daily imagery job did wipe the ready-made trips, and the same job
collapsed every mirrored routing URL to a bare filename.

The first attempt at this check could not fail. It asked "are there any routing
packs at all?", and there are forty countries of upstream ones, so dropping the
mirror left it green. It asked "are there any imagery packs at all?", so losing
eleven of twelve passed. This one compares the catalogue against what each
index SAYS is published, pack by pack, and names what is missing.
"""
import argparse
import json
import os
import sys


def load(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def packs_in(catalogue):
    for continent in catalogue.get("continents", []):
        for country in continent.get("countries", []):
            for area in country.get("areas", []):
                for pack in area.get("packs", []):
                    yield pack


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("catalogue")
    ap.add_argument("--satellite", default="satellite/index.json")
    ap.add_argument("--routing", default="routing/index.json")
    ap.add_argument("--trips", default="trips/gb.tbtrips")
    ap.add_argument("--published", default="catalogue.json",
                    help="the catalogue currently published, to compare "
                         "against; '' to skip")
    args = ap.parse_args()

    catalogue = load(args.catalogue)
    if not catalogue or not catalogue.get("continents"):
        return fail("the catalogue is empty or unreadable")

    by_id = {}
    for pack in packs_in(catalogue):
        by_id.setdefault(pack.get("id"), pack)
    problems = []

    # --- imagery: every pack the index claims, by id -------------------------
    sat = load(args.satellite, {"packs": []})
    want = {p["id"] for p in sat.get("packs", []) if p.get("id")}
    missing = sorted(want - set(by_id))
    if missing:
        problems.append(
            "%d imagery pack(s) published but absent from the catalogue: %s"
            % (len(missing), ", ".join(missing[:5])))

    # --- routing: mirrored tiles must carry the MIRROR url -------------------
    #
    # Not "are there routing packs". There are forty countries of upstream
    # ones, so that question can never answer no. The mirror is what gets lost.
    routing = load(args.routing, {"tiles": {}})
    mirrored = set(routing.get("tiles", {}))
    if mirrored:
        wrong = []
        for name in sorted(mirrored):
            pack = by_id.get(name)
            if pack is None:
                wrong.append("%s (absent)" % name)
            elif not str(pack.get("file", "")).startswith("http"):
                # The exact failure: no --routing-mirror-base, so the URL
                # collapses to a bare "E0_N50.rd5" and 404s.
                wrong.append("%s (file=%r)" % (name, pack.get("file")))
        if wrong:
            problems.append(
                "%d mirrored routing tile(s) wrong or missing: %s"
                % (len(wrong), ", ".join(wrong[:5])))

    # --- place names ---------------------------------------------------------
    #
    # The kind this check did not know about, and the gap was live: the
    # gazetteer packs were written to dist/, which is gitignored, and
    # rebuild_catalogue.sh - "THE only way any workflow may do it" - never
    # passed --names. So every catalogue any workflow built offered no place
    # search at all, exactly the silent-deletion failure this file exists to
    # catch, in the one index it had never been told about.
    # ONCE PUBLISHED, NEVER DROPPED - which is not the same question as "is
    # everything on disk offered".
    #
    # The first version asked the second question and was wrong the first time
    # it mattered. The gazetteer packs are built and committed but deliberately
    # NOT published yet: `PackKind.parse` returns null for a kind it does not
    # know and `Pack.fromJson` turns that into a refusal of the WHOLE
    # catalogue, so shipping `names` before an app that reads it took every
    # download on every older install with it. A check that cannot tell "held
    # back on purpose" from "dropped by accident" forces you to disable it,
    # and a disabled check catches nothing.
    #
    # So this compares against what was LAST published, which is the actual
    # subject of this file: a kind that has reached riders must not vanish.
    published = load(args.published) if args.published else None
    if published:
        was = {p.get("id") for p in packs_in(published)
               if p.get("kind") == "names"}
        now = {p.get("id") for p in packs_in(catalogue)
               if p.get("kind") == "names"}
        lost = sorted(was - now)
        if lost:
            problems.append(
                "%d gazetteer pack(s) were published and are now absent: %s"
                % (len(lost), ", ".join(lost[:5])))

    # --- trips ---------------------------------------------------------------
    book = load(args.trips)
    if book and book.get("trips"):
        trips = [p for p in packs_in(catalogue) if p.get("kind") == "trips"]
        if not trips:
            problems.append(
                "%d ready-made trips are published and the catalogue offers "
                "none - the pack was dropped" % len(book["trips"]))

    if problems:
        return fail("; ".join(problems))

    print("catalogue keeps everything already published:")
    print("  imagery packs      %d" % len(want))
    print("  mirrored routing   %d" % len(mirrored))
    print("  ready-made trips   %d" % len((book or {}).get("trips", [])))
    return 0


def fail(why):
    print("REFUSING TO PUBLISH: %s" % why, file=sys.stderr)
    print("\nThe catalogue is rebuilt from scratch, so this almost always "
          "means a workflow rebuilt it without one of the indexes. Every job "
          "must go through tools/rebuild_catalogue.sh.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

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
import hashlib
import json
import os
import subprocess
import sys

# Where a pack URL has to start before the file behind it is ours to check.
OUR_SITE = "https://lpsd-1.github.io/trailblazer-datasets/"


def _local_path(file, root, staged=None):
    """The file on disk behind a pack's `file`, or None if we do not host it.

    Packs are addressed three ways: a path relative to the catalogue, an
    absolute URL on our own Pages site, and an absolute URL somewhere else
    entirely (brouter.de for routing, a GitHub release for imagery). Only the
    first two are files this repository can be asked to stand behind.

    STAGED FIRST, because the question is "does the catalogue match the file we
    are about to serve" and not "does it match the one we served last month".
    A run builds into `dist/` and the publish step moves it into place
    afterwards, so at check time the new file is staged and the old one is
    still sitting at the published path.

    That distinction was invisible while the only things checked were packs:
    those builds are reproducible, so unchanged data gives byte-identical packs
    and staged and published agree. Containers are not reproducible yet, so
    reading the published copy compared this build's catalogue against last
    build's bytes and refused 129 packs that were perfectly correct.
    """
    if not isinstance(file, str) or not file:
        return None
    if file.startswith(OUR_SITE):
        file = file[len(OUR_SITE):]
    elif file.startswith("http://") or file.startswith("https://"):
        return None
    # A published path is always forward-slashed; join it the same way on
    # every platform rather than trusting os.path to read it.
    parts = file.split("/")
    if staged:
        candidate = os.path.join(staged, *parts)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(root, *parts)


def _rewritten_by_git(paths):
    """Which of [paths] git would not store byte-for-byte.

    Returns the empty list when git cannot be asked at all — an unpacked
    tarball, a container with no git — because a check that cannot run must
    not become a check that fails.
    """
    out = []
    for path in paths:
        # RUN IT BESIDE THE FILE. `git hash-object` applies the attributes and
        # config of the repository it is INVOKED in, not of the one the path
        # happens to live in - so asked from somewhere else it answers about
        # the wrong .gitattributes and quietly says everything is fine.
        where = os.path.dirname(os.path.abspath(path)) or "."
        name = os.path.basename(path)
        try:
            filtered = subprocess.run(
                ["git", "hash-object", "--", name],
                cwd=where, capture_output=True, check=True).stdout
            raw = subprocess.run(
                ["git", "hash-object", "--no-filters", "--", name],
                cwd=where, capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return []
        if filtered != raw:
            out.append(path)
    return out


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
    ap.add_argument("--root", default=".",
                    help="where the packs this repository hosts live, for "
                         "the size-and-hash check; '' to skip")
    ap.add_argument("--staged", default="dist",
                    help="where a run builds before publishing; checked "
                         "before --root so the catalogue is compared against "
                         "the file about to be served, not last build's")
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

    # --- ground height -------------------------------------------------------
    #
    # Same rule as the gazetteer above, and it is here on the day the kind was
    # added rather than after it has been silently dropped once. A height pack
    # is what makes hill shading and 3D ground do anything at all, and it
    # enters the catalogue through `--height` in rebuild_catalogue.sh - which
    # is exactly the shape of flag that has now been forgotten three times.
    #
    # Published-versus-published, not disk-versus-catalogue, so an index built
    # but deliberately held back does not fail the build.
    if published:
        was = {p.get("id") for p in packs_in(published)
               if p.get("kind") == "height"}
        now = {p.get("id") for p in packs_in(catalogue)
               if p.get("kind") == "height"}
        lost = sorted(was - now)
        if lost:
            problems.append(
                "%d ground-height pack(s) were published and are now absent: "
                "%s" % (len(lost), ", ".join(lost[:5])))

    # --- traffic orders ------------------------------------------------------
    #
    # The same rule again, and this kind is the one it protects hardest. Every
    # other dataset here degrades gracefully when it goes missing: a rider
    # without imagery has a map, a rider without place names can still pan to
    # where they are going. A rider without traffic orders is shown a map with
    # no closures on it, which is indistinguishable from a map where nothing is
    # closed — so a dropped pack is not a missing feature, it is a wrong answer
    # delivered confidently.
    #
    # It enters through `--tro` in rebuild_catalogue.sh, and it is rebuilt four
    # times a day by a job that does not build anything else. Both of those are
    # exactly the conditions under which the other kinds got lost.
    if published:
        was = {p.get("id") for p in packs_in(published)
               if p.get("kind") == "tro"}
        now = {p.get("id") for p in packs_in(catalogue)
               if p.get("kind") == "tro"}
        lost = sorted(was - now)
        if lost:
            problems.append(
                "traffic orders were published and are now absent: %s - "
                "riders would see a map with no closures on it"
                % ", ".join(lost))

    # --- trips ---------------------------------------------------------------
    book = load(args.trips)
    if book and book.get("trips"):
        trips = [p for p in packs_in(catalogue) if p.get("kind") == "trips"]
        if not trips:
            problems.append(
                "%d ready-made trips are published and the catalogue offers "
                "none - the pack was dropped" % len(book["trips"]))

    # --- the size and hash of every pack WE host ----------------------------
    #
    # The catalogue's whole job is to describe files, and until now nothing
    # checked that it described them correctly. The app does: it refuses any
    # download whose SHA-256 does not match, bins it, and says "That download
    # was corrupted. Try again." A pack whose published hash is wrong is
    # therefore not "slightly off", it is undownloadable, permanently, for
    # every rider, with a message that blames their connection.
    #
    # It happened. `trips/gb.tbtrips` is the one pack written in text mode, so
    # git treats it as text and rewrites its line endings; a catalogue built on
    # Windows hashed the CRLF copy in the working tree (7007 bytes,
    # 4daf8ca3...) while the LF copy was what got committed and served (6764
    # bytes, e3c05ef5...). Every build was green. See .gitattributes, which
    # stops the rewrite; this stops anything else of the same shape, whatever
    # causes it.
    #
    # Only packs this repository hosts. Routing tiles come from brouter.de and
    # imagery from a GitHub release, and neither is on disk here.
    if args.root:
        wrong = []
        for pack in packs_in(catalogue):
            path = _local_path(pack.get("file"), args.root, args.staged)
            if path is None or not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                body = f.read()
            want_bytes = pack.get("bytes")
            want_sha = (pack.get("sha256") or "").lower()
            if want_bytes is not None and len(body) != want_bytes:
                wrong.append("%s: %d bytes on disk, catalogue says %s"
                             % (pack.get("id"), len(body), want_bytes))
            elif want_sha and hashlib.sha256(body).hexdigest() != want_sha:
                wrong.append("%s: sha256 on disk is %s, catalogue says %s"
                             % (pack.get("id"),
                                hashlib.sha256(body).hexdigest()[:12],
                                want_sha[:12]))
        if wrong:
            problems.append(
                "%d pack(s) do not match the file this repository serves, so "
                "the app would refuse every download of them: %s"
                % (len(wrong), "; ".join(wrong[:5])))

        # And the same failure one step earlier, where it is still cheap.
        #
        # The check above compares the catalogue to the working tree, and both
        # are written by the same run on the same machine — so on the machine
        # that CAUSES this it agrees with itself and passes. What actually goes
        # wrong is that git stores something different from what was hashed.
        #
        # `git hash-object` runs the clean filter; `--no-filters` does not. If
        # they disagree, git rewrites this file on the way in, the hash in the
        # catalogue describes a file nobody will ever be served, and every
        # download of that pack fails its checksum for ever.
        rewritten = _rewritten_by_git(
            sorted({p for p in (
                _local_path(pack.get("file"), args.root)
                for pack in packs_in(catalogue)) if p and os.path.exists(p)}))
        if rewritten:
            problems.append(
                "git rewrites %d pack file(s) on the way in, so their "
                "published hash would describe bytes nobody is served: %s "
                "(mark the extension `-text` in .gitattributes)"
                % (len(rewritten), ", ".join(rewritten[:5])))

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

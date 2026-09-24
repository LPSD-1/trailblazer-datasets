#!/usr/bin/env python3
"""Publish the `.tbchange` files that keep a rider off the whole download.

    python tools/publish_changesets.py \
        --old containers --new dist/containers --out changes

THE HALF THAT WAS MISSING. `build_changeset.py` could write a changeset and the
app could apply one, and for as long as nothing ran the first and nothing
announced the result, neither half was reachable from the other: `find . -name
"*.tbchange"` returned nothing, catalogue.json contained zero occurrences of
the word, and the app's applier had exactly one importer in the whole codebase
- its own test. This is what runs in CI, and `changes/index.json` is what the
catalogue reads to tell the app a changeset exists.

WHY IT MATTERS. A container is hundreds of megabytes and lane data refreshes
four times a day. Without this, staying current means re-downloading the
country; with it, a rider on a phone tether pays for what actually changed.

A RIDER TWO UPDATES BEHIND. Each run has the bytes of exactly one older build -
the one it is replacing - so it can write exactly one changeset per container.
Six hours later that changeset is one link of a chain, and the app walks the
chain: 104->105 then 105->106 brings a rider who missed a run all the way up.
So the PUBLISHED SET IS A WINDOW, not a single file, and `--keep` decides how
far back a rider may be and still avoid the whole download.

That is not the patch chain `ContainerUpdate` was chosen over. Every link is a
whole transaction against a build the publisher actually made, so a chain that
stops half way leaves the container at a real build rather than at a state
nobody published.

REFUSALS, because a changeset is the one artefact that modifies data a rider
already holds:

  * NOT SMALLER, NOT PUBLISHED. See `build_changeset.is_worth_publishing`.
  * NOT VALID, NOT PUBLISHED. Every file is put through
    `validate_changeset.problems_with` before it is announced, so the gate that
    exists runs on the artefacts that ship rather than on a self-test.
  * UNSIGNED IS SAID OUT LOUD. The app refuses to apply an unsigned changeset
    to a container it has verified, so an unsigned one is a file nobody can
    use. It is still written, and the index still lists it, exactly as the
    containers do - and the workflow's own guard is what refuses to publish.
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_changeset  # noqa: E402
import validate_changeset  # noqa: E402

try:
    import sign_release
except Exception:  # pragma: no cover - cryptography missing locally
    sign_release = None

#: How many published builds back a rider may be and still avoid the whole
#: download.
#:
#: SIX, which is a day and a half at the four-times-a-day lane cadence. Long
#: enough that a phone left in a jacket over a weekend still catches up
#: cheaply; short enough that the window is a handful of small files per
#: container rather than an archive nobody prunes.
DEFAULT_KEEP = 6


def _built_at(path):
    """The build stamp inside a container, or None."""
    try:
        db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                             uri=True)
    except sqlite3.Error:
        return None
    try:
        row = db.execute(
            "SELECT value FROM meta WHERE key='built_at'").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


def _index_path(out_dir):
    return os.path.join(out_dir, "index.json")


def _load_index(out_dir):
    path = _index_path(out_dir)
    if not os.path.isfile(path):
        return {"schema": 1, "builds": {}, "changesets": []}
    try:
        with open(path, encoding="utf8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"schema": 1, "builds": {}, "changesets": []}
    data.setdefault("builds", {})
    data.setdefault("changesets", [])
    data["schema"] = 1
    return data


def _write_index(out_dir, index):
    """Write index.json, and say whether it actually moved.

    NO RUN STAMP IN IT, deliberately. The Publish step asks "did this build
    produce anything new" by diffing trees, and a `generated` field would
    answer yes on every one of the 121 runs a month where councils published
    nothing - which is the fault `containers/manifest.json` already has and
    this file is not going to add a second instance of.
    """
    path = _index_path(out_dir)
    body = json.dumps(index, indent=1, sort_keys=True).encode("utf8")
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            if fh.read() == body:
                return False
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)
    return True


def _containers(directory):
    """{published file path: (container id, path on disk)} for a container tree.

    KEYED BY THE FILE, NOT BY THE ID, and that is not a detail.

    The container manifest calls the South West container `gb-south-west`. The
    catalogue calls the pack built from it `gb-south-west-ways`, because
    `build_catalogue.lane_areas` appends the dataset name. Keyed on the id, the
    announcement matched nothing at all for any lane pack - the changesets were
    built, validated, published and then never mentioned in catalogue.json,
    which is precisely the "both halves exist and neither is connected" defect
    this whole feature exists to close, reproduced inside the fix for it.
    Caught by the golden build: catalogue.json barely moved.

    The FILE is the one string both sides copy verbatim - `lane_areas` and the
    overview block both write `container["file"]` straight through - so it is
    the join that cannot drift.
    """
    manifest = os.path.join(directory, "manifest.json")
    out = {}
    if os.path.isfile(manifest):
        with open(manifest, encoding="utf8") as fh:
            for entry in json.load(fh).get("containers", []):
                served = entry.get("file", "")
                rel = os.path.basename(served)
                path = os.path.join(directory, rel)
                if rel and os.path.isfile(path):
                    out[served] = (entry.get("id") or rel, path)
    return out


def publish(old_dir, new_dir, out_dir, base_url="", keep=DEFAULT_KEEP,
            signing_key=None, max_ratio=build_changeset.MAX_RATIO):
    """Build, check and announce the changesets between two container trees.

    Returns a report dict. Writes nothing when nothing changed, which is the
    ordinary case: councils do not amend a definitive map four times a day.
    """
    index = _load_index(out_dir)
    old = _containers(old_dir)
    new = _containers(new_dir)

    report = {"built": [], "skipped": [], "pruned": [], "unsigned": 0,
              "measured": []}

    for served in sorted(new):
        pack_id, new_path = new[served]
        to_build = _built_at(new_path)
        if not to_build:
            report["skipped"].append((pack_id, "no built_at in the container"))
            continue
        # The build the catalogue will announce, whether or not a changeset
        # came out of this run. Without it the app cannot tell "already
        # current" from "no changeset published", and would fetch the whole
        # container to find out.
        index["builds"][served] = to_build

        was = old.get(served)
        if not was:
            report["skipped"].append((pack_id, "nothing published before"))
            continue
        from_build = _built_at(was[1])
        if not from_build or from_build == to_build:
            report["skipped"].append((pack_id, "unchanged"))
            continue
        if any(c["container"] == served and c["from"] == from_build
               and c["to"] == to_build for c in index["changesets"]):
            report["skipped"].append((pack_id, "already published"))
            continue

        pack_dir = os.path.join(out_dir, pack_id)
        os.makedirs(pack_dir, exist_ok=True)
        name = "%s-%s.tbchange" % (_stamp(from_build), _stamp(to_build))
        path = os.path.join(pack_dir, name)
        try:
            stats = build_changeset.build_changeset(was[1], new_path, path)
        except SystemExit as e:
            # A schema change, a different record table, two builds the app
            # cannot tell apart. Every one of them means "the rider needs the
            # whole build", which is what happens when no changeset is
            # announced - so this is a skip, not a failure.
            report["skipped"].append((pack_id, str(e)))
            _unlink(path)
            continue

        whole = os.path.getsize(new_path)
        report["measured"].append(
            (pack_id, stats["bytes"], whole, stats["bytes"] / float(whole)))
        if not build_changeset.is_worth_publishing(stats["bytes"], whole,
                                                   max_ratio):
            report["skipped"].append(
                (pack_id, "not smaller: %d of %d bytes" % (stats["bytes"],
                                                           whole)))
            _unlink(path)
            continue

        # THE GATE RUNS ON THE ARTEFACT THAT SHIPS. validate_changeset.py had
        # a --selftest and nothing else ever ran it against a published file.
        problems = validate_changeset.problems_with(path)
        if problems:
            report["skipped"].append(
                (pack_id, "; ".join(problems)[:200]))
            _unlink(path)
            continue

        with open(path, "rb") as fh:
            payload = fh.read()
        signature = None
        if signing_key is not None and sign_release is not None:
            signature = base64.b64encode(
                sign_release.sign_bytes(payload, signing_key)).decode("ascii")
        else:
            report["unsigned"] += 1

        entry = {
            # The join key: what the catalogue writes as the pack's `file`.
            "container": served,
            # For a human reading the tree. NOT the catalogue's pack id.
            "pack": pack_id,
            "from": from_build,
            "to": to_build,
            "file": _url(base_url, "%s/%s/%s" % (
                os.path.basename(out_dir.rstrip("/\\")), pack_id, name)),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
        if signature:
            entry["signature"] = signature
        index["changesets"].append(entry)
        report["built"].append((pack_id, from_build, to_build, len(payload)))

    _prune(index, out_dir, keep, report)
    index["changesets"].sort(key=lambda c: (c["container"], c["to"], c["from"]))
    report["index_written"] = _write_index(out_dir, index)
    report["index"] = index
    return report


def _stamp(build):
    """A build stamp as a filename: no colons, which Windows will not have."""
    return "".join(ch for ch in build if ch.isalnum())


def _url(base_url, rel):
    if not base_url:
        return rel
    if not base_url.endswith("/"):
        base_url += "/"
    return base_url + rel


def _unlink(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _prune(index, out_dir, keep, report):
    """Keep the newest [keep] changesets per pack; delete the rest.

    A WINDOW, NOT AN ARCHIVE. Every retained changeset is a file this
    repository serves forever, and the rider it would help is one who has not
    opened the app in longer than the window - who is going to be re-fetching
    imagery and orders anyway. Sorted by `to` then `from`, which is the order
    they were published in, because the stamps are ISO-8601 and sort as text.
    """
    by_pack = {}
    for entry in index["changesets"]:
        by_pack.setdefault(entry["pack"], []).append(entry)
    kept = []
    for pack_id, entries in by_pack.items():
        entries.sort(key=lambda c: (c["to"], c["from"]))
        for gone in entries[:-keep] if keep > 0 else entries:
            path = os.path.join(out_dir, gone["pack"],
                                "%s-%s.tbchange" % (_stamp(gone["from"]),
                                                    _stamp(gone["to"])))
            _unlink(path)
            report["pruned"].append((pack_id, gone["from"], gone["to"]))
        kept.extend(entries[-keep:] if keep > 0 else [])
    index["changesets"] = kept
    # A pack with no changesets left keeps no folder either.
    for pack_id in list(by_pack):
        folder = os.path.join(out_dir, pack_id)
        if os.path.isdir(folder) and not os.listdir(folder):
            shutil.rmtree(folder, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="containers",
                    help="the published container tree")
    ap.add_argument("--new", default="dist/containers",
                    help="the container tree this run built")
    ap.add_argument("--out", default="changes",
                    help="where the .tbchange files and index.json go")
    ap.add_argument("--base-url", default="",
                    help="where the changes tree is served from")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help="how many builds back a rider may be (default %d)"
                         % DEFAULT_KEEP)
    ap.add_argument("--max-ratio", type=float,
                    default=build_changeset.MAX_RATIO,
                    help="the most of a container a changeset may weigh")
    ap.add_argument("--require-signing", action="store_true",
                    help="exit 1 if anything was published unsigned")
    args = ap.parse_args()

    signing_key = None
    if sign_release is not None and (os.environ.get("TB_SIGNING_KEY")
                                     or os.environ.get("TB_SIGNING_KEY_PEM")):
        signing_key = sign_release._private_key()
    else:
        print("  WARNING: no signing key; changesets will be unsigned, and "
              "the app refuses an unsigned changeset against a verified "
              "container.")

    report = publish(args.old, args.new, args.out,
                     base_url=args.base_url, keep=args.keep,
                     signing_key=signing_key, max_ratio=args.max_ratio)

    print("changesets: %d built, %d skipped, %d pruned"
          % (len(report["built"]), len(report["skipped"]),
             len(report["pruned"])))
    for pack_id, delta, whole, ratio in sorted(report["measured"]):
        print("  %-28s %9d of %10d bytes  %.1f%% of the whole build"
              % (pack_id, delta, whole, 100.0 * ratio))
    for pack_id, why in sorted(report["skipped"])[:20]:
        print("  skipped %-24s %s" % (pack_id, why))
    if report["built"]:
        total = sum(b[3] for b in report["built"])
        print("  %d changeset(s), %.2f MB in all" % (len(report["built"]),
                                                     total / 1048576.0))
    if report["unsigned"] and args.require_signing:
        print("REFUSED: %d changeset(s) went unsigned." % report["unsigned"],
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

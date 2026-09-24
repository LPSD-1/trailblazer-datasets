#!/usr/bin/env python3
"""Re-hash and re-sign containers that were edited AFTER build_containers.py.

    python tools/restamp_containers.py --manifest dist/containers/manifest.json

WHY THIS FILE HAD TO EXIST
--------------------------
`build_containers.py build_all` does four things to every container and then
writes them all down in `containers/manifest.json`:

    sha256          the digest the app checks a finished download against
    bytes           the plain size
    downloadBytes   the gzipped size, which section 18.2 turns on
    signature       Ed25519 over the WHOLE file, plus a sibling `.tbmap.sig`

Every one of those is taken from the container's bytes at that instant. The
conditions pipeline (`build_wet.py assign`, `build_fords.py build`,
`evidence_age.py --write`) then writes new TABLES into the same file, in place,
because that is what the static half is for.

MEASURED, on the published `containers/motor-south-west.tbmap`:

    before  sha256 12f0fff663dbd006...   2,347,008 bytes
    after   sha256 95a7fe247a6d26e1...   2,437,120 bytes   (+90,112, one region)

and `containers/manifest.json` still said `12f0fff663dbd006` and `2347008`.

That is not a cosmetic drift. A rider downloading that container gets bytes
whose digest does not match the catalogue, and the app bins the download and
reports it as corrupted - permanently, on every retry, because the file it
fetches is always the same wrong-against-the-manifest one. The signature is
worse: `unsignedIsAccepted` is false, so a container whose `.sig` was made over
the pre-edit bytes downloads in full, fails at mount, and its ground reads as
"not downloaded" with nothing anywhere saying why.

So: anything that edits a container in place must be followed by this, and the
workflow's guard below fails if it was not.

WHY IT REUSES build_containers' OWN FUNCTIONS
---------------------------------------------
`_digest`, `_download_bytes` and `sign_release.sign_file` are imported rather
than reimplemented. A second copy of "how a container is hashed" is a second
thing to keep in step with the first, and the whole failure above is what
happens when two places disagree about one container's bytes. If the builder
changes how it measures, this changes with it.

SIGNING IS NOT OPTIONAL HERE, WHICH IS THE ONE DIFFERENCE.
`build_containers.py` warns and publishes unsigned when no key is set, because
an unsigned build is caught further down the workflow. This REFUSES, unless
`--allow-unsigned` is passed: a container that arrives here already carrying a
signature and leaves without one has been actively downgraded by this script,
and that is a different thing from never having been signed. `--allow-unsigned`
exists so the unsigned local case (no key, e.g. a developer's laptop) is a
stated choice rather than a silent one.
"""
import argparse
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_containers as BC       # noqa: E402

try:
    import sign_release
except Exception:                                    # pragma: no cover
    sign_release = None


#: The four fields that are a function of the container's bytes. Named rather
#: than "everything except id/label", so a field ADDED to the manifest later is
#: left alone by default instead of being silently recomputed as something.
BYTE_DERIVED = ("sha256", "bytes", "downloadBytes", "signature")


def container_path(manifest_path, entry):
    """Where the file behind a manifest row actually is.

    `file` is written as `containers/<name>` - relative to the repository root,
    not to the manifest - so the manifest in `dist/containers/` names
    `containers/x.tbmap` and the file sits beside the manifest. Resolved by
    basename against the manifest's own directory, which is true of both the
    `dist/containers` and the published `containers` layouts.
    """
    return os.path.join(os.path.dirname(os.path.abspath(manifest_path)),
                        os.path.basename(entry["file"]))


def restamp(manifest_path, signing_key=None, allow_unsigned=False, log=print):
    """Rewrite every byte-derived field from the files as they are now.

    Returns (entries that moved, entries that did not). Both are reported: a
    run where NOTHING moved is the interesting one, because it means either the
    static half did not write anything or this ran before it rather than after.
    """
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    entries = manifest.get("containers") or []
    if not entries:
        raise SystemExit("%s lists no containers; nothing to restamp, and a "
                         "container manifest with no containers is itself the "
                         "fault worth stopping on" % manifest_path)

    moved, same, missing = [], [], []
    for entry in entries:
        path = container_path(manifest_path, entry)
        if not os.path.exists(path):
            missing.append(entry.get("file"))
            continue
        before = {k: entry.get(k) for k in BYTE_DERIVED}

        entry["sha256"] = BC._digest(path)
        entry["bytes"] = os.path.getsize(path)
        entry["downloadBytes"] = BC._download_bytes(path)

        if signing_key is not None:
            signature, _ = sign_release.sign_file(path, signing_key)
            entry["signature"] = base64.b64encode(signature).decode("ascii")
        elif before.get("signature"):
            # It WAS signed and we cannot sign it again. Publishing it now
            # would ship a signature over bytes that no longer exist, which
            # the app refuses at mount after paying for the whole download.
            if not allow_unsigned:
                raise SystemExit(
                    "%s carries a signature made over its old bytes and no "
                    "signing key is available to remake it. Set "
                    "TB_SIGNING_KEY_PEM, or pass --allow-unsigned and accept "
                    "that this container will be refused at mount."
                    % entry["file"])
            entry["signature"] = None

        after = {k: entry.get(k) for k in BYTE_DERIVED}
        (moved if after != before else same).append(entry["file"])

    if missing:
        raise SystemExit(
            "%d container(s) in the manifest are not on disk: %s. A manifest "
            "describing a file nobody can download is worse than no manifest."
            % (len(missing), ", ".join(sorted(missing)[:6])))

    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)

    log("restamped %s" % manifest_path)
    log("    %d container(s) moved, %d unchanged" % (len(moved), len(same)))
    for name in sorted(moved)[:8]:
        log("      moved  %s" % name)
    if len(moved) > 8:
        log("      ... and %d more" % (len(moved) - 8))
    return moved, same


def selftest(log=print):
    """Offline. Proves the two things a caller cannot check for itself: that an
    edited container's digest is rewritten, and that an unsigned restamp of a
    signed container is REFUSED rather than quietly downgraded."""
    import shutil
    import sqlite3
    import tempfile

    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append("%s%s" % (name, (": " + str(detail)) if detail else ""))

    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "ways-south-west.tbmap")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('schema_version','1')")
        db.commit()
        db.close()

        manifest_path = os.path.join(tmp, "manifest.json")
        stale = {"schema": 1, "containers": [{
            "id": "gb-south-west", "kind": "area",
            "file": "containers/ways-south-west.tbmap",
            "sha256": "0" * 64, "bytes": 1, "downloadBytes": 1,
        }]}
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(stale, fh)

        restamp(manifest_path, log=lambda *a: None)
        got = json.load(open(manifest_path, encoding="utf-8"))["containers"][0]
        check("a stale digest is rewritten", got["sha256"] != "0" * 64,
              got["sha256"][:16])
        check("and it is the digest of the file on disk",
              got["sha256"] == BC._digest(path), got["sha256"][:16])
        check("the plain size is the file's size",
              got["bytes"] == os.path.getsize(path), got["bytes"])
        check("downloadBytes is the gzipped size, not the plain one",
              got["downloadBytes"] == BC._download_bytes(path),
              got["downloadBytes"])

        # And now the one that matters: an edit AFTER the stamp must move it.
        first = got["sha256"]
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE way_wetness (id INTEGER PRIMARY KEY,"
                   " susceptibility TEXT NOT NULL)")
        db.execute("INSERT INTO way_wetness VALUES (1,'soft')")
        db.commit()
        db.close()
        restamp(manifest_path, log=lambda *a: None)
        got = json.load(open(manifest_path, encoding="utf-8"))["containers"][0]
        check("writing a table into the container moves the recorded digest",
              got["sha256"] != first, "%s vs %s" % (got["sha256"][:16],
                                                    first[:16]))

        # A signed row, with no key: refuse rather than downgrade.
        signed = json.load(open(manifest_path, encoding="utf-8"))
        signed["containers"][0]["signature"] = "Zm9v"
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(signed, fh)
        refused = False
        try:
            restamp(manifest_path, log=lambda *a: None)
        except SystemExit:
            refused = True
        check("an unsigned restamp of a SIGNED container is refused", refused)

        allowed = restamp(manifest_path, allow_unsigned=True,
                          log=lambda *a: None)
        check("and --allow-unsigned lets it through", bool(allowed))
        got = json.load(open(manifest_path, encoding="utf-8"))["containers"][0]
        check("with the dead signature removed, not kept",
              got["signature"] is None, repr(got["signature"]))

        # A manifest naming a file that is not there must stop the build.
        os.remove(path)
        stopped = False
        try:
            restamp(manifest_path, allow_unsigned=True, log=lambda *a: None)
        except SystemExit:
            stopped = True
        check("a manifest naming a missing container is refused", stopped)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for failure in failures:
        log("  FAIL " + failure)
    log("selftest: %s" % ("FAILED %d" % len(failures) if failures else "ok"))
    return 1 if failures else 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="re-hash and re-sign containers edited after the build")
    ap.add_argument("--manifest", default="dist/containers/manifest.json")
    ap.add_argument("--allow-unsigned", action="store_true",
                    help="accept dropping a signature we cannot remake")
    ap.add_argument("--require-moved", action="store_true",
                    help="exit 1 if no container's bytes moved - the guard "
                         "against a static half that silently did nothing")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.selftest:
        return selftest()

    signing_key = None
    if sign_release is not None and (os.environ.get("TB_SIGNING_KEY")
                                     or os.environ.get("TB_SIGNING_KEY_PEM")):
        signing_key = sign_release._private_key()
    else:
        print("  WARNING: no signing key; containers cannot be re-signed.")

    moved, _same = restamp(args.manifest, signing_key,
                           allow_unsigned=args.allow_unsigned)
    if args.require_moved and not moved:
        print("REFUSED: not one container's bytes moved. Either the static "
              "half of the conditions pipeline wrote nothing, or this ran "
              "before it instead of after it. A green step that did nothing "
              "is the failure this flag exists for.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

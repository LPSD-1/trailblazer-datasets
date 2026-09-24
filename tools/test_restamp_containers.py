#!/usr/bin/env python3
"""restamp_containers.py, and the failure it exists to stop.

    python tools/test_restamp_containers.py

`build_containers.py` takes a container's sha256, its plain size, its gzipped
size and its Ed25519 signature at the moment it finishes writing that file, and
records all four in `containers/manifest.json`. The conditions pipeline then
writes five new tables into the same file, in place, because that is what the
static half is for.

MEASURED, on the published `containers/motor-south-west.tbmap`:

    before   sha256 12f0fff663dbd006...   2,347,008 bytes
    after    sha256 95a7fe247a6d26e1...   2,437,120 bytes

and the manifest still said the first one. A rider downloading that container
gets bytes whose digest is not the one the catalogue promises, so the app bins
the download and reports it corrupted - every time, because the file it
re-fetches is always the same. The signature is worse: `unsignedIsAccepted` is
false, so it downloads in full, is refused at mount, and the ground reads as
"not downloaded" with nothing saying why.

WHAT THIS FILE CHECKS IS THAT THE FIELDS MOVE, not that a function was called.
Each test edits a container the way the pipeline does and asserts the recorded
figure followed - including the case that is the actual bug, where the restamp
runs BEFORE the write instead of after it and every figure is already right.
"""
import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_containers as BC          # noqa: E402
import restamp_containers as R         # noqa: E402

try:
    import sign_release
except Exception:                                    # pragma: no cover
    sign_release = None

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


def container(path, tables=()):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS meta"
               " (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version','1')")
    for name in tables:
        db.execute("CREATE TABLE %s (id INTEGER PRIMARY KEY, v TEXT)" % name)
        # Rows, not just a table. An empty table can fit in pages SQLite had
        # already allocated, and a test whose "edit" does not change the file
        # length is a test that would pass against a restamp doing nothing.
        for i in range(200):
            db.execute("INSERT INTO %s VALUES (?,?)" % name,
                       (i, "x" * 64))
    db.commit()
    db.close()
    return path


def manifest(tmp, path, **fields):
    entry = {"id": "gb-north", "kind": "area", "dataset": "ways",
             "area": "north", "label": "North",
             "file": "containers/%s" % os.path.basename(path),
             "sha256": BC._digest(path), "bytes": os.path.getsize(path),
             "downloadBytes": BC._download_bytes(path),
             "laneCount": 1, "generated": "2026-01-02T03:04:05Z"}
    entry.update(fields)
    where = os.path.join(tmp, "manifest.json")
    with open(where, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "format": "tbmap", "containers": [entry]}, fh)
    return where


def row(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["containers"][0]


# ------------------------------------------------------------- the bug itself

def test_a_table_written_after_the_build_moves_every_recorded_figure():
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "ways-north.tbmap"))
        where = manifest(tmp, path)
        before = row(where)

        # Exactly what build_wet.assign and build_fords.build do.
        container(path, tables=("way_wetness", "fords"))
        stale = row(where)
        check("the manifest has NOT noticed on its own",
              stale["sha256"] == before["sha256"], "the fixture is wrong")
        check("although the file really did change",
              BC._digest(path) != before["sha256"])

        R.restamp(where, log=lambda *a: None)
        after = row(where)
        check("the digest is rewritten", after["sha256"] != before["sha256"])
        check("to the digest of the file as it is now",
              after["sha256"] == BC._digest(path))
        check("the plain size follows", after["bytes"] == os.path.getsize(path))
        check("and it really grew", after["bytes"] > before["bytes"],
              "%d -> %d" % (before["bytes"], after["bytes"]))
        check("the gzipped size follows too",
              after["downloadBytes"] == BC._download_bytes(path))
        check("downloadBytes is not just the plain size",
              after["downloadBytes"] != after["bytes"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fields_that_are_not_about_the_bytes_are_left_alone():
    """A restamp that rewrote the whole row would quietly drop `laneCount`,
    `area` or `generated` - and `generated` is the date a rider is shown as
    when their lane data was cut."""
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "ways-north.tbmap"))
        where = manifest(tmp, path)
        container(path, tables=("way_wetness",))
        R.restamp(where, log=lambda *a: None)
        after = row(where)
        for field, want in (("id", "gb-north"), ("kind", "area"),
                            ("area", "north"), ("label", "North"),
                            ("laneCount", 1),
                            ("generated", "2026-01-02T03:04:05Z")):
            check("%s survives the restamp" % field, after.get(field) == want,
                  repr(after.get(field)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------- the guard against no-op

def test_a_restamp_that_moved_nothing_is_reported_as_moving_nothing():
    """--require-moved in the workflow turns this into a failure. The case it
    catches is the restamp step sitting ABOVE the writes instead of below
    them: every figure is already correct, every step is green, and every
    container ships with a manifest describing the file it was before the
    conditions went in."""
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "ways-north.tbmap"))
        where = manifest(tmp, path)
        moved, same = R.restamp(where, log=lambda *a: None)
        check("nothing moved, because nothing was written", moved == [], moved)
        check("and the unchanged container is still counted",
              len(same) == 1, same)

        container(path, tables=("way_wetness",))
        moved, same = R.restamp(where, log=lambda *a: None)
        check("after a write, it moves", len(moved) == 1, moved)
        check("and nothing is reported unchanged", same == [], same)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_require_moved_exits_nonzero_when_nothing_moved():
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "ways-north.tbmap"))
        where = manifest(tmp, path)
        code = R.main(["--manifest", where, "--require-moved",
                       "--allow-unsigned"])
        check("--require-moved fails a restamp that moved nothing", code == 1,
              code)
        container(path, tables=("way_wetness",))
        code = R.main(["--manifest", where, "--require-moved",
                       "--allow-unsigned"])
        check("and passes one that moved something", code == 0, code)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_require_moved_still_fails_when_a_signing_key_is_set():
    """The same guard, WITH A KEY, which is how CI runs it.

    The test above passed on a developer machine and failed in the cutover
    dry run: CI sets TB_SIGNING_KEY_PEM, the unsigned fixture row gained a
    signature, and a signature counted as the file moving. So the guard could
    not fire in the one place it runs.
    """
    if sign_release is None:
        check("sign_release imports, so the keyed case is tested at all",
              False, "cryptography missing")
        return
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode("ascii")
    saved = {k: os.environ.get(k) for k in ("TB_SIGNING_KEY_PEM",
                                            "TB_SIGNING_KEY")}
    os.environ["TB_SIGNING_KEY_PEM"] = pem
    os.environ.pop("TB_SIGNING_KEY", None)
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "ways-north.tbmap"))
        where = manifest(tmp, path)
        code = R.main(["--manifest", where, "--require-moved"])
        check("with a key, a restamp that moved nothing still fails",
              code == 1, code)
        # THE PREMISE: the key really was used, so the row did gain a
        # signature - otherwise this is the keyless test again.
        with open(where, encoding="utf-8") as fh:
            row = json.load(fh)["containers"][0]
        check("and the key was really used", bool(row.get("signature")),
              repr(row.get("signature")))
        container(path, tables=("way_wetness",))
        code = R.main(["--manifest", where, "--require-moved"])
        check("and with a key, one that moved something passes", code == 0,
              code)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- signatures

def test_a_signed_container_is_not_silently_downgraded():
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "ways-north.tbmap"))
        where = manifest(tmp, path, signature="Zm9vYmFy")
        container(path, tables=("fords",))
        refused = False
        try:
            R.restamp(where, log=lambda *a: None)
        except SystemExit as e:
            refused = True
            check("and says why", "signature" in str(e).lower(), str(e))
        check("no key and a signed container is REFUSED", refused)
        check("and the manifest is left untouched",
              row(where)["signature"] == "Zm9vYmFy")

        R.restamp(where, allow_unsigned=True, log=lambda *a: None)
        check("--allow-unsigned drops the dead signature rather than keeping "
              "it", row(where)["signature"] is None, row(where)["signature"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_new_signature_verifies_against_the_new_bytes():
    if sign_release is None:
        print("  (no cryptography module; signature round-trip not run)")
        return
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    key = Ed25519PrivateKey.generate()
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "ways-north.tbmap"))
        signature, _ = sign_release.sign_file(path, key)
        where = manifest(tmp, path, signature=base64.b64encode(
            signature).decode("ascii"))

        container(path, tables=("way_wetness", "fords"))
        old = base64.b64decode(row(where)["signature"])
        payload = open(path, "rb").read()
        check("the OLD signature no longer verifies the edited container",
              not sign_release.verify_bytes(payload, old, key.public_key()))

        R.restamp(where, signing_key=key, log=lambda *a: None)
        new = base64.b64decode(row(where)["signature"])
        payload = open(path, "rb").read()
        check("the re-made one does",
              sign_release.verify_bytes(payload, new, key.public_key()))
        check("and it is a different signature", new != old)
        check("the sibling .sig file was rewritten too",
              open(path + ".sig", "rb").read() == new)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -------------------------------------------------------------- refusals

def test_a_manifest_naming_a_file_that_is_not_there_is_refused():
    tmp = tempfile.mkdtemp()
    try:
        path = container(os.path.join(tmp, "ways-north.tbmap"))
        where = manifest(tmp, path)
        os.remove(path)
        refused = False
        try:
            R.restamp(where, log=lambda *a: None)
        except SystemExit:
            refused = True
        check("a manifest describing a missing container is refused", refused)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_empty_manifest_is_refused_rather_than_passed():
    """ZERO SUBJECTS IS BLIND. A container manifest with no containers in it is
    itself the fault worth stopping on, and a restamp that shrugged at it would
    report a clean run over nothing at all."""
    tmp = tempfile.mkdtemp()
    try:
        where = os.path.join(tmp, "manifest.json")
        with open(where, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "containers": []}, fh)
        refused = False
        try:
            R.restamp(where, log=lambda *a: None)
        except SystemExit:
            refused = True
        check("a manifest listing no containers is refused", refused)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_selftest_passes():
    check("restamp_containers.py --selftest", R.selftest(lambda *a: None) == 0)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for failure in _failed:
        print("  FAIL %s" % failure)
    print("%d checks, %d failed" % (_passed + len(_failed), len(_failed)))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

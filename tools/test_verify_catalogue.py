#!/usr/bin/env python3
"""Guards for the catalogue check that would have caught the trips pack.

    python tools/test_verify_catalogue.py

WHY THESE EXIST
---------------
catalogue.json publishes a length and a SHA-256 for every pack, and the app
refuses any download that does not match: it bins the file and tells the rider
"That download was corrupted. Try again." A pack whose published hash is wrong
is therefore not slightly wrong, it is permanently undownloadable, for every
rider, with a message that blames their connection.

That shipped. `trips/gb.tbtrips` is the only pack written in text mode, so git
treated it as text; a catalogue built on Windows hashed the CRLF copy in the
working tree (7007 bytes, 4daf8ca3...) while the LF copy is what was committed
and what GitHub Pages serves (6764 bytes, e3c05ef5...). Every build was green.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
VERIFY = os.path.join(HERE, "verify_catalogue.py")

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


def catalogue_with(pack):
    return {
        "schema": 2,
        "generated": "2026-09-12T00:00:00Z",
        "attribution": "test",
        "continents": [{
            "id": "europe",
            "label": "Europe",
            "countries": [{
                "code": "GB",
                "label": "United Kingdom",
                "bounds": {"west": -8.7, "south": 49.8,
                           "east": 1.8, "north": 60.9},
                "areas": [{
                    "id": "gb-midlands",
                    "label": "Midlands",
                    "bounds": {"west": -3.25, "south": 51.9,
                               "east": 0.15, "north": 53.6},
                    "packs": [pack],
                }],
            }],
        }],
    }


def run_verify(root, catalogue_path, staged=""):
    return subprocess.run(
        [sys.executable, VERIFY, catalogue_path,
         "--root", root,
         # Explicit, so a `dist/` in the working directory cannot quietly
         # become the answer for a test that never mentioned one.
         "--staged", staged,
         # The other checks compare against indexes this fixture has none of.
         "--satellite", os.path.join(root, "none.json"),
         "--routing", os.path.join(root, "none.json"),
         "--trips", os.path.join(root, "none.json"),
         "--published", ""],
        capture_output=True, text=True, encoding="utf-8", errors="replace")


def write_pack(root, body):
    path = os.path.join(root, "trips", "gb.tbtrips")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(body)
    return path


def sha_of(body):
    import hashlib
    return hashlib.sha256(body).hexdigest()


def test_a_matching_pack_passes():
    root = tempfile.mkdtemp()
    try:
        body = b'{"trips": []}\n'
        write_pack(root, body)
        cat = os.path.join(root, "catalogue.json")
        with open(cat, "w", encoding="utf-8") as f:
            json.dump(catalogue_with({
                "id": "gb-trips", "kind": "trips", "label": "Trips",
                "file": "trips/gb.tbtrips",
                "sha256": sha_of(body), "bytes": len(body),
            }), f)
        r = run_verify(root, cat)
        check("a pack that matches its file is published", r.returncode == 0,
              r.stdout + r.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_wrong_hash_is_refused():
    root = tempfile.mkdtemp()
    try:
        body = b'{"trips": []}\n'
        write_pack(root, body)
        cat = os.path.join(root, "catalogue.json")
        with open(cat, "w", encoding="utf-8") as f:
            json.dump(catalogue_with({
                "id": "gb-trips", "kind": "trips", "label": "Trips",
                "file": "trips/gb.tbtrips",
                # The exact shipped failure: the hash of the OTHER copy.
                "sha256": sha_of(body.replace(b"\n", b"\r\n")),
                "bytes": len(body),
            }), f)
        r = run_verify(root, cat)
        check("a pack whose hash does not match its file is refused",
              r.returncode != 0, r.stdout + r.stderr)
        check("and it says which pack and what it found",
              "gb-trips" in (r.stdout + r.stderr), r.stdout + r.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_wrong_length_is_refused():
    root = tempfile.mkdtemp()
    try:
        body = b'{"trips": []}\n'
        write_pack(root, body)
        cat = os.path.join(root, "catalogue.json")
        with open(cat, "w", encoding="utf-8") as f:
            json.dump(catalogue_with({
                "id": "gb-trips", "kind": "trips", "label": "Trips",
                "file": "trips/gb.tbtrips",
                "sha256": sha_of(body), "bytes": len(body) + 243,
            }), f)
        r = run_verify(root, cat)
        check("a pack whose length does not match its file is refused",
              r.returncode != 0, r.stdout + r.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_pack_git_would_rewrite_is_refused():
    """The one that actually happened, caught one step earlier.

    The size-and-hash check above compares the catalogue with the working
    tree, and on the machine that CAUSES this they agree with each other - the
    same run wrote both. What goes wrong is that git stores something else.
    """
    root = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init", "-q", root], capture_output=True)
        subprocess.run(["git", "-C", root, "config", "core.autocrlf", "true"],
                       capture_output=True)
        # CRLF in the working tree, which git with autocrlf will store as LF.
        body = b'{"trips": []}\r\n'
        write_pack(root, body)
        cat = os.path.join(root, "catalogue.json")
        with open(cat, "w", encoding="utf-8") as f:
            json.dump(catalogue_with({
                "id": "gb-trips", "kind": "trips", "label": "Trips",
                "file": "trips/gb.tbtrips",
                "sha256": sha_of(body), "bytes": len(body),
            }), f)

        r = run_verify(root, cat)
        check("a pack git would rewrite on the way in is refused",
              r.returncode != 0, r.stdout + r.stderr)

        # And marking the extension binary is the fix, not a suppression: the
        # bytes stop moving, so the check stops objecting.
        with open(os.path.join(root, ".gitattributes"), "w") as f:
            f.write("*.tbtrips -text\n")
        r = run_verify(root, cat)
        check("and `-text` in .gitattributes settles it", r.returncode == 0,
              r.stdout + r.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_dropped_traffic_orders_are_refused():
    """The kind whose absence is a wrong answer, not a missing feature.

    A rider without imagery still has a map. A rider without traffic orders is
    shown a map with no closures on it, which looks exactly like a map where
    nothing is closed. So a rebuild that loses this pack must fail loudly.
    """
    with tempfile.TemporaryDirectory() as root:
        tro = {
            "id": "gb-tro", "kind": "tro", "label": "Traffic orders",
            "file": "https://example.test/tro/gb-tro.tbpack",
            "sha256": "a" * 64, "bytes": 3751859,
        }
        was = os.path.join(root, "published.json")
        with open(was, "w", encoding="utf-8") as fh:
            json.dump(catalogue_with(tro), fh)

        # The same catalogue with the orders gone - which is what a rebuild
        # that forgot --tro produces.
        lanes = {
            "id": "gb-midlands-motor", "kind": "lanes", "label": "Lanes",
            "file": "packages/gb-midlands-motor.tbpack",
            "sha256": sha_of(b"x"), "bytes": 1,
        }
        write_pack(root, b"x")
        now = os.path.join(root, "catalogue.json")
        with open(now, "w", encoding="utf-8") as fh:
            json.dump(catalogue_with(lanes), fh)

        done = subprocess.run(
            [sys.executable, VERIFY, now, "--root", root,
             "--satellite", os.path.join(root, "none.json"),
             "--routing", os.path.join(root, "none.json"),
             "--trips", os.path.join(root, "none.json"),
             "--published", was],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        check("a dropped traffic-orders pack is refused",
              done.returncode != 0 and "traffic orders" in done.stdout.lower()
              + done.stderr.lower(),
              done.stdout + done.stderr)


def test_the_staged_file_is_what_gets_checked():
    """A run is publishing dist/; the published copy is last build's.

    This refused 129 correct container packs. The check read the file at the
    PUBLISHED path - last month's bytes - and compared it against this build's
    catalogue. Packs had hidden it for as long as it existed: their builds are
    reproducible, so unchanged data gives byte-identical packs and the two
    copies agree. Containers are not reproducible yet, so every one of them
    mismatched, on a build whose data was perfectly good.
    """
    root = tempfile.mkdtemp()
    try:
        published = b"what we served last month"
        staged_body = b"what this run actually built, and will serve"

        # The published copy, at the path the catalogue names.
        write_pack(root, published)
        # The staged copy, where a run builds before the publish step moves it.
        staged = os.path.join(root, "dist")
        os.makedirs(os.path.join(staged, "trips"), exist_ok=True)
        with open(os.path.join(staged, "trips", "gb.tbtrips"), "wb") as f:
            f.write(staged_body)

        cat = os.path.join(root, "catalogue.json")
        with open(cat, "w") as f:
            json.dump(catalogue_with({
                "id": "gb-trips",
                "kind": "trips",
                "label": "Trips",
                "file": "trips/gb.tbtrips",
                "sha256": sha_of(staged_body),
                "bytes": len(staged_body),
            }), f)

        done = run_verify(root, cat, staged=staged)
        check("the staged file is what gets checked",
              done.returncode == 0,
              done.stdout + done.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_and_a_staged_file_that_is_wrong_is_still_refused():
    """The check must still be able to come back red."""
    root = tempfile.mkdtemp()
    try:
        staged_body = b"what this run built"
        # The published copy matches the catalogue; the staged one does not.
        # Reading the wrong copy would call this build good.
        write_pack(root, staged_body)
        staged = os.path.join(root, "dist")
        os.makedirs(os.path.join(staged, "trips"), exist_ok=True)
        with open(os.path.join(staged, "trips", "gb.tbtrips"), "wb") as f:
            f.write(b"a truncated or corrupted build")

        cat = os.path.join(root, "catalogue.json")
        with open(cat, "w") as f:
            json.dump(catalogue_with({
                "id": "gb-trips",
                "kind": "trips",
                "label": "Trips",
                "file": "trips/gb.tbtrips",
                "sha256": sha_of(staged_body),
                "bytes": len(staged_body),
            }), f)

        done = run_verify(root, cat, staged=staged)
        check("a wrong staged file is still refused",
              done.returncode != 0
              and "do not match" in (done.stdout + done.stderr),
              done.stdout + done.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_with_nothing_staged_the_published_file_still_answers():
    """The ordinary case for a job that is not mid-publish."""
    root = tempfile.mkdtemp()
    try:
        body = b"published and staged are the same thing here"
        write_pack(root, body)
        cat = os.path.join(root, "catalogue.json")
        with open(cat, "w") as f:
            json.dump(catalogue_with({
                "id": "gb-trips",
                "kind": "trips",
                "label": "Trips",
                "file": "trips/gb.tbtrips",
                "sha256": sha_of(body),
                "bytes": len(body),
            }), f)
        done = run_verify(root, cat, staged=os.path.join(root, "no-such-dist"))
        check("with nothing staged the published file still answers",
              done.returncode == 0,
              done.stdout + done.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d tests" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

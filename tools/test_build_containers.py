"""The clock must not reach a container.

WHAT THIS IS GUARDING. `build_containers.py` stamped every container with
`manifest["generated"]` - when THIS RUN wrote the manifest - so every container
was new on every run even when no council had amended anything. Measured on two
CI builds of unchanged data with one SQLite: motor-wales.tbmap differed in five
bytes out of 1,114,112, `built_at` moving 21:47:46Z -> 22:52:13Z. Five bytes
move the sha256, so every rider re-downloads all 343 MB. It also refused a
publish: the catalogue check compares each pack against the file on disk, and
129 containers disagreed with what had been published an hour earlier.

A pack carries its own `generated`, and build_packages keeps that stamp when the
lanes have not changed - the whole mechanism that makes packs rebuild
byte-identical. Containers have to inherit it.

WHY THIS READS THE SOURCE rather than building. Building a container needs the
pack key and real sealed packs, which a unit test has no business requiring -
the same reason test_sign_release.py reads the source to prove the private key
can never be passed on a command line. The behaviour itself was verified by
building: same packs with run stamps an hour apart gave seven byte-identical
containers, and moving one pack's own stamp changed that pack's container and
its overview and nothing else.
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "build_containers.py")

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


def _tree():
    return ast.parse(io.open(SOURCE, encoding="utf-8").read())


def _calls_named(tree, name):
    """Every call to `<anything>.name(...)` or `name(...)`."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == name:
            out.append(node)
        elif isinstance(f, ast.Name) and f.id == name:
            out.append(node)
    return out


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def test_no_container_is_written_with_the_run_stamp():
    tree = _tree()
    calls = _calls_named(tree, "write_container")
    check("write_container is still called", len(calls) >= 2,
          "found %d" % len(calls))
    for call in calls:
        used = set()
        for arg in call.args:
            used |= _names_in(arg)
        for kw in call.keywords:
            used |= _names_in(kw.value)
        check("write_container at line %d does not take the run stamp"
              % call.lineno,
              "run_stamp" not in used,
              "a container stamped with the clock is new every month")


def test_the_entries_do_not_carry_the_run_stamp_either():
    # The entry's `generated` reaches the catalogue. Stamping it with the run
    # makes the catalogue disagree with the container it describes.
    tree = _tree()
    for call in _calls_named(tree, "_entry"):
        for kw in call.keywords:
            if kw.arg == "generated":
                check("_entry at line %d does not take the run stamp"
                      % call.lineno,
                      "run_stamp" not in _names_in(kw.value))


def test_the_pack_stamp_is_what_is_read():
    src = io.open(SOURCE, encoding="utf-8").read()
    check("the pack's own generated is read",
          'pack.get("generated")' in src,
          "nothing reads the stamp build_packages preserved")


def test_the_run_stamp_still_describes_the_manifest():
    # It is not wrong, it just belongs on the manifest and nowhere else:
    # nothing hashes that field.
    src = io.open(SOURCE, encoding="utf-8").read()
    check("the manifest still records when it was written",
          'manifest.get("generated")' in src)


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d checks" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

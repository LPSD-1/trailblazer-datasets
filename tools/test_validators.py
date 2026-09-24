"""The three validator selftests, inside the glob that CI and check.py run.

WHY THIS FILE EXISTS. `validate_container.py`, `validate_changeset.py` and
`validate_catalogue.py` each carry a `--selftest` that builds a good artefact,
corrupts it a dozen-odd ways and requires every corruption to be refused. That
is the whole evidence for step 0.12. Nothing ran any of them.

Not CI - `grep -rn "validate_" .github/workflows/` returned nothing in either
repository. Not the one-command runner - `greenroadmap-app/tool/check.py`
globs `tools/test_*.py`, and no test file called one. So the three selftests
sat in the tree being correct at nobody.

Two of them had in fact stopped working entirely, and for a whole phase nobody
could have known:

  * `validate_container.py --selftest` died with FileNotFoundError on
    `motor-south-west.tbmap` - the vehicle-partitioned name step 1.2 deleted.
  * `validate_changeset.py --selftest` died with "unable to open database
    file" for the same reason, one layer down.

And behind those, four defects they were supposed to have caught: the
container validator read every post-pivot container through the compat VIEW
and never looked at `way_uid`, `legal_tier` or `access_evidence`; its
write-corruptions could not be applied to a view at all; its geometry and id
checks were written `if table == "lanes"` and skipped `ways`; and
`build_changeset.py` did not know `ways` either, so NO changeset could be
built for any container the pipeline now produces - which is the mechanism the
4x-daily refresh rests on.

The lesson is the wiring, not the bugs. A check that runs nowhere decays at
exactly the speed of the code around it, and says nothing while it does. This
file is named `test_*` so the existing glob picks it up and no future
validator needs a line adding anywhere.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: Each is a whole proof, not an assertion: a good artefact accepted and every
#: corruption refused. Their exit code is the reading.
VALIDATORS = (
    "validate_container.py",
    "validate_changeset.py",
    "validate_catalogue.py",
)


def run(name):
    done = subprocess.run(
        [sys.executable, os.path.join(HERE, name), "--selftest"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=os.path.dirname(HERE))
    tail = [line for line in (done.stdout or "").splitlines() if line.strip()]
    return done.returncode, (tail[-1] if tail else ""), done


def main():
    failed = []
    for name in VALIDATORS:
        code, last, done = run(name)
        if code == 0:
            print("  %-26s %s" % (name, last))
        else:
            failed.append(name)
            print("  %-26s FAILED (exit %d)" % (name, code))
            for line in (done.stdout or "").splitlines()[-6:]:
                print("      %s" % line)
            for line in (done.stderr or "").splitlines()[-6:]:
                print("      %s" % line)

    if failed:
        print("\nFAILED: %s" % ", ".join(failed))
        return 1
    print("validators: all %d selftests passed" % len(VALIDATORS))
    return 0


if __name__ == "__main__":
    sys.exit(main())

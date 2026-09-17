"""Build one small region end to end and run every publish guard, locally.

WHY. A change to the pipeline was only ever validated by dispatching the real
workflow: twenty-five minutes, most of it rebuilding data the change did not
touch. Two runs in one afternoon were spent discovering faults this would have
caught in under a minute:

  * the overview floor chooser measured only the CANDIDATE zoom, so it offered
    a build carrying a 535 kB z5 tile against a 512 kB ceiling - and the
    publish guard refused it, correctly, after twenty minutes of building
  * the overviews were published without `legalBasis`, which the APP's own test
    caught against a catalogue that had already gone out

    python tools/smoke.py                 # motor: 7 containers, ~14 MB
    python tools/smoke.py --vehicle foot  # the dense one, when that matters

It uses the packs already committed here, so it needs no fetch and no network.
The pack key is read the same way the real build reads it.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def run(args, **kw):
    return subprocess.run([sys.executable] + args, capture_output=True,
                          text=True, cwd=ROOT, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicle", default="motor",
                    help="which vehicle's packs to build (default: motor, the "
                         "smallest and the app's actual audience)")
    ap.add_argument("--keep", action="store_true",
                    help="leave the built containers behind for inspection")
    args = ap.parse_args()

    key = os.environ.get("DATASET_KEY_FILE")
    if not key or not os.path.isfile(key):
        sys.exit("set DATASET_KEY_FILE to the pack key: the packs are sealed "
                 "and cannot be read without it")

    manifest_path = os.path.join(ROOT, "manifest.json")
    if not os.path.isfile(manifest_path):
        sys.exit("no manifest.json: this reads the packs already committed here")

    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    packages = [p for p in manifest.get("packages", [])
                if p.get("package") == args.vehicle]
    if not packages:
        sys.exit("no %s packs in the manifest" % args.vehicle)

    work = tempfile.mkdtemp(prefix="tb-smoke-")
    small = os.path.join(work, "manifest.json")
    with open(small, "w", encoding="utf-8") as fh:
        json.dump(dict(manifest, packages=packages), fh)

    out = os.path.join(work, "containers")
    print("building %d %s packs into containers..." % (len(packages), args.vehicle))
    built = run(["tools/build_containers.py", "--manifest", small,
                 "--root", ROOT, "--out", out, "--key", key])
    if built.returncode != 0:
        sys.stdout.write(built.stdout[-3000:])
        sys.stderr.write(built.stderr[-3000:])
        sys.exit("the container build failed")
    for line in built.stdout.strip().splitlines():
        print("  " + line)

    print("\nchecking them, with the guard the publish uses...")
    # FILES, not the directory. The guard takes container paths -
    # handed a folder it tries to open it as a database and says only
    # "unable to open database file", which reads like a corrupt build
    # rather than a wrong argument.
    containers = sorted(glob.glob(os.path.join(out, "*.tbmap")))
    if not containers:
        sys.exit("the build produced no containers")
    checked = run(["tools/check_containers.py"] + containers)
    sys.stdout.write(checked.stdout)
    if checked.returncode != 0:
        sys.stderr.write(checked.stderr)
        sys.exit("THE GUARD REFUSED THIS BUILD - it would have failed the "
                 "publish, twenty-five minutes in")

    # The floors, which is the number the guard and the builder disagreed about.
    with open(os.path.join(out, "manifest.json"), encoding="utf-8") as fh:
        for entry in json.load(fh)["containers"]:
            if entry.get("kind") == "overview":
                print("  overview floor: z%s" % entry.get("minZoom"))

    if not args.keep:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    else:
        print("\nleft in %s" % work)
    print("\nok")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Delete the withdrawn 2024 imagery from the `satellite` release, by asset id.

THE OWNER RUNS THIS, BY HAND, WHEN NO SATELLITE RUN IS ACTIVE. No workflow
calls it. It is a dry run unless --apply is given.

    # 1. The list: every asset the index's `withdrawn` ledger vouches for.
    python tools/delete_withdrawn_assets.py --list > withdrawn-ids.txt

    # 2. Read it. Then a dry run of exactly that list:
    python tools/delete_withdrawn_assets.py --ids withdrawn-ids.txt

    # 3. Only then:
    python tools/delete_withdrawn_assets.py --ids withdrawn-ids.txt --apply

HOW IT CANNOT TOUCH A 2016 ASSET
--------------------------------
- It deletes by numeric release asset id (DELETE .../releases/assets/<id>),
  never by name, and nothing here matches a name against a pattern.
- The ids to delete are the ones in --ids, and every one of them must be on
  the list imagery_withdrawn.deletion_list() generates at that moment from
  satellite/index.json and the release as GitHub lists it. One id off the
  list and the whole run is refused, with nothing deleted.
- That list takes an asset only when one ledger record vouches for both its
  name and its sha256 digest, on a recorded layer off the allowlist, with a
  hash among the 14 withdrawn on 9 Oct 2026. A 2016 pack uploaded under a
  2024 pack's name has other bytes and a new id.
- --apply always reads the release live, with `gh api`, so a saved listing
  can neither go stale nor be edited into saying something else.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import imagery_withdrawn as iw  # noqa: E402

REPO = "LPSD-1/trailblazer-datasets"
RELEASE_API = "repos/%s/releases/tags/%s" % (REPO, iw.SATELLITE_RELEASE)
ASSET_API = "repos/%s/releases/assets/%d"


def gh(cmd):
    """Run `gh` and return its stdout; any failure raises."""
    return subprocess.run(cmd, check=True, capture_output=True,
                          text=True).stdout


def read_ids(path):
    """Asset ids, one per line, first word; '#' lines are comments."""
    ids = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            words = line.split()
            if not words or words[0][:1] == "#":
                continue
            if not words[0].isdigit():
                raise ValueError("%s:%d: %r is not an asset id"
                                 % (path, n, words[0]))
            ids.append(int(words[0]))
    if not ids:
        raise ValueError("%s holds no asset ids" % path)
    if len(set(ids)) != len(ids):
        raise ValueError("%s names an asset id twice" % path)
    return ids


def main(argv=None, run=gh):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index",
                    default=os.path.join(ROOT, "satellite", "index.json"),
                    help="the index whose `withdrawn` ledger vouches for "
                         "each asset")
    ap.add_argument("--release-json",
                    help="a saved `gh api %s` listing, for --list or a dry "
                         "run only; --apply always reads the release live"
                         % RELEASE_API)
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--list", action="store_true",
                      help="print the generated list of ids, one per line")
    what.add_argument("--ids", help="file of asset ids to delete, each of "
                                    "which must be on the generated list")
    ap.add_argument("--apply", action="store_true",
                    help="really delete; without it nothing is deleted")
    args = ap.parse_args(argv)

    if args.apply and args.release_json:
        print("refused: --apply reads the release live; drop --release-json",
              file=sys.stderr)
        return 2
    if args.apply and not args.ids:
        print("refused: --apply needs --ids", file=sys.stderr)
        return 2

    try:
        with open(args.index, encoding="utf-8") as f:
            index = json.load(f)
        if args.release_json:
            with open(args.release_json, encoding="utf-8") as f:
                release = json.load(f)
        else:
            release = json.loads(run(["gh", "api", RELEASE_API]))
        listed = {d["id"]: d for d in iw.deletion_list(index, release)}
        wanted = read_ids(args.ids) if args.ids else None
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2

    if args.list:
        print("# %d withdrawn asset(s) in the %s release: id, name, layer"
              % (len(listed), iw.SATELLITE_RELEASE))
        for d in sorted(listed.values(), key=lambda d: d["id"]):
            print("%d  %s  %s  sha256:%s" % (d["id"], d["name"], d["layer"],
                                             d["sha256"]))
        return 0

    stray = [i for i in wanted if i not in listed]
    if stray:
        print("refused: not on the generated list of withdrawn assets, so "
              "nothing is deleted: %s" % ", ".join(str(i) for i in stray),
              file=sys.stderr)
        return 2

    if not args.apply:
        print("DRY RUN: nothing is deleted. With --apply this deletes "
              "%d asset(s):" % len(wanted))
        for i in wanted:
            d = listed[i]
            print("  would delete %d  %s  %s" % (i, d["name"], d["layer"]))
        return 0

    for i in wanted:
        d = listed[i]
        print("deleting %d  %s  %s" % (i, d["name"], d["layer"]))
        run(["gh", "api", "-X", "DELETE", ASSET_API % (REPO, i)])
    print("deleted %d asset(s)" % len(wanted))
    return 0


if __name__ == "__main__":
    sys.exit(main())

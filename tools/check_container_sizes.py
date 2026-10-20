#!/usr/bin/env python3
"""Refuse to publish a map container that suddenly shrank, swelled or emptied.

    python tools/check_container_sizes.py --published catalogue.json \\
        --new dist/catalogue.json [--override tools/container_size_override.json]

Exit 0: every container is within its limits. Exit 1: at least one is not, or
an index could not be read; refresh-data.yml then publishes nothing. Each
finding is one line starting REFUSED, naming the container and both numbers.

THE CONTAINERS ARE WHAT A RIDER DOWNLOADS (containers/ways-<region>.tbmap and
the overview), and a republished container REPLACES the good copy on every
phone. tools/check_build.py judges the packages the containers are built from;
nothing compared the containers themselves with what is published, so a build
step that truncated one, or filtered one to nothing, would publish cleanly.

WHAT IT COMPARES, per container id, the new catalogue entry against the entry
in the catalogue that is published now: `bytes` (the file size) and
`featureCount` (the lanes it holds).

  * zero lanes                  - always refused
  * a container that vanished   - refused
  * a fall beyond MAX_BYTES_DROP or MAX_FEATURES_DROP
  * a rise beyond MAX_GROWTH, in either number

THE LIMITS COME FROM THE PUBLISHED HISTORY. Every change to a container in
catalogue.json's git history since the cutover to containers (24 Sep to 20 Oct
2026: 23 changes, each comparing the same file to itself):

  * largest fall in lanes:  -26.7%, gb-south-east-ways, 3954 -> 2899, on
    3 Oct 2026 (c825e54e), when the build began joining a byway's pieces into
    one lane (17fc4dd). Legitimate, and the largest by far; -19.3% (north),
    the same day, is next.
  * largest fall in bytes:  -2.9%, the same container the same day.
  * largest rise:           +37.5% in bytes (gb-overview, 8 Oct, 74e21409);
    lanes never rose more than +0.7%.

So a lane fall of 40% is half as much again as the largest real one; a byte
fall of 25% is eight times the largest real one, and a container is mostly
tiles, which move far less than the lane count; and doubling either number is
2.7 times the largest real rise.

A DELIBERATE CHANGE IS NAMED, NOT FORCED. tools/container_size_override.json:

    {"containers": {"gb-north-ways": {
        "against_sha256": "<sha256 of the published entry it excuses>",
        "reason": "why this container may move this much"}}}

It excuses that one container and stops applying the moment the published
entry is no longer the one it names, so it cannot be left behind to excuse a
later fault. "against_sha256": null excuses a container not yet published.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OVERRIDE = os.path.join(HERE, "container_size_override.json")

MAX_FEATURES_DROP = 0.40
MAX_BYTES_DROP = 0.25
MAX_GROWTH = 2.0


def containers(catalogue):
    """{id: entry} for every map container (.tbmap) the catalogue lists.

    The overview appears under `overviews` and again inside each lane area;
    both copies describe one file, so they collapse to one id.
    """
    out = {}
    packs = list(catalogue.get("overviews") or [])
    for cont in catalogue.get("continents") or []:
        for country in cont.get("countries") or []:
            for area in country.get("areas") or []:
                packs.extend(area.get("packs") or [])
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        if pack.get("format") != "tbmap" and not str(
                pack.get("file") or "").endswith(".tbmap"):
            continue
        out.setdefault(pack.get("id"), pack)
    return out


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def judge(cid, old, new):
    """The reasons one container may not publish, or []."""
    problems = []
    if new is None:
        return ["it is published now and the new build has no %s at all"
                % cid]
    n_bytes, n_feat = _number(new.get("bytes")), _number(new.get("featureCount"))
    if n_feat is None or n_feat <= 0:
        problems.append("it holds %r lanes" % (new.get("featureCount"),))
    if n_bytes is None or n_bytes <= 0:
        problems.append("its size is %r bytes" % (new.get("bytes"),))
    if old is None or problems:
        return problems
    o_bytes, o_feat = _number(old.get("bytes")), _number(old.get("featureCount"))
    for label, was, now, drop in (("lanes", o_feat, n_feat, MAX_FEATURES_DROP),
                                  ("bytes", o_bytes, n_bytes, MAX_BYTES_DROP)):
        if not was or was <= 0:
            continue  # nothing published to compare with
        change = (now - was) / was
        if change < -drop:
            problems.append("%s fell %.1f%% (%d -> %d); the limit is -%d%%"
                            % (label, -100 * change, was, now, 100 * drop))
        elif now > was * MAX_GROWTH:
            problems.append("%s rose %.1f%% (%d -> %d); the limit is x%g"
                            % (label, 100 * change, was, now, MAX_GROWTH))
    return problems


def load_override(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data.get("containers") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        raise ValueError("%s has no `containers` object" % path)
    for cid, entry in entries.items():
        if not isinstance(entry, dict) or "against_sha256" not in entry \
                or not str(entry.get("reason") or "").strip():
            raise ValueError("%s: %s needs `against_sha256` and a `reason`"
                             % (path, cid))
    return entries


def excused(cid, old, override):
    entry = override.get(cid)
    if not entry:
        return None
    published = old.get("sha256") if old else None
    if entry.get("against_sha256") != published:
        return None
    return entry["reason"]


def check(published, new, override):
    """(refused lines, note lines)."""
    old_c, new_c = containers(published), containers(new)
    refused, notes = [], []
    for cid in sorted(set(old_c) | set(new_c), key=str):
        problems = judge(cid, old_c.get(cid), new_c.get(cid))
        if not problems:
            continue
        reason = excused(cid, old_c.get(cid), override)
        if reason:
            notes.append("excused %s: %s - by %s: %s"
                         % (cid, "; ".join(problems),
                            "container_size_override.json", reason))
        else:
            refused.append("REFUSED %s: %s" % (cid, "; ".join(problems)))
    return refused, notes


def _read(path=None, rev=None):
    """A catalogue from a file, or from `git show REV:catalogue.json`."""
    if rev is not None:
        try:
            text = subprocess.run(
                ["git", "show", "%s:catalogue.json" % rev],
                capture_output=True, check=True, cwd=os.path.dirname(HERE),
            ).stdout.decode("utf-8")
        except (subprocess.CalledProcessError, UnicodeDecodeError) as exc:
            raise ValueError("git show %s:catalogue.json failed: %s"
                             % (rev, exc))
        data = json.loads(text)
        path = "%s:catalogue.json" % rev
    else:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % path)
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    have = ap.add_mutually_exclusive_group(required=True)
    have.add_argument("--published", help="the catalogue riders have now")
    have.add_argument("--published-rev", metavar="REV",
                      help="read it from git instead: REV:catalogue.json "
                           "(the Publish retry passes the new tip, HEAD)")
    ap.add_argument("--new", required=True, help="the catalogue to publish")
    ap.add_argument("--override", default=DEFAULT_OVERRIDE)
    args = ap.parse_args(argv)
    try:
        published = _read(args.published, args.published_rev)
        new = _read(args.new)
        override = load_override(args.override)
    except (OSError, ValueError) as exc:
        print("REFUSED: cannot judge the containers: %s" % exc)
        return 1
    if not containers(new):
        print("REFUSED: %s lists no map containers at all" % args.new)
        return 1
    refused, notes = check(published, new, override)
    for line in notes + refused:
        print(line)
    if refused:
        print("\n%d container(s) refused; nothing may publish. A deliberate "
              "change is named in tools/container_size_override.json."
              % len(refused))
        return 1
    print("%d containers within their limits" % len(containers(new)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

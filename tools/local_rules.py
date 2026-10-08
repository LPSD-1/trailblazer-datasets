#!/usr/bin/env python3
"""Local rules, per place: what a national park, a council or a scheme asks
of riders on its lanes, with where it says so.

    python tools/local_rules.py check          # validate local-rules/rules.json

WHY. The law over a lane is the definitive map, the highway record and the
traffic orders, and those reach the map through the ways and the orders. But
much of what a rider needs to know is local and is none of those: a national
park's code of conduct, a voluntary restraint on a fragile lane, a seasonal
policy, a council's own account of what its unsurfaced roads are like. The
owner asked on 8 October 2026 that "local rules are taken into account on a
per place basis". This file is where they are written down, each with the
official page it came from and the day it was checked, and every area
container carries the ones that apply to it (meta `local_rules`), so the
app can show the right one on a lane's sheet.

THE FILE: local-rules/rules.json, {"format": 1, "rules": [rule, ...]}.

    id          unique, lower case: "<authority code>-<what>"
    kind        national_park_scheme | voluntary_restraint | seasonal_policy
                | code_of_conduct | traffic_order | guidance
    effect      info        - for information, binds nobody
                voluntary   - a request, not the law
                legal_order - a legal order; give order_ref where the page
                              names it. It is CITED here, not enforced: an
                              order closes a lane on the map only through the
                              orders pipeline (build_tro.py, council_orders.py)
    title       short, for the sheet
    summary     one or two plain sentences for a rider
    applies_to  every key optional; every key given must match (AND):
                  authorities  [authority name as the containers spell it]
                  areas        [[west, south, east, north], ...] - any box
                  polygon      [[lon, lat], ...] - one outer ring
                  way_classes  ["boat", "ucr", ...]
                  way_uids     [...]
                A rule with no `applies_to` keys at all is refused: a rule for
                everywhere is not a local rule.
    order_ref   optional, the order's name or number
    season      optional {"from": "MM-DD", "to": "MM-DD"}, may run over the
                new year
    source      {"publisher", "url" (https), "checked" (YYYY-MM-DD),
                 "quote" (optional: the page's own words, so it can be
                 re-checked)}

ONLY WHAT AN OFFICIAL PAGE SAYS, READ POLITELY. Nothing is seeded from a host
tools/polite_http.py blocks (Dartmoor's, Exmoor's and the Peak District's
sites are behind bot challenges), and a page is never read by working round a
refusal. The national-park survey running separately drops its findings in as
more entries of the same shape.
"""
import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import polite_http  # noqa: E402

RULES_FILE = os.path.join(ROOT, "local-rules", "rules.json")

KINDS = ("national_park_scheme", "voluntary_restraint", "seasonal_policy",
         "code_of_conduct", "traffic_order", "guidance")
EFFECTS = ("info", "voluntary", "legal_order")
APPLIES = ("authorities", "areas", "polygon", "way_classes", "way_uids")
_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_MMDD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


class Invalid(ValueError):
    pass


def _box_ok(box):
    return (isinstance(box, list) and len(box) == 4
            and all(isinstance(v, (int, float)) for v in box)
            and -180 <= box[0] < box[2] <= 180
            and -90 <= box[1] < box[3] <= 90)


def validate(doc):
    """The rules, or Invalid naming the first fault and the rule it is in."""
    if not isinstance(doc, dict) or doc.get("format") != 1:
        raise Invalid("not a format 1 local rules file")
    rules = doc.get("rules")
    if not isinstance(rules, list):
        raise Invalid("`rules` is not a list")
    seen = set()
    for r in rules:
        rid = r.get("id") if isinstance(r, dict) else None

        def bad(why):
            raise Invalid("%s: %s" % (rid or "a rule with no id", why))
        if not isinstance(rid, str) or not _ID.match(rid):
            bad("id must be lower-case words joined by hyphens")
        if rid in seen:
            bad("id used twice")
        seen.add(rid)
        if r.get("kind") not in KINDS:
            bad("kind %r is not one of %s" % (r.get("kind"), ", ".join(KINDS)))
        if r.get("effect") not in EFFECTS:
            bad("effect %r is not one of %s"
                % (r.get("effect"), ", ".join(EFFECTS)))
        for key in ("title", "summary"):
            if not isinstance(r.get(key), str) or not r[key].strip():
                bad("no %s" % key)
        applies = r.get("applies_to")
        if not isinstance(applies, dict) or not applies:
            bad("no applies_to: a rule for everywhere is not a local rule")
        unknown = set(applies) - set(APPLIES)
        if unknown:
            bad("applies_to has unknown keys %s" % sorted(unknown))
        for key in ("authorities", "way_classes", "way_uids"):
            if key in applies and not (
                    isinstance(applies[key], list) and applies[key]
                    and all(isinstance(v, str) and v for v in applies[key])):
                bad("applies_to.%s must be a non-empty list of names" % key)
        if "areas" in applies and not (
                isinstance(applies["areas"], list) and applies["areas"]
                and all(_box_ok(b) for b in applies["areas"])):
            bad("applies_to.areas must be [west, south, east, north] boxes")
        if "polygon" in applies:
            ring = applies["polygon"]
            if not (isinstance(ring, list) and len(ring) >= 3 and all(
                    isinstance(p, list) and len(p) == 2
                    and all(isinstance(v, (int, float)) for v in p)
                    for p in ring)):
                bad("applies_to.polygon must be a ring of [lon, lat]")
        season = r.get("season")
        if season is not None and not (
                isinstance(season, dict) and _MMDD.match(season.get("from")
                                                         or "")
                and _MMDD.match(season.get("to") or "")):
            bad("season must be {from: MM-DD, to: MM-DD}")
        if "order_ref" in r and not isinstance(r["order_ref"], str):
            bad("order_ref must be text")
        src = r.get("source")
        if not isinstance(src, dict):
            bad("no source: every rule cites the page it came from")
        if not isinstance(src.get("publisher"), str) or \
                not src["publisher"].strip():
            bad("source has no publisher")
        if not isinstance(src.get("url"), str) or \
                not src["url"].startswith("https://"):
            bad("source.url must be an https:// page")
        try:
            datetime.date.fromisoformat(src.get("checked") or "")
        except (TypeError, ValueError):
            bad("source.checked must be the YYYY-MM-DD it was read")
        if polite_http.blocked(polite_http.host_of(src["url"])):
            bad("source.url is on a host polite_http blocks; it cannot have "
                "been read politely")
    return rules


def load(path=None):
    """The validated rules; [] when there is no file."""
    path = path or RULES_FILE
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return validate(json.load(fh))


def _boxes_meet(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _ring_box(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return [min(xs), min(ys), max(xs), max(ys)]


def for_container(rules, bounds, authorities, classes=None):
    """The rules that could apply to some way in a container.

    `bounds` is the container's "west,south,east,north" (or a 4-list),
    `authorities` the authorities it holds ways for, `classes` the way
    classes it holds (None: any). A rule is carried when every place key it
    gives could be met here; the app then decides lane by lane. Sorted by id
    so a rebuild writes the same bytes.
    """
    if isinstance(bounds, str):
        bounds = [float(v) for v in bounds.split(",")] if bounds else None
    held = set(authorities or ())
    out = []
    for r in rules:
        a = r["applies_to"]
        if "authorities" in a and not held & set(a["authorities"]):
            continue
        if classes is not None and "way_classes" in a and \
                not set(classes) & set(a["way_classes"]):
            continue
        boxes = list(a.get("areas") or [])
        if "polygon" in a:
            boxes.append(_ring_box(a["polygon"]))
        if boxes and (bounds is None
                      or not any(_boxes_meet(b, bounds) for b in boxes)):
            continue
        out.append(r)
    return sorted(out, key=lambda r: r["id"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["check"])
    ap.add_argument("--file", default=RULES_FILE)
    args = ap.parse_args(argv)
    try:
        rules = load(args.file)
    except (Invalid, ValueError) as e:
        print("::error::%s: %s" % (args.file, e))
        return 1
    print("%d local rules, every one valid" % len(rules))
    for r in rules:
        print("  %-36s %-12s %s" % (r["id"], r["effect"], r["source"]["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

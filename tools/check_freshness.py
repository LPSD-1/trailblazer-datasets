#!/usr/bin/env python3
"""Is anything we publish older than its own schedule says it can be?

    python tools/check_freshness.py                  # the checkout's published files
    python tools/check_freshness.py --now 2026-10-20T06:00:00Z --root DIR

Exit 0: every gated date is within its limit. Exit 1: at least one is not, or
a file it needs could not be read (a date nobody can read cannot show it is
fresh). Every finding is one line starting STALE, naming the kind and its age.

THREE FAILURES THIS EXISTS FOR, each of which went unseen for days:

  * The DfT D-TRO extract stopped moving on 6 September 2026. Every traffic
    order run stayed green for weeks; the only person who could see it was a
    rider reading the order date on a phone.
  * Data CI exited 2 ("BLIND") and the lane refresh stopped publishing from
    27 September to 2 October 2026. Nothing read the gap.
  * A cold cache once published an old traffic-order pack.

WHAT IT READS, all from this checkout (main is what Pages serves), with no
network at all:

  1. published/status.json `generated`, against status.yml's cron. If the
     status file stops moving, every other answer below is a stale snapshot.
  2. Every dataset in published/status.json: `last_ok`, the start of its
     workflow's newest successful run, against THAT workflow's cron (the
     workflow file is build_status.DATASETS'). This is the gate for every kind
     whose own published date does not move on a clock (below).
  3. The conditions feeds in catalogue.json (`asOf`), against refresh-data.yml's
     cron, which runs the conditions job.
  4. The traffic orders: tro/index.json `generated` (the applied-through day)
     and `cut` (the D-TRO extract day). See ORDER_DATA_LIMIT and KNOWN_STALLS.

THE LIMIT is the workflow's longest gap between two scheduled runs, read from
its `cron:` lines, plus a margin of max(that gap, MIN_MARGIN). One missed run
never alarms; two in a row do, and so does a dead workflow.

KINDS WHOSE PUBLISHED DATE IS NOT GATED HERE, and why (each is still gated by
its workflow's `last_ok` in 2):

  * lanes and the overview: `generated`/`waysCut` move only when a council's
    data changes. Measured from catalogue.json's history: up to 138 hours
    without a move. A cron limit on them would ring on quiet weeks.
  * height: `generated` is when the pack was built from a static Environment
    Agency source (13 Sep 2026); it is right for it never to move.
  * routing, names, trips: the catalogue carries no date for them at all.
  * satellite imagery: nothing is published (every pack withdrawn 9 Oct 2026).
"""
import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_status  # noqa: E402  (standard library only, like this file)

ROOT = os.path.dirname(HERE)

UTC = datetime.timezone.utc

#: The smallest margin on top of a workflow's cadence. Measured on main from
#: 5 to 20 Oct 2026, while everything was healthy: the status file went up to
#: 9.8 hours between commits (hourly cron; it commits only when something
#: other than its own time moved), and the wet feeds up to 14.1 hours
#: (six-hourly). Twelve hours clears both; a cadence-sized margin alone
#: (1 h, 6 h) would ring on an ordinary day.
MIN_MARGIN = datetime.timedelta(hours=12)

#: How old the traffic-order data dates may be. Their own clock, not the
#: workflow's: `generated` moves when D-TRO or a council publishes something
#: new, which a quiet weekend does not. Measured on main: generated sat at
#: 2026-10-08 until 12 Oct, four days, in a healthy week. Seven days is the
#: figure tools/check_build.py already uses for the same date
#: (MAX_ORDER_CUT_AGE_DAYS: "longer than the service's normal re-cut and far
#: shorter than" the 26 days that went unseen), imported so the two cannot
#: drift apart. Set by _order_limit() below.

#: A stall already known and already covered, excused for EXACTLY the date
#: recorded. The moment the value moves, the excuse no longer matches and the
#: gate is armed again, so a later stall of the same feed still rings.
KNOWN_STALLS = {
    ("orders", "cut", "2026-09-06"):
        "the DfT /dtros/all extract has served one cut since 6 Sep 2026; "
        "tools/tro_events.py lays the /events feed over it, and `generated` "
        "(gated above) is the date that moves",
}

#: How far ahead of the clock a date may be before it is treated as wrong. A
#: run stamp can lead a runner's clock by seconds, never by an hour; a date in
#: the future would otherwise pass as fresh for as long as it is ahead.
MAX_SKEW = datetime.timedelta(hours=1)

#: The workflow whose cron the status file and the conditions feeds keep.
STATUS_WORKFLOW = "status.yml"
CONDITIONS_WORKFLOW = "refresh-data.yml"


def _order_limit():
    try:
        import check_build
        return datetime.timedelta(days=check_build.MAX_ORDER_CUT_AGE_DAYS)
    except Exception:  # noqa: BLE001 - a missing constant must not hide staleness
        return datetime.timedelta(days=7)


ORDER_DATA_LIMIT = _order_limit()


# --------------------------------------------------------------------------
# cron
# --------------------------------------------------------------------------

def _field(text, low, high):
    """The set of values one cron field allows. ValueError if unreadable."""
    out = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise ValueError("bad step %r" % text)
            step = int(s)
        if part == "*":
            a, b = low, high
        elif re.match(r"^\d+-\d+$", part):
            a, b = (int(x) for x in part.split("-"))
        elif part.isdigit():
            a = b = int(part)
            if step != 1:
                b = high
        else:
            raise ValueError("unreadable cron field %r" % text)
        if a < low or b > high or a > b:
            raise ValueError("cron field %r out of range" % text)
        out.update(range(a, b + 1, step))
    return out


def cron_times(cron, start, days):
    """Every firing of one five-field cron in [start, start + days)."""
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError("cron %r does not have five fields" % cron)
    minutes = sorted(_field(parts[0], 0, 59))
    hours = sorted(_field(parts[1], 0, 23))
    doms = _field(parts[2], 1, 31)
    months = _field(parts[3], 1, 12)
    dows = {d % 7 for d in _field(parts[4], 0, 7)}  # 0 and 7 are Sunday
    dom_any, dow_any = parts[2] == "*", parts[4] == "*"
    out = []
    for n in range(days):
        day = start + datetime.timedelta(days=n)
        if day.month not in months:
            continue
        dow = (day.weekday() + 1) % 7  # cron: Sunday is 0
        dom_ok, dow_ok = day.day in doms, dow in dows
        # Cron's rule: when both day fields are restricted, either may match.
        if dom_any and dow_any:
            ok = True
        elif dom_any:
            ok = dow_ok
        elif dow_any:
            ok = dom_ok
        else:
            ok = dom_ok or dow_ok
        if not ok:
            continue
        for h in hours:
            for m in minutes:
                out.append(day.replace(hour=h, minute=m))
    return out


def cadence(crons):
    """The longest gap between two scheduled runs, over two whole years.

    None when there is no schedule. ValueError when a cron cannot be read:
    the caller treats that as a finding, never as "fresh".
    """
    if not crons:
        return None
    start = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    times = sorted(t for c in crons for t in cron_times(c, start, 730))
    if len(times) < 2:
        raise ValueError("cron %r fires fewer than twice in two years"
                         % (crons,))
    return max(b - a for a, b in zip(times, times[1:]))


_CRON_LINE = re.compile(
    r"""^\s*-\s*cron:\s*(?:'([^']*)'|"([^"]*)"|([^#'"\n]*?))\s*(?:#.*)?$""",
    re.M)


def workflow_crons(path):
    """The `cron:` strings in a workflow file, quoted or not.

    build_status.workflow_crons reads only quoted ones; YAML accepts both, and
    a cron this missed would read as "no schedule".
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    return [next(g for g in m.groups() if g is not None).strip()
            for m in _CRON_LINE.finditer(text)]


def limit_for(gap):
    return gap + max(gap, MIN_MARGIN)


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

def parse_when(value):
    """A UTC datetime from an ISO time or day, or None.

    A bare day ("2026-10-19") is read as the END of that day: it says the data
    runs through that day, and reading it as midnight would age it by up to a
    day it does not deserve.
    """
    if not isinstance(value, str):
        return None
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            d = datetime.datetime.strptime(value, "%Y-%m-%d")
            return d.replace(hour=23, minute=59, second=59, tzinfo=UTC)
        d = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        return None
    return d.astimezone(UTC)


def human(delta):
    hours = delta.total_seconds() / 3600
    if hours < 48:
        return "%.1f hours" % hours
    return "%.1f days" % (hours / 24)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.stale = []
        self.ok = []
        self.notes = []

    def judge(self, kind, field, value, limit, why, now):
        when = parse_when(value)
        if when is None:
            self.stale.append("STALE %s: %s is %r, which is not a date; it "
                              "cannot show it is fresh" % (kind, field, value))
            return
        # A bare day covers the whole day, so only its START can be early.
        earliest = when
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            earliest -= datetime.timedelta(hours=23, minutes=59, seconds=59)
        if earliest > now + MAX_SKEW:
            self.stale.append("STALE %s: %s %s is in the future (now %s); a "
                              "date that has not happened cannot show "
                              "anything is fresh" % (kind, field, value,
                                                     now.strftime(
                                                         "%Y-%m-%dT%H:%M:%SZ")))
            return
        age = now - when
        if age > limit:
            self.stale.append("STALE %s: %s %s is %s old; the limit is %s (%s)"
                              % (kind, field, value, human(age), human(limit),
                                 why))
        else:
            self.ok.append("ok    %s: %s %s is %s old (limit %s)"
                           % (kind, field, value, human(age), human(limit)))


def _workflow_limit(root, workflow, report, kind):
    """(limit, why) from a workflow's cron, or None after recording why not."""
    path = os.path.join(root, ".github", "workflows", workflow)
    if not os.path.exists(path):
        report.stale.append("STALE %s: its workflow %s is missing, so nothing "
                            "says how fresh it should be" % (kind, workflow))
        return None
    try:
        gap = cadence(workflow_crons(path))
    except ValueError as exc:
        report.stale.append("STALE %s: the schedule of %s cannot be read (%s)"
                            % (kind, workflow, exc))
        return None
    if gap is None:
        report.stale.append("STALE %s: %s has no schedule, so it no longer "
                            "runs on its own and nothing keeps it fresh"
                            % (kind, workflow))
        return None
    return limit_for(gap), "%s runs at least every %s" % (workflow, human(gap))


def check(root, now):
    report = Report()

    status = _read_json(os.path.join(root, "published", "status.json"))
    if not isinstance(status, dict):
        report.stale.append("STALE status: published/status.json cannot be "
                            "read, so no workflow's last good run is known")
        status = {}
    else:
        got = _workflow_limit(root, STATUS_WORKFLOW, report, "status")
        if got:
            report.judge("status", "generated", status.get("generated"),
                         got[0], got[1], now)

    datasets = {d.get("id"): d for d in status.get("datasets") or []
                if isinstance(d, dict)}
    for dataset_id, workflow, _name, _what in build_status.DATASETS:
        got = _workflow_limit(root, workflow, report, dataset_id)
        if not got:
            continue
        if status and dataset_id not in datasets:
            report.stale.append("STALE %s: published/status.json does not "
                                "list it" % dataset_id)
            continue
        if not status:
            continue
        report.judge(dataset_id, "last good run",
                     datasets[dataset_id].get("last_ok"), got[0], got[1], now)

    catalogue = _read_json(os.path.join(root, "catalogue.json"))
    if not isinstance(catalogue, dict):
        report.stale.append("STALE conditions: catalogue.json cannot be read")
    else:
        feeds = (catalogue.get("conditions") or {}).get("feeds") or []
        got = _workflow_limit(root, CONDITIONS_WORKFLOW, report, "conditions")
        if got:
            if not feeds:
                report.stale.append("STALE conditions: catalogue.json lists "
                                    "no conditions feeds")
            for feed in feeds:
                report.judge("conditions %s" % feed.get("id"), "asOf",
                             feed.get("asOf"), got[0], got[1], now)

    index = _read_json(os.path.join(root, "tro", "index.json"))
    if not isinstance(index, dict):
        report.stale.append("STALE orders: tro/index.json cannot be read")
    else:
        why = ("traffic-order data moves when an authority publishes; "
               "check_build.MAX_ORDER_CUT_AGE_DAYS")
        for field in ("generated", "cut"):
            value = index.get(field)
            excuse = KNOWN_STALLS.get(("orders", field, value))
            if excuse:
                report.notes.append("note  orders: %s %s is a known stall, "
                                    "excused for that date only: %s"
                                    % (field, value, excuse))
                continue
            report.judge("orders", field, value, ORDER_DATA_LIMIT, why, now)

    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=ROOT,
                    help="checkout to read (default: this repository)")
    ap.add_argument("--now", help="ISO time to judge at (default: the clock)")
    args = ap.parse_args(argv)
    if args.now:
        now = parse_when(args.now)
        if now is None:
            ap.error("--now %r is not an ISO time with a zone" % args.now)
    else:
        now = datetime.datetime.now(UTC)
    report = check(args.root, now)
    for line in report.ok + report.notes + report.stale:
        print(line)
    if report.stale:
        print("\n%d stale: see the STALE lines above" % len(report.stale))
        return 1
    print("\neverything gated is within its limit")
    return 0


if __name__ == "__main__":
    sys.exit(main())

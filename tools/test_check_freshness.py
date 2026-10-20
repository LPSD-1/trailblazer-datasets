#!/usr/bin/env python3
"""Tests for tools/check_freshness.py, on fixtures only.

    python tools/test_check_freshness.py

HERMETIC ON PURPOSE. The lane refresh runs every tools/test_*.py before it
publishes, so a test that read the real published files would stop lane
publishing whenever some other dataset went stale - the gate would become the
outage. Every case here builds its own checkout in a temporary directory.

The dates in the main case are the real ones from the lane outage: the last
good lane refresh started 2026-09-26T23:40:19Z and the next one did not succeed
until 2026-10-01T23:12:40Z (gh run list, refresh-data.yml).
"""
import contextlib
import datetime
import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_status          # noqa: E402
import check_freshness as F  # noqa: E402

UTC = datetime.timezone.utc

#: The real crons, copied so the fixture is a fair picture of the repository.
CRONS = {
    "refresh-data.yml": ["23 3,9,15,21 * * *"],
    "traffic-orders.yml": ["23 2,8,14,20 * * *"],
    "council-orders.yml": ["53 1,7,13,19 * * *"],
    "council-ways.yml": ["17 3 * * *"],
    "street-manager.yml": ["29 4 2 * *"],
    "order-register.yml": ["41 5 3 1,4,7,10 *"],
    "status-changes.yml": ["37 6 * * 1"],
    "height.yml": ["17 3 * * *"],
    "satellite.yml": ["41 2 * * *", "41 14 * * *"],
    "mirror-routing.yml": ["23 4 * * 2"],
    "status.yml": ["47 * * * *"],
}

NOW = datetime.datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def fresh_tree(root, now=NOW):
    """A checkout where everything is a little over an hour old."""
    wf = os.path.join(root, ".github", "workflows")
    os.makedirs(wf)
    for name, crons in CRONS.items():
        with open(os.path.join(wf, name), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("name: x\non:\n  schedule:\n")
            for c in crons:
                fh.write("    - cron: '%s'   # comment\n" % c)
            fh.write("  workflow_dispatch:\n")
    hour_ago = iso(now - datetime.timedelta(hours=1))
    status = {
        "schema": 1,
        "generated": hour_ago,
        "datasets": [{"id": d[0], "last_ok": hour_ago}
                     for d in build_status.DATASETS],
    }
    write(root, "published/status.json", status)
    write(root, "catalogue.json", {"schema": 2, "conditions": {"feeds": [
        {"id": "gb-wales-wet", "asOf": hour_ago},
        {"id": "gb-wales-rivers", "asOf": hour_ago}]}})
    write(root, "tro/index.json", {"generated": now.strftime("%Y-%m-%d"),
                                   "cut": now.strftime("%Y-%m-%d")})


def write(root, rel, obj):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh)


def read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return json.load(fh)


def set_last_ok(root, dataset_id, value):
    s = read(root, "published/status.json")
    for d in s["datasets"]:
        if d["id"] == dataset_id:
            d["last_ok"] = value
    write(root, "published/status.json", s)


def run(root, now=NOW):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = F.main(["--root", root, "--now", iso(now)])
    return rc, out.getvalue()


@contextlib.contextmanager
def tree(now=NOW):
    root = tempfile.mkdtemp(prefix="freshness-")
    try:
        fresh_tree(root, now)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def stale_lines(out):
    return [l for l in out.splitlines() if l.startswith("STALE")]


def test_cron_cadence():
    cases = [
        (["23 3,9,15,21 * * *"], datetime.timedelta(hours=6)),
        (["47 * * * *"], datetime.timedelta(hours=1)),
        (["17 3 * * *"], datetime.timedelta(days=1)),
        (["41 2 * * *", "41 14 * * *"], datetime.timedelta(hours=12)),
        (["23 4 * * 2"], datetime.timedelta(days=7)),
        # Monthly: the longest month gap is 31 days (e.g. 2 Jan -> 2 Feb).
        (["29 4 2 * *"], datetime.timedelta(days=31)),
        # Quarterly: 3 Oct -> 3 Jan is 92 days, the longest of the four.
        (["41 5 3 1,4,7,10 *"], datetime.timedelta(days=92)),
        (["0 */6 * * *"], datetime.timedelta(hours=6)),
        (["0 0-23/12 * * *"], datetime.timedelta(hours=12)),
        (["0 0 * * 7"], datetime.timedelta(days=7)),
        (["0 0 * * 1-5"], datetime.timedelta(days=3)),
        # Both day fields restricted: cron fires when EITHER matches.
        (["0 0 1 * 1"], datetime.timedelta(days=7)),
    ]
    for crons, want in cases:
        got = F.cadence(crons)
        check("cadence %s" % crons, got == want, "%s != %s" % (got, want))
    check("no schedule is None", F.cadence([]) is None)
    for bad in ("23 3 * *", "61 * * * *", "x * * * *", "0 0 31 2 *",
                "0 0 1-0 * *", "*/0 * * * *"):
        try:
            F.cadence([bad])
        except ValueError:
            check("unreadable %r refused" % bad, True)
        else:
            check("unreadable %r refused" % bad, False, "no ValueError")


def test_limit():
    check("6h cadence -> 18h", F.limit_for(datetime.timedelta(hours=6))
          == datetime.timedelta(hours=18))
    check("1h cadence -> 13h", F.limit_for(datetime.timedelta(hours=1))
          == datetime.timedelta(hours=13))
    check("1d cadence -> 2d", F.limit_for(datetime.timedelta(days=1))
          == datetime.timedelta(days=2))
    check("12h cadence -> 24h", F.limit_for(datetime.timedelta(hours=12))
          == datetime.timedelta(hours=24))


def test_parse_when():
    check("day is its end", F.parse_when("2026-10-19")
          == datetime.datetime(2026, 10, 19, 23, 59, 59, tzinfo=UTC))
    check("Z time", F.parse_when("2026-10-19T09:10:54Z")
          == datetime.datetime(2026, 10, 19, 9, 10, 54, tzinfo=UTC))
    check("offset time to UTC", F.parse_when("2026-10-19T10:10:54+01:00")
          == datetime.datetime(2026, 10, 19, 9, 10, 54, tzinfo=UTC))
    for bad in (None, "", "yesterday", 20261019, "2026-10-19T09:10:54",
                "2026-13-01"):
        check("not a date: %r" % (bad,), F.parse_when(bad) is None)


def test_all_fresh_passes():
    with tree() as root:
        rc, out = run(root)
        check("fresh tree exits 0", rc == 0, out)
        check("fresh tree has no STALE", not stale_lines(out), out)
        # Every dataset was actually judged, not skipped.
        for d in build_status.DATASETS:
            check("judged %s" % d[0], ("ok    %s: last good run" % d[0])
                  in out, out)
        check("status file judged", "ok    status: generated" in out, out)
        check("orders generated judged", "ok    orders: generated" in out)
        check("orders cut judged", "ok    orders: cut" in out)
        check("both feeds judged", out.count("ok    conditions ") == 2, out)


def test_the_lane_outage_rings():
    """27 Sep 2026: no good lane run since 26 Sep 23:40."""
    last_good = "2026-09-26T23:40:19Z"
    for hours, want in ((17.5, 0), (18.5, 1), (5 * 24, 1)):
        now = datetime.datetime(2026, 9, 26, 23, 40, 19, tzinfo=UTC) \
            + datetime.timedelta(hours=hours)
        with tree(now) as root:
            set_last_ok(root, "lanes", last_good)
            rc, out = run(root, now)
            check("lanes %sh after last good -> rc %d" % (hours, want),
                  rc == want, out)
            lines = stale_lines(out)
            if want:
                check("one STALE line at %sh" % hours, len(lines) == 1, lines)
                check("names lanes and its age at %sh" % hours,
                      lines and lines[0].startswith("STALE lanes: last good "
                                                    "run " + last_good)
                      and "old; the limit is 18.0 hours" in lines[0], lines)


def test_every_dataset_is_gated_by_its_own_cron():
    for dataset_id, workflow, _n, _w in build_status.DATASETS:
        limit = F.limit_for(F.cadence(CRONS[workflow]))
        for extra, want in ((-datetime.timedelta(minutes=5), 0),
                            (datetime.timedelta(minutes=5), 1)):
            with tree() as root:
                # Age = limit + extra: five minutes inside, then outside.
                set_last_ok(root, dataset_id, iso(NOW - limit - extra))
                rc, out = run(root)
                check("%s at limit%+dm -> %d" % (
                    dataset_id, extra.total_seconds() // 60, want),
                    rc == want, stale_lines(out))
                if want:
                    check("%s named" % dataset_id, any(
                        l.startswith("STALE %s:" % dataset_id)
                        for l in stale_lines(out)), out)


def test_missing_last_ok_is_stale():
    with tree() as root:
        set_last_ok(root, "routing", None)
        rc, out = run(root)
        check("null last_ok rings", rc == 1 and any(
            l.startswith("STALE routing:") and "not a date" in l
            for l in stale_lines(out)), out)


def test_dataset_missing_from_status_is_stale():
    with tree() as root:
        s = read(root, "published/status.json")
        s["datasets"] = [d for d in s["datasets"] if d["id"] != "height"]
        write(root, "published/status.json", s)
        rc, out = run(root)
        check("unlisted dataset rings", rc == 1 and any(
            l.startswith("STALE height:") for l in stale_lines(out)), out)


def test_status_file_itself():
    with tree() as root:
        s = read(root, "published/status.json")
        s["generated"] = iso(NOW - datetime.timedelta(hours=13, minutes=5))
        write(root, "published/status.json", s)
        rc, out = run(root)
        check("status 13h05m old rings", rc == 1 and any(
            l.startswith("STALE status: generated") for l in stale_lines(out)),
            out)
    with tree() as root:
        s = read(root, "published/status.json")
        s["generated"] = iso(NOW - datetime.timedelta(hours=12, minutes=55))
        write(root, "published/status.json", s)
        rc, out = run(root)
        check("status 12h55m old passes", rc == 0, out)
    with tree() as root:
        os.remove(os.path.join(root, "published", "status.json"))
        rc, out = run(root)
        check("missing status file rings", rc == 1 and any(
            l.startswith("STALE status:") for l in stale_lines(out)), out)
    with tree() as root:
        with open(os.path.join(root, "published", "status.json"), "w") as fh:
            fh.write("{not json")
        rc, out = run(root)
        check("unreadable status file rings", rc == 1, out)


def test_orders():
    for field in ("generated", "cut"):
        for days, want in ((6, 0), (8, 1)):
            with tree() as root:
                idx = read(root, "tro/index.json")
                idx[field] = (NOW - datetime.timedelta(days=days)) \
                    .strftime("%Y-%m-%d")
                write(root, "tro/index.json", idx)
                rc, out = run(root)
                check("orders %s %d days -> %d" % (field, days, want),
                      rc == want, out)
                if want:
                    check("orders %s named" % field, any(
                        l.startswith("STALE orders: %s" % field)
                        for l in stale_lines(out)), out)
    with tree() as root:
        os.remove(os.path.join(root, "tro", "index.json"))
        rc, out = run(root)
        check("missing order index rings", rc == 1 and any(
            l.startswith("STALE orders:") for l in stale_lines(out)), out)
    with tree() as root:
        idx = read(root, "tro/index.json")
        del idx["generated"]
        write(root, "tro/index.json", idx)
        rc, out = run(root)
        check("undated order index rings", rc == 1, out)


def test_known_stall_is_excused_for_its_date_only():
    now = datetime.datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    with tree(now) as root:
        idx = read(root, "tro/index.json")
        idx["cut"] = "2026-09-06"
        write(root, "tro/index.json", idx)
        rc, out = run(root, now)
        check("the recorded stall is excused", rc == 0, out)
        check("and says so", "known stall" in out, out)
    with tree(now) as root:
        idx = read(root, "tro/index.json")
        idx["cut"] = "2026-09-07"   # moved once, then stalled again
        write(root, "tro/index.json", idx)
        rc, out = run(root, now)
        check("a later stall rings", rc == 1 and any(
            l.startswith("STALE orders: cut 2026-09-07")
            for l in stale_lines(out)), out)
    with tree(now) as root:
        idx = read(root, "tro/index.json")
        idx["generated"] = "2026-09-06"   # the excuse is for `cut` only
        write(root, "tro/index.json", idx)
        rc, out = run(root, now)
        check("the excuse does not cover generated", rc == 1, out)


def test_conditions_feeds():
    for hours, want in ((17.9, 0), (18.1, 1)):
        with tree() as root:
            c = read(root, "catalogue.json")
            c["conditions"]["feeds"][1]["asOf"] = iso(
                NOW - datetime.timedelta(hours=hours))
            write(root, "catalogue.json", c)
            rc, out = run(root)
            check("rivers feed %sh -> %d" % (hours, want), rc == want, out)
            if want:
                check("feed named", any(
                    l.startswith("STALE conditions gb-wales-rivers: asOf")
                    for l in stale_lines(out)), out)
    with tree() as root:
        c = read(root, "catalogue.json")
        c["conditions"]["feeds"] = []
        write(root, "catalogue.json", c)
        rc, out = run(root)
        check("no feeds at all rings", rc == 1, out)
    with tree() as root:
        os.remove(os.path.join(root, "catalogue.json"))
        rc, out = run(root)
        check("missing catalogue rings", rc == 1, out)


def test_workflow_problems():
    with tree() as root:
        os.remove(os.path.join(root, ".github", "workflows", "height.yml"))
        rc, out = run(root)
        check("missing workflow rings", rc == 1 and any(
            l.startswith("STALE height:") and "missing" in l
            for l in stale_lines(out)), out)
    with tree() as root:
        with open(os.path.join(root, ".github", "workflows", "height.yml"),
                  "w", encoding="utf-8", newline="\n") as fh:
            fh.write("on:\n  schedule:\n    - cron: '17 3 * *'\n")
        rc, out = run(root)
        check("unreadable cron rings", rc == 1 and any(
            l.startswith("STALE height:") and "cannot be read" in l
            for l in stale_lines(out)), out)
    with tree() as root:
        # A workflow taken off its schedule is said, and not judged.
        with open(os.path.join(root, ".github", "workflows", "height.yml"),
                  "w", encoding="utf-8", newline="\n") as fh:
            fh.write("on:\n  workflow_dispatch:\n")
        set_last_ok(root, "height", "2020-01-01T00:00:00Z")
        rc, out = run(root)
        check("unscheduled workflow is a note, not stale", rc == 0
              and "note  height:" in out, out)


def test_bad_now_is_refused():
    with tree() as root:
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                F.main(["--root", root, "--now", "2026-10-20T06:00:00"])
        except SystemExit as exc:
            check("naive --now refused", exc.code == 2, exc.code)
        else:
            check("naive --now refused", False, "no SystemExit")


def test_the_workflow_runs_it_and_rings():
    """freshness.yml: scheduled and on demand, red on stale, one issue."""
    root = os.path.dirname(HERE)
    path = os.path.join(root, ".github", "workflows", "freshness.yml")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        check("freshness.yml exists", False, exc)
        return
    crons = build_status.workflow_crons(path)
    check("it is scheduled", bool(crons), crons)
    try:
        gap = F.cadence(crons)
    except ValueError as exc:
        gap = None
        check("its cron reads", False, exc)
    check("it runs at least every six hours", gap is not None
          and gap <= datetime.timedelta(hours=6), gap)
    check("it can be run on demand",
          re.search(r"^  workflow_dispatch:", text, re.M) is not None)
    check("it runs the check", "python tools/check_freshness.py" in text)
    check("it runs the tests first", -1 < text.find(
        "python tools/test_check_freshness.py") < text.find(
        "python tools/check_freshness.py"))
    check("the check's exit code reaches the step",
          'exit "${PIPESTATUS[0]}"' in text)
    check("never continue-on-error", "continue-on-error" not in text)
    check("an issue is raised on failure", "if: failure()" in text
          and "gh issue create --label data-stale" in text)
    check("one issue, not one per run", "--label data-stale --state open"
          in text and 'if [ -n "$EXISTING" ]' in text)
    check("stood down on success", "if: success()" in text
          and "gh issue close" in text)
    check("least privilege", "contents: read" in text
          and "contents: write" not in text and "secrets." not in text)
    check("no pull_request_target", not re.search(
        r"^\s*pull_request_target\s*:", text, re.M))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                _failed.append("%s raised %r" % (name, exc))
    for f in _failed:
        print("FAIL", f)
    print("%d passed, %d failed" % (_passed, len(_failed)))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())

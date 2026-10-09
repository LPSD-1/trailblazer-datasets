#!/usr/bin/env python3
"""The public data status file: where our data comes from, and how fresh it is.

    python tools/build_status.py                  # writes published/status.json
    python tools/build_status.py --out /tmp/s.json --no-api

Served by GitHub Pages at <baseUrl>published/status.json, beside the
catalogue (which names it as `status`). The app's "Where our data comes from"
screen and the help site's data-status page both read it. The format is
schema 1, fixed by the app: every key is always present, unknowns are null.

WHO READS IT. Riders and the public, so every string in it is plain English
for a rider. It holds NO PERSONAL DATA - no officer names, no e-mail
addresses - and nothing that could read as criticism of a council: no due
dates, no "overdue", no "failed". Praise is allowed (`honours`); blame is not.
D-TRO is said neutrally, in `dtro` (DTRO_YES / DTRO_NOT_YET): never
"dormant" or "inactive" - a council not on D-TRO yet has broken no rule, as
publishing to it is not yet required of anyone.
Every string here comes from a short list of fields we write ourselves (the
council table, the source names in council_sources.SOURCES and
council_ways.LAYERS, the council name on a UCR layer, the register's council
name); nothing is copied out of a council's records, and `public_check`
refuses to write a file with an "@", a phone number, a postcode or a word
of blame (FORBIDDEN) anywhere in it.

WHAT IT IS BUILT FROM, all committed except the run results:
  * tools/tro_authorities.csv - every highway authority. Rows whose note says
    "no byways" (National Highways, the Welsh Government, TfL) are left out.
  * the published ways containers, through byway_match.load_byways - the
    byways (BOATs) and unsurfaced roads (UCRs) riders actually have, and
    each way's `source` column, which says whether it came from rowmaps.com,
    from the council's own byway layer, or from its highway records.
  * tro/council/status.json and council_sources.SOURCES - the councils'
    own closure and order feeds, and their licences.
  * council-ways/status.json and council_ways.LAYERS - councils' own byway
    layers; council-ucrs/<CODE>.json and status.json - their UCR layers.
  * tro/register/orders.json and pages.json - the order register; only
    entries a person approved count.
  * tro/publishers.json - how many records each authority has published to
    D-TRO, written by build_tro.py beside tro/index.json. Missing, every
    `dtro_records` is null: unknown, never zero.
  * the GitHub REST API - each data workflow's ten most recent runs. Behind
    `github_runs`, which the tests replace; with no answer, a result is
    "unknown" and the last known times are kept from the previous file.

WHERE A LANE NAME COUNTS. The ways carry rowmaps' authority name
(`lane_names` in the table). A name one row lists is that row's. A name
several rows list belongs to the row that lists it ALONE where there is one
- a National Park Authority, whose records rowmaps files under the park,
though each council whose ground it covers lists the park too (build_tro
needs that for orders) - and otherwise to every row that lists it: "Cumbria",
which rowmaps still files lanes under, is counted under both of its
successors, exactly as the council table says.

AS_OF FOR ROWMAPS IS NULL, on purpose. When we last fetched a rowmaps file
is recorded only in cache/rowmaps-checks.json, which is gitignored and lives
in the lane job's Actions cache; this job never has it. A way's
`source_date` is not it either: it is the day the lane data was last CUT
(build_packages.sealed_at), which moves whenever any way in the region does.
A date we cannot vouch for is worse than none.

WRITTEN ONLY WHEN SOMETHING BUT `generated` MOVED (`write_if_changed`), so
the hourly job commits nothing on a quiet hour - and `generated` therefore
says when the picture last changed, which is what "Last updated" means.
"""
import argparse
import calendar
import csv
import datetime
import glob
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "published", "status.json")
SCHEMA = 1
REPO = "lpsd-1/trailblazer-datasets"
RUNS_API = ("https://api.github.com/repos/%s/actions/workflows/%s/runs"
            "?per_page=10")

# The data workflows a rider is shown, in the order the schema fixes. `name`
# and `what` are written for a rider; the schedule is read from the workflow
# file itself (`schedule_text`), so it cannot drift from what actually runs.
# Any workflow not listed here is not shown.
DATASETS = [
    ("lanes", "refresh-data.yml", "Byways and unsurfaced roads",
     "The lanes on the map, from councils' rights of way and highway "
     "records"),
    ("orders", "traffic-orders.yml", "Traffic orders and closures",
     "Legal orders and closures on the lanes, from the Department for "
     "Transport's D-TRO service and councils' own notices"),
    ("council-closures", "council-orders.yml", "Council closures",
     "Closures and orders read from councils' own websites and maps"),
    ("council-roads", "council-ways.yml", "Unsurfaced roads from councils",
     "Byways and unsurfaced roads read straight from councils' own maps"),
    ("street-works", "street-manager.yml", "Street works",
     "Road closures for street works, from the Department for Transport's "
     "Street Manager"),
    ("order-lists", "order-register.yml", "Councils' lists of orders",
     "Long-standing and seasonal byway orders that councils list on their "
     "websites, each checked by a person"),
    ("status-changes", "status-changes.yml", "Changes to the map",
     "Planning Inspectorate decisions on byways, applications to change the "
     "map, and military firing times"),
    ("height", "height.yml", "Ground height",
     "Hill shading and 3D ground, from Environment Agency height data"),
    ("imagery", "satellite.yml", "Satellite imagery",
     "Satellite photos of the ground, from the 2017 Sentinel-2 cloudless "
     "mosaic"),
    ("routing", "mirror-routing.yml", "Route planning",
     "The road and track network routes are planned on, from OpenStreetMap "
     "via BRouter"),
]

# The 22 Welsh principal councils and the Welsh National Park Authority that
# appears in the council table, by `display_name`. Everything else in the
# table is in England (the Welsh Government row has no byways and is left
# out). tools/test_build_status.py holds this to the GeoPlace code ranges:
# every 68xx and 69xx row is here, and nothing else with a code.
WALES = frozenset([
    "Isle of Anglesey County Council", "Gwynedd Council",
    "City of Cardiff Council", "Ceredigion County Council",
    "Carmarthenshire County Council", "Denbighshire County Council",
    "Flintshire County Council", "Monmouthshire County Council",
    "Pembrokeshire County Council", "Powys County Council",
    "City and County of Swansea Council", "Conwy County Borough Council",
    "Blaenau Gwent County Borough Council", "Bridgend County Borough Council",
    "Caerphilly County Borough Council",
    "Merthyr Tydfil County Borough Council",
    "Neath Port Talbot County Borough Council", "Newport City Council",
    "Rhondda Cynon Taf County Borough Council",
    "Torfaen County Borough Council", "Vale of Glamorgan Council",
    "Wrexham County Borough Council",
    "Bannau Brycheiniog National Park Authority",
])

# The honours, schema 1. Praise only, and only for two or more (MIN_HONOURS):
# one alone is too easy to earn by accident of how we happen to read a
# council. More criteria are added later; the strings are shown as written.
OPEN_DATA = "Shares its own data openly"
OGL = "Open Government Licence"
DTRO = "Publishes to D-TRO"
MIN_HONOURS = 2

# Each authority's `dtro`: a neutral label, never a judgement. Null where we
# cannot say (no tro/publishers.json yet, or a National Park with no code of
# its own): a gap in our records is never a statement about a council.
DTRO_YES = DTRO
DTRO_NOT_YET = "Not on D-TRO yet"

RESULTS = ("ok", "problem", "running", "unknown")


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _day(value):
    """The YYYY-MM-DD a timestamp starts with, or None."""
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return None


def dtro_label(records):
    """The neutral D-TRO label for a record count, or None if unknown."""
    if records is None:
        return None
    return DTRO_YES if records > 0 else DTRO_NOT_YET


def _is_ogl(licence):
    text = (licence or "").strip().lower()
    return "open government licence" in text or text.startswith("ogl")


# ------------------------------------------------------------ authorities

# Short names where stripping the usual words would say something else:
# "City of London Corporation" is the City, not London.
_SHORT = {"City of London Corporation": "City of London"}
_SUFFIXES = (" Metropolitan Borough Council",
             " Metropolitan District Council", " County Borough Council",
             " Borough Council", " County Council", " City Council",
             " Council", " Corporation", " Authority")
_PREFIXES = ("Council of the ", "London Borough of ", "Royal Borough of ",
             "City and County of ", "City of ")


def short_name(full):
    """"Devon" for "Devon County Council"; the name riders call it."""
    if full in _SHORT:
        return _SHORT[full]
    name = full
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    for prefix in _PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix):]
            break
    return name


def country_of(full):
    return "Wales" if full in WALES else "England"


def kind_of(full):
    return "park" if full.endswith("National Park Authority") else "council"


def load_table(path):
    """The council table's rows, less those with no byways at all."""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            full = (row.get("display_name") or "").strip()
            if not full:
                continue
            if "no byways" in (row.get("note") or "").lower():
                continue
            code = (row.get("swa_code") or "").strip()
            rows.append({
                "swa": (code.lstrip("0") or "0") if code else None,
                "full_name": full,
                "lanes": [n.strip() for n in
                          (row.get("lane_names") or "").split(";")
                          if n.strip()],
            })
    return rows


def lane_owners(rows):
    """{lane name: [row index]} - see WHERE A LANE NAME COUNTS above."""
    listed = {}
    for i, row in enumerate(rows):
        for lane in row["lanes"]:
            listed.setdefault(lane, []).append(i)
    owners = {}
    for lane, idx in listed.items():
        alone = [i for i in idx if rows[i]["lanes"] == [lane]]
        owners[lane] = alone if len(idx) > 1 and len(alone) == 1 else idx
    return owners


# ------------------------------------------------------------------ ways

def way_sources(pattern):
    """{way_uid: source} from the regional containers' own columns.

    byway_match.load_byways carries no source, and it is the one thing that
    says whether a byway came from rowmaps or from the council's own layer.
    The overview is skipped for the reason load_byways skips it.
    """
    out = {}
    for path in sorted(p for p in glob.glob(pattern)
                       if not p.endswith("ways-overview.tbmap")):
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                               uri=True)
        try:
            tables = set(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"))
            for table in ("ways", "ucr_ways"):
                if table not in tables:
                    continue
                for uid, source in conn.execute(
                        "SELECT way_uid, source FROM %s" % table):
                    out[uid] = source
        finally:
            conn.close()
    return out


def load_ways(pattern=None):
    """[(lane name, way_class, source)] for every way riders have."""
    import byway_match
    pattern = pattern or os.path.join(ROOT, "containers", "ways-*.tbmap")
    byways = byway_match.load_byways(pattern)
    sources = way_sources(pattern)
    return [(w.authority, w.way_class, sources.get(w.uid) or "")
            for w in byways.ways.values()]


# --------------------------------------------------------------- datasets

def workflow_crons(path):
    """The `cron:` strings in a workflow file, read as text (no YAML here)."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    return re.findall(r"""^\s*-\s*cron:\s*['"]([^'"]+)['"]""", text, re.M)


def _count(field):
    if field == "*":
        return None
    total = 0
    for part in field.split(","):
        m = re.match(r"^\*/(\d+)$", part)
        if m:
            return None if 24 % int(m.group(1)) else 24 // int(m.group(1))
        if not re.match(r"^\d+$", part):
            return None
        total += 1
    return total


def schedule_text(crons):
    """A rider's words for how often a workflow runs: "Every 6 hours"."""
    if not crons:
        return "When needed"
    fields = [c.split() for c in crons]
    if any(len(f) != 5 for f in fields):
        return "On a schedule"
    if all(f[2] == "*" and f[3] == "*" and f[4] == "*" for f in fields):
        if any(f[1] == "*" for f in fields):
            return "Every hour"
        counts = [_count(f[1]) for f in fields]
        if None in counts:
            return "On a schedule"
        per_day = sum(counts)
        if per_day == 1:
            return "Daily"
        if per_day == 2:
            return "Twice a day"
        if 24 % per_day == 0:
            return "Every %d hours" % (24 // per_day)
        return "%d times a day" % per_day
    if all(f[2] == "*" and f[3] == "*" and f[4] != "*" for f in fields):
        return "Weekly"
    if all(f[2] != "*" and f[4] == "*" for f in fields):
        months = set()
        for f in fields:
            if f[3] == "*":
                return "Monthly"
            months.update(f[3].split(","))
        return {1: "Yearly", 2: "Every 6 months",
                4: "Every 3 months"}.get(len(months), "On a schedule")
    return "On a schedule"


def result_of(run):
    """A workflow run as ok, problem, running or unknown.

    GitHub puts a run's progress in `status` and its outcome in
    `conclusion`; a queued or running run has no conclusion yet."""
    status = run.get("status")
    if status in ("in_progress", "queued"):
        return "running"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "ok"
    if conclusion in ("failure", "timed_out", "startup_failure"):
        return "problem"
    return "unknown"


def _run_time(run):
    return run.get("run_started_at") or run.get("created_at")


def summarise_runs(runs, previous=None):
    """(last_run, result, last_ok) from a workflow's newest runs.

    `runs` None is "the API did not answer": the result is unknown, and the
    times are the previous file's, which are still true. A last_ok older
    than the ten runs read is likewise kept from the previous file.
    """
    previous = previous or {}
    if runs is None:
        return (previous.get("last_run"), "unknown",
                previous.get("last_ok"))
    runs = sorted((r for r in runs if _run_time(r)), key=_run_time,
                  reverse=True)
    if not runs:
        return previous.get("last_run"), "unknown", previous.get("last_ok")
    ok = [r for r in runs if result_of(r) == "ok"]
    last_ok = _run_time(ok[0]) if ok else previous.get("last_ok")
    return _run_time(runs[0]), result_of(runs[0]), last_ok


def github_runs(workflow_file, token=None, timeout=30):
    """The workflow's ten newest runs, newest first, or None if GitHub did
    not answer. Read-only; the token only lifts the rate limit."""
    req = urllib.request.Request(RUNS_API % (REPO, workflow_file), headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "trailblazer-datasets status (build_status.py)",
    })
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            body = json.load(fh)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # The reason only, never the request: it carries the token.
        print("::warning::runs of %s not read: %s"
              % (workflow_file, getattr(e, "reason", e.__class__.__name__)))
        return None
    runs = body.get("workflow_runs") if isinstance(body, dict) else None
    return runs if isinstance(runs, list) else None


def _newest(values):
    days = [d for d in (_day(v) for v in values) if d]
    return max(days) if days else None


def data_as_of(dataset_id, root):
    """The date of the data itself, where one is recorded; else None.

    Not where it is not: the satellite mosaic is 2017 and the height data
    older still, and `generated` on those packs is when WE built them -
    the same conflation evidence_age.py refuses. The order register's
    transcriptions carry no read date at all.
    """
    if dataset_id == "lanes":
        manifest = _read_json(os.path.join(root, "containers",
                                           "manifest.json"), {}) or {}
        return _newest(c.get("waysCut") or c.get("generated")
                       for c in manifest.get("containers") or []
                       if c.get("dataset") == "ways")
    if dataset_id == "orders":
        index = _read_json(os.path.join(root, "tro", "index.json"), {}) or {}
        return _day(index.get("generated"))
    if dataset_id == "council-closures":
        status = _read_json(os.path.join(root, "tro", "council",
                                         "status.json"), {}) or {}
        return _newest(v.get("last_ok") for v in status.values()
                       if isinstance(v, dict))
    if dataset_id == "council-roads":
        days = []
        for name in ("council-ways", "council-ucrs"):
            status = _read_json(os.path.join(root, name, "status.json"),
                                {}) or {}
            days += [v.get("last_ok") for v in status.values()
                     if isinstance(v, dict)]
        return _newest(days)
    if dataset_id == "street-works":
        # Street Manager is read as whole monthly archives, so the data runs
        # to the last day of the newest month read.
        sm = _read_json(os.path.join(root, "tro", "streetworks", "orders",
                                     "street-manager.json"), {}) or {}
        months = sorted(m for m in sm.get("months") or []
                        if isinstance(m, str) and re.match(r"^\d{4}-\d{2}$",
                                                           m))
        if not months:
            return None
        year, month = map(int, months[-1].split("-"))
        return "%04d-%02d-%02d" % (year, month,
                                   calendar.monthrange(year, month)[1])
    return None


def build_datasets(root, runs_for, previous):
    before = dict((d.get("id"), d) for d in previous.get("datasets") or []
                  if isinstance(d, dict))
    out = []
    for dataset_id, workflow, name, what in DATASETS:
        crons = workflow_crons(os.path.join(root, ".github", "workflows",
                                            workflow))
        last_run, result, last_ok = summarise_runs(
            runs_for(workflow), before.get(dataset_id))
        out.append({
            "id": dataset_id,
            "name": name,
            "what": what,
            "schedule": schedule_text(crons),
            "last_run": last_run,
            "result": result,
            "last_ok": last_ok,
            "data_as_of": data_as_of(dataset_id, root),
        })
    return out


# ---------------------------------------------------------------- sources

def default_closure_sources():
    import council_sources
    return council_sources.by_id()


def default_byway_layers():
    import council_ways
    return council_ways.LAYERS


def _closure_what(name):
    low = name.lower()
    if "closure" in low:
        return "Closures"
    if "order" in low or re.search(r"\bt?tros?\b", low):
        return "Traffic orders"
    return "Notices"


def build_authorities(root, rows, ways, closure_sources, byway_layers):
    owners = lane_owners(rows)
    per_row = [dict(boat=0, ucr=0, byway_from={}, ucr_seen=False,
                    sources=[], open=False, ogl=False) for _ in rows]

    # The ways, each counted once under every owner of its lane name.
    unplaced = {}
    for lane, way_class, source in ways:
        idx = owners.get(lane)
        if not idx:
            unplaced[lane] = unplaced.get(lane, 0) + 1
            continue
        for i in idx:
            acc = per_row[i]
            if way_class == "boat":
                acc["boat"] += 1
                key = (lane, source.split(":", 1)[0])
                acc["byway_from"][key] = acc["byway_from"].get(key, 0) + 1
            elif way_class == "ucr":
                acc["ucr"] += 1
    if unplaced:
        print("::warning::ways whose authority no table row lists, counted "
              "under nobody: %s" % ", ".join(
                  "%s (%d)" % kv for kv in sorted(unplaced.items())))

    # BYWAYS, by where each lane name's ways came from.
    ways_status = _read_json(os.path.join(root, "council-ways",
                                          "status.json"), {}) or {}
    layers = dict((l.get("council"), l) for l in byway_layers)
    for i, row in enumerate(rows):
        acc = per_row[i]
        for (lane, kind), _n in sorted(acc["byway_from"].items()):
            alone = owners.get(lane) == [i]
            if kind == "council":
                layer = layers.get(row["full_name"]) or {}
                last = (ways_status.get(layer.get("code")) or {})
                acc["sources"].append({
                    "what": "Byways", "from": row["full_name"],
                    "as_of": _day(last.get("last_ok"))})
                acc["open"] = True
                acc["ogl"] = acc["ogl"] or _is_ogl(layer.get("licence"))
            else:
                acc["sources"].append({
                    "what": "Byways",
                    "from": "%s, via rowmaps.com"
                            % (row["full_name"] if alone else lane),
                    "as_of": None})

    # UNSURFACED ROADS, from the councils' own highway records.
    ucr_status = _read_json(os.path.join(root, "council-ucrs",
                                         "status.json"), {}) or {}
    for path in sorted(glob.glob(os.path.join(root, "council-ucrs",
                                              "*.json"))):
        if os.path.basename(path) == "status.json":
            continue
        source = (_read_json(path, {}) or {}).get("source") or {}
        code = source.get("code")
        last_ok = _day((ucr_status.get(code) or {}).get("last_ok"))
        if not last_ok:
            continue   # never read successfully: nothing of it is ours
        for i in _own(rows, owners, source.get("authority"),
                      source.get("council")):
            acc = per_row[i]
            acc["sources"].append({
                "what": "Unsurfaced roads",
                "from": source.get("council") or rows[i]["full_name"],
                "as_of": last_ok})
            acc["open"] = True
            acc["ogl"] = acc["ogl"] or _is_ogl(source.get("licence"))

    # CLOSURES AND ORDERS, from the councils' own feeds. Only `name`,
    # `authority` and `last_ok` of a status entry are read: its `error` text
    # is a server's words, never ours to publish.
    closures = _read_json(os.path.join(root, "tro", "council",
                                       "status.json"), {}) or {}
    for sid in sorted(closures):
        entry = closures[sid]
        if not isinstance(entry, dict):
            continue
        last_ok = _day(entry.get("last_ok"))
        if not last_ok:
            continue
        known = closure_sources.get(sid) or {}
        name = known.get("name") or entry.get("name") or ""
        for i in _own(rows, owners, known.get("authority")
                      or entry.get("authority"), name):
            acc = per_row[i]
            acc["sources"].append({"what": _closure_what(name),
                                   "from": name, "as_of": last_ok})
            acc["open"] = True
            acc["ogl"] = acc["ogl"] or _is_ogl(known.get("licence"))

    # THE ORDER REGISTER: a council whose page gave us an order a person
    # approved. Matched on the council's own name, so a National Park's page
    # about a council's ground is not counted as the council's.
    orders = _read_json(os.path.join(root, "tro", "register",
                                     "orders.json"), []) or []
    approved = set(o.get("council") for o in orders
                   if isinstance(o, dict) and o.get("status") == "approved")
    for i, row in enumerate(rows):
        if row["full_name"] in approved:
            per_row[i]["sources"].append({
                "what": "Traffic orders",
                "from": "%s - byway orders listed on its website"
                        % row["full_name"],
                "as_of": None})
            per_row[i]["open"] = True

    # D-TRO.
    publishers = _read_json(os.path.join(root, "tro", "publishers.json"))
    dtro = None
    if isinstance(publishers, dict):
        dtro = dict((str(a.get("swa")), a)
                    for a in publishers.get("authorities") or []
                    if isinstance(a, dict))

    out = []
    for i, row in enumerate(rows):
        acc = per_row[i]
        records = None
        if dtro is not None and row["swa"] in dtro:
            got = dtro[row["swa"]].get("records")
            records = got if isinstance(got, int) else None
        sources = list(acc["sources"])
        if records:
            sources.append({
                "what": "Traffic orders",
                "from": "%s, via the Department for Transport's D-TRO "
                        "service" % row["full_name"],
                "as_of": _day(dtro[row["swa"]].get("newest"))})
        reasons = [r for r, met in ((OPEN_DATA, acc["open"]),
                                    (OGL, acc["ogl"]),
                                    (DTRO, bool(records))) if met]
        out.append({
            "name": short_name(row["full_name"]),
            "full_name": row["full_name"],
            "country": country_of(row["full_name"]),
            "kind": kind_of(row["full_name"]),
            "byways": acc["boat"],
            "unsurfaced_roads": acc["ucr"],
            "dtro_records": records,
            "dtro": dtro_label(records),
            "sources": sources,
            "honours": reasons if len(reasons) >= MIN_HONOURS else [],
            "activity": [],
        })
    out.sort(key=lambda a: (a["country"], a["name"].lower()))
    return out


def _own(rows, owners, lane, name):
    """The rows a council's own source speaks for: the owners of its lane
    name, narrowed to the one whose name it carries where several own it."""
    idx = owners.get(lane) or []
    if len(idx) > 1 and name:
        named = [i for i in idx if name.startswith(rows[i]["full_name"])]
        if named:
            return named
    return idx


# ------------------------------------------------------------------ notes

def load_notes(path):
    """The hand-kept notes, newest first, at most ten.

    HAND-KEPT, NOT GIT LOG. The commit subjects here are written for
    whoever maintains the pipeline ("polite_http: robots.txt asked
    plainly"), and most are a bot's "Traffic orders: data cut ..." four
    times a day; rewritten by rule they become noise or, worse, a claim
    about a council nobody checked. A CI checkout is also one commit deep,
    so there is no log to read. A note is written when riders would notice
    the change, in their words.
    """
    notes = _read_json(path, []) or []
    good = [{"date": n["date"], "text": n["text"]} for n in notes
            if isinstance(n, dict) and _day(n.get("date")) == n.get("date")
            and isinstance(n.get("text"), str) and n["text"].strip()]
    good.sort(key=lambda n: n["date"], reverse=True)
    return good[:10]


# ------------------------------------------------------------------ build

def build(root, runs_for, ways, closure_sources, byway_layers, previous=None,
          now=None, notes_path=None):
    previous = previous or {}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rows = load_table(os.path.join(root, "tools", "tro_authorities.csv"))
    authorities = build_authorities(root, rows, ways, closure_sources,
                                    byway_layers)
    status = {
        "schema": SCHEMA,
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datasets": build_datasets(root, runs_for, previous),
        "authorities": authorities,
        "honours": [{"name": a["name"], "reasons": list(a["honours"])}
                    for a in authorities if a["honours"]],
        "notes": load_notes(notes_path or os.path.join(
            root, "tools", "status_notes.json")),
    }
    public_check(status)
    return status


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            for s in _strings(v):
                yield s
    elif isinstance(value, list):
        for v in value:
            for s in _strings(v):
                yield s


# What must never be published, whatever a source carried. A UK phone
# number (01392 000000, 07700 900123, +44 20 7946 0000) and a postcode
# (EX2 4QD, SW1A 1AA). The words are blame, which the file never deals in.
PHONE = re.compile(r"(?:\+44\s?\(?0?\)?\s?|\b0)\d{2,4}[\s-]?\d{3,4}"
                   r"(?:[\s-]?\d{3,4})?\b")
POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)
FORBIDDEN = re.compile(r"\b(?:dormant|inactive|overdue|failed)\b", re.I)


def public_check(status):
    """Refuse to publish what must never be: an e-mail address (anything
    with an "@"), a phone number, a postcode, a word of blame, or a result
    outside the four the app knows."""
    for text in _strings(status):
        if "@" in text:
            raise ValueError("REFUSING TO PUBLISH: %r looks like an e-mail "
                             "address" % text[:80])
        for pattern, what in ((PHONE, "a phone number"),
                              (POSTCODE, "a postcode"),
                              (FORBIDDEN, "a word of blame")):
            if pattern.search(text):
                raise ValueError("REFUSING TO PUBLISH: %r holds %s"
                                 % (text[:80], what))
    for d in status["datasets"]:
        if d["result"] not in RESULTS:
            raise ValueError("REFUSING TO PUBLISH: result %r" % d["result"])


def same_but_generated(a, b):
    """True when two status files differ in `generated` at most."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    strip = lambda d: dict((k, v) for k, v in d.items() if k != "generated")
    return strip(a) == strip(b)


def write_if_changed(path, status):
    """Write `status` unless only `generated` differs from what is there.
    Returns True when the file was written."""
    if same_but_generated(_read_json(path), status):
        return False
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(status, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=ROOT, help="the repository checkout")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--containers", help="ways container glob (default: "
                    "<root>/containers/ways-*.tbmap)")
    ap.add_argument("--no-api", action="store_true",
                    help="ask GitHub nothing: every result is unknown")
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or None
    if args.no_api:
        runs_for = lambda _workflow: None
    else:
        runs_for = lambda workflow: github_runs(workflow, token)
    ways = load_ways(args.containers or os.path.join(
        args.root, "containers", "ways-*.tbmap"))
    previous = _read_json(args.out, {}) or {}
    status = build(args.root, runs_for, ways, default_closure_sources(),
                   default_byway_layers(), previous=previous)
    wrote = write_if_changed(args.out, status)
    print("%s: %d datasets, %d authorities, %d honoured, %d notes; %s"
          % (args.out, len(status["datasets"]), len(status["authorities"]),
             len(status["honours"]), len(status["notes"]),
             "written" if wrote else "unchanged but for the time, not "
             "written"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

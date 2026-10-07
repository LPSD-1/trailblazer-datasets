#!/usr/bin/env python3
"""Reads the councils that refuse GitHub's runners, from the collector server.

    python tools/home_collector.py --machine server   # collect, commit, push
    python tools/home_collector.py --no-git           # collect only
    python tools/home_collector.py --check-stale 3 --report FILE   # (CI)

A few councils refuse GitHub's shared runners (403, measured 7 October
2026) and answer an ordinary server: Dorset's GeoServer (its rights of way
closures and its register of definitive map applications) and Powys's
order pages and the order documents they link to. This runs on one fixed
server in London, reads only those sources, and commits what it read to
home-collected/; the Actions builds then read those snapshots instead of
asking the councils themselves (HomeClient, below). The name is historical:
the sources were first read from a machine at home.

THE OWNER'S POLICY (7 October 2026). Reading a council from this one fixed,
honestly named server after it refuses GitHub's shared runners is allowed:
it is not a disguise and not a rotation of addresses. If a council refuses
this server too, it is not asked from anywhere else. A real block - a 403
to this server, a bot challenge - is never worked around: no proxies, no
other addresses, no borrowed User-Agent, no headless browser.

IT DISGUISES NOTHING. Every request goes through tools/polite_http.py
exactly as in CI: the same honest User-Agent naming this repository,
robots.txt obeyed (with the owner's recorded robots.txt decisions, read at
most weekly), the same pacing, read only. It only runs somewhere else.

IT IS LIGHT ON THE COUNCILS. Conditional requests where the council
supports them; a snapshot rewritten only when its content changed (a
page's readable text, a layer's features, a document's bytes - not a
timestamp in a footer); every file written to a temporary name and
renamed, so a crash leaves the last good one; a commit and a push only
when something changed (plus a heartbeat once a day, so CI can tell a quiet
week from a stopped server). Standard library only; Python 3.9 or later.

ORDER DOCUMENTS ARE NOT KEPT. A PDF a page links to (Powys's orders) is
recorded by its SHA-256, size and date only - the one thing the order
register's check uses from it - never its bytes: whole council documents
do not belong in a public repository, and a person transcribing one opens
it at the council's own URL.

Configuration: home-collected/collector.json. See HOME-COLLECTOR.md.
"""
import argparse
import datetime
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import polite_http  # noqa: E402
from polite_http import FetchFailed, NotDue, Refused  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOME = os.path.join(ROOT, "home-collected")
CONFIG = os.path.join(HOME, "collector.json")
INDEX = "index.json"
HEARTBEAT = "heartbeat.json"
PROVENANCE = "collected directly from the council"

# WHICH MACHINE READS WHICH COUNCIL. Measured 7 October 2026 from the
# collector server in London: Dorset and Powys answer it. Norfolk and
# Wiltshire (both behind Cloudflare's bot challenge) refuse it with a 403, as
# they refuse GitHub's runners; they were retired rather than read from
# anywhere else. Every source now says `"machine": "server"`. The code still
# allows more than one machine (a source without one belongs to "home"):
# each reads only its own sources, writes only its own snapshots and
# heartbeat, and touches only its own entries in the shared index.json, and
# a stopped one is noticed while another carries on.
MACHINES = ("home", "server")


def machine_of(source):
    return source.get("machine") or "home"


def heartbeat_name(machine):
    """heartbeat.json for the machine at home, as it always was, so the CI
    check needs no migration; heartbeat-<machine>.json for any other."""
    if machine in (None, "home"):
        return HEARTBEAT
    return "heartbeat-%s.json" % machine

log = logging.getLogger("home-collector")


# ---------------------------------------------------------------- files

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_atomic(path, data):
    """Bytes or JSON to `path` by a temporary file and a rename: a crash
    at any moment leaves the previous file whole."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not isinstance(data, bytes):
        data = (json.dumps(data, indent=1, sort_keys=True,
                           ensure_ascii=False) + "\n").encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def write_if_changed(path, data):
    body = (json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")
    try:
        with open(path, "rb") as fh:
            if fh.read() == body:
                return False
    except OSError:
        pass
    write_atomic(path, body)
    return True


def snapshot_name(source_id, url):
    path = urllib.parse.urlsplit(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".pdf", ".json", ".html", ".htm", ".txt", ".csv"):
        ext = ".json" if "outputformat=application%2fjson" in \
            url.lower() or "outputformat=application/json" in \
            url.lower() else ".html"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return "%s-%s%s" % (re.sub(r"[^a-z0-9]+", "-", source_id.lower())
                        .strip("-")[:60], digest, ext)


# ------------------------------------------------------------- content

def is_pdf(url, body):
    """Whether a followed document is a PDF: by its bytes, or its name."""
    path = urllib.parse.urlsplit(url).path.lower()
    return (body or b"")[:4] == b"%PDF" or path.endswith(".pdf")


def forget_document_bytes(home, entry):
    """Drop a document's stored bytes, keeping its digest. Returns whether
    anything was removed (snapshots from before documents were kept as a
    digest only)."""
    name = entry.pop("file", None)
    if not name:
        return False
    try:
        os.remove(os.path.join(home, "snapshots", name))
    except OSError:
        pass
    return True


def canonical(kind, body):
    """(bytes to store, digest of what matters). A page's digest is of its
    readable text, so a session token in its markup is not a change."""
    if kind == "json":
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except ValueError:
            raise FetchFailed("not JSON")
        if isinstance(data, dict):
            data.pop("timeStamp", None)        # GeoServer's own clock
            feats = data.get("features")
            if isinstance(feats, list):
                for f in feats:
                    # A GeoServer view numbers its features afresh on every
                    # request ("view.fid--28fd165a_1a115fe11b7_6715"): not
                    # an identity, and not a change.
                    if isinstance(f, dict) and _VOLATILE_ID.search(
                            str(f.get("id") or "")):
                        f.pop("id", None)
                feats.sort(key=lambda f: json.dumps(f, sort_keys=True))
        stored = json.dumps(data, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False).encode("utf-8")
        return stored, hashlib.sha256(stored).hexdigest()
    if kind == "page":
        from order_register import page_text
        text = "\n".join(page_text(body)).encode("utf-8")
        return body, hashlib.sha256(text).hexdigest()
    return body, hashlib.sha256(body).hexdigest()


_VOLATILE_ID = re.compile(r"\.fid--[0-9a-f]+_[0-9a-f]+_-?[0-9a-f]+$")


# --------------------------------------------------------------- collect

def collect(client, home=HOME, today=None, config=None, machine=None):
    """One pass over the configured sources - only [machine]'s when one is
    named, every source when not. Returns (changed, failures)."""
    today = today or datetime.date.today().isoformat()
    config = config or read_json(os.path.join(home, "collector.json"), {})
    index_path = os.path.join(home, INDEX)
    index = read_json(index_path, {}) or {}
    queue = [dict(s) for s in config.get("sources") or []
             if machine is None or machine_of(s) == machine]
    changed, failures, seen = [], [], set()
    while queue:
        src = queue.pop(0)
        url = src["url"]
        if url in seen:
            continue
        seen.add(url)
        entry = dict(index.get(url) or {})
        try:
            fresh, body, headers = client.get_if_changed(
                url, last_modified=entry.get("last_modified"),
                etag=entry.get("etag"))
        except NotDue:
            fresh, body = False, None
        except (Refused, FetchFailed) as e:
            log.warning("%s: %s", src["id"], e)
            failures.append("%s: %s" % (src["id"], e))
            entry.setdefault("failing_since", today)
            entry["error"] = str(e)[:200]
            index[url] = dict(entry, id=src["id"])
            continue
        if fresh:
            try:
                stored, digest = canonical(src.get("kind", "page"), body)
            except FetchFailed as e:
                failures.append("%s: %s" % (src["id"], e))
                continue
            for key, header in (("last_modified", "last-modified"),
                                ("etag", "etag")):
                value = next((v for k, v in (headers or {}).items()
                              if k.lower() == header), None)
                if value:
                    entry[key] = value
            keep_bytes = not (src.get("kind") == "document"
                              and is_pdf(url, body))
            if digest != entry.get("digest"):
                if keep_bytes:
                    name = entry.get("file") or snapshot_name(src["id"], url)
                    write_atomic(os.path.join(home, "snapshots", name),
                                 stored)
                    entry["file"] = name
                else:
                    # A council's order PDF: its digest, size and type are
                    # what the order register uses; the bytes stay with
                    # the council.
                    forget_document_bytes(home, entry)
                    entry["bytes"] = len(body)
                    kind_of = next((v for k, v in (headers or {}).items()
                                    if k.lower() == "content-type"), None)
                    if kind_of:
                        entry["content_type"] = kind_of
                entry.update({"digest": digest, "collected": today})
                changed.append(src["id"])
                log.info("%s: changed, %s", src["id"],
                         "snapshot written" if keep_bytes
                         else "digest recorded")
            if getattr(client, "overridden", {}).get(url):
                entry["override"] = client.overridden[url]
        if entry.get("kind") == "document" and entry.get("file", "") \
                .lower().endswith(".pdf"):
            forget_document_bytes(home, entry)
        entry.pop("failing_since", None)
        entry.pop("error", None)
        entry.update({"id": src["id"], "kind": src.get("kind", "page"),
                      "council": src.get("council"),
                      "authority": src.get("authority")})
        index[url] = entry
        # Documents a page links to (Powys's order PDFs), from the stored
        # snapshot, so an unchanged page still yields its links.
        if src.get("follow") and entry.get("file"):
            from order_register import linked_documents
            with open(os.path.join(home, "snapshots", entry["file"]),
                      "rb") as fh:
                page_body = fh.read()
            for doc in linked_documents({"id": src["id"], "url": url,
                                         "follow": src["follow"]},
                                        page_body):
                queue.append({"id": doc["id"], "url": doc["url"],
                              "kind": "document",
                              "council": src.get("council"),
                              "authority": src.get("authority")})
    if write_if_changed(index_path, index):
        changed.append("index")
    beat_path = os.path.join(home, heartbeat_name(machine))
    beat = read_json(beat_path, {}) or {}
    ok = [s for s in seen if not (index.get(s) or {}).get("error")]
    new_beat = {"last_run": today,
                "last_ok": today if ok else beat.get("last_ok"),
                "sources_read": len(ok), "sources_failing": len(failures)}
    if write_if_changed(beat_path, new_beat):
        changed.append("heartbeat")
    return changed, failures


# ------------------------------------------------------------------- git

def git(repo, *args, check=True):
    exe = shutil.which("git") or "git"
    # No console window for git when the collector itself runs without one
    # (pythonw under the Windows task); the flag does not exist elsewhere.
    out = subprocess.run([exe] + list(args), cwd=repo, check=False,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True,
                         creationflags=getattr(subprocess,
                                               "CREATE_NO_WINDOW", 0))
    if check and out.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args),
                                                  out.stdout[-500:]))
    return out


def sync(repo):
    """Bring the clone level with GitHub, whatever state a crash left it in.
    Returns True if it rebased cleanly, False if it had to reset.

    More than one machine may push to home-collected/. They write different
    snapshots and different entries of index.json, so a rebase is clean -
    but if one ever is not, a clone left mid-rebase
    would fail every run after it, silently, until someone logged in. So a
    failed rebase is abandoned and the clone reset to GitHub's copy: the
    snapshots of this run are lost, and the next run reads them again. The
    clone is the collector's own; nobody edits it by hand."""
    git_dir = os.path.join(repo, ".git")
    if os.path.isdir(os.path.join(git_dir, "rebase-merge")) or \
            os.path.isdir(os.path.join(git_dir, "rebase-apply")):
        git(repo, "rebase", "--abort", check=False)
    if git(repo, "pull", "-q", "--rebase", check=False).returncode != 0:
        log.warning("pull --rebase failed; resetting to GitHub's copy")
        git(repo, "rebase", "--abort", check=False)
        git(repo, "fetch", "-q")
        git(repo, "reset", "-q", "--hard", "@{u}")
        return False
    return True


def publish(repo, message, tries=3, machine="home"):
    """Commit home-collected/ and push it; on a lost race, rebase onto the
    other machine's push (it writes other entries) and try again."""
    git(repo, "add", "-A", "home-collected")
    if git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        log.info("nothing changed; nothing pushed")
        return False
    git(repo, "commit", "-q", "-m", message, "-m",
        "Collected by tools/home_collector.py on the %s machine."
        % machine)
    for attempt in range(1, tries + 1):
        if git(repo, "push", "-q", check=False).returncode == 0:
            log.info("pushed")
            return True
        log.info("push rejected; pull --rebase and try again (%d)", attempt)
        time.sleep(5 * attempt)
        if not sync(repo):
            # The reset took this run's commit with it: a push now would
            # push nothing and say "pushed". Stop, and say what happened.
            log.warning("this run's changes were dropped when the clone was "
                        "reset to GitHub's copy; nothing was pushed, and the "
                        "next run reads them again")
            return False
    raise RuntimeError("could not push after %d attempts" % tries)


# -------------------------------------------------- read by the CI builds

def stale(home=HOME, days=3, today=None):
    """A sentence if any collecting machine has not run for `days`, or any
    one source has been failing for longer than that, else None. Each
    machine with sources in collector.json is asked after by its own
    heartbeat: the server carrying on must not hide a PC that has stopped,
    nor the other way round. And a heartbeat moves when ANY source reads,
    so one council refusing for a week is named here by index.json's
    `failing_since`, or nothing would ever say so."""
    today = datetime.date.fromisoformat(
        today or datetime.date.today().isoformat())
    config = read_json(os.path.join(home, "collector.json"), {}) or {}
    machines = sorted(set(machine_of(s) for s in
                          config.get("sources") or [])) or ["home"]
    said = []
    for machine in machines:
        name = heartbeat_name(machine)
        who = ("the home collector" if machine == "home"
               else "the %s collector" % machine)
        beat = read_json(os.path.join(home, name), {}) or {}
        last = beat.get("last_ok")
        if not last:
            said.append("%s has never reported (no %s)" % (who, name))
            continue
        age = (today - datetime.date.fromisoformat(last)).days
        if age > days:
            said.append("%s last read its councils on %s, %d days ago: the "
                        "machine may be off, offline or failing"
                        % (who, last, age))
    index = read_json(os.path.join(home, INDEX), {}) or {}
    for url, entry in sorted(index.items(),
                             key=lambda kv: str(kv[1].get("id"))):
        since = (entry or {}).get("failing_since")
        if not since:
            continue
        age = (today - datetime.date.fromisoformat(since)).days
        if age > days:
            said.append("%s has not been read since %s, %d days ago (%s): "
                        "%s" % (entry.get("id") or url, since, age, url,
                                entry.get("error") or "no error recorded"))
    return "; ".join(said) or None


class HomeClient(object):
    """What the CI builds use in place of a PoliteClient: the councils the
    home collector reads are answered from home-collected/, and never asked
    from GitHub's runners (they refuse them); everything else is passed to
    the real client. `served` says what came from home, for the credits."""

    def __init__(self, client, home=HOME):
        self.client = client
        self.home = home
        self.index = read_json(os.path.join(home, INDEX), {}) or {}
        config = read_json(os.path.join(home, "collector.json"), {}) or {}
        self.hosts = set(polite_http.host_of(s["url"])
                         for s in config.get("sources") or [])
        self.served = {}
        self._overridden = {}

    @property
    def overridden(self):
        """{url: override id} read under the owner's robots.txt decision:
        the collector's, as recorded in its index, merged with the inner
        client's AS IT IS NOW - it records its own during the run, so a copy
        taken when this was made would miss every one of them."""
        merged = dict(getattr(self.client, "overridden", {}) or {})
        merged.update(self._overridden)
        return merged

    def __getattr__(self, name):
        return getattr(self.client, name)

    def document_digest(self, url):
        """The SHA-256 of a document the collector holds only as a digest
        (a council's order PDF), or None. Credited like any snapshot."""
        entry = self.index.get(url)
        if not entry or entry.get("file") or not entry.get("digest") or \
                entry.get("kind") != "document":
            return None
        self.served[url] = entry.get("collected")
        if entry.get("override"):
            self._overridden[url] = entry["override"]
        return entry["digest"]

    def _home(self, url):
        entry = self.index.get(url)
        if entry and entry.get("file"):
            path = os.path.join(self.home, "snapshots", entry["file"])
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                body = None
            if body is not None:
                self.served[url] = entry.get("collected")
                if entry.get("override"):
                    self._overridden[url] = entry["override"]
                return body
        if entry and entry.get("digest") and not entry.get("file"):
            raise Refused("home-collected/ keeps only the digest of %s, not "
                          "its bytes; open it at the council" % url)
        if polite_http.host_of(url) in self.hosts:
            raise Refused("%s is read only by the collector, and "
                          "home-collected/ holds no snapshot of %s"
                          % (polite_http.host_of(url), url))
        return None

    def get(self, url):
        body = self._home(url)
        return body if body is not None else self.client.get(url)

    def get_json(self, url):
        body = self._home(url)
        if body is None:
            return self.client.get_json(url)
        try:
            return json.loads(body.decode("utf-8-sig"))
        except ValueError as e:
            raise FetchFailed("home snapshot of %s is not JSON: %s"
                              % (url, e))

    def take_served(self):
        """{url: collected date} since the last call, and forget them."""
        served, self.served = self.served, {}
        return served


def provenance_of(served):
    if not served:
        return None
    return "%s %s" % (PROVENANCE, max(d or "" for d in served.values()))


# ------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-git", action="store_true",
                    help="collect only: no pull, commit or push")
    ap.add_argument("--check-stale", type=int, metavar="DAYS",
                    help="(CI) report whether the collector has gone quiet")
    ap.add_argument("--report", help="with --check-stale: write it here")
    ap.add_argument("--machine", choices=MACHINES, default="home",
                    help="read only this machine's sources (default: home)")
    args = ap.parse_args(argv)

    if args.check_stale is not None:
        why = stale(days=args.check_stale)
        print(why or "home collector: reported within %d days"
              % args.check_stale)
        if args.report:
            with open(args.report, "w", encoding="utf-8",
                      newline="\n") as fh:
                fh.write((why + "\n") if why else "")
        return 0

    config = read_json(CONFIG, {}) or {}
    log_path = os.path.join(ROOT, config.get("log") or "collector.log")
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=256 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    if sys.stdout is not None:     # pythonw has no console to write to
        log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)
    log.info("run starts (%s)", args.machine)
    try:
        if not args.no_git:
            sync(ROOT)
        client = polite_http.PoliteClient(
            min_gap=float(config.get("min_gap_s") or 4.0),
            override_log=os.path.join(HOME, "override-reads.json"),
            log=log.info)
        changed, failures = collect(client, HOME, config=config,
                                    machine=args.machine)
        for line in failures:
            log.warning("not read: %s", line)
        if changed and not args.no_git:
            publish(ROOT, "Collector (%s): %s"
                    % (args.machine, datetime.date.today().isoformat()),
                    machine=args.machine)
        log.info("run ends: %d change(s), %d source(s) not read",
                 len(changed), len(failures))
        return 0
    except Exception:  # noqa: BLE001 - the log is the only witness
        log.exception("run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

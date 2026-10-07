#!/usr/bin/env python3
"""The one way this repository's council fetchers reach the web.

Every source added for closures, traffic orders, decisions and the order
register goes through `PoliteClient`, so the rules below are kept in one place
rather than re-stated (and eventually forgotten) in each fetcher:

  * SAY WHO WE ARE. The User-Agent names this repository and how to reach it.
    No fetcher here ever presents itself as a browser.
  * OBEY robots.txt, per host, before the first request to it, by RFC 9309: a
    4xx robots.txt means "no rules", a 5xx or an unreachable one means "assume
    everything is disallowed" until it can be read. A disallowed path raises
    `Refused`; nothing here works around it.
  * NEVER WORK AROUND A BLOCK. A 403, or a Cloudflare-style challenge page,
    is a refusal and is reported as one - there is no retry with different
    headers. The hosts listed in `BLOCKED_HOSTS` are refused outright, before
    any request: sites behind bot challenges, and sources the project has
    ruled out.
  * BE GENTLE. At least `min_gap` seconds between two requests to one host,
    a small retry budget, and a server's Retry-After honoured (up to a cap) on
    429 and 503.
  * THE OWNER'S ROBOTS.TXT DECISIONS are the one exception to the second
    rule: specific council document paths listed in robots_override.json,
    read at most weekly, recorded as overridden wherever they are credited.
    Nothing else in that rule, or in any other, is relaxed for them.
  * READ ONLY. GET only. An ArcGIS URL is allowed only for layer metadata or
    `/query`, and an OGC service only for GetCapabilities, DescribeFeature-
    Type and GetFeature: some council FeatureServers and GeoServers advertise
    editing to anonymous users, and nothing in this repository ever calls an
    edit operation.

Pure standard library, so every CI job can import it without installing
anything.
"""
import datetime
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = ("TrailBlazer-datasets/1.0 "
              "(+https://github.com/LPSD-1/trailblazer-datasets; "
              "open-data fetcher)")

# Hosts never contacted, whatever a source definition says.
#
# Two kinds. Sites that put automated clients behind a challenge (measured
# 7 October 2026): asking them for a feed is the route, not getting past the
# challenge. And sources the project has ruled out on their terms or on
# principle, listed so a later source definition cannot quietly add one.
BLOCKED_HOSTS = (
    # Behind Cloudflare or another bot challenge.
    "hants.gov.uk", "kent.gov.uk", "leicestershire.gov.uk",
    "peakdistrict.gov.uk", "exmoor-nationalpark.gov.uk",
    "dartmoor.gov.uk", "northyorkmoors.org.uk",
    # robots.txt disallows the whole host: ROBOTS_HOSTS, below.
    # A bot challenge measured on the council's own pages, 7 October 2026:
    # the www hosts. Their GIS servers (gis.westberks.gov.uk,
    # arcgis.iow.gov.uk) answer without one and have no robots.txt; they are
    # read like any council layer. gis2.westberks.gov.uk is ROBOTS_HOSTS.
    "www.westberks.gov.uk", "www.iow.gov.uk",
    # Ruled out.
    "publicnoticeportal.uk", "one.network", "roadworks.org",
    "thegazette.co.uk", "glass-uk.org", "trf.org.uk", "trailwise.org.uk",
    "openstreetmap.org", "overpass-api.de",
)

# ArcGIS REST operations that change data. Refused before any request.
_ARCGIS_WRITES = re.compile(
    r"/(applyEdits|addFeatures|updateFeatures|deleteFeatures|"
    r"calculate|append|truncate|addAttachment|updateAttachment|"
    r"deleteAttachments|uploads)(/|$)", re.I)
_ARCGIS_PATH = re.compile(r"/rest/services/", re.I)
_ARCGIS_READS = re.compile(r"/(FeatureServer|MapServer)(/\d+)?(/query)?/?$",
                           re.I)

# The only OGC (WFS/WMS) requests ever sent. WFS-T `Transaction` and
# `LockFeature` change or lock data and are refused.
_OGC_READS = ("getcapabilities", "describefeaturetype", "getfeature",
              "getpropertyvalue", "liststoredqueries",
              "describestoredqueries")

# What a bot challenge looks like when a site answers 200 or 403 with one.
_CHALLENGE = re.compile(
    rb"(cf-browser-verification|challenge-platform|__cf_chl_|"
    rb"Attention Required! \| Cloudflare|Just a moment\.\.\.)", re.I)


class Refused(Exception):
    """The request was not made, or the server refused it. Never retried."""


class NotDue(Refused):
    """Not requested: a path read under the owner's robots.txt decision was
    read within the last week. Its last reading stands."""


class FetchFailed(Exception):
    """The request was made and did not succeed (network, 5xx, bad body)."""


# Hosts refused because their robots.txt disallows the whole host. Unlike
# the hosts above, a path on one of these may be read when the owner has
# chosen to (robots_override.json) - and only that path.
ROBOTS_HOSTS = ("apps.derbyshire.gov.uk", "gis2.westberks.gov.uk",
                "map.cornwall.gov.uk", "roam.somerset.gov.uk")

BLOCKED_HOSTS = BLOCKED_HOSTS + ROBOTS_HOSTS

# THE OWNER'S ROBOTS.TXT DECISIONS (8 October 2026). The specific council
# documents robots.txt alone keeps us from - byway order PDFs and the like,
# public records - that the owner has chosen to read anyway, each listed by
# URL prefix in tools/robots_override.json. For those paths only: robots.txt
# is not consulted; everything else here still applies (honest User-Agent,
# pacing, read only, a 403 or a bot challenge is still a refusal and is
# never got past). An overridden path is read at most once a week.
OVERRIDE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "robots_override.json")
OVERRIDE_EVERY_DAYS = 7


def _under(host, hosts):
    return any(host == b or host.endswith("." + b) for b in hosts)


def load_overrides(path=OVERRIDE_FILE):
    """The override entries, each checked: a prefix on https, never on a
    host refused for anything but robots.txt."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError:
        return []
    out = []
    never = [h for h in BLOCKED_HOSTS if h not in ROBOTS_HOSTS]
    for entry in data.get("paths") or []:
        prefix = entry.get("prefix") or ""
        host = host_of(prefix)
        if not prefix.startswith("https://") or not host or \
                urllib.parse.urlsplit(prefix).path in ("", "/"):
            raise ValueError("override %r: not an https path prefix"
                             % entry.get("id"))
        if _under(host, never):
            raise ValueError("override %r: %s is refused for a bot "
                             "challenge or by project decision, not by "
                             "robots.txt; no override reaches it"
                             % (entry.get("id"), host))
        out.append(entry)
    return out


def override_for(url, overrides):
    for entry in overrides or ():
        if url.startswith(entry["prefix"]) and (
                not entry.get("pattern")
                or re.search(entry["pattern"], url)):
            return entry
    return None


def host_of(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def blocked(host):
    """Whether `host` is, or is under, a blocked host."""
    host = (host or "").lower()
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


def check_read_only(url):
    """Refuse any ArcGIS URL that is not metadata or a query."""
    path = urllib.parse.urlsplit(url).path
    # Only ArcGIS REST paths: "/uploads/" is also every WordPress site's
    # media folder (the Yorkshire Dales' order PDFs live there).
    if _ARCGIS_PATH.search(path) and _ARCGIS_WRITES.search(path):
        raise Refused("refusing an ArcGIS edit operation: %s" % url)
    if _ARCGIS_PATH.search(path) and not (
            _ARCGIS_READS.search(path) or path.rstrip("/").endswith(
                "/rest/services")):
        raise Refused("refusing an ArcGIS URL that is neither layer "
                      "metadata nor /query: %s" % url)
    # OGC services the same way: Cheshire's and Derbyshire's GeoServers
    # advertise WFS-T Transaction to anonymous users. Only reads are sent.
    params = dict((k.lower(), v) for k, v in urllib.parse.parse_qsl(
        urllib.parse.urlsplit(url).query, keep_blank_values=True))
    request = (params.get("request") or "").lower()
    if request and request not in _OGC_READS:
        raise Refused("refusing an OGC %s request: %s"
                      % (params.get("request"), url))


PRODUCT = "trailblazer-datasets"


class Robots(object):
    """One host's robots.txt, matched by RFC 9309 - not urllib.robotparser.

    The standard library's parser gets two councils' files wrong, measured
    on 7 October 2026: it does not understand wildcards (Norfolk's
    `Disallow: /*.pdf` blocks nothing in it), and it reads only the first
    `User-agent: *` group where Powys has two. RFC 9309 says: groups naming
    the same agent are combined; our own product token's groups win over
    `*`; within them the LONGEST matching path pattern decides, with `*` and
    a trailing `$`; an allow beats a disallow of the same length.
    """

    def __init__(self, text):
        groups, agents, rules, in_rules = [], [], [], False
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if in_rules:
                    groups.append((agents, rules))
                    agents, rules, in_rules = [], [], False
                agents.append(value.lower())
            elif key in ("allow", "disallow"):
                in_rules = True
                if agents:
                    rules.append((key == "allow", value))
        if agents:
            groups.append((agents, rules))
        own = [r for a, rs in groups for r in rs
               if any(x and x != "*" and x in PRODUCT for x in a)]
        self.rules = own if own else [r for a, rs in groups for r in rs
                                      if "*" in a]

    @staticmethod
    def _matches(pattern, path):
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if c == "*" else re.escape(c) for c in body)
        return re.match(regex + ("$" if anchored else ""), path) is not None

    def can_fetch(self, url):
        parts = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parts.path or "/")
        if parts.query:
            path += "?" + parts.query
        if path == "/robots.txt":
            return True
        best, allowed = -1, True
        for allow, pattern in self.rules:
            if not pattern:
                continue
            if self._matches(pattern, path):
                length = len(pattern)
                if length > best or (length == best and allow):
                    best, allowed = length, allow
        return allowed


def robots_verdict(status, text):
    """What one robots.txt response means (RFC 9309 section 2.3.1).

    Returns a Robots, or the strings "allow-all" / "disallow-all": a 4xx
    file is "unavailable" and allows everything, a 5xx or no answer at all
    is "unreachable" and allows nothing.
    """
    if status is None or status >= 500:
        return "disallow-all"
    if 400 <= status < 500:
        return "allow-all"
    return Robots(text)


class PoliteClient(object):
    """GET with robots.txt, pacing, a block list and an honest User-Agent."""

    def __init__(self, min_gap=2.0, retries=2, timeout=90, max_wait=120,
                 opener=None, sleep=time.sleep, clock=time.monotonic,
                 log=print, overrides=None, override_log=None, today=None):
        self.min_gap = min_gap
        self.retries = retries
        self.timeout = timeout
        self.max_wait = max_wait
        self._open = opener or self._urlopen
        self._sleep = sleep
        self._clock = clock
        self._log = log
        self._last = {}
        self._robots = {}
        # Conditional-request headers for the request in flight only
        # (get_if_changed); never anything that changes who we say we are.
        self._extra_headers = {}
        # The owner's robots.txt decisions, and the file keeping the date
        # each overridden URL was last read. Without that file a client
        # never uses an override.
        self.overrides = load_overrides() if overrides is None else overrides
        self.override_log = override_log
        self.today = today or datetime.date.today().isoformat()
        self.overridden = {}   # url -> override id, for this run
        self.requests = 0

    # -- plumbing ---------------------------------------------------------

    def _urlopen(self, url, timeout):
        """(status, headers, body). HTTP errors come back as a status."""
        # HTML first: Durham's site answers 404 to an Accept that leads
        # with JSON (measured 7 October 2026); a JSON API ignores it.
        headers = {"User-Agent": USER_AGENT,
                   "Accept": "text/html, application/json;q=0.9, */*;q=0.8"}
        headers.update(self._extra_headers)
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:  # noqa: BLE001 - a body is a courtesy
                body = b""
            return e.code, dict(e.headers or {}), body

    def _pace(self, host):
        last = self._last.get(host)
        if last is not None:
            wait = self.min_gap - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[host] = self._clock()

    def _raw(self, url):
        host = host_of(url)
        self._pace(host)
        self.requests += 1
        try:
            return self._open(url, self.timeout)
        except Exception as e:  # noqa: BLE001 - network faults are data here
            return None, {}, str(e).encode("utf-8", "replace")

    # -- the owner's overrides ---------------------------------------------

    def _override_due(self, url, override):
        """An overridden URL is read at most every OVERRIDE_EVERY_DAYS."""
        log = {}
        try:
            with open(self.override_log, encoding="utf-8") as fh:
                log = json.load(fh)
        except (OSError, ValueError):
            pass
        last = (log.get(url) or {}).get("read")
        if last:
            age = (datetime.date.fromisoformat(self.today)
                   - datetime.date.fromisoformat(last)).days
            if age < OVERRIDE_EVERY_DAYS:
                raise NotDue("%s was read %d day(s) ago; a path read under "
                              "the owner's robots.txt decision is read at "
                              "most every %d days"
                              % (url, age, OVERRIDE_EVERY_DAYS))
        log[url] = {"read": self.today, "override": override.get("id")}
        folder = os.path.dirname(self.override_log)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(self.override_log, "w", encoding="utf-8",
                  newline="\n") as fh:
            json.dump(log, fh, indent=1, sort_keys=True)
            fh.write("\n")
        self.overridden[url] = override.get("id")

    # -- robots -----------------------------------------------------------

    def allowed(self, url):
        host = host_of(url)
        if host not in self._robots:
            parts = urllib.parse.urlsplit(url)
            robots_url = "%s://%s/robots.txt" % (parts.scheme, parts.netloc)
            status, _headers, body = self._raw(robots_url)
            self._robots[host] = robots_verdict(
                status, body.decode("utf-8", "replace") if body else "")
        verdict = self._robots[host]
        if verdict == "allow-all":
            return True
        if verdict == "disallow-all":
            return False
        return verdict.can_fetch(url)

    # -- the one public call ----------------------------------------------

    def get(self, url):
        """The body of `url`, or Refused / FetchFailed. GET only."""
        return self._get(url)[1]

    def get_if_changed(self, url, last_modified=None, etag=None):
        """(changed, body, headers) - a conditional GET.

        With the validators a server gave last time, an unchanged file
        answers 304 with no body: re-checking costs the server almost
        nothing, which is what makes a weekly re-check of every file polite.
        """
        extra = {}
        if last_modified:
            extra["If-Modified-Since"] = last_modified
        if etag:
            extra["If-None-Match"] = etag
        self._extra_headers = extra
        try:
            status, body, headers = self._get(url, allow_304=True)
        finally:
            self._extra_headers = {}
        if status == 304:
            return False, None, headers
        return True, body, headers

    def _get(self, url, allow_304=False):
        """(status, body, headers) for a 200 (or a 304 when asked)."""
        host = host_of(url)
        if not url.lower().startswith("https://"):
            raise Refused("only https is fetched: %s" % url)
        override = override_for(url, self.overrides) \
            if self.override_log else None
        if blocked(host) and not (override and _under(host, ROBOTS_HOSTS)):
            raise Refused("%s is on the block list" % host)
        check_read_only(url)
        if override is None and not self.allowed(url):
            raise Refused("robots.txt on %s disallows %s" % (host, url))
        if override is not None and not self.allowed(url):
            # robots.txt says no, and the owner has decided to read this.
            self._override_due(url, override)

        attempt = 0
        while True:
            status, headers, body = self._raw(url)
            if status == 304 and allow_304:
                return status, None, headers or {}
            if status == 200:
                if body and _CHALLENGE.search(body[:20000]):
                    raise Refused("%s answered with a bot challenge" % host)
                return status, body, headers or {}
            if status in (401, 403, 451):
                raise Refused("%s refused %s (HTTP %s)" % (host, url, status))
            if status == 404:
                raise FetchFailed("HTTP 404 for %s" % url)
            retryable = status is None or status in (429, 500, 502, 503, 504)
            if not retryable or attempt >= self.retries:
                raise FetchFailed("HTTP %s for %s: %s"
                                  % (status, url, (body or b"")[:200]))
            attempt += 1
            wait = self._retry_after(headers, attempt)
            self._log("  %s answered %s; waiting %.0f s (attempt %d of %d)"
                      % (host, status, wait, attempt, self.retries))
            self._sleep(wait)

    def _retry_after(self, headers, attempt):
        value = None
        for key, val in (headers or {}).items():
            if key.lower() == "retry-after":
                value = val
        try:
            wait = float(value)
        except (TypeError, ValueError):
            wait = 15.0 * attempt
        return max(1.0, min(wait, self.max_wait))

    def get_json(self, url):
        body = self.get(url)
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except ValueError as e:
            raise FetchFailed("not JSON from %s: %s" % (url, e))
        # ArcGIS reports its own failures inside a 200.
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            err = data["error"]
            code = err.get("code")
            if code in (401, 403, 499):
                raise Refused("%s refused %s (%s)" % (host_of(url), url, err))
            raise FetchFailed("service error from %s: %s" % (url, err))
        return data


def arcgis_query(client, layer_url, where="1=1", out_fields="*",
                 geometry=True, out_sr=27700, page=1000, limit=20000):
    """Every feature a layer's /query returns, paged, as an esri JSON list.

    Asks for the geometry in British National Grid (`out_sr`), so every
    source reaches the matcher in the same frame whatever it stores in.
    """
    out = []
    offset = 0
    while True:
        params = {
            "where": where, "outFields": out_fields, "f": "json",
            "returnGeometry": "true" if geometry else "false",
            "outSR": str(out_sr), "resultOffset": str(offset),
            "resultRecordCount": str(page), "orderByFields": "",
        }
        url = layer_url.rstrip("/") + "/query?" + urllib.parse.urlencode(
            dict((k, v) for k, v in params.items() if v != ""))
        data = client.get_json(url)
        feats = data.get("features")
        if not isinstance(feats, list):
            raise FetchFailed("no features list from %s" % url)
        out.extend(feats)
        if not data.get("exceededTransferLimit") or not feats:
            return out
        offset += len(feats)
        if offset >= limit:
            raise FetchFailed("%s holds more than %d features; refusing to "
                              "page further" % (layer_url, limit))

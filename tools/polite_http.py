#!/usr/bin/env python3
"""The one way this repository's council fetchers reach the web.

Every source added for closures, traffic orders, decisions and the order
register goes through `PoliteClient`, so the rules below are kept in one place
rather than re-stated (and eventually forgotten) in each fetcher:

  * SAY WHO WE ARE. The User-Agent names this repository and how to reach it.
    No fetcher here ever presents itself as a browser.
  * OBEY robots.txt, per host, before the first request to it, by RFC 9309: a
    4xx robots.txt means "no rules"; a 200 is read and obeyed; anything else
    (a 5xx, no answer at all, a 304 or any other status) means "assume
    everything is disallowed" until it can be read. robots.txt is always
    asked for plainly, never with another request's validators. A
    disallowed path raises `Refused`; nothing here works around it.
  * NEVER WORK AROUND A BLOCK. A 403, or a Cloudflare-style challenge page,
    is a refusal and is reported as one - there is no retry with different
    headers, no proxy, no other address, no browser. The hosts listed in
    `BLOCKED_HOSTS` are refused outright, before any request: sites behind
    bot challenges, and sources the project has ruled out. A redirect is
    followed only to a URL that passes every check here itself (https, not
    blocked, read only, allowed by robots.txt).
  * ONE FIXED, NAMED COLLECTOR SERVER (the owner's policy, 7 October 2026).
    Dorset and Powys refuse GitHub's shared runners but answer an ordinary
    server, and Wiltshire's closures register answers that server (whether
    it answers the runners is unknown), so tools/home_collector.py reads
    them from one fixed server in London, through this same client and
    these same rules, and commits what it read for the builds
    (HOME-COLLECTOR.md). That is not a disguise and
    not a rotation of addresses: the User-Agent is the same, the address
    never changes, and a council that refuses that server too is not asked
    from anywhere else.
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
  * THE ONE POST: THE OWNER'S DECISION OF 7 OCTOBER 2026. Wiltshire Council's
    register of rights of way closures is a public search form and nothing
    else (a GET with the same fields answers "no closures"). The owner
    approved reading it by submitting that form, read only, exactly as a
    person pressing Search does: a narrowly scoped exception to "GET only",
    and the only one. `post_form` is the sole way a POST is ever sent: only
    to an action listed in FORM_POSTS (one host and one path, the register's
    Result address), only after GETting the listed page that holds the form
    and only if that page's form still posts there, and only with that
    form's own fields - its hidden fields and anti-forgery token as the page
    gave them, and the search fields FORM_POSTS names, each set to a value
    the form itself offers where it offers a choice. Anything else is refused
    before any request is made. Every other rule here applies to both
    requests: the honest User-Agent, robots.txt, the pacing, https only,
    checked redirects, the block list. Nothing here writes to any council
    service.

Pure standard library, so every CI job can import it without installing
anything.
"""
import datetime
import html.parser
import http.cookiejar
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


# THE ONE POST (the owner's decision, 7 October 2026; see the docstring).
# Keyed by the exact action URL - scheme, host and path, no query. `page` is
# the page holding the form, GET first for its token and cookie; `search` the
# only fields a caller may set. The form's other fields (hidden ones, the
# anti-forgery token) are sent exactly as the page gave them.
FORM_POSTS = {
    "https://apps.wiltshire.gov.uk/RightsOfWay/Closure/Result": {
        "page": "https://apps.wiltshire.gov.uk/RightsOfWay/Closure",
        "search": ("AppID", "RowID", "Day", "Month", "Year", "Parish",
                   "GridReference", "PostCode", "ClosureType", "RowType",
                   "Act"),
        "decided": "7 October 2026, by the owner: Wiltshire Council's "
                   "rights of way closures register, read by its public "
                   "search form",
    },
}


def form_key(url, fields):
    """How a form search's answer is filed (home-collected/index.json): the
    action URL with the search in a fragment, which no request ever sends,
    so it can never be mistaken for a URL to GET."""
    return "%s#post:%s" % (url, urllib.parse.urlencode(sorted(
        fields.items())))


class _Forms(html.parser.HTMLParser):
    """Every <form> on a page: its method, action, and fields - the value of
    each input, the options of each select, the values of each named
    button. Enough to submit a form as its page offers it, nothing more."""

    def __init__(self):
        html.parser.HTMLParser.__init__(self, convert_charrefs=True)
        self.forms, self._form, self._select = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict((k, v if v is not None else "") for k, v in attrs)
        if tag == "form":
            self._form = {"method": (a.get("method") or "get").lower(),
                          "action": a.get("action") or "", "fields": {},
                          "choices": {}}
            self.forms.append(self._form)
            return
        form = self._form
        if form is None:
            return
        if tag == "option" and self._select:
            value = a.get("value", "")
            form["choices"][self._select].append(value)
            # The selected option, or else the first, as a browser sends.
            if "selected" in a or form["fields"][self._select] is None:
                form["fields"][self._select] = value
            return
        name = a.get("name")
        if not name:
            return
        if tag == "input":
            kind = (a.get("type") or "text").lower()
            if kind in ("submit", "button", "image", "reset", "file",
                        "checkbox", "radio"):
                # Pressed or ticked by a person, never sent unasked.
                form["choices"].setdefault(name, []).append(
                    a.get("value") or "")
            else:
                form["fields"][name] = a.get("value") or ""
        elif tag == "button":
            form["choices"].setdefault(name, []).append(a.get("value") or "")
        elif tag == "select":
            self._select = name
            form["choices"][name] = []
            form["fields"][name] = None
        elif tag == "textarea":
            form["fields"][name] = ""

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
            self._select = None
        elif tag == "select" and self._form is not None and self._select:
            if self._form["fields"].get(self._select) is None:
                self._form["fields"][self._select] = ""
            self._select = None


def page_forms(body, base):
    """The forms on a page, each action resolved against `base`."""
    parser = _Forms()
    parser.feed(body.decode("utf-8", "replace") if isinstance(body, bytes)
                else body)
    parser.close()
    for form in parser.forms:
        if form["action"]:
            form["action"] = urllib.parse.urljoin(base, form["action"])
        else:
            form["action"] = base
    return parser.forms


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

# THE OWNER'S ROBOTS.TXT DECISIONS (7 October 2026). The specific council
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


def _is_us(agent):
    """Whether a robots.txt user-agent value names our product token
    ("TrailBlazer-datasets", any case, with or without a version)."""
    token = (agent or "").strip().lower().split("/", 1)[0].strip()
    return token == PRODUCT


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
        # RFC 9309 2.2.1: our group is one whose user-agent value is our
        # product token, matched case-insensitively - equality, not a
        # substring, or a `User-agent: data` group would be taken as ours.
        own = [r for a, rs in groups for r in rs
               if any(_is_us(x) for x in a)]
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

    Returns a Robots, or the strings "allow-all" / "disallow-all": a 200 is
    the file, a 4xx file is "unavailable" and allows everything, and
    anything else allows nothing. That includes a 5xx or no answer at all
    ("unreachable"), and also a 304 or any other status that is not a file:
    an empty body there is not an empty robots.txt.
    """
    if status == 200:
        return Robots(text)
    if status is not None and 400 <= status < 500:
        return "allow-all"
    return "disallow-all"


# A host whose robots.txt is being read right now (PoliteClient.allowed).
_READING = object()


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to a URL that passes every check the URL
    asked for passed: https, not on the block list, read only, allowed by
    robots.txt. urllib would otherwise follow any Location silently - to
    http, to a blocked host, to a path robots.txt disallows."""

    def __init__(self, check):
        self._check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            self._check(newurl)
        except Refused as e:
            fp.close()
            raise Refused("%s redirected to a URL that is not fetched: %s"
                          % (req.full_url, e))
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


class PoliteClient(object):
    """GET with robots.txt, pacing, a block list and an honest User-Agent."""

    #: Extra urllib handlers for the real opener (a stand-in transport in
    #: the tests); the redirect check is always installed.
    _handlers = ()

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
        # Cookies, only for the length of one post_form; None otherwise, so
        # no other request ever carries or keeps one.
        self._cookies = None
        # The owner's robots.txt decisions, and the file keeping the date
        # each overridden URL was last read. Without that file a client
        # never uses an override.
        self.overrides = load_overrides() if overrides is None else overrides
        self.override_log = override_log
        self.today = today or datetime.date.today().isoformat()
        self.overridden = {}   # url -> override id, for this run
        self.requests = 0

    # -- plumbing ---------------------------------------------------------

    def _urlopen(self, url, timeout, data=None):
        """(status, headers, body). HTTP errors come back as a status.

        A GET, always - unless `data` is given, which only `post_form`
        does, for an action it has checked against FORM_POSTS."""
        # HTML first: Durham's site answers 404 to an Accept that leads
        # with JSON (measured 7 October 2026); a JSON API ignores it.
        headers = {"User-Agent": USER_AGENT,
                   "Accept": "text/html, application/json;q=0.9, */*;q=0.8"}
        headers.update(self._extra_headers)
        request = urllib.request.Request(
            url, data=data, headers=headers,
            method="GET" if data is None else "POST")
        handlers = [_CheckedRedirects(self._check)] + list(self._handlers)
        if self._cookies is not None:
            # Only inside post_form: the form's anti-forgery cookie, kept
            # for its one GET and one POST and then dropped.
            handlers.append(urllib.request.HTTPCookieProcessor(
                self._cookies))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(request, timeout=timeout) as resp:
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

    def _raw(self, url, data=None):
        host = host_of(url)
        self._pace(host)
        self.requests += 1
        try:
            if data is None:
                return self._open(url, self.timeout)
            return self._open(url, self.timeout, data=data)
        except Refused:
            raise   # a refused redirect: a refusal, never a retry
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
            # Never with the validators of the request that brought us here
            # (get_if_changed): robots.txt would answer 304 with no body.
            saved, self._extra_headers = self._extra_headers, {}
            # While robots.txt itself is being read, a redirect it answers
            # with on its own host is followed to the file (RFC 9309
            # 2.3.1.2) rather than checked against the robots.txt still
            # being read: that check read robots.txt again, which redirected
            # again, about a thousand times over (Derbyshire's wms. host
            # answers robots.txt with a 302 to its GetCapabilities).
            self._robots[host] = _READING
            try:
                status, _headers, body = self._raw(robots_url)
            except Refused:
                # Redirected somewhere we never go: not a file we can read.
                status, body = None, b""
            finally:
                self._extra_headers = saved
            self._robots[host] = robots_verdict(
                status, body.decode("utf-8", "replace") if body else "")
        verdict = self._robots[host]
        if verdict is _READING:
            return True   # where this host's own robots.txt redirects
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

    def _get(self, url, allow_304=False, data=None):
        """(status, body, headers) for a 200 (or a 304 when asked). With
        `data`, the one POST post_form has already checked."""
        host = host_of(url)
        self._check(url)

        attempt = 0
        while True:
            status, headers, body = self._raw(url, data)
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

    def post_form(self, page_url, action_url, search):
        """The body a search form answers with: the one POST ever sent.

        Only for an action listed in FORM_POSTS, from the page listed with
        it, with `search` naming only the fields listed there. The page is
        fetched first (a GET, under every rule here) for its form, hidden
        fields, anti-forgery token and cookie; the form must still post to
        `action_url`; each search field must be one the form has, set to a
        value it offers where it offers a choice. Then that form, and
        nothing else, is submitted. Refused before any request otherwise.
        """
        rule = FORM_POSTS.get(action_url)
        if rule is None:
            raise Refused("a POST is sent only to a search form the owner "
                          "approved (FORM_POSTS); not to %s" % action_url)
        if page_url != rule["page"]:
            raise Refused("the form for %s is read from %s, not %s"
                          % (action_url, rule["page"], page_url))
        outside = sorted(set(search) - set(rule["search"]))
        if outside:
            raise Refused("only the form's search fields are set; not %s"
                          % ", ".join(outside))
        for name, value in search.items():
            if not isinstance(value, str) or len(value) > 40 or \
                    re.search(r"[\x00-\x1f]", value):
                raise Refused("%s=%r is not a search value" % (name, value))
        self._check(page_url)
        self._check(action_url)
        self._cookies = http.cookiejar.CookieJar()
        try:
            _status, page, _headers = self._get(page_url)
            forms = [f for f in page_forms(page, page_url)
                     if f["method"] == "post" and f["action"] == action_url]
            if len(forms) != 1:
                raise FetchFailed("%s no longer holds one form posting to %s;"
                                  " nothing was sent" % (page_url, action_url))
            form = forms[0]
            fields = dict((k, v) for k, v in form["fields"].items()
                          if v is not None)
            for name, value in sorted(search.items()):
                if name in form["choices"]:
                    if value not in form["choices"][name]:
                        raise Refused("%s=%r is not one of the form's own "
                                      "choices" % (name, value))
                elif name not in fields:
                    raise Refused("the form at %s has no field %s"
                                  % (page_url, name))
                fields[name] = value
            data = urllib.parse.urlencode(fields).encode("ascii")
            return self._get(action_url, data=data)[1]
        finally:
            self._cookies = None

    def _check(self, url):
        """Refused unless `url` may be requested: https, not blocked, read
        only, and allowed by robots.txt (or by the owner's decision). Run on
        the URL asked for and on every URL a redirect leads to."""
        host = host_of(url)
        if not url.lower().startswith("https://"):
            raise Refused("only https is fetched: %s" % url)
        override = override_for(url, self.overrides) \
            if self.override_log else None
        if blocked(host) and not (override and _under(host, ROBOTS_HOSTS)):
            raise Refused("%s is on the block list" % host)
        check_read_only(url)
        if urllib.parse.urlsplit(url).path == "/robots.txt":
            return   # robots.txt is never disallowed by robots.txt
        if override is None and not self.allowed(url):
            raise Refused("robots.txt on %s disallows %s" % (host, url))
        if override is not None and not self.allowed(url):
            # robots.txt says no, and the owner has decided to read this.
            self._override_due(url, override)

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

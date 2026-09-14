#!/usr/bin/env python3
"""What has changed in D-TRO since a given moment.

The bulk extract is a snapshot and it is NOT cut daily. Measured: on both the
morning and the afternoon of 14 September, `/dtros/all` served
`dtros_20260906_010013.csv` — the same file, nine days old. So a pipeline built
on the extract alone cannot be fresher than that, however often it runs, and
refreshing four times a day would fetch nine-day-old data four times.

`/events` is the live half. On the same afternoon it reported 54 events for
that day alone. Seed from the extract, then keep up with the feed: that is what
makes "a closure made this morning" reach a rider before they set off.

Two documented behaviours this has to live with, both awkward:

  * **Paging is at the D-TRO level, not the event level.** `pageSize = 50`
    fetches fifty ORDERS and expands them into events, so a page routinely
    holds more events than the size asked for. Confirmed against the service:
    `pageSize = 5` returned six. A loop that stops when a page looks "full"
    never stops.
  * **`totalCount` is hardcoded to -1.** There is no way to know how many
    pages there are; you page until one comes back short or empty.

And one that looks like a failure and is not: **404 means "no events match"**.
That is a successful empty answer, not an error, and telling the two apart is
the difference between "nothing changed today" and "the run failed".
"""
import json
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 60

# THE SERVICE RATE-LIMITS, AND DOES NOT DOCUMENT IT.
#
# TRO_SPEC.md 1.9 listed rate limits as unknown. They are no longer unknown:
# paging /events quickly returned **429 Too Many Requests** on 14 September
# 2026, after a run of back-to-back page requests. Found on production, because
# the credentials issued are production-only and there is no sandbox to find it
# in - which is why the email to d-tro@dft.gov.uk asks for the real figure.
#
# Until they answer, this is deliberately slower than it needs to be. A build
# that runs four times a day has hours to spare and no reason at all to be in
# a hurry; being throttled - or worse, being noticed - costs far more than the
# seconds saved.
PAUSE_BETWEEN_PAGES = 1.0

# On a 429: wait, then wait longer. Gives up rather than hammering.
BACKOFF_SECONDS = [5, 15, 45, 120]

# Fifty ORDERS per page. The service caps /search at 50 and states no bound for
# /events; this stays at the documented cap rather than probing for a higher
# one, because rate limits are undocumented (TRO_SPEC.md 1.9) and there is no
# sandbox to find them in.
PAGE_SIZE = 50

# A runaway guard, not an expectation. At fifty orders a page this is 25,000
# orders changed in one window, which has never happened; if it ever does, the
# right answer is to reseed from the extract rather than page forever.
MAX_PAGES = 500


def _post(url, access, body):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST")
    request.add_header("Authorization", "Bearer " + access)
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode())


def _with_backoff(call):
    """Run a request, waiting out a 429 rather than arguing with it.

    Honours `Retry-After` when the service sends one, because that is the
    service telling us exactly what it wants and guessing instead would be
    rude. Falls back to a fixed ladder when it does not.
    """
    for attempt, pause in enumerate(BACKOFF_SECONDS + [None]):
        try:
            return call()
        except urllib.error.HTTPError as e:
            if e.code != 429 or pause is None:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                wait = float(retry_after) if retry_after else pause
            except (TypeError, ValueError):
                wait = pause
            print("  rate limited; waiting %.0fs (attempt %d)"
                  % (wait, attempt + 1), flush=True)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def changes(base, access, since, to, on_page=None):
    """Every event between two ISO timestamps, as a list.

    Returns [] when nothing changed — including on a 404, which the service
    documents as "no events match the search criteria".
    """
    url = base.rstrip("/") + "/events"
    out = []
    for page in range(1, MAX_PAGES + 1):
        body = {"since": since, "to": to, "page": page, "pageSize": PAGE_SIZE}
        if page > 1:
            time.sleep(PAUSE_BETWEEN_PAGES)
        try:
            reply = _with_backoff(lambda: _post(url, access, body))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break  # documented empty result
            raise
        events = reply.get("events") if isinstance(reply, dict) else None
        if not events:
            break
        out.extend(events)
        if on_page:
            on_page(page, len(events), len(out))
        # Page until one comes back SHORT IN ORDERS. Events cannot be counted
        # against pageSize because the paging is by order, so the only honest
        # signal is a page that produced fewer distinct orders than asked for.
        if len({e.get("id") for e in events if isinstance(e, dict)}) < PAGE_SIZE:
            break
    return out


def hydrate(base, access, dtro_id):
    """The full record for one D-TRO, in the same shape the extract carries.

    `GET /dtros/{id}` wraps it: the record the rest of this pipeline expects is
    under `data`. Returns None when it has gone.
    """
    url = "%s/dtros/%s" % (base.rstrip("/"), dtro_id)
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", "Bearer " + access)
    def fetch():
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode())

    try:
        body = _with_backoff(fetch)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return None
        raise
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else None


def main():
    """Print what has changed in a window, for a human checking the feed."""
    import datetime
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dtro_token import load_env, token

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    env = load_env()
    base = env.get("DTRO_BASE_URL", "").strip()
    access = token(base, env["DTRO_CLIENT_ID"].strip(),
                   env["DTRO_CLIENT_SECRET"].strip())["access_token"]

    today = datetime.datetime.now(datetime.timezone.utc).date()
    since = (today - datetime.timedelta(days=days - 1)).isoformat()
    to = (today + datetime.timedelta(days=1)).isoformat()
    print("events %s .. %s" % (since, to))

    events = changes(base, access, since + "T00:00:00Z", to + "T00:00:00Z",
                     on_page=lambda p, n, t: print("  page %d: %d events (%d)"
                                                   % (p, n, t)))
    kinds = {}
    orders = set()
    for event in events:
        kinds[event.get("eventType")] = kinds.get(event.get("eventType"), 0) + 1
        orders.add(event.get("id"))
    print("%d events over %d orders" % (len(events), len(orders)))
    for kind, count in sorted(kinds.items()):
        print("  %-8s %d" % (kind, count))
    return 0


if __name__ == "__main__":
    sys.exit(main())

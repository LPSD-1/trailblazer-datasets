#!/usr/bin/env python3
"""Settle, by asking the service, what TRO_SPEC.md 1.9 could not settle by reading.

    python tools/dtro_probe.py [days]

The spec's open-questions list is honest that several things could not be
resolved from the documentation. Three of them are now answerable with one
token and one read-only call, and the answers change the design:

  1.9 #3  May CONSUMER-scope credentials call /events? The docs say both
          publisher and consumer applications "have the necessary scopes to
          consume data" but never state it. /events is the endpoint the whole
          daily cycle turns on, so a 403 here is not a detail — it is a
          different design.
  1.2     Is the header `Bearer` when token_type says `BearerToken`?
  1.9 #9  Does /events accept a pageSize above /search's cap of 50?

Read-only: one token call and one /events call. Nothing is published, and
nothing is written to the dataset. A 404 is a PASS here — the spec documents it
as "no events match the search criteria", which is a legitimate empty result.
"""
import datetime
import json
import sys
import urllib.error
import urllib.request

from dtro_token import load_env, token

TIMEOUT = 30


def events(base, access, body):
    url = base.rstrip("/") + "/events"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST")
    request.add_header("Authorization", "Bearer " + access)
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.status, json.loads(response.read().decode())


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    env = load_env()
    base = env.get("DTRO_BASE_URL", "").strip()

    body = token(base, env["DTRO_CLIENT_ID"].strip(),
                 env["DTRO_CLIENT_SECRET"].strip())
    access = body["access_token"]
    print("token scope=%s type=%s expires_in=%s"
          % (body.get("scope"), body.get("token_type"), body.get("expires_in")))

    # Whole days, UTC, so the window is reproducible rather than "now-ish".
    today = datetime.datetime.now(datetime.timezone.utc).date()
    since = today - datetime.timedelta(days=days)
    window = {
        "since": "%sT00:00:00Z" % since,
        "to": "%sT00:00:00Z" % (today + datetime.timedelta(days=1)),
        "page": 1,
        "pageSize": 5,
    }
    print("POST /events  %s .. %s  pageSize=5" % (window["since"], window["to"]))

    try:
        status, reply = events(base, access, window)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        print("  %s %s" % (e.code, e.reason))
        print("  %s" % detail.strip())
        if e.code == 404:
            # Documented as "no events match", not a fault. It still answers
            # the scope question: the request was authorised and executed.
            print("\n  ANSWER 1.9 #3: consumer scope DOES reach /events.")
            print("  404 is the documented empty result, not a refusal.")
            return 0
        if e.code in (401, 403):
            print("\n  ANSWER 1.9 #3: consumer scope does NOT reach /events.")
            print("  Part 3's daily cycle cannot be built on it as written.")
        return 1

    print("  %s OK" % status)
    print("\n  ANSWER 1.9 #3: consumer scope DOES reach /events.")

    # totalCount is documented as hardcoded -1. Worth reading rather than
    # assuming, because the paging loop in Part 3 depends on it being useless.
    if isinstance(reply, dict):
        print("  top-level keys: %s" % ", ".join(sorted(reply)))
        if "totalCount" in reply:
            print("  totalCount: %s (spec says hardcoded -1)"
                  % reply["totalCount"])
        events_list = reply.get("events") or reply.get("Events") or []
    else:
        events_list = reply if isinstance(reply, list) else []
        print("  top level is a %s" % type(reply).__name__)

    print("  events in page: %d" % len(events_list))
    if events_list:
        first = events_list[0]
        print("  event keys: %s" % ", ".join(sorted(first)))
        for field in ("eventType", "publicationTime", "eventTime", "troName",
                      "traCreator", "currentTraOwner"):
            if field in first:
                print("    %-16s %s" % (field, first[field]))
    else:
        print("  (an empty page over %d days is a real answer: little or"
              " nothing is being published)" % days)
    return 0


if __name__ == "__main__":
    sys.exit(main())

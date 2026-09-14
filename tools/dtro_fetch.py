#!/usr/bin/env python3
"""Fetch the national D-TRO extract.

    python tools/dtro_fetch.py [out_dir]

`GET /dtros/all` does not return the data. It returns a signed Google Storage
URL, valid for an hour, pointing at a CSV whose `Data` column holds each order
as JSON - geometry included. So the whole national corpus is one download and
needs no per-order hydration, which is what makes a daily rebuild cheap.

The extract is a SNAPSHOT, not a live view. Measured: on 14 September the URL
served `dtros_20260906_010013.csv`, eight days old. That is fine for a baseline
and is exactly why `/events` exists for the deltas - but it does mean the pack
can never be fresher than whatever the service last cut, and nothing downstream
should imply otherwise. The cut date travels with the pack.

Credentials come from `.env`; see `.env.example`. Nothing here prints a secret.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dtro_token import load_env, token  # noqa: E402

TIMEOUT = 120


def signed_url(base, access):
    request = urllib.request.Request(base.rstrip("/") + "/dtros/all",
                                     method="GET")
    request.add_header("Authorization", "Bearer " + access)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode())


def download_corpus(out_dir):
    """Download the extract and return the local path.

    Named from the SERVICE's own filename, which carries the date the extract
    was cut. That name is what makes the build reproducible: the pack is
    stamped with the cut date rather than with today's, so rebuilding an
    unchanged extract produces identical bytes.
    """
    env = load_env()
    base = env.get("DTRO_BASE_URL", "").strip()
    body = token(base, env["DTRO_CLIENT_ID"].strip(),
                 env["DTRO_CLIENT_SECRET"].strip())
    url = signed_url(base, body["access_token"])

    name = url.split("?")[0].rsplit("/", 1)[-1] or "dtros_all.csv"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)

    # Already have this exact cut: nothing to fetch. The extract is half a
    # gigabyte and is rebuilt by the service on its own schedule, so a build
    # run twice in one day should not pull it twice.
    if os.path.isfile(path) and os.path.getsize(path) > 1_000_000:
        print("using the copy already here: %s" % name)
        return path

    print("downloading %s" % name)
    urllib.request.urlretrieve(url, path)
    print("  %.1f MB" % (os.path.getsize(path) / 1e6))
    return path


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    print(download_corpus(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

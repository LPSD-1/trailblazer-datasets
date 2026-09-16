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
    #
    # THE CACHED FILE IS ONLY TRUSTED IF IT WAS FINISHED. This test used to be
    # `size > 1 MB` against a file `urlretrieve` wrote in place, so a network
    # blip left a partial CSV on disk that every later run accepted as
    # complete — and the workflow caches this directory, so the truncated copy
    # came back for as long as the service kept the same filename, which has
    # been nine days. A pack built from it would publish four hundred of
    # thirty-six thousand restrictions and look exactly like a quiet day: every
    # closed road in the country shown open, with nothing anywhere saying so.
    #
    # Downloading beside the target and renaming only on success is what makes
    # "the file is here" mean "the file is whole". A rename within one
    # directory is atomic on every filesystem this runs on.
    done = path + ".done"
    if os.path.isfile(path) and os.path.isfile(done):
        print("using the copy already here: %s" % name)
        return path

    part = path + ".part"
    print("downloading %s" % name)
    if os.path.isfile(done):
        os.remove(done)
    _, headers = urllib.request.urlretrieve(url, part)

    # The service sends Content-Length; when it does, a short file is a failed
    # download however cleanly urlretrieve returned.
    got = os.path.getsize(part)
    expected = headers.get("Content-Length")
    if expected is not None:
        try:
            expected = int(expected)
        except ValueError:
            expected = None
    if expected is not None and got != expected:
        os.remove(part)
        raise IOError("short download: got %d bytes of %d for %s"
                      % (got, expected, name))
    if got < 1_000_000:
        os.remove(part)
        raise IOError("the extract is %d bytes, which is not an extract" % got)

    os.replace(part, path)
    with open(done, "w", encoding="utf-8") as handle:
        handle.write(str(got) + chr(10))
    print("  %.1f MB" % (got / 1e6))
    _forget_older_cuts(out_dir, keep=name)
    return path


def _forget_older_cuts(out_dir, keep):
    """Drop extracts from earlier cuts.

    The filename carries the cut date, so a new cut arrives under a NEW name
    and the old one stays beside it for ever. Each is about half a gigabyte and
    the workflow caches this directory between runs, so it grew by an extract
    per cut with nothing ever removing one - until it passed the repository's
    cache allowance and began evicting whatever else was in there, which
    includes the council-data cache that turns a 25-minute fetch into seconds.

    Only ever removes what this module writes: an extract, its `.done` marker,
    or an abandoned `.part`.
    """
    # Exactly two names survive, and `.part` is not one of them even for the
    # cut in use: the marker is written only after the size check, so a `.part`
    # sitting beside a finished file is always the wreckage of a run that died
    # and is always the same half gigabyte as a real extract.
    survives = {keep, keep + ".done"}
    for entry in os.listdir(out_dir):
        if entry in survives:
            continue
        try:
            os.remove(os.path.join(out_dir, entry))
            print("  forgot the earlier cut %s" % entry)
        except OSError:
            # Best effort. A file that will not delete costs disk, not
            # correctness: which cut gets used is decided by name above.
            pass


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    print(download_corpus(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

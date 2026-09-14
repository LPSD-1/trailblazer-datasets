#!/usr/bin/env python3
"""Exchange the D-TRO credentials for a token, and say what came back.

    python tools/dtro_token.py

The first thing to run when credentials arrive. It answers three questions that
everything else in the D-TRO work depends on, and it answers them by asking the
service rather than by assuming:

  1. Do these credentials work at all?
  2. WHICH of the three fields the portal issues is the client ID? The portal
     gives an App ID, an API Key and an API Secret; the token call wants a
     client ID and a secret (TRO_SPEC.md 1.2). Two names, three values. This
     tries the documented mapping first and the other candidate second, and
     tells you which one the service accepted.
  3. What SCOPE do they carry? A consumer application's scope differs from a
     publisher's, and the publishing endpoints reject consumer credentials — so
     a token is not proof that the intended endpoints will answer.

It prints no secret. The token is reported by length and by its first eight
characters, which is enough to tell two tokens apart in a log and useless to
anybody who reads one.

Exit code 0 means a token came back.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV = os.path.join(ROOT, ".env")

TIMEOUT = 30


def load_env(path=ENV):
    """Read .env into a dict. No dependency on python-dotenv for five lines.

    Values are taken verbatim after the first `=`, with surrounding quotes
    stripped — a secret that happens to contain `=` is not a reason to fail.
    """
    if not os.path.isfile(path):
        sys.exit(
            "No .env found at %s\n\n"
            "Copy .env.example to .env and fill in the three fields the D-TRO "
            "portal gave you." % path)
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def token(base_url, client_id, secret):
    """POST /oauth-generator. Returns the parsed body, or raises HTTPError."""
    url = base_url.rstrip("/") + "/oauth-generator"
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    basic = base64.b64encode(
        ("%s:%s" % (client_id, secret)).encode()).decode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", "Basic " + basic)
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode())


def describe(body):
    access = body.get("access_token") or ""
    print("  token      %d chars, starts %s…" % (len(access), access[:8]))
    for field in ("scope", "token_type", "expires_in", "issued_at"):
        if field in body:
            print("  %-10s %s" % (field, body[field]))
    # The scope is the part that decides whether the consumer endpoints will
    # answer, so it is worth a sentence rather than a field dump.
    scope = str(body.get("scope", ""))
    if scope and "publish" in scope.lower():
        print("\n  NOTE: this scope mentions publishing. TRO_SPEC.md 1.2 says a")
        print("  consumer scope differs from a publisher's; check which was")
        print("  issued before reading anything into a 403 later.")


KNOWN_BASES = [
    ("integration", "https://dtro-integration.dft.gov.uk/v1"),
    ("production", "https://dtro.dft.gov.uk/v1"),
]


def try_base(base, attempts):
    """Every candidate client ID against one environment.

    Returns the parsed body and the label that worked, or (None, None) when
    every candidate came back 401/400 — the case the next environment exists
    for. Anything else is not a question of which field is which, so it stops.
    """
    for label, candidate in attempts:
        if not candidate:
            continue
        print("  trying %s" % label)
        try:
            return token(base, candidate, attempts.secret), label
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            print("    %s %s  %s" % (e.code, e.reason, detail.strip()))
            if e.code not in (400, 401):
                return None, None
        except urllib.error.URLError as e:
            print("    could not reach %s: %s" % (base, e.reason))
            return None, None
    return None, None


class Attempts(list):
    """The candidate client IDs, carrying the secret they all share."""
    secret = ""


def main():
    env = load_env()
    base = env.get("DTRO_BASE_URL", "").strip()
    if not base:
        sys.exit("DTRO_BASE_URL is empty. See .env.example for the two URLs.")

    key = env.get("DTRO_CLIENT_ID", "").strip()
    secret = env.get("DTRO_CLIENT_SECRET", "").strip()
    app_id = env.get("DTRO_APP_ID", "").strip()
    if not secret:
        sys.exit("DTRO_CLIENT_SECRET is empty.")

    print("D-TRO token check")
    print("  app id     %s" % ("set" if app_id else "EMPTY"))
    print("  client id  %s" % ("set" if key else "EMPTY"))
    print("  secret     %s" % ("set" if secret else "EMPTY"))
    print()

    # The documented mapping first: API Key as the client ID. Then the other
    # candidate, because two names for three values is a guess until the
    # service settles it.
    attempts = Attempts([("DTRO_CLIENT_ID (the API Key)", key)])
    if app_id and app_id != key:
        attempts.append(("DTRO_APP_ID", app_id))
    attempts.secret = secret

    # The configured environment first, then the other one. Credentials are
    # per environment and a pair pointed at the wrong one fails as 401 with
    # the same wording as a bad secret — so the only way to tell those apart
    # is to ask the other environment, which costs one request.
    bases = [b for b in [base] if b]
    for name, url in KNOWN_BASES:
        if url.rstrip("/") != base.rstrip("/"):
            bases.append(url)

    for url in bases:
        named = dict((u.rstrip("/"), n) for n, u in KNOWN_BASES)
        print("%s  (%s)" % (url, named.get(url.rstrip("/"), "unrecognised URL")))
        body, label = try_base(url, attempts)
        if body is None:
            print()
            continue
        print("  OK")
        describe(body)
        print()
        print("Client ID: %s" % label)
        print("Environment: %s" % url)
        if url.rstrip("/") != base.rstrip("/"):
            print("NOTE: that is NOT the DTRO_BASE_URL in .env. Change it, or")
            print("every later call repeats this 401.")
        print("Both are worth writing into TRO_SPEC.md 1.2, which describes the")
        print("pair without saying which portal field is which.")
        return 0

    print("Rejected by every environment, with both fields tried as the client")
    print("ID. The error says the CLIENT ID is unrecognised rather than the")
    print("secret, and the values are well-formed, so this is most likely an")
    print("account that is registered but not yet activated for the API.")
    print()
    print("That is a question for d-tro@dft.gov.uk: quote the App ID (it is an")
    print("account identifier, not a secret), say which environment you were")
    print("issued for, and ask whether the application has been approved.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

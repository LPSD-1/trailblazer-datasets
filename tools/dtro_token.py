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
    print("  base       %s" % base)
    print("  app id     %s" % ("set" if app_id else "EMPTY"))
    print("  client id  %s" % ("set" if key else "EMPTY"))
    print("  secret     %s" % ("set" if secret else "EMPTY"))
    print()

    # The documented mapping first: API Key as the client ID.
    attempts = [("DTRO_CLIENT_ID (the API Key)", key)]
    # And the other candidate, because two names for three values is a guess
    # until the service settles it. Skipped when they are the same string or
    # the app id is absent.
    if app_id and app_id != key:
        attempts.append(("DTRO_APP_ID", app_id))

    last = None
    for label, candidate in attempts:
        if not candidate:
            continue
        print("Trying %s as the client ID…" % label)
        try:
            body = token(base, candidate, secret)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            print("  %s %s" % (e.code, e.reason))
            if detail.strip():
                print("  %s" % detail.strip())
            last = e
            # 401 is "wrong credentials", which is the case the next attempt
            # exists for. Anything else is not about which field is which.
            if e.code not in (400, 401):
                break
            print()
            continue
        except urllib.error.URLError as e:
            sys.exit("  could not reach %s: %s" % (base, e.reason))

        print("  OK")
        describe(body)
        print("\nUse %s as the client ID. Worth writing into TRO_SPEC.md 1.2,"
              % label)
        print("which describes the pair without saying which portal field is")
        print("which.")
        return 0

    print()
    if last is not None and last.code in (400, 401):
        print("Neither field was accepted as the client ID.")
        print("Check the secret first — it is the one that is easy to truncate")
        print("when copying. Then check DTRO_BASE_URL: credentials are per")
        print("environment, and a sandbox pair pointed at production fails as")
        print("401, which looks exactly like a wrong secret.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

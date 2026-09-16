"""Tests for release signing.

These generate their own throwaway key rather than touching the real one: a
test that needs the publisher's private key is a test nobody can run in CI, and
a private key that has to be present for tests to pass is a private key that
ends up somewhere it should not.

Run: python tools/test_sign_release.py
"""

import os
import sys
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sign_release  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


def test_roundtrip():
    print("a signature made here verifies here")
    key = Ed25519PrivateKey.generate()
    pub = key.public_key()
    payload = b"a container full of rights of way"

    sig = sign_release.sign_bytes(payload, key)
    check("signatures are 64 bytes", len(sig) == SIG_LEN, len(sig))
    check("it verifies", sign_release.verify_bytes(payload, sig, pub))


def test_tampering_is_caught():
    print("and does not verify for anything else")
    key = Ed25519PrivateKey.generate()
    pub = key.public_key()
    payload = b"this byway is open"
    sig = sign_release.sign_bytes(payload, key)

    # THE CASE THIS EXISTS FOR. A substituted container, sealed under the key
    # the app ships and hashed into a substituted catalogue, telling a rider a
    # closed byway is open.
    check("a changed payload fails",
          not sign_release.verify_bytes(b"this byway is shut", sig, pub))

    # One bit, in the middle of a lane's geometry.
    flipped = bytearray(payload)
    flipped[5] ^= 0x01
    check("one flipped bit fails",
          not sign_release.verify_bytes(bytes(flipped), sig, pub))

    # A valid signature from the wrong signer.
    other = Ed25519PrivateKey.generate()
    check("another key's signature fails",
          not sign_release.verify_bytes(
              payload, sign_release.sign_bytes(payload, other), pub))


def test_malformed_signatures():
    print("a malformed signature is a no, not an exception")
    key = Ed25519PrivateKey.generate()
    pub = key.public_key()
    payload = b"x" * 100
    sig = sign_release.sign_bytes(payload, key)

    # A truncated download, which is far more likely than a forgery.
    check("a short signature fails", not sign_release.verify_bytes(payload, sig[:-1], pub))
    check("a long signature fails", not sign_release.verify_bytes(payload, sig + b"\x00", pub))
    check("an empty signature fails", not sign_release.verify_bytes(payload, b"", pub))


def test_signing_a_file():
    print("signing a file writes a .sig beside it")
    key = Ed25519PrivateKey.generate()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "motor-derbyshire.tbmap")
        body = os.urandom(5000)
        with open(path, "wb") as fh:
            fh.write(body)

        sig, digest = sign_release.sign_file(path, key)
        check("the .sig is there", os.path.exists(path + ".sig"))
        with open(path + ".sig", "rb") as fh:
            on_disk = fh.read()
        check("and holds the signature", on_disk == sig)
        check("which verifies over the WHOLE file",
              sign_release.verify_bytes(body, on_disk, key.public_key()))
        check("the digest is the file's", len(digest) == 64)


def test_the_key_never_comes_from_argv():
    print("the private key cannot be passed on a command line")
    # argv is readable by every process on the machine, so the key arrives by
    # file or environment only. Asserted by READING THE SOURCE rather than by
    # driving argparse: the convenient thing for somebody to add later is
    # exactly `--key`, and this notices the moment they do.
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "sign_release.py"), "r", encoding="utf-8") as fh:
        source = fh.read()
    banned = ["--key", "--private-key", "--signing-key", "--secret",
              "--private", "--pem-key"]
    found = [flag for flag in banned if '"%s"' % flag in source]
    check("no argument takes a private key", not found, found)
    # And the two ways it IS allowed to arrive are both present.
    check("it reads a key file from the environment",
          "TB_SIGNING_KEY" in source)
    check("and a PEM from the environment, as CI provides it",
          "TB_SIGNING_KEY_PEM" in source)


def test_missing_key_is_a_clear_refusal():
    print("no key is a refusal that says what to do")
    saved = (os.environ.pop("TB_SIGNING_KEY", None),
             os.environ.pop("TB_SIGNING_KEY_PEM", None))
    try:
        sign_release._private_key()
        check("it refuses", False, "it did not refuse")
    except SystemExit as e:
        message = str(e)
        check("it refuses", True)
        check("and names the environment variables",
              "TB_SIGNING_KEY" in message and "TB_SIGNING_KEY_PEM" in message)
    finally:
        if saved[0]:
            os.environ["TB_SIGNING_KEY"] = saved[0]
        if saved[1]:
            os.environ["TB_SIGNING_KEY_PEM"] = saved[1]


def test_key_from_environment():
    print("a key can arrive as a secret in the environment, as CI gives it")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    saved = os.environ.pop("TB_SIGNING_KEY", None)
    os.environ["TB_SIGNING_KEY_PEM"] = pem
    try:
        loaded = sign_release._private_key()
        payload = b"from CI"
        check("and it signs with it",
              sign_release.verify_bytes(
                  payload, loaded.sign(payload), key.public_key()))
    finally:
        del os.environ["TB_SIGNING_KEY_PEM"]
        if saved:
            os.environ["TB_SIGNING_KEY"] = saved


SIG_LEN = sign_release.SIGNATURE_LEN


def main():
    for fn in (test_roundtrip, test_tampering_is_caught,
               test_malformed_signatures, test_signing_a_file,
               test_the_key_never_comes_from_argv,
               test_missing_key_is_a_clear_refusal,
               test_key_from_environment):
        fn()
    print()
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all signing tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

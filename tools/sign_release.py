"""Sign published files, so the app can tell the publisher's data from anyone's.

WHAT THIS CLOSES. Packs are sealed with AES-256-GCM under a key that ships in
the app - `PackageKey` says so itself: "can be recovered by someone determined",
"a speed bump, not a lock". So a substituted catalogue can point at a
substituted pack sealed under that same recovered key, and the app believes it.
The SHA-256 in the catalogue does not help, because whoever rewrote the
catalogue wrote the hash too.

For an app whose job is telling a rider what they may legally ride, a tampered
pack saying a closed byway is open is a safety problem. That is worth more than
secrecy over data councils publish anyway, which is why
docs/MAP_ARCHITECTURE.md section 7 drops the sealing of lane data and adds this.

The verification half has existed in the app since the first version -
`Tbpack.verify`, Ed25519 - and has never had a caller, because nothing signed
anything. This is the missing half.

KEY HANDLING. The private key never appears in a command line or in output. It
is read from a file named by TB_SIGNING_KEY, or from TB_SIGNING_KEY_PEM in the
environment, which is how it arrives in CI as a repository secret. It is never
logged, never echoed, and this module has no debug mode that would.

Usage:
    python tools/sign_release.py --sign FILE [FILE ...]      -> FILE.sig
    python tools/sign_release.py --verify FILE --sig FILE.sig
    python tools/sign_release.py --public-key                -> base64, for the app
"""

import argparse
import base64
import hashlib
import os
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: Ed25519 signatures are always this long, and the app checks it before doing
#: any work. A short signature is a corrupted download, not a forgery attempt,
#: but both get the same answer.
SIGNATURE_LEN = 64


def _private_key():
    """The signing key, from a file or the environment, never from argv."""
    pem = os.environ.get("TB_SIGNING_KEY_PEM")
    if pem:
        data = pem.encode("utf-8")
    else:
        path = os.environ.get("TB_SIGNING_KEY")
        if not path:
            raise SystemExit(
                "No signing key. Set TB_SIGNING_KEY to a PEM file, or "
                "TB_SIGNING_KEY_PEM to its contents. Never pass it as an "
                "argument - argv is visible to every process on the machine."
            )
        with open(path, "rb") as fh:
            data = fh.read()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("The signing key is not an Ed25519 private key.")
    return key


def _public_key(path=None):
    path = path or os.environ.get("TB_SIGNING_PUBLIC_KEY")
    if not path:
        raise SystemExit("Set TB_SIGNING_PUBLIC_KEY to the public PEM.")
    with open(path, "rb") as fh:
        key = serialization.load_pem_public_key(fh.read())
    if not isinstance(key, Ed25519PublicKey):
        raise SystemExit("That is not an Ed25519 public key.")
    return key


def sign_bytes(payload, key=None):
    """Raw 64-byte signature over [payload]."""
    return (key or _private_key()).sign(payload)


def verify_bytes(payload, signature, key=None):
    if len(signature) != SIGNATURE_LEN:
        return False
    try:
        (key or _public_key()).verify(signature, payload)
        return True
    except InvalidSignature:
        return False


def sign_file(path, key=None):
    """Write `path.sig` and return (signature, sha256) for the catalogue.

    THE WHOLE FILE IS SIGNED, not its hash as recorded somewhere else. Signing a
    digest that the catalogue also states would let a substituted catalogue
    choose the thing being vouched for.
    """
    with open(path, "rb") as fh:
        payload = fh.read()
    signature = sign_bytes(payload, key)
    with open(path + ".sig", "wb") as fh:
        fh.write(signature)
    return signature, hashlib.sha256(payload).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--sign", nargs="+", metavar="FILE")
    group.add_argument("--verify", metavar="FILE")
    group.add_argument("--public-key", action="store_true",
                       help="print the public key as base64, for the app")
    ap.add_argument("--sig", metavar="FILE", help="with --verify")
    ap.add_argument("--public-pem", help="with --verify or --public-key")
    args = ap.parse_args()

    if args.public_key:
        raw = _public_key(args.public_pem).public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        print(base64.b64encode(raw).decode("ascii"))
        return 0

    if args.verify:
        sig_path = args.sig or (args.verify + ".sig")
        with open(args.verify, "rb") as fh:
            payload = fh.read()
        with open(sig_path, "rb") as fh:
            signature = fh.read()
        ok = verify_bytes(payload, signature, _public_key(args.public_pem))
        print("%s: %s" % (args.verify, "ok" if ok else "FAILED"))
        return 0 if ok else 1

    key = _private_key()
    for path in args.sign:
        signature, digest = sign_file(path, key)
        # The signature is printed as base64 for the catalogue writer. The KEY
        # is not printed, here or anywhere.
        print("%s  sha256=%s  sig=%s"
              % (os.path.basename(path), digest,
                 base64.b64encode(signature).decode("ascii")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign the list of withdrawn releases (UPD-402).

    python scripts/release/sign_revocations.py --out dist/

Reads release/revocations.json (this checkout), checks it is a list the
updater reads, and writes dist/release-revocations.json with its .sig,
signed with RELEASE_SIGNING_KEY / RELEASE_SIGNING_KEY_ID as the manifests
are. The release workflow attaches both to every release; to withdraw a
release, add it to release/revocations.json on main and re-run the
release-manifest workflow for the latest release tag.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))

from updater import manifest, revocations  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", type=Path, default=REPO / "release" / "revocations.json")
    ap.add_argument("--out", type=Path, default=Path("dist"))
    args = ap.parse_args(argv)

    raw = args.source.read_bytes()
    withdrawn = revocations.parse(raw)  # the updater's own checks
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / revocations.NAME).write_bytes(raw)
    print(f"{len(withdrawn)} withdrawn release(s): {', '.join(sorted(withdrawn)) or 'none'}", file=sys.stderr)

    secret = os.environ.get("RELEASE_SIGNING_KEY", "").strip()
    if not secret:
        print("RELEASE_SIGNING_KEY not set: the list is left UNSIGNED (the updater will refuse it)", file=sys.stderr)
        return 0
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key_id = os.environ.get("RELEASE_SIGNING_KEY_ID", "").strip()
    if not key_id:
        raise SystemExit("RELEASE_SIGNING_KEY_ID must name the key")
    sig = manifest.sign(raw, Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret, validate=True)), key_id)
    revocations.verify(raw, sig)  # with the keys this commit ships
    (args.out / revocations.SIGNATURE_NAME).write_bytes(sig)
    print(f"signed with {key_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

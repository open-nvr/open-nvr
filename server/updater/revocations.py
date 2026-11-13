# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Withdrawn releases (UPD-402): a release found bad after it shipped.

``release-revocations.json`` lists versions no site should install, each
with a reason, and is signed with the release key like a manifest (same
Ed25519 check over the exact bytes). The project keeps the list in
``release/revocations.json`` and the release workflow attaches the signed
copy to every release, so ``…/releases/latest/download/`` always has the
current list. A bundle carries the list as it was when the bundle was made.

No file means nothing is withdrawn. A file whose signature does not check
out stops the update: someone changed it, and it is not safe to guess
which releases it was hiding.
"""

from __future__ import annotations

import json
from pathlib import Path

from updater.manifest import ManifestError, check_signature, parse_version

NAME = "release-revocations.json"
SIGNATURE_NAME = NAME + ".sig"


def parse(raw: bytes) -> dict[str, str]:
    """{version: reason} from the (already verified) file."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ManifestError("the revocation list is not valid JSON") from exc
    if not isinstance(doc, dict) or doc.get("schema") != 1 or not isinstance(doc.get("revoked"), list):
        raise ManifestError("the revocation list is not one this updater reads")
    out = {}
    for item in doc["revoked"]:
        if not isinstance(item, dict) or not isinstance(item.get("version"), str):
            raise ManifestError("an entry in the revocation list is malformed")
        parse_version(item["version"])
        out[item["version"].removeprefix("v")] = str(item.get("reason") or "withdrawn by the OpenNVR project")
    return out


def verify(raw: bytes, signature: bytes, keys=None) -> dict[str, str]:
    """The withdrawn versions, if *signature* is a trusted key's over *raw*."""
    check_signature(raw, signature, keys, what="revocation list")
    return parse(raw)


def from_dir(path: Path) -> dict[str, str]:
    """The list in a directory (a bundle); none there means none withdrawn."""
    raw_f, sig_f = path / NAME, path / SIGNATURE_NAME
    if not raw_f.is_file():
        return {}
    if not sig_f.is_file():
        raise ManifestError(f"{path} has a revocation list without its signature")
    return verify(raw_f.read_bytes(), sig_f.read_bytes())


def fetch(base: str | None = None, *, timeout: float = 30.0) -> dict[str, str]:
    """The current list from GitHub or a mirror; a 404 means none withdrawn."""
    import httpx

    from updater.source import GITHUB_RELEASES

    if base is None or base.rstrip("/") == GITHUB_RELEASES:
        root = f"{GITHUB_RELEASES}/latest/download"
    else:
        root = f"{base.rstrip('/')}/latest"
    got = []
    for name in (NAME, SIGNATURE_NAME):
        try:
            r = httpx.get(f"{root}/{name}", follow_redirects=True, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ManifestError(f"could not fetch the revocation list: {exc}") from exc
        if r.status_code == 404:
            if name == NAME:
                return {}
            raise ManifestError("the revocation list is published without its signature")
        if r.status_code != 200:
            raise ManifestError(f"the revocation list answered HTTP {r.status_code}")
        got.append(r.content)
    return verify(got[0], got[1])

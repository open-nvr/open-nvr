# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The signed release manifest: the only thing the updater trusts.

A release publishes ``release-manifest.json`` and ``release-manifest.json.sig``.
The signature is Ed25519 over the manifest file's exact bytes (no
re-serialising, so nothing can differ between what was signed and what is
read), made by the project's release key in CI. The public keys this build
trusts are in ``updater/keys.json``; several can be listed, so a key is
rotated by a release that trusts both. Verification needs no network, which
is what lets an air-gapped site check a bundle carried in on a USB stick
exactly as an online site checks a download.

The manifest pins every image by registry digest (what ``docker pull``
fetches) and by image ID per platform (what ``docker load`` and
``docker inspect`` show), so both online and offline installs are checked
against it.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from updater import UPDATER_PROTOCOL

MANIFEST_NAME = "release-manifest.json"
SIGNATURE_NAME = MANIFEST_NAME + ".sig"
SCHEMA = 1

#: Extra trusted keys for testing the update flow with a key of your own
#: (a JSON file shaped like keys.json). Whoever can set this variable on the
#: host already controls the stack, so it grants nothing new.
EXTRA_KEYS_ENV = "OPENNVR_UPDATE_EXTRA_KEYS"

_KEYS_FILE = Path(__file__).with_name("keys.json")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PIN_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


class ManifestError(Exception):
    """The manifest is not genuine, not readable, or not for this updater.
    The message is meant for the operator."""


# ── versions ─────────────────────────────────────────────────────────


def parse_version(v: str) -> tuple:
    """A sortable key for ``X.Y.Z`` or ``X.Y.Z-pre``; a pre-release sorts
    before its release, and pre-release parts compare numerically when both
    are numbers (rc.2 < rc.10)."""
    m = _VERSION.match((v or "").strip().removeprefix("v"))
    if not m:
        raise ManifestError(f"not a release version: {v!r}")
    major, minor, patch, pre = m.groups()
    if pre is None:
        pre_key: tuple = (1,)
    else:
        parts = []
        for part in pre.split("."):
            parts.append((0, int(part), "") if part.isdigit() else (1, 0, part))
        pre_key = (0, tuple(parts))
    return (int(major), int(minor), int(patch), pre_key)


def is_newer(candidate: str, installed: str) -> bool:
    return parse_version(candidate) > parse_version(installed)


def upgrade_kind(installed: str, candidate: str) -> str:
    """"patch", "minor" or "major": how big a step this update is."""
    a, b = parse_version(installed), parse_version(candidate)
    if a[0] != b[0]:
        return "major"
    if a[1] != b[1]:
        return "minor"
    return "patch"


# ── the manifest ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Image:
    #: Repository and tag as compose names it, e.g. ``ghcr.io/open-nvr/core:0.1.9``.
    ref: str
    #: Registry manifest(-list) digest: what ``docker pull ref@digest`` fetches.
    digest: str
    #: Image ID (config digest) per platform, e.g. ``{"linux/amd64": "sha256:…"}``:
    #: what ``docker inspect``/``docker load`` show on a host of that platform.
    ids: dict[str, str]
    #: The registry manifest digest of each platform's image (an entry of the
    #: index). An offline bundle holds single-platform images, and a
    #: containerd image store reports that digest as the loaded image's ID.
    platform_manifests: dict[str, str] = field(default_factory=dict)

    @property
    def repository(self) -> str:
        return self.ref.rsplit(":", 1)[0] if ":" in self.ref.rsplit("/", 1)[-1] else self.ref


@dataclass(frozen=True)
class ManualStep:
    id: str
    text: str


@dataclass(frozen=True)
class Manifest:
    version: str
    channel: str
    git_commit: str
    git_tag: str
    alembic_head: str
    #: Every migration this release knows (None in manifests made before the
    #: field existed): a database on a revision outside it is newer than the
    #: release, and the update is refused before anything changes.
    alembic_revisions: frozenset[str] | None
    min_upgrade_from: str | None
    min_updater: int
    pins: dict[str, str]
    images: tuple[Image, ...]
    manual_steps: tuple[ManualStep, ...] = ()
    state_migrations: bool = False
    urgent: bool = False
    notes_url: str | None = None
    released_at: str | None = None
    #: The whole parsed document, for fields this updater does not use yet.
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    def image_for(self, repository: str) -> Image | None:
        return next((i for i in self.images if i.repository == repository), None)


def _need(doc: dict, key: str, kind: type | tuple):
    if key not in doc:
        raise ManifestError(f"manifest is missing {key!r}")
    value = doc[key]
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is int):
        raise ManifestError(f"manifest field {key!r} has the wrong type")
    return value


def parse(doc: dict) -> Manifest:
    """Validate a decoded manifest. Only call this on bytes whose signature
    has been checked (``verify``); it is separate so tests and the release
    tooling can build manifests without a key."""
    if not isinstance(doc, dict):
        raise ManifestError("manifest is not a JSON object")
    if _need(doc, "schema", int) != SCHEMA:
        raise ManifestError(f"manifest schema {doc['schema']} is not one this updater reads")
    if _need(doc, "product", str) != "opennvr":
        raise ManifestError("manifest is not for OpenNVR")
    min_updater = _need(doc, "min_updater", int)
    if min_updater > UPDATER_PROTOCOL:
        raise ManifestError(
            "this release needs a newer updater than the one installed; "
            "update to an intermediate release first (see its release notes)")
    version = _need(doc, "version", str)
    parse_version(version)
    channel = _need(doc, "channel", str)
    if channel not in ("stable", "edge"):
        raise ManifestError(f"unknown channel {channel!r}")
    commit = _need(doc, "git_commit", str)
    if not _HEX40.match(commit):
        raise ManifestError("git_commit is not a full commit hash")
    min_from = doc.get("min_upgrade_from")
    if min_from is not None:
        if not isinstance(min_from, str):
            raise ManifestError("min_upgrade_from has the wrong type")
        parse_version(min_from)

    pins = _need(doc, "pins", dict)
    for k, v in pins.items():
        if not _PIN_KEY.match(k) or not isinstance(v, str) or not v or any(c in v for c in "\r\n\"'` $"):
            raise ManifestError(f"pin {k!r} is not a plain KEY=value")

    images = []
    for item in _need(doc, "images", list):
        if not isinstance(item, dict):
            raise ManifestError("an image entry is not an object")
        ref, digest, ids = item.get("ref"), item.get("digest"), item.get("ids")
        if not isinstance(ref, str) or not ref or any(c.isspace() for c in ref):
            raise ManifestError("an image has no usable ref")
        if not isinstance(digest, str) or not _DIGEST.match(digest):
            raise ManifestError(f"image {ref} has no sha256 digest")
        if not isinstance(ids, dict) or not ids or not all(
                isinstance(p, str) and isinstance(i, str) and _DIGEST.match(i) for p, i in ids.items()):
            raise ManifestError(f"image {ref} has no image IDs")
        per_platform = item.get("platform_manifests") or {}
        if not isinstance(per_platform, dict) or not all(
                isinstance(p, str) and isinstance(d, str) and _DIGEST.match(d) for p, d in per_platform.items()):
            raise ManifestError(f"image {ref} has malformed platform manifests")
        images.append(Image(ref=ref, digest=digest, ids=dict(ids), platform_manifests=dict(per_platform)))
    if not images:
        raise ManifestError("manifest lists no images")

    revisions = doc.get("alembic_revisions")
    if revisions is not None and (not isinstance(revisions, list) or not all(isinstance(r, str) for r in revisions)):
        raise ManifestError("alembic_revisions has the wrong type")

    steps = []
    for s in doc.get("manual_steps") or []:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not isinstance(s.get("text"), str):
            raise ManifestError("a manual step is malformed")
        steps.append(ManualStep(id=s["id"], text=s["text"]))

    return Manifest(
        version=version, channel=channel, git_commit=commit,
        git_tag=_need(doc, "git_tag", str), alembic_head=_need(doc, "alembic_head", str),
        alembic_revisions=frozenset(revisions) if revisions is not None else None,
        min_upgrade_from=min_from, min_updater=min_updater, pins=dict(pins),
        images=tuple(images), manual_steps=tuple(steps),
        state_migrations=bool(doc.get("state_migrations", False)), urgent=bool(doc.get("urgent", False)),
        notes_url=doc.get("notes_url") if isinstance(doc.get("notes_url"), str) else None,
        released_at=doc.get("released_at") if isinstance(doc.get("released_at"), str) else None,
        raw=doc,
    )


# ── keys and signatures ──────────────────────────────────────────────


def _load_key_file(path: Path) -> dict[str, Ed25519PublicKey]:
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = {}
    for entry in data.get("keys", []):
        raw = base64.b64decode(entry["public_key"], validate=True)
        keys[entry["id"]] = Ed25519PublicKey.from_public_bytes(raw)
    return keys


def trusted_keys() -> dict[str, Ed25519PublicKey]:
    """The release keys this build trusts, by key id."""
    keys = _load_key_file(_KEYS_FILE)
    extra = os.environ.get(EXTRA_KEYS_ENV)
    if extra:
        keys.update(_load_key_file(Path(extra)))
    return keys


def check_signature(raw: bytes, signature: bytes, keys: dict[str, Ed25519PublicKey] | None = None,
                    *, what: str = "manifest") -> None:
    """ManifestError unless *signature* (a ``.sig`` file) is a trusted key's
    signature over exactly *raw*. Shared by every signed release file."""
    keys = trusted_keys() if keys is None else keys
    if not keys:
        raise ManifestError(f"this build trusts no release key, so it cannot check the {what}")
    try:
        sig = json.loads(signature.decode("utf-8"))
        key_id, value = sig["key_id"], base64.b64decode(sig["signature"], validate=True)
    except Exception as exc:
        raise ManifestError(f"the {what} signature file is unreadable") from exc
    key = keys.get(key_id)
    if key is None:
        raise ManifestError(f"the {what} is signed with key {key_id!r}, which this build does not trust")
    try:
        key.verify(value, raw)
    except InvalidSignature as exc:
        raise ManifestError(f"the {what} signature does not match: it was changed or corrupted") from exc


def verify(raw: bytes, signature: bytes, keys: dict[str, Ed25519PublicKey] | None = None) -> Manifest:
    """The manifest in *raw*, if *signature* (the ``.sig`` file) is a valid
    signature over exactly these bytes by a trusted key; else ManifestError."""
    check_signature(raw, signature, keys)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ManifestError("the manifest is not valid JSON") from exc
    return parse(doc)


def sign(raw: bytes, private_key: Ed25519PrivateKey, key_id: str) -> bytes:
    """The ``.sig`` file for *raw* (release tooling and tests)."""
    value = base64.b64encode(private_key.sign(raw)).decode("ascii")
    return (json.dumps({"key_id": key_id, "signature": value}, indent=2) + "\n").encode("utf-8")


def public_key_entry(private_key: Ed25519PrivateKey, key_id: str) -> dict:
    """The keys.json entry for a private key (to publish its public half)."""
    from cryptography.hazmat.primitives import serialization

    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"id": key_id, "public_key": base64.b64encode(raw).decode("ascii")}

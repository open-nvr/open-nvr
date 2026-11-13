#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build and sign a release manifest (docs/design/auto-update-implementation-plan.md, UPD-102).

Run by the release workflow on a ``v*`` tag, after the images are published::

    python scripts/release/make_manifest.py --version 0.1.9 --commit $GITHUB_SHA \\
        --out dist/ [--extra release/0.1.9.json]

It reads the compose files and ``.env.example`` exactly as
``verify_release_pins.py`` does, resolves every image a release pulls to its
registry digest and per-platform image IDs (anonymous registry API), finds
the Alembic head, and writes ``release-manifest.json``. With
``RELEASE_SIGNING_KEY`` in the environment (base64 of the raw 32-byte
Ed25519 private key, from ``python -m updater keygen``) it also writes the
``.sig``; without it the manifest is left unsigned and the updater will
refuse it, which is what a dry run on a fork wants.

``--extra`` merges release-specific fields an operator must know about:
``min_upgrade_from``, ``manual_steps``, ``urgent``, ``state_migrations``,
``notes_url``.

Images whose service has a ``build:`` section are not in the manifest:
compose builds them from the checkout the release pins.
"""

from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "server"))

import verify_release_pins as pins_lib  # noqa: E402

from updater import (  # noqa: E402
    UPDATER_PROTOCOL,
    manifest as man,
)

_INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
_MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


#: Tag suffixes of the hardware flavours (docs/HARDWARE.md). Kept in step
#: with FLAVOUR_SUFFIXES in scripts/verify_release_pins.py.
FLAVOUR_SUFFIXES = ("-intel", "-qualcomm", "-nvidia", "-rocm")


def is_flavour_ref(ref: str) -> bool:
    """Is *ref* a hardware-flavour tag (``…:0.1.9-qualcomm``)?"""
    _, _, tag = Registry.split(ref)
    return any(tag.endswith(sfx) for sfx in FLAVOUR_SUFFIXES)


class Registry:
    """Anonymous pull access to ghcr.io and Docker Hub: enough to read
    manifests and image configs."""

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    @staticmethod
    def split(ref: str) -> tuple[str, str, str]:
        """(registry host, repository, tag) for a compose image ref."""
        name, _, tag = (
            ref.rpartition(":")
            if ":" in ref.rsplit("/", 1)[-1]
            else (ref, "", "latest")
        )
        first = name.split("/", 1)[0]
        if "." in first or ":" in first or first == "localhost":
            host, repo = first, name.split("/", 1)[1]
        else:
            host, repo = (
                "registry-1.docker.io",
                name if "/" in name else f"library/{name}",
            )
        return host, repo, tag or "latest"

    def _token(self, host: str, repo: str) -> str:
        key = f"{host}/{repo}"
        if key not in self._tokens:
            url = (
                f"https://ghcr.io/token?scope=repository:{repo}:pull"
                if host == "ghcr.io"
                else f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
            )
            with urllib.request.urlopen(url, timeout=20) as r:
                self._tokens[key] = json.load(r)["token"]
        return self._tokens[key]

    def _get(
        self, host: str, repo: str, path: str, accept: tuple[str, ...]
    ) -> tuple[bytes, dict]:
        req = urllib.request.Request(
            f"https://{host}/v2/{repo}/{path}",
            headers={
                "Authorization": f"Bearer {self._token(host, repo)}",
                "Accept": ",".join(accept),
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(), dict(r.headers)

    def resolve(self, ref: str) -> tuple[str, dict[str, str], dict[str, str]]:
        """(registry digest, {platform: image ID}, {platform: platform manifest digest}) for *ref*."""
        host, repo, tag = self.split(ref)
        body, headers = self._get(
            host, repo, f"manifests/{tag}", _INDEX_TYPES + _MANIFEST_TYPES
        )
        digest = headers.get("Docker-Content-Digest") or headers.get(
            "docker-content-digest"
        )
        if not digest:
            raise RuntimeError(f"{ref}: registry sent no digest")
        doc = json.loads(body)
        ids: dict[str, str] = {}
        per_platform: dict[str, str] = {}
        if doc.get("mediaType") in _INDEX_TYPES or "manifests" in doc:
            for entry in doc["manifests"]:
                plat = entry.get("platform") or {}
                if plat.get("os") in (None, "unknown") or plat.get("architecture") in (
                    None,
                    "unknown",
                ):
                    continue  # attestation manifests
                name = f"{plat['os']}/{plat['architecture']}" + (
                    f"/{plat['variant']}" if plat.get("variant") else ""
                )
                sub, _ = self._get(
                    host, repo, f"manifests/{entry['digest']}", _MANIFEST_TYPES
                )
                ids[name] = json.loads(sub)["config"]["digest"]
                per_platform[name] = entry["digest"]
        else:
            config_digest = doc["config"]["digest"]
            cfg, _ = self._get(
                host, repo, f"blobs/{config_digest}", ("application/json", "*/*")
            )
            c = json.loads(cfg)
            name = f"{c.get('os', 'linux')}/{c.get('architecture', 'amd64')}"
            ids[name] = config_digest
            per_platform[name] = digest  # a single-platform image is its own manifest
        return digest, ids, per_platform


def _module_values(path: Path) -> dict:
    """Top-level ``name = literal`` and ``name: type = literal`` assignments."""
    out = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name, value = node.targets[0].id, node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            name, value = node.target.id, node.value
        else:
            continue
        with contextlib.suppress(ValueError):
            out[name] = ast.literal_eval(value)
    return out


def alembic_graph(versions_dir: Path) -> tuple[str, list[str]]:
    """The single head of the migration graph (fails if there are several)
    and every revision in it. Read with ``ast`` rather than imported, so CI
    needs no app dependencies."""
    revs, downs = set(), set()
    for f in versions_dir.glob("*.py"):
        values = _module_values(f)
        if not isinstance(values.get("revision"), str):
            continue
        revs.add(values["revision"])
        down = values.get("down_revision")
        downs.update(
            down if isinstance(down, (tuple, list)) else [down] if down else []
        )
    heads = sorted(revs - downs)
    if len(heads) != 1:
        raise SystemExit(
            f"expected one Alembic head, found {heads}: merge the heads before releasing"
        )
    return heads[0], sorted(revs)


def alembic_head(versions_dir: Path) -> str:
    return alembic_graph(versions_dir)[0]


def release_images(env_pins: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Every pulled image ref of the release (resolved), and the pins used."""
    refs, buildable = pins_lib.collect_image_refs()
    out, used = [], {}
    for raw in refs:
        if raw in buildable:
            continue
        for name in re.findall(r"\$\{([A-Z0-9_]+)", raw):
            if name.endswith("_TAG") and env_pins.get(name):
                used[name] = env_pins[name]
        resolved = pins_lib.substitute(raw, env_pins)
        if resolved.endswith(":") or ":" not in resolved.rsplit("/", 1)[-1]:
            raise SystemExit(f"{raw} resolves to {resolved!r}: pin it in .env.example")
        out.append(resolved)
    return sorted(set(out)), dict(sorted(used.items()))


def use_source(root: Path) -> None:
    """Read the release's compose files, .env.example and migrations from
    *root* (a checkout of the release tag) instead of this checkout: a tag
    cut before this tool existed is then described by its own files."""
    global SOURCE
    SOURCE = root.resolve()
    pins_lib.REPO_ROOT = SOURCE
    pins_lib.ENV_EXAMPLE = SOURCE / ".env.example"


#: Where the release's own files are read from (see use_source).
SOURCE = REPO


def build(
    version: str,
    commit: str,
    extra: dict,
    registry: Registry,
    *,
    channel: str = "stable",
    pin_overrides: dict[str, str] | None = None,
) -> dict:
    """*pin_overrides* replace .env.example's pins: an edge build of main pins
    CORE_TAG to the sha-<commit> images CI published for it."""
    env_pins = {**pins_lib.load_env_pins(pins_lib.ENV_EXAMPLE), **(pin_overrides or {})}
    refs, pins = release_images(env_pins)
    if channel == "stable" and pins.get("CORE_TAG") != version:
        raise SystemExit(
            f".env.example pins CORE_TAG={pins.get('CORE_TAG')!r}, but this is release {version}"
        )
    head, revisions = alembic_graph(SOURCE / "server" / "migrations" / "versions")
    images = []
    for ref in refs:
        try:
            digest, ids, per_platform = registry.resolve(ref)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and is_flavour_ref(ref):
                # A hardware flavour the registry does not have (an adapter
                # release older than the flavour, a flavour not built for
                # this tag). start.sh falls back to cpu when the flavour
                # image is missing, so the release is whole without it; the
                # cpu image of the same service is pinned separately and
                # stays required. Same rule as scripts/verify_release_pins.py.
                print(
                    f"  {ref}  not published — hardware flavour, left out",
                    file=sys.stderr,
                )
                continue
            raise
        images.append(
            {
                "ref": ref,
                "digest": digest,
                "ids": ids,
                "platform_manifests": per_platform,
            }
        )
        print(f"  {ref}  {digest[:19]}…  {', '.join(sorted(ids))}", file=sys.stderr)
    doc = {
        "schema": man.SCHEMA,
        "product": "opennvr",
        "version": version,
        "channel": channel,
        "released_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "git_commit": commit,
        # An edge build is an untagged main commit: fetched by its hash.
        "git_tag": f"v{version}" if channel == "stable" else "",
        "alembic_head": head,
        "alembic_revisions": revisions,
        "min_upgrade_from": None,
        "min_updater": UPDATER_PROTOCOL,
        "urgent": False,
        "state_migrations": False,
        "manual_steps": [],
        "notes_url": f"https://github.com/open-nvr/open-nvr/releases/tag/{'v' + version if channel == 'stable' else 'edge'}",
        "pins": pins,
        "images": images,
    }
    allowed = {
        "min_upgrade_from",
        "manual_steps",
        "urgent",
        "state_migrations",
        "notes_url",
    }
    unknown = set(extra) - allowed
    if unknown:
        raise SystemExit(
            f"--extra may only set {sorted(allowed)}; got {sorted(unknown)}"
        )
    doc.update(extra)
    man.parse(doc)  # the same checks the updater makes
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--version", required=True)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--channel", default="stable", choices=("stable", "edge"))
    ap.add_argument(
        "--pin",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a pin from .env.example (edge: CORE_TAG=sha-<short commit>)",
    )
    ap.add_argument("--extra", type=Path)
    ap.add_argument("--out", type=Path, default=Path("dist"))
    ap.add_argument(
        "--source",
        type=Path,
        help="checkout of the release tag to describe (default: this one)",
    )
    args = ap.parse_args(argv)

    if args.source:
        use_source(args.source)
    extra = json.loads(args.extra.read_text(encoding="utf-8")) if args.extra else {}
    overrides = dict(p.split("=", 1) for p in args.pin)
    doc = build(
        args.version.removeprefix("v"),
        args.commit,
        extra,
        Registry(),
        channel=args.channel,
        pin_overrides=overrides,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    (args.out / man.MANIFEST_NAME).write_bytes(raw)
    print(
        f"wrote {args.out / man.MANIFEST_NAME} ({len(doc['images'])} images)",
        file=sys.stderr,
    )

    secret = os.environ.get("RELEASE_SIGNING_KEY", "").strip()
    if not secret:
        print(
            "RELEASE_SIGNING_KEY not set: manifest left UNSIGNED (the updater will refuse it)",
            file=sys.stderr,
        )
        return 0
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret, validate=True))
    key_id = os.environ.get("RELEASE_SIGNING_KEY_ID", "").strip()
    if not key_id:
        raise SystemExit(
            "RELEASE_SIGNING_KEY_ID must name the key (its id in server/updater/keys.json)"
        )
    sig = man.sign(raw, key, key_id)
    # Sign-then-check with the keys this very commit ships: a release whose
    # key is not in its own keys.json would be refused by every site.
    man.verify(raw, sig)
    (args.out / man.SIGNATURE_NAME).write_bytes(sig)
    print(f"signed with {key_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

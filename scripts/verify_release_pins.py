#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Release-pin verification — every pinned image must exist before we ship it.

Issue #212's failure class: main's compose referenced
``ghcr.io/open-nvr/detect-pipeline:${CORE_TAG}`` while .env pinned a release
tag whose image was never published — a fresh install died on ``docker pull``.
The pin and the registry drifted apart, and nothing checked.

This script IS that check: it collects every ``image:`` reference from the
compose files, resolves ``${VAR:-default}`` substitutions using the pins in
``.env.example``, and asks GHCR (anonymous pull token, HEAD-equivalent
manifest GET) whether each open-nvr tag actually exists. Non-GHCR images
(postgres, nats, alpine…) are assumed published — Docker Hub availability is
not our release's job.

CI runs it on any PR touching the pins or compose files, and on release
tags. Exit 1 = a pinned image is missing = the install is broken; fix the
pin or publish the image before merging.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_GLOBS = ("docker-compose*.yml",)
ENV_EXAMPLE = REPO_ROOT / ".env.example"
GHCR_PREFIX = "ghcr.io/open-nvr/"

_VAR = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)(?::-(?P<default>[^}]*))?\}")


def load_env_pins(path: Path) -> dict[str, str]:
    """KEY=value pairs from .env.example (comments/blank lines skipped)."""
    pins: dict[str, str] = {}
    if not path.is_file():
        return pins
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pins[key.strip()] = value.split("#", 1)[0].strip()
    return pins


def substitute(ref: str, pins: dict[str, str]) -> str:
    """Resolve ${VAR:-default} exactly like compose would with this .env."""
    def repl(m: re.Match) -> str:
        return pins.get(m.group("name")) or (m.group("default") or "")
    return _VAR.sub(repl, ref)


def collect_image_refs() -> list[str]:
    refs: set[str] = set()
    for pattern in COMPOSE_GLOBS:
        for f in sorted(REPO_ROOT.glob(pattern)):
            for line in f.read_text().splitlines():
                line = line.strip()
                if line.startswith("image:"):
                    refs.add(line.split("image:", 1)[1].strip().strip('"').strip("'"))
    return sorted(refs)


def ghcr_manifest_exists(image: str, tag: str) -> bool:
    """Anonymous-pull manifest check against ghcr.io (works from CI runners)."""
    name = image.removeprefix("ghcr.io/")
    try:
        with urllib.request.urlopen(
            f"https://ghcr.io/token?scope=repository:{name}:pull", timeout=15
        ) as resp:
            token = json.load(resp)["token"]
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{name}/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.oci.image.index.v1+json,"
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        if e.code in (404, 403):
            return False
        raise


def main() -> int:
    pins = load_env_pins(ENV_EXAMPLE)
    failures: list[str] = []
    checked = 0
    for raw in collect_image_refs():
        resolved = substitute(raw, pins)
        if not resolved.startswith(GHCR_PREFIX):
            continue  # upstream image; not this release's job
        image, _, tag = resolved.partition(":")
        tag = tag or "latest"
        checked += 1
        exists = ghcr_manifest_exists(image, tag)
        status = "ok " if exists else "MISSING"
        print(f"  [{status}] {image}:{tag}    (from {raw})")
        if not exists:
            failures.append(f"{image}:{tag}")
    if failures:
        print(
            f"\nRELEASE PIN FAILURE: {len(failures)} pinned image(s) do not "
            "exist on GHCR — a fresh install would die on `docker pull` "
            "(issue #212's failure class):\n  " + "\n  ".join(failures) +
            "\nFix the pin in .env.example, or publish the image first.",
            file=sys.stderr,
        )
        return 1
    print(f"\nAll {checked} pinned GHCR images exist. Install path is pullable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

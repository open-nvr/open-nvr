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

# A single ``${VAR}`` / ``${VAR:-default}`` whose default contains NO further
# ``${``-expansion — i.e. an *innermost* substitution. Nested defaults like
# ``${CAPTION_ADAPTER_TAG:-${ADAPTER_TAG:-latest}}`` (valid compose, used by
# the camera-agent caption image) are handled by ``substitute`` resolving
# innermost-first until a fixpoint; a default-stops-at-first-``}`` regex
# would leave a stray trailing brace in the tag and 404 every check.
_VAR = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)(?::-(?P<default>[^{}]*))?\}")


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
        value = value.split("#", 1)[0].strip()
        # CORE_TAG="0.1.2" and CORE_TAG=0.1.2 must pin identically.
        pins[key.strip()] = value.strip('"').strip("'")
    return pins


def substitute(ref: str, pins: dict[str, str]) -> str:
    """Resolve ${VAR:-default} exactly like compose would with this .env.

    Resolves innermost expansions first and repeats until nothing changes,
    so nested defaults (``${A:-${B:-x}}``) reduce the same way compose
    reduces them: a set/pinned outer VAR wins outright, and only an
    unset/empty one falls through to the (already-resolved) inner default.
    Terminates because every pass either removes a ``${...}`` group or
    changes nothing.
    """
    def repl(m: re.Match) -> str:
        return pins.get(m.group("name")) or (m.group("default") or "")
    prev = None
    while prev != ref:
        prev = ref
        ref = _VAR.sub(repl, ref)
    return ref


def collect_image_refs() -> tuple[list[str], set[str]]:
    """Every ``image:`` in the compose files, plus the subset that a
    service can BUILD if the pull fails.

    The buildable set matters because this script's whole premise is
    "a fresh install would die on ``docker pull``" — and for a service
    carrying a ``build:`` section that premise is false. Compose builds
    it instead, which is exactly why the fallback is there. Failing the
    release on such an image would block publishing the very first
    version of an image that is designed to survive not being published.

    Parsed with an indentation walk rather than a YAML load on purpose:
    this script is stdlib-only so CI can run it with bare python3, and
    adding PyYAML to make a release gate work is a worse trade than
    twenty lines of scanning.
    """
    refs: set[str] = set()
    buildable: set[str] = set()
    for pattern in COMPOSE_GLOBS:
        for f in sorted(REPO_ROOT.glob(pattern)):
            in_services = False
            svc_image: str | None = None
            svc_builds = False

            def flush() -> None:
                if svc_image is not None:
                    refs.add(svc_image)
                    if svc_builds:
                        buildable.add(svc_image)

            for raw in f.read_text().splitlines():
                if not raw.strip() or raw.lstrip().startswith("#"):
                    continue
                indent = len(raw) - len(raw.lstrip())
                if indent == 0:
                    flush()
                    svc_image, svc_builds = None, False
                    in_services = raw.split(":", 1)[0].strip() == "services"
                    continue
                if not in_services:
                    continue
                if indent == 2 and raw.rstrip().endswith(":"):
                    # Next service block begins.
                    flush()
                    svc_image, svc_builds = None, False
                    continue
                stripped = raw.strip()
                if stripped.startswith("image:"):
                    svc_image = stripped.split("image:", 1)[1].strip().strip('"').strip("'")
                elif stripped.startswith("build:") or stripped == "build:":
                    svc_builds = True
            flush()
    return sorted(refs), buildable


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
    all_refs, buildable = collect_image_refs()
    for raw in all_refs:
        resolved = substitute(raw, pins)
        if not resolved.startswith(GHCR_PREFIX):
            continue  # upstream image; not this release's job
        image, _, tag = resolved.partition(":")
        if not tag and "${" in raw:
            # ${VAR} with no default and no pin: compose would render an
            # empty tag and docker pull would fail — do NOT mask it as
            # :latest; name it as a broken pin.
            checked += 1
            print(f"  [MISSING] {image}:<unpinned>    (from {raw})")
            failures.append(f"{image}: unpinned variable in {raw}")
            continue
        tag = tag or "latest"
        checked += 1
        exists = ghcr_manifest_exists(image, tag)
        if exists:
            status = "ok "
        elif raw in buildable:
            # Not a release blocker: this service declares a `build:`,
            # so compose builds the image when the pull fails and the
            # install still comes up. Reported, not failed — the pull is
            # still the fast path we WANT published, so a missing one is
            # worth seeing on every run.
            status = "build"
        else:
            status = "MISSING"
        print(f"  [{status}] {image}:{tag}    (from {raw})")
        if status == "MISSING":
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

"""Apps built from the checkout never ask a registry for `:local-build`.

`docker compose up -d <app>` with the default `pull_policy: missing`
tries Docker Hub for `opennvr/<app>:local-build` before building it, and
prints "pull access denied … repository does not exist" on every clean
install — noise that reads like a failure. Every locally built service
must carry `pull_policy: ${APPS_PULL_POLICY:-build}`, and the installer
must flip it to `missing` when it pins a published image, or a pin would
be built over instead of pulled.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = "pull_policy: ${APPS_PULL_POLICY:-build}"


def _local_build_services(text: str) -> list[tuple[str, str]]:
    """(service, next line) for every `image: …:local-build` line."""
    lines = text.split("\n")
    out, svc = [], "?"
    for i, line in enumerate(lines):
        m = re.match(r"^  ([a-z0-9-]+):$", line)
        if m:
            svc = m.group(1)
        if re.match(r"^\s+image: .*local-build\}?\s*$", line):
            out.append((svc, lines[i + 1].strip() if i + 1 < len(lines) else ""))
    return out


def test_every_local_build_service_builds_without_asking_a_registry():
    for fname in ("docker-compose.apps.yml", "docker-compose.camera-agent.yml"):
        found = _local_build_services((REPO_ROOT / fname).read_text(encoding="utf-8"))
        assert found, f"{fname}: no local-build services found — the pattern drifted"
        missing = [svc for svc, nxt in found if nxt != POLICY]
        assert not missing, f"{fname}: add `{POLICY}` right after the image line of: {missing}"


def test_a_pinned_install_pulls_the_pin_instead_of_building_over_it():
    src = (REPO_ROOT / "scripts" / "app-installer" / "reconciler.py").read_text(encoding="utf-8")
    body = src[src.index("def _run_env("):]
    body = body[:body.index("\ndef ", 10)]
    assert '"APPS_PULL_POLICY": "missing"' in body

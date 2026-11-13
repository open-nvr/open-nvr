# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""After the switch: is the stack really the release, and is it up?

Run from the image of the release that should now be running (and, after a
rollback, from the old one). ``problems`` is pure; ``wait_until_healthy``
polls until it comes back empty or the time is up, since core migrates and
starts for a while after ``compose up`` returns.

Proof is the running containers' image IDs against the signed manifest,
the database's migration, and core's own /health: the version string alone
cannot tell builds apart.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from updater.check import accepted_ids, repository
from updater.manifest import Manifest

HEALTH_URL = "http://opennvr-core:8000/health"


@dataclass(frozen=True)
class Running:
    name: str
    image_id: str
    ref: str


def read_running(text: str) -> list[Running]:
    """Lines of ``<container name> <image id> <image ref>`` (host-written)."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3:
            out.append(Running(parts[0].lstrip("/"), parts[1], parts[2]))
    return out


def problems(expected: Manifest, platform: str, health: dict | None, db_revision: str | None,
             running: list[Running]) -> list[str]:
    found: list[str] = []
    if health is None:
        found.append("core is not answering /health yet")
    else:
        if health.get("version") != expected.version:
            found.append(f"core reports version {health.get('version')}, not {expected.version}")
        commit = health.get("commit")
        if commit and commit != expected.git_commit:
            found.append(f"core was built from {commit[:12]}, not {expected.git_commit[:12]}")
    if db_revision != expected.alembic_head:
        found.append(f"the database is at migration {db_revision}, not {expected.alembic_head}")
    for c in running:
        image = expected.image_for(repository(c.ref))
        if image is None or c.ref != image.ref:
            continue  # not pinned by the release (built locally, or kept by the operator)
        if c.image_id not in accepted_ids(image, platform):
            found.append(f"{c.name} runs {c.image_id[:19]}, not the release's {image.ref}")
    if not any(repository(c.ref) == "ghcr.io/open-nvr/core" for c in running):
        found.append("the core container is not running")
    return found


def get_health(url: str = HEALTH_URL) -> dict | None:
    import httpx

    try:
        r = httpx.get(url, timeout=5.0)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def db_revision(url: str | None) -> str | None:
    if not url:
        return None
    from sqlalchemy import create_engine, text

    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        engine.dispose()
        return row[0] if row else None
    except Exception:  # not up yet, or not migrated: reported as a problem by the caller
        return None


def wait_until_healthy(expected: Manifest, platform: str, running: list[Running], database_url: str | None,
                       *, timeout: float = 300.0, interval: float = 5.0,
                       health: Callable[[], dict | None] | None = None,
                       revision: Callable[[str | None], str | None] | None = None,
                       now: Callable[[], float] = time.monotonic,
                       sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """[] once everything checks out, else the last problems seen."""
    health = health or get_health
    revision = revision or db_revision
    deadline = now() + timeout
    while True:
        found = problems(expected, platform, health(), revision(database_url), running)
        if not found or now() >= deadline:
            return found
        sleep(interval)

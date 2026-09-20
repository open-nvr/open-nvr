# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Opt-in lookup of the latest OpenNVR release.

Off unless ``UPDATE_CHECK=true``: an offline-first NVR makes no outbound call
the operator did not ask for. When on, the GitHub "latest release" endpoint
is asked at most once per ``UPDATE_CHECK_TTL_HOURS``. Any failure (no network,
rate limit, bad JSON) reads as "unknown" and never raises into the request.
"""

from __future__ import annotations

import logging
import time

import httpx

from core.config import settings

_log = logging.getLogger(__name__)

LATEST_RELEASE_URL = "https://api.github.com/repos/open-nvr/open-nvr/releases/latest"

# (fetched_at_monotonic, version or None)
_cache: tuple[float, str | None] | None = None


def _normalise(tag: str | None) -> str | None:
    if not tag or not isinstance(tag, str):
        return None
    tag = tag.strip()
    return tag[1:] if tag[:1] in ("v", "V") else tag or None


async def latest_version() -> str | None:
    """The newest released version, or None when disabled or unknown."""
    global _cache
    if not settings.update_check:
        return None
    ttl = max(60.0, settings.update_check_ttl_hours * 3600)
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < ttl:
        return _cache[1]
    version: str | None = None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(LATEST_RELEASE_URL,
                                 headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            version = _normalise(r.json().get("tag_name"))
    except Exception:  # noqa: BLE001 — see module docstring
        _log.info("update check failed; latest version unknown", exc_info=True)
    _cache = (now, version)
    return version


def _reset_cache_for_tests() -> None:
    global _cache
    _cache = None

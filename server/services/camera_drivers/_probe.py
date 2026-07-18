# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Shared HTTP fingerprint helper used by vendor package probe() functions.

A probe answers one question cheaply: "does this device speak <vendor>'s native
control API?" It is used to route OEM rebadges (a CP Plus or Secureye unit whose
manufacturer string lies) to the right native driver, and to confirm a string
match before committing to a native driver. Probes must be strict — a Digest
challenge or a vendor-shaped body, never mere TCP connectivity — so one vendor's
camera never fingerprints as another's.
"""

from __future__ import annotations

import httpx

# Kept short: a probe runs at most once per camera (result is persisted), but a
# few of them may run in sequence during first-contact detection.
_PROBE_TIMEOUT = 4.0


async def fingerprint_get(
    ip: str,
    port: int,
    path: str,
    username: str | None,
    password: str | None,
    *,
    timeout: float = _PROBE_TIMEOUT,
) -> tuple[int | None, str]:
    """GET a vendor-signature URL over HTTP Digest.

    Returns ``(status_code, body)``; ``(None, "")`` on any transport failure
    (unreachable host, timeout, TLS error). Never raises — detection must
    degrade to "not this vendor", not crash the selection flow.
    """
    url = f"http://{ip}:{port}{path}"
    auth = httpx.DigestAuth(username or "", password or "")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, auth=auth)
            return resp.status_code, resp.text
    except Exception:
        return None, ""

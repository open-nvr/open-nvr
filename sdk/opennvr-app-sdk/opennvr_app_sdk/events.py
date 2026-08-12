# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
Events-store client — query the platform's memory (RFC-0001 C1).

The canonical event store keeps one row per object visit (and, in later
phases, per alarm/incident), each with its best-frame evidence photo. This
client is the SDK's read primitive for it: agents and apps ask "what
happened?" here instead of keeping private history (the Challenge-3 rule:
shared primitives live in the SDK, both agents import them).

Talks to core's internal endpoints (``X-Internal-Api-Key``), like the
camera-discovery client — the agent is a platform component, not a browser
session. ``http_get`` is injectable for tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

HttpGetH = Callable[[str, dict], Awaitable[tuple[int, bytes]]]


async def _default_http_get(url: str, headers: dict) -> tuple[int, bytes]:  # pragma: no cover - network
    import httpx
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        resp = await client.get(url, headers=headers)
        return resp.status_code, resp.content


@dataclass(frozen=True)
class StoredEvent:
    """One remembered visit, as the store serves it."""

    id: int
    camera_id: int
    label: str | None
    score: float | None
    started_at: str | None
    ended_at: str | None
    stationary: bool | None
    has_evidence: bool


class EventsClient:
    """Query visits and fetch their evidence photos from core."""

    def __init__(self, core_url: str, api_key: str | None, *,
                 http_get: HttpGetH | None = None) -> None:
        self._base = core_url.rstrip("/")
        self._headers = {"X-Internal-Api-Key": api_key} if api_key else {}
        self._get = http_get or _default_http_get

    async def search(
        self,
        *,
        label: str | None = None,
        camera_id: int | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 50,
    ) -> list[StoredEvent] | None:
        """Visits overlapping [start, end), newest first.

        Returns ``[]`` for a genuinely empty window and ``None`` on ANY
        failure (transport, auth, or a rejected query) — the caller must be
        able to say "nothing came" and "I couldn't check" differently; in a
        security product those are different answers."""
        params: dict[str, Any] = {"limit": limit}
        if label:
            params["label"] = label
        if camera_id is not None:
            params["camera_id"] = camera_id
        if start is not None:
            params["from"] = start.isoformat() if isinstance(start, datetime) else start
        if end is not None:
            params["to"] = end.isoformat() if isinstance(end, datetime) else end
        url = f"{self._base}/api/v1/internal/camera-agent/events?{urlencode(params)}"
        try:
            status, body = await self._get(url, self._headers)
            if status != 200:
                return None
            import json
            rows = json.loads(body.decode("utf-8")).get("events", [])
        except Exception:
            return None
        out = []
        for r in rows:
            try:
                out.append(StoredEvent(
                    id=int(r["id"]), camera_id=int(r["camera_id"]),
                    label=r.get("label"), score=r.get("score"),
                    started_at=r.get("started_at"), ended_at=r.get("ended_at"),
                    stationary=r.get("stationary"),
                    has_evidence=bool(r.get("has_evidence")),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def evidence(self, event_id: int) -> bytes | None:
        """The visit's best-frame JPEG, or None."""
        url = f"{self._base}/api/v1/internal/camera-agent/events/{int(event_id)}/evidence"
        try:
            status, body = await self._get(url, self._headers)
        except Exception:
            return None
        return body if status == 200 and body else None

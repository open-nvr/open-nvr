# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

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
    plate_text: str | None
    has_evidence: bool


class SearchResult(list):
    """The rows ``EventsClient.search`` returns — a plain list, plus
    ``pending``: core's account of what it is describing now because the
    query asked for a claim the matching visits did not have yet."""

    pending: dict | None = None


class EventsClient:
    """Query visits and fetch their evidence photos from core."""

    def __init__(self, core_url: str, api_key: str | None = None, *,
                 http_get: HttpGetH | None = None) -> None:
        #: The `pending` block of the last search that answered (see search()).
        self.last_pending: dict | None = None
        self._base = core_url.rstrip("/")
        # The app's own key when it has one (scoped to its cameras), else
        # the explicit/site key — see credentials.py.
        from .credentials import AppCredentials

        key = AppCredentials(api_key).token()
        self._headers = {"X-Internal-Api-Key": key} if key else {}
        self._get = http_get or _default_http_get

    async def search(
        self,
        *,
        label: str | None = None,
        camera_id: int | None = None,
        plate: str | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 50,
        attrs: list[str] | None = None,
    ) -> list[StoredEvent] | None:
        """Visits overlapping [start, end), newest first.

        Returns ``[]`` for a genuinely empty window and ``None`` on ANY
        failure (transport, auth, or a rejected query) — the caller must be
        able to say "nothing came" and "I couldn't check" differently; in a
        security product those are different answers.

        **"Not yet" is not "no".** When ``attrs`` asks for a claim ("blue")
        that some matching visits do not carry yet, core answers from what
        exists and queues the rest for description now. The returned list
        is a :class:`SearchResult` whose ``.pending`` says so — ``None``
        when nothing was needed, else ``{"missing": n, "requested": n,
        "already_queued": n, "eta_s": s, "request_id": ...}`` — and the
        same block is mirrored on ``self.last_pending`` (cleared at the
        start of every call). Tell the user: "N visits in that window have
        not been described yet — about T; ask again then." The
        ``enrichment_request_done`` system alert on the bus announces the
        request's completion."""
        params: dict[str, Any] = {"limit": limit}
        if label:
            params["label"] = label
        if camera_id is not None:
            params["camera_id"] = camera_id
        if plate:
            params["plate"] = plate
        # What a skill SAID about the visit — "blue", "van", "hi-vis" —
        # as opposed to what the detector classified it as. Repeatable
        # and ANDed server-side.
        #
        # Bare values are the documented spelling. "kind:value" scopes to
        # one kind and is exact, which means `color:blue` finds nothing
        # because the kind is spelled `colour` — a wrong answer that
        # looks precisely like "there were no blue cars". Send `blue`.
        attr_list = [a.strip() for a in (attrs or []) if str(a).strip()]
        if start is not None:
            params["from"] = start.isoformat() if isinstance(start, datetime) else start
        if end is not None:
            params["to"] = end.isoformat() if isinstance(end, datetime) else end
        # doseq: the endpoint takes `attr` repeated, and urlencode would
        # otherwise send one param whose value is the string "['blue']".
        url = (f"{self._base}/api/v1/internal/camera-agent/events?"
               f"{urlencode({**params, 'attr': attr_list}, doseq=True)}")
        self.last_pending = None          # never a previous question's answer
        try:
            status, body = await self._get(url, self._headers)
            if status != 200:
                return None
            import json
            payload = json.loads(body.decode("utf-8"))
            rows = payload.get("events", [])
            # What core is describing NOW because this query asked for a
            # claim the visits did not have yet (core's `pending`). It
            # rides on the returned list (`.pending`), which is still a
            # list — an app built against the list-of-events contract
            # keeps working — and is mirrored on `last_pending` for
            # callers that hold the client rather than the rows.
            pending = payload.get("pending") if isinstance(payload, dict) else None
            self.last_pending = pending
        except Exception:
            return None
        out = SearchResult()
        out.pending = pending
        for r in rows:
            try:
                out.append(StoredEvent(
                    id=int(r["id"]), camera_id=int(r["camera_id"]),
                    label=r.get("label"), score=r.get("score"),
                    started_at=r.get("started_at"), ended_at=r.get("ended_at"),
                    stationary=r.get("stationary"),
                    plate_text=r.get("plate_text"),
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

    async def recording_frame(self, camera_id: int, at: str) -> bytes | None:
        """One JPEG from recorded footage at instant ``at`` (ISO 8601), or None.

        Powers the agent's describe_window: sample a few instants across a past
        window and caption each. Internal-key authed like ``evidence``.
        """
        from urllib.parse import urlencode

        q = urlencode({"camera_id": int(camera_id), "at": at})
        url = f"{self._base}/api/v1/internal/camera-agent/recordings/frame?{q}"
        try:
            status, body = await self._get(url, self._headers)
        except Exception:
            return None
        return body if status == 200 and body else None

# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""WebRTC live view over WHEP (RFC 9725) against OpenNVR's MediaMTX.

The Home Assistant camera entity relays the browser's SDP offer here and
the answer back; the media itself flows browser <-> MediaMTX.

* ``offer()``: ``GET /streams/{id}/info`` for the WHEP URL and a short-lived
  stream token, then POST the offer (``Authorization: Bearer <token>``).
  Returns the answer SDP and the session URL for trickle ICE and teardown.
* The session URL comes from ``Location``, which MediaMTX writes relative to
  its own root (``/cam-1/whep/<id>``) while the client reached it through
  nginx under ``/webrtc``. The stripped prefix is put back, exactly as the
  OpenNVR web UI does; used as-is the DELETE would hit core and 405.
* ``add_candidate()`` PATCHes an ICE candidate (``application/trickle-ice-sdpfrag``);
  ``close()`` DELETEs the session.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp

from .client import OpenNVRClient
from .exceptions import OpenNVRAuthError, OpenNVRConnectionError, OpenNVRError, OpenNVRNotFoundError


@dataclass(frozen=True)
class WhepSession:
    answer_sdp: str
    session_url: str | None
    stream_token: str


def resolve_session_url(location: str | None, whep_url: str) -> str | None:
    """The absolute session URL from a WHEP POST's ``Location`` header.
    None when it points at another origin: the stream token goes with every
    PATCH/DELETE, and must never leave the site it was issued by."""
    if not location:
        return None
    if location.startswith("/") and not location.startswith("//"):
        parent = location[: location.rfind("/")]
        whep_path = urlsplit(whep_url).path.rstrip("/")
        if parent and whep_path.endswith(parent):
            prefix = whep_path[: len(whep_path) - len(parent)]
            return urljoin(whep_url, prefix + location)
    resolved = urljoin(whep_url, location)
    a, b = urlsplit(resolved), urlsplit(whep_url)
    return resolved if (a.scheme, a.netloc) == (b.scheme, b.netloc) else None


def candidate_fragment(candidate: str, sdp_mid: str | None = None,
                       ufrag: str | None = None, pwd: str | None = None) -> str:
    """An SDP fragment carrying one trickled ICE candidate (RFC 8840)."""
    lines = []
    if ufrag:
        lines.append(f"a=ice-ufrag:{ufrag}")
    if pwd:
        lines.append(f"a=ice-pwd:{pwd}")
    lines.append("m=audio 9 RTP/AVP 0")
    if sdp_mid is not None:
        lines.append(f"a=mid:{sdp_mid}")
    cand = candidate if candidate.startswith("candidate:") else f"candidate:{candidate}"
    lines.append(f"a={cand}")
    return "\r\n".join(lines) + "\r\n"


class Whep:
    def __init__(self, client: OpenNVRClient, session: aiohttp.ClientSession) -> None:
        self._client = client
        self._session = session

    async def offer(self, camera_id: int, offer_sdp: str) -> WhepSession:
        info = await self._client.get_stream_info(camera_id)
        if not info.webrtc_url:
            raise OpenNVRError(f"camera {camera_id} has no WebRTC URL")
        try:
            async with self._session.post(
                info.webrtc_url, data=offer_sdp.encode(),
                headers={"Content-Type": "application/sdp",
                         "Authorization": f"Bearer {info.token}"},
                ssl=self._client.ssl, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 404:
                    raise OpenNVRNotFoundError(f"camera {camera_id} is not streaming")
                if resp.status in (400, 401, 403):
                    raise OpenNVRAuthError(f"WHEP refused ({resp.status})", resp.status)
                if resp.status not in (200, 201):
                    raise OpenNVRConnectionError(f"WHEP POST failed ({resp.status})")
                answer = await resp.text()
                location = resp.headers.get("Location")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise OpenNVRConnectionError(f"WHEP POST failed: {exc!r}") from exc
        return WhepSession(answer, resolve_session_url(location, info.webrtc_url), info.token)

    async def add_candidate(self, session: WhepSession, candidate: str,
                            sdp_mid: str | None = None) -> None:
        if not session.session_url:
            return
        try:
            async with self._session.patch(
                session.session_url, data=candidate_fragment(candidate, sdp_mid).encode(),
                headers={"Content-Type": "application/trickle-ice-sdpfrag",
                         "Authorization": f"Bearer {session.stream_token}"},
                ssl=self._client.ssl, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400 and resp.status != 405:
                    raise OpenNVRConnectionError(f"WHEP PATCH failed ({resp.status})")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise OpenNVRConnectionError(f"WHEP PATCH failed: {exc!r}") from exc

    async def close(self, session: WhepSession) -> None:
        if not session.session_url:
            return
        try:
            async with self._session.delete(
                session.session_url, headers={"Authorization": f"Bearer {session.stream_token}"},
                ssl=self._client.ssl, timeout=aiohttp.ClientTimeout(total=10),
            ):
                pass
        except (aiohttp.ClientError, TimeoutError):
            pass  # the session times out on MediaMTX anyway

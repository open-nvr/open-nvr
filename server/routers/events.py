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
WebSocket endpoint that streams live AI inference events to subscribers.

Used by:
  * The OpenNVR web UI (live dashboard overlays).
  * Agent integration shims (pipecat, GetStream vision-agents) that must
    consume already-computed detection/face/transcript events instead of
    running their own models on the same stream.

Auth
----
FastAPI's HTTPBearer dependency doesn't work on the WS handshake — browsers
can't set custom headers when opening a WebSocket. So the client first calls
the authenticated ``POST /events/ws-ticket`` endpoint to mint a short-lived,
single-use ticket, then opens ``/events/ws?ticket=<ticket>``. This keeps the
long-lived JWT out of URLs (and therefore out of access logs). Server-to-server
clients authenticate the same way: call ``POST /events/ws-ticket`` with their
bearer token, then open the socket with the returned ticket.

Filters
-------
    ?camera_id=<int>          → only events for that camera
    ?task=<name>[&task=<name>]→ only these task names (person_detection,
                                face_detection, audio_transcription, …)

Both can be combined. Missing filters mean "everything".
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.logging_config import main_logger
from models import User
from routers.apps import get_read_principal
from services.camera_scope import visible_camera_ids
from services.event_bus_service import get_event_bus

router = APIRouter()


# ---------------------------------------------------------------------------
# Single-use WebSocket tickets
#
# Browsers cannot attach an Authorization header to a WebSocket handshake, so
# historically the long-lived JWT was passed as ``?token=<jwt>`` — which lands
# in nginx/proxy access logs and browser history. Instead, an authenticated
# REST call mints a short-lived, single-use ticket and the client opens
# ``/events/ws?ticket=<ticket>``. An in-memory store is safe because the API
# runs as a single uvicorn worker (see supervisord.conf); if that ever becomes
# multiple workers, move this to a shared store (e.g. Redis).
# ---------------------------------------------------------------------------
_WS_TICKET_TTL_SECONDS = 30
# ticket -> (username | None, expires_at). None = a platform SERVICE
# identity (the deployment's INTERNAL_API_KEY): no user row, no camera
# scope. Minted for trusted in-stack consumers — the camera agent relays
# core's overlay tracks to its own viewers and scopes them itself.
_ws_tickets: dict[str, tuple[str | None, float]] = {}


class ServiceIdentity:
    """The WS-side stand-in for a service ticket. Shaped like the two
    User attributes the stream reads (``username`` for the log line,
    ``is_active`` for the gate) so the handler needs no isinstance
    branching beyond the scope decision."""

    username = "service:internal"
    is_active = True
    is_superuser = True   # unrestricted scope, same as a superuser

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<ServiceIdentity>"


SERVICE = ServiceIdentity()

#: Sentinel distinguishing "ticket found, it is a service ticket" from
#: "no such ticket" — both would otherwise be None.
_SERVICE_TICKET = object()


def _prune_ws_tickets(now: float) -> None:
    for tok in [t for t, (_, exp) in _ws_tickets.items() if exp <= now]:
        _ws_tickets.pop(tok, None)


def _mint_ws_ticket(username: str | None) -> tuple[str, int]:
    """``username=None`` mints a SERVICE ticket."""
    now = time.time()
    _prune_ws_tickets(now)
    ticket = secrets.token_urlsafe(32)
    _ws_tickets[ticket] = (username, now + _WS_TICKET_TTL_SECONDS)
    return ticket, _WS_TICKET_TTL_SECONDS


def _consume_ws_ticket(ticket: str):
    """Validate and *consume* a ticket (single use).

    Returns the username for a user ticket, ``_SERVICE_TICKET`` for a
    service ticket, or ``None`` when the ticket is unknown or expired —
    three outcomes, because a service ticket has no username and must
    not be mistaken for a missing one.
    """
    entry = _ws_tickets.pop(ticket, None)  # pop() => cannot be replayed
    if entry is None:
        return None
    username, expires_at = entry
    if expires_at <= time.time():
        return None
    return _SERVICE_TICKET if username is None else username


@router.post("/events/ws-ticket")
async def create_ws_ticket(principal=Depends(get_read_principal)):
    """Mint a short-lived, single-use ticket for opening the events WebSocket.

    A browser cannot set an Authorization header on a WebSocket, so it
    calls this authenticated endpoint first, then opens
    ``/events/ws?ticket=<ticket>``.

    Three credentials are accepted, and they yield different tickets:

    * a user JWT → a ticket bound to that user; the stream is scoped to
      the cameras they may see (``visible_camera_ids``);
    * the deployment's ``INTERNAL_API_KEY`` → a SERVICE ticket, unscoped.
      For trusted in-stack consumers only: the camera agent relays core's
      overlay tracks to its own viewers and applies its own per-viewer
      scope, the same way it does for everything else it shows;
    * an app key → refused (403). An installed app is not a platform
      service; its own view of the bus is what the SDK gives it.
    """
    from services.app_keys import AppPrincipal

    if isinstance(principal, AppPrincipal):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An app key cannot open the site-wide event stream",
        )
    if principal is None:
        ticket, ttl = _mint_ws_ticket(None)
        return {"ticket": ticket, "expires_in": ttl, "kind": "service"}
    ticket, ttl = _mint_ws_ticket(principal.username)
    return {"ticket": ticket, "expires_in": ttl, "kind": "user"}


def _authenticate_ws(ticket: str | None, db: Session) -> User | ServiceIdentity | None:
    """Authenticate a WS handshake using a single-use ticket.

    We deliberately do NOT raise here — the caller closes the socket with a
    proper code so the client sees a clean rejection. A service ticket
    yields ``SERVICE`` without touching the users table.
    """
    if not ticket:
        return None
    subject = _consume_ws_ticket(ticket)
    if subject is None:
        return None
    if subject is _SERVICE_TICKET:
        return SERVICE
    user = db.query(User).filter(User.username == subject).first()
    if user is None or not user.is_active:
        return None
    return user


def _ws_scope_for(principal, db: Session) -> set[int] | None:
    """Cameras this socket may receive events for; ``None`` = unrestricted.

    Factored out so the decision is testable on its own: a service
    identity is unrestricted (it re-scopes downstream), a user gets
    exactly ``visible_camera_ids`` — a superuser's ``None`` included.
    """
    if principal is SERVICE:
        return None
    return visible_camera_ids(db, principal)


@router.websocket("/events/ws")
async def events_stream(
    websocket: WebSocket,
    ticket: str | None = Query(default=None, description="Single-use WS ticket"),
    camera_id: int | None = Query(default=None, description="Filter to one camera"),
    task: list[str] | None = Query(default=None, description="Filter to these task names"),
):
    """
    Stream inference events over WebSocket.

    Event frame format (JSON text frames)::

        {
            "event_type": "inference_result",
            "camera_id": 3,
            "model_id": 42,
            "task": "person_detection",
            "timestamp": 1712345678901,
            "payload": { ...adapter response... }
        }

    Live Tier-0 tracks for the detection overlay arrive as their own type
    (bridged from NATS by services/tier0_track_consumer.py), boxes already
    normalized to 0..1 of the frame so the client needs no resolution::

        {
            "event_type": "tracks",
            "camera_id": 3,
            "task": "tier0",
            "payload": {
                "schema": "opennvr.overlay.tracks.v1",
                "calibrating": false,
                "frame": {"w": 1920, "h": 1080},
                "tracks": [
                    {"id": 5, "label": "person", "score": 0.91,
                     "box": [0.1, 0.1, 0.4, 0.4], "stationary": false}
                ]
            }
        }

    Filter with ``task=tier0`` to receive only these. They are subject to
    the same per-camera entitlement as every other event on this socket.

    The server also sends two control frames:
      * ``{"event_type": "subscribed", "filters": {...}}`` on accept
      * ``{"event_type": "lagged", "dropped": N}`` when the client was too
        slow and we had to drop events (sent opportunistically).
    """
    # Authenticate BEFORE accepting so bad clients get a clean 4401.
    db_gen = get_db()
    db: Session = next(db_gen)
    try:
        user = _authenticate_ws(ticket, db)
        # AUTHORIZE the subscription, not just the connection. Being logged
        # in said nothing about WHICH cameras you may watch: `camera_id` was
        # taken from the query string unchecked, and leaving it off meant
        # "every camera on the site" — so any active account could stream
        # every other user's detections and alerts. Resolve what this user
        # may see and hand that to the bus, which enforces it per event.
        #
        # Resolved in the SAME session that loaded `user`: visible_camera_ids
        # reads user.id/is_superuser and queries on them, and doing that
        # against a detached instance works only for as long as its
        # attributes happen to still be loaded. Not a gamble worth taking
        # on an authorization path.
        allowed = _ws_scope_for(user, db) if user is not None else set()
    finally:
        # Mirror FastAPI's get_db teardown without relying on Depends here
        # (WebSocket routes can't use Depends() for request-scoped DB sessions
        # cleanly because there's no response boundary).
        try:
            next(db_gen)
        except StopIteration:
            pass

    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="unauthorized")
        return

    # Asking for a camera you cannot see is refused outright rather than
    # silently answered with an empty stream — an authorization failure
    # the caller can act on beats a feed that looks broken.
    if camera_id is not None and allowed is not None and camera_id not in allowed:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="forbidden")
        return
    # A user with no cameras has nothing to stream; say so instead of
    # holding an idle socket open forever.
    if allowed is not None and not allowed:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="no cameras")
        return

    await websocket.accept()

    bus = get_event_bus()
    filters = {"camera_id": camera_id, "task": task}

    try:
        await websocket.send_text(json.dumps({
            "event_type": "subscribed",
            "filters": filters,
        }))
    except Exception:
        return

    reported_drops = 0

    async with bus.subscribe(camera_id=camera_id, tasks=task,
                             allowed_camera_ids=allowed) as sub:
        main_logger.info(
            "events_stream opened: user=%s filters=%s subscribers_total=%d",
            user.username, filters, bus.subscriber_count,
        )
        try:
            while True:
                event: dict[str, Any] = await sub.queue.get()
                await websocket.send_text(json.dumps(event, default=str))

                # Surface cumulative drops so slow clients know they missed data.
                if sub.dropped > reported_drops:
                    try:
                        await websocket.send_text(json.dumps({
                            "event_type": "lagged",
                            "dropped": sub.dropped,
                        }))
                    except Exception:
                        break
                    reported_drops = sub.dropped

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            main_logger.warning(
                "events_stream closing on error for user=%s: %s", user.username, exc,
            )
        finally:
            main_logger.info(
                "events_stream closed: user=%s dropped=%d", user.username, sub.dropped,
            )

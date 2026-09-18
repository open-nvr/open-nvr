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

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.client_ip import get_client_ip, is_internal_service, is_loopback
from core.database import get_db
from core.logging_config import main_logger
from models import User
from routers.apps import get_read_principal
from services import api_tokens
from services.camera_scope import visible_camera_ids
from services import device_firewall_service as dfw
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


@dataclass(frozen=True)
class WsTicketBinding:
    """What the device firewall knew about the client that minted a ticket.

    The firewall middleware is HTTP-only: it never sees the WebSocket
    handshake. The mint request DID pass it, so its facts ride along with the
    ticket and are re-checked at the handshake (see ``_ws_firewall_allows``).
    Without this, a ticket leaked within its 30 s life could be opened from a
    machine the firewall would refuse.
    """

    client_ip: str | None
    device_token: str | None
    #: Minted with the deployment's INTERNAL_API_KEY (a sibling service).
    internal_key: bool = False
    #: Minted with an API token (HA-103): the socket runs as that token,
    #: re-checked at the handshake, never as its owner.
    api_token_id: int | None = None


# ticket -> binding. Kept beside _ws_tickets (same keys, same lifetime) so
# the ticket store's (username, expires_at) shape is unchanged.
_ws_ticket_bindings: dict[str, WsTicketBinding] = {}


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
        _ws_ticket_bindings.pop(tok, None)
    # A binding whose ticket is gone (consumed or pruned) is dead weight.
    for tok in [t for t in _ws_ticket_bindings if t not in _ws_tickets]:
        _ws_ticket_bindings.pop(tok, None)


def _mint_ws_ticket(
    username: str | None, binding: WsTicketBinding | None = None
) -> tuple[str, int]:
    """``username=None`` mints a SERVICE ticket."""
    now = time.time()
    _prune_ws_tickets(now)
    ticket = secrets.token_urlsafe(32)
    _ws_tickets[ticket] = (username, now + _WS_TICKET_TTL_SECONDS)
    if binding is not None:
        _ws_ticket_bindings[ticket] = binding
    return ticket, _WS_TICKET_TTL_SECONDS


def _consume_ws_ticket(ticket: str):
    """Validate and *consume* a ticket (single use).

    Returns the username for a user ticket, ``_SERVICE_TICKET`` for a
    service ticket, or ``None`` when the ticket is unknown or expired —
    three outcomes, because a service ticket has no username and must
    not be mistaken for a missing one.
    """
    entry = _ws_tickets.pop(ticket, None)  # pop() => cannot be replayed
    _ws_ticket_bindings.pop(ticket, None)
    if entry is None:
        return None
    username, expires_at = entry
    if expires_at <= time.time():
        return None
    return _SERVICE_TICKET if username is None else username


@router.post("/events/ws-ticket")
async def create_ws_ticket(request: Request, principal=Depends(get_read_principal)):
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
      service; its own view of the bus is what the SDK gives it;
    * an API token (needs ``cameras.view``) → a ticket bound to the token.
      The socket keeps the token's camera allow-list and receives only the
      event types its scopes cover (``api_tokens.TOKEN_EVENT_SCOPES``).
    """
    from services.app_keys import AppPrincipal

    if isinstance(principal, AppPrincipal):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An app key cannot open the site-wide event stream",
        )
    binding = WsTicketBinding(
        client_ip=get_client_ip(request) or None,
        device_token=dfw.token_from_request(request),
        internal_key=principal is None,
        api_token_id=(principal.token_id
                      if api_tokens.is_token_principal(principal) else None),
    )
    if principal is None:
        ticket, ttl = _mint_ws_ticket(None, binding)
        return {"ticket": ticket, "expires_in": ttl, "kind": "service"}
    ticket, ttl = _mint_ws_ticket(principal.username, binding)
    kind = "token" if binding.api_token_id is not None else "user"
    return {"ticket": ticket, "expires_in": ttl, "kind": kind}


def _ws_firewall_allows(websocket, binding: WsTicketBinding | None) -> bool:
    """Device-firewall decision for a WebSocket handshake.

    Mirrors DeviceFirewallMiddleware's order (loopback / internal key, then
    enforcement off, then the device token, then sibling containers), with
    two WebSocket-specific facts:

    * a browser cannot send the device header on a WebSocket, so the token
      bound at mint time is used, falling back to one on the handshake;
    * while enforcement is on, the handshake must come from the client that
      minted the ticket. The ticket carries that client's approval; a copy
      opened elsewhere would borrow it.

    A service ticket (minted with the internal key) is honoured only from
    inside the stack: it is unscoped and travels in the query string, so a
    leaked one must not open the stream from an arbitrary machine.
    """
    ip = get_client_ip(websocket)
    if is_loopback(ip):
        return True
    if binding is not None and binding.internal_key and is_internal_service(ip):
        return True
    if not dfw.enforcement_active_cached():
        return True
    if binding is not None and binding.client_ip and ip != binding.client_ip:
        return False
    if binding is not None and binding.api_token_id is not None:
        # An API token is a bound credential and passes the HTTP firewall on
        # its own (the mint did); it is re-validated in _authenticate_ws.
        return True
    bound_token = binding.device_token if binding is not None else None
    token = bound_token or dfw.token_from_request(websocket)
    if token:
        return dfw.is_allowed_browser_cached(token)
    return is_internal_service(ip)


def _authenticate_ws(
    ticket: str | None, db: Session,
    binding: WsTicketBinding | None = None, ip: str = "",
):
    """Authenticate a WS handshake using a single-use ticket.

    We deliberately do NOT raise here — the caller closes the socket with a
    proper code so the client sees a clean rejection. A service ticket
    yields ``SERVICE`` without touching the users table; a token ticket
    yields a ``TokenPrincipal`` after re-checking the token (revoked,
    expired, owner disabled, allowed networks, scope).
    """
    if not ticket:
        return None
    subject = _consume_ws_ticket(ticket)
    if subject is None:
        return None
    if subject is _SERVICE_TICKET:
        return SERVICE
    if binding is not None and binding.api_token_id is not None:
        principal = api_tokens.principal_for_ws(db, binding.api_token_id, ip)
        if principal is None or principal.username != subject:
            return None
        return principal
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
    v: int = Query(default=1, ge=1, le=2, description="Protocol version"),
    since: int | None = Query(default=None, ge=0, description="v2: resume after this seq"),
    epoch: str | None = Query(default=None, max_length=32, description="v2: epoch of `since`"),
    types: list[str] | None = Query(default=None, description="v2: only these event types"),
):
    """
    Stream inference events over WebSocket.

    **v2** (``v=2``, HA-111) numbers every frame (``seq``), can resume after
    a reconnect (``since`` + ``epoch`` replays up to 5 minutes), opens with
    a ``state_snapshot`` when it cannot, filters by ``types`` and sends a
    ``heartbeat`` when idle; see :func:`_stream_v2`. v1, below, is unchanged.

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
    # Read the ticket's firewall binding before the ticket is consumed.
    binding = _ws_ticket_bindings.get(ticket) if ticket else None
    # Authenticate BEFORE accepting so bad clients get a clean 4401.
    db_gen = get_db()
    db: Session = next(db_gen)
    try:
        user = _authenticate_ws(ticket, db, binding, get_client_ip(websocket))
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
        # A token receives only the event types its scopes cover. Same
        # session for the same reason: this reads the owner's role.
        event_types = (api_tokens.token_event_types(user)
                       if api_tokens.is_token_principal(user) else None)
        # Entity scopes this connection holds (HA-114), for entity_state.
        from services.entity_descriptors import held_scopes

        held = held_scopes(user) if user is not None and user is not SERVICE else None
        # What the socket was opened with, for the periodic re-check of a
        # token socket (a revoke or a scope change must not wait for HA to
        # reconnect, which can be days).
        recheck = (TokenRecheck(user.token_id, user.username, get_client_ip(websocket),
                                allowed, event_types, held)
                   if api_tokens.is_token_principal(user) else None)
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

    # The HTTP device-firewall middleware never sees a WebSocket handshake.
    if not _ws_firewall_allows(websocket, binding):
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="device_not_approved")
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

    if v == 2:
        await _stream_v2(websocket, user, allowed, event_types, camera_id=camera_id,
                         task=task, since=since, epoch=epoch, types=types, held=held,
                         recheck=recheck)
        return

    bus = get_event_bus()
    filters: dict[str, Any] = {"camera_id": camera_id, "task": task}
    if event_types is not None:
        filters["event_types"] = sorted(event_types)

    try:
        await websocket.send_text(json.dumps({
            "event_type": "subscribed",
            "filters": filters,
        }))
    except Exception:
        return

    reported_drops = 0

    async with bus.subscribe(camera_id=camera_id, tasks=task,
                             allowed_camera_ids=allowed,
                             allowed_event_types=event_types) as sub:
        main_logger.info(
            "events_stream opened: user=%s filters=%s subscribers_total=%d",
            user.username, filters, bus.subscriber_count,
        )
        try:
            while True:
                try:
                    event: dict[str, Any] = await asyncio.wait_for(
                        sub.queue.get(), WS_RECHECK_S)
                except TimeoutError:
                    if recheck is not None and not await recheck.still_ok():
                        await recheck.close(websocket)
                        break
                    continue
                if recheck is not None and not await recheck.still_ok():
                    await recheck.close(websocket)
                    break
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


# ── token sockets are re-checked while open ──────────────────────────────

#: How often an open token socket re-validates its token (and the camera
#: and event scope it was opened with).
WS_RECHECK_S = 30.0


class TokenRecheck:
    """Re-validate a token socket at most every WS_RECHECK_S.

    Closes (4401) when the token was revoked or expired, the owner disabled,
    the address left ``allowed_cidrs``, or its cameras / event types /
    entity scopes changed. The client reconnects and gets exactly what it
    may now have; a revoked token gets nothing.
    """

    def __init__(self, token_id, username, ip, allowed, event_types, held):
        self.token_id, self.username, self.ip = token_id, username, ip
        self.opened_with = (allowed, event_types, held)
        self.checked_at = time.monotonic()
        self.ok = True

    def _check(self) -> bool:
        from core.database import SessionLocal
        from services.entity_descriptors import held_scopes

        with SessionLocal() as db:
            p = api_tokens.principal_for_ws(db, self.token_id, self.ip)
            if p is None or p.username != self.username:
                return False
            now = (_ws_scope_for(p, db), api_tokens.token_event_types(p), held_scopes(p))
        return now == self.opened_with

    async def still_ok(self) -> bool:
        if not self.ok:
            return False
        if time.monotonic() - self.checked_at < WS_RECHECK_S:
            return True
        self.checked_at = time.monotonic()
        try:
            self.ok = await asyncio.to_thread(self._check)
        except Exception:  # noqa: BLE001 - fail closed
            self.ok = False
        return self.ok

    async def close(self, websocket) -> None:
        main_logger.info("events_stream: closing token socket (token %s revoked or changed)",
                         self.token_id)
        try:
            await websocket.close(code=4401, reason="token revoked or changed")
        except Exception:  # noqa: BLE001
            pass


# ── v2 (HA-111) ───────────────────────────────────────────────────────────

#: Seconds of silence before a v2 socket sends a heartbeat, so a client can
#: tell "quiet" from "dead" without waiting for TCP to notice.
V2_HEARTBEAT_S = 25.0


def _v2_snapshot_cameras(allowed: set[int] | None, camera_id: int | None) -> list[dict]:
    """Live state and connectivity of every camera this socket may see."""
    from core.database import SessionLocal
    from services.camera_status_service import get_camera_status_service
    from services.live_state import get_live_state
    from models import Camera

    with SessionLocal() as db:
        q = (db.query(Camera.id)
             .filter(Camera.deleted_at.is_(None), Camera.is_active.is_(True)))
        if camera_id is not None:
            q = q.filter(Camera.id == camera_id)
        ids = [cid for (cid,) in q.order_by(Camera.id).all()
               if allowed is None or cid in allowed]
    online = get_camera_status_service().snapshot(ids)
    live = get_live_state()
    return [{**live.camera(cid), "online": online.get(cid)} for cid in ids]


def _v2_entity_states(event_types, held, allowed) -> dict:
    """Every resolved entity state this socket may see, for the snapshot."""
    if event_types is not None and "entity_state" not in event_types:
        return {}
    from services.entity_state_publisher import visible_states

    return visible_states(held, allowed)


def _v2_site_mode(event_types) -> dict | None:
    """The site mode for the snapshot, when this socket may see it."""
    if event_types is not None and "site_mode" not in event_types:
        return None
    from core.database import SessionLocal
    from services import site_mode

    with SessionLocal() as db:
        return site_mode.get(db)


def _v2_may_send(event: dict, held: set[str] | None) -> bool:
    """entity_state carries its descriptor's scope; the rest were filtered
    by the bus."""
    if event.get("event_type") == "entity_state" and held is not None:
        return event.get("required_scope") in held
    return True


def _v2_frame(seq: int, event: dict) -> str:
    out = {k: v for k, v in event.items() if k not in ("v2_only", "required_scope")}
    return json.dumps({"v": 2, "seq": seq, **out}, default=str)


async def _stream_v2(websocket, user, allowed, event_types, *, camera_id, task,
                     since, epoch, types, held=None, recheck=None) -> None:
    """The v2 stream.

    Order matters and is what makes resume lossless: subscribe FIRST (from
    then on every event reaches the queue, numbered above the subscriber's
    ``start_seq``), THEN send what came before (replay from the ring, or a
    snapshot), then drain the queue.
    """
    bus = get_event_bus()
    wanted = set(types) if types else None
    async with bus.subscribe(camera_id=camera_id, tasks=task, allowed_camera_ids=allowed,
                             allowed_event_types=event_types, event_types=wanted,
                             with_seq=True) as sub:
        try:
            replayed: list = []
            complete = False
            if since is not None and epoch == bus.epoch:
                replayed, complete = await bus.replay(sub, since)
            hello = {"v": 2, "event_type": "subscribed", "epoch": bus.epoch,
                     "seq": sub.start_seq,
                     "filters": {"camera_id": camera_id, "task": task,
                                 "types": sorted(wanted) if wanted else None,
                                 **({"event_types": sorted(event_types)}
                                    if event_types is not None else {})},
                     "resumed": complete}
            await websocket.send_text(json.dumps(hello))
            if complete:
                for seq, event in replayed:
                    if _v2_may_send(event, held):
                        await websocket.send_text(_v2_frame(seq, event))
            else:
                cameras = await asyncio.to_thread(_v2_snapshot_cameras, allowed, camera_id)
                await websocket.send_text(json.dumps({
                    "v": 2, "event_type": "state_snapshot", "seq": sub.start_seq,
                    # True when the client asked to resume and could not:
                    # it missed events and must rebuild its state from this.
                    "resync": since is not None,
                    "cameras": cameras,
                    "site_mode": await asyncio.to_thread(_v2_site_mode, event_types),
                    "entity_states": _v2_entity_states(event_types, held, allowed),
                }, default=str))
            main_logger.info("events_stream v2 opened: user=%s since=%s resumed=%s",
                             user.username, since, complete)
            reported_drops = 0
            while True:
                try:
                    seq, event = await asyncio.wait_for(sub.queue.get(), V2_HEARTBEAT_S)
                except TimeoutError:
                    if recheck is not None and not await recheck.still_ok():
                        await recheck.close(websocket)
                        break
                    await websocket.send_text(json.dumps(
                        {"v": 2, "event_type": "heartbeat", "seq": bus.current_seq}))
                    continue
                if recheck is not None and not await recheck.still_ok():
                    await recheck.close(websocket)
                    break
                if not _v2_may_send(event, held):
                    continue
                await websocket.send_text(_v2_frame(seq, event))
                if sub.dropped > reported_drops:
                    # Events were dropped for this slow client: it can
                    # reconnect with since=<last seq it got> to fill the gap.
                    await websocket.send_text(json.dumps(
                        {"v": 2, "event_type": "lagged", "dropped": sub.dropped}))
                    reported_drops = sub.dropped
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            main_logger.warning("events_stream v2 closing on error for user=%s: %s",
                                user.username, exc)

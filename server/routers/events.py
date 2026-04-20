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
can't set custom headers when opening a WebSocket. So we accept the JWT as
a ``?token=<jwt>`` query param and validate it with the same ``verify_token``
used for the REST API. Server-to-server clients can send the token either
way (query param wins if both present).

Filters
-------
    ?camera_id=<int>          → only events for that camera
    ?task=<name>[&task=<name>]→ only these task names (person_detection,
                                face_detection, audio_transcription, …)

Both can be combined. Missing filters mean "everything".
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from core.auth import verify_token
from core.database import get_db
from core.logging_config import main_logger
from models import User
from services.event_bus_service import get_event_bus

router = APIRouter()


def _authenticate_ws_token(token: str | None, db: Session) -> User | None:
    """
    Validate a JWT supplied via query param, return the matching active user
    or None. We deliberately do NOT raise here — the caller closes the socket
    with a proper code so the client sees a clean rejection.
    """
    if not token:
        return None

    token_data = verify_token(token)
    if token_data is None:
        return None

    user = db.query(User).filter(User.username == token_data.username).first()
    if user is None or not user.is_active:
        return None
    return user


@router.websocket("/events/ws")
async def events_stream(
    websocket: WebSocket,
    token: str | None = Query(default=None, description="JWT access token"),
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

    The server also sends two control frames:
      * ``{"event_type": "subscribed", "filters": {...}}`` on accept
      * ``{"event_type": "lagged", "dropped": N}`` when the client was too
        slow and we had to drop events (sent opportunistically).
    """
    # Authenticate BEFORE accepting so bad clients get a clean 4401.
    db_gen = get_db()
    db: Session = next(db_gen)
    try:
        user = _authenticate_ws_token(token, db)
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

    async with bus.subscribe(camera_id=camera_id, tasks=task) as sub:
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

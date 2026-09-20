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
Audit logging service.

Provides a helper to record audit events with consistent structure.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from core.request_context import current as current_request_context
from models import AuditLog

_log = logging.getLogger(__name__)


def _safe_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        try:
            # If it's already a JSON string, keep as-is
            json.loads(value)  # type: ignore[arg-type]
            return (
                value
                if isinstance(value, str)
                else value.decode("utf-8", errors="ignore")
            )
        except Exception:
            # Treat as plain text
            return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _with_actor(details: Any, actor: str) -> Any:
    """Record the request's non-user actor without clobbering the caller's own.

    A dict gains ``actor`` unless it already names one. Anything else (a JSON
    string or plain text) is wrapped so the actor is still recorded.
    """
    if details is None:
        return {"actor": actor}
    if isinstance(details, dict):
        return details if "actor" in details else {**details, "actor": actor}
    return {"actor": actor, "details": details}


def write_audit_log(
    db: Session,
    *,
    action: str,
    user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    details: Any = None,
    ip: str | None = None,
    user_agent: str | None = None,
    correlation_id: str | None = None,
) -> AuditLog:
    # Request context (set by RequestLoggingMiddleware) supplies the
    # correlation id and, for non-user principals such as API tokens, the
    # actor. Outside a request (background work) both are simply absent.
    ctx = current_request_context()
    if correlation_id is None and ctx is not None:
        correlation_id = ctx.correlation_id
    if ctx is not None and ctx.actor:
        details = _with_actor(details, ctx.actor)
    row = AuditLog(
        action=action,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=_safe_json(details),
        ip=ip,
        user_agent=user_agent,
        correlation_id=correlation_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _audit_session():
    """A fresh session for one audit write (patched in tests)."""
    from core.database import SessionLocal

    return SessionLocal()


def audit_request(
    db: Session,
    request: Any,
    *,
    action: str,
    user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    details: Any = None,
) -> None:
    """Write an audit row for an HTTP action, never failing the action itself.

    The row is written through its OWN short-lived session, never the
    caller's *db*: write_audit_log commits, and on failure this rolls back,
    so sharing the caller's session would commit (or discard) whatever the
    caller had not yet committed. *db* is accepted so call sites read
    naturally and is deliberately not used.

    Fills ip (the real client, via core.client_ip) and user_agent from
    *request* (may be None). An audit-write failure is logged, not raised:
    the operator's PTZ move or acknowledge must not 500 because the audit
    table hiccuped.
    """
    ip = user_agent = None
    if request is not None:
        from core.client_ip import get_client_ip

        ip = get_client_ip(request) or None
        headers = getattr(request, "headers", None)
        user_agent = headers.get("user-agent") if headers is not None else None
    session = None
    try:
        session = _audit_session()
        write_audit_log(
            session,
            action=action,
            user_id=user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            ip=ip,
            user_agent=user_agent,
        )
    except Exception:  # noqa: BLE001 — see docstring
        _log.error("Failed to write audit log for %s", action, exc_info=True)
        if session is not None:
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

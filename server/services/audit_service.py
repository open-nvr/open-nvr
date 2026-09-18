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
from typing import Any

from sqlalchemy.orm import Session

from core.request_context import current as current_request_context
from models import AuditLog


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

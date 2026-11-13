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

"""Per-request context: correlation id and non-user actor.

Why a *mutable object* in one ContextVar, rather than a ContextVar per field:
FastAPI runs sync dependencies (such as ``get_current_user``) and sync
endpoints in a threadpool on a *copy* of the current context. A
``ContextVar.set()`` made inside one of them is invisible to the endpoint
and to ``write_audit_log``. Mutating the object that the request middleware
put in the var works everywhere, because every copy of the context points at
the same object.

So: the middleware calls :func:`begin_request` once, and later code only
*mutates* the returned object (``current().actor = "token:ha"``). Nothing
else may call ``request_ctx_var.set``.

A client-supplied correlation id is a HINT for joining records, never
attribution: any client may send any id, including another client's.
``AuditLog.user_id`` and ``details.actor`` say who acted. WebSocket handlers
run outside the HTTP middleware, so audit rows they write carry no
correlation id.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass

#: Longest correlation id accepted from a client; matches AuditLog.correlation_id.
MAX_CORRELATION_ID_LEN = 64

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


@dataclass
class RequestContext:
    """Mutable per-request state shared across threadpool context copies."""

    correlation_id: str | None = None
    #: Non-user principal acting in this request (e.g. ``"token:ha-main"``).
    #: ``None`` for ordinary logged-in users, whose id is on the audit row.
    actor: str | None = None


request_ctx_var: ContextVar[RequestContext | None] = ContextVar(
    "opennvr_request_ctx", default=None
)


def valid_correlation_id(value: str | None) -> str | None:
    """Return *value* if it is a safe client-supplied correlation id, else None.

    Only a conservative charset is allowed, because the id ends up in log lines
    and audit rows and is echoed back in a response header.
    """
    if value and _CORRELATION_ID_RE.fullmatch(value):
        return value
    return None


def begin_request(correlation_id: str) -> tuple[RequestContext, Token]:
    """Install a fresh context for this request; the caller must :func:`end_request`."""
    ctx = RequestContext(correlation_id=correlation_id)
    return ctx, request_ctx_var.set(ctx)


def end_request(token: Token) -> None:
    request_ctx_var.reset(token)


def current() -> RequestContext | None:
    """The current request's context, or None outside a request (background work)."""
    return request_ctx_var.get()

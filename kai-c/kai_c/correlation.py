# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Correlation-ID middleware for KAI-C.

§3.8 of the contract specifies that every request from KAI-C carries
an ``X-Correlation-Id: <uuid>`` header. This middleware:

* Reads ``X-Correlation-Id`` from inbound requests, OR mints one if
  absent. KAI-C is the canonical mint point — OpenNVR backend or any
  client SHOULD send a value, but if they don't we want a stable id
  for the audit log.
* Stashes it on ``request.state.correlation_id`` so handlers can
  attach it to every audit event they emit.
* Echoes it on the response.

Forwarding to the adapter is the handler's job (it adds the header
when calling httpx).
"""
from __future__ import annotations

from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from kai_c.audit import new_correlation_id

CORRELATION_ID_HEADER: str = "X-Correlation-Id"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or new_correlation_id()
        request.state.correlation_id = correlation_id
        response: Response = await call_next(request)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response

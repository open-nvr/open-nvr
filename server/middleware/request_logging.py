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
Request logging middleware for comprehensive API request tracking.
"""

import time
import uuid
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from core.logging_config import api_logger
from utils.url_redaction import redact_query_params, redact_url_query


# High-frequency media paths: an HLS playback session issues hundreds of
# byte-range requests per minute, and each used to emit TWO structured log
# records with full header dumps. Those paths get a pass-through with no
# per-request logging (failures still log via the exception path).
_QUIET_PREFIXES = (
    "/api/v1/recordings/playback/hls",
    "/assets/",
)

# Evidence images sit between the two extremes. The Vehicles page mounts one
# per row, so the pair of records with a full header dump was ~400 synchronous
# writes per page view, on the event loop — but these are the most sensitive
# bytes the product serves, so going fully quiet would delete the only record
# that anyone looked at them. Compromise: drop the request_start record and the
# header dumps, keep one request_complete line.
#
# Suffix, not prefix: the event id sits mid-path, and the JSON siblings
# (/events/plate-stats, /vehicle-report, ...) must keep logging in full.
_EVENTS_PREFIX = "/api/v1/events/"
_SLIM_SUFFIXES = (
    "/evidence",
    "/plate-evidence",
    "/scene-evidence",
    "/plate-frame",
)


def _is_slim_path(path: str) -> bool:
    """True for evidence images: one lean record instead of two fat ones."""
    return path.startswith(_EVENTS_PREFIX) and path.endswith(_SLIM_SUFFIXES)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all incoming HTTP requests and responses."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path.startswith(_QUIET_PREFIXES):
            return await call_next(request)
        slim = _is_slim_path(request.url.path)

        # Generate unique request ID
        request_id = str(uuid.uuid4())

        # Get client info
        client_host = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")

        # Start timer
        start_time = time.time()

        # Sanitize headers to remove sensitive information. Only the
        # request_start record uses these, so slim paths skip the copy.
        sanitized_headers: dict = {}
        if not slim:
            sanitized_headers = dict(request.headers)
            sensitive_headers = {
                "authorization", "cookie", "x-api-key", "x-auth-token",
            }
            for header in sanitized_headers:
                if header.lower() in sensitive_headers:
                    sanitized_headers[header] = "[REDACTED]"

        # Log incoming request. Evidence images skip this one entirely —
        # the completion record below carries everything that matters.
        if not slim:
            api_logger.log_action(
                "api.request_start",
                message=f"{request.method} {request.url.path}",
                extra_data={
                    "method": request.method,
                    # Redact secrets carried in the URL / query string (camera
                    # creds, stream tokens, MFA codes) before they hit the log.
                    "url": redact_url_query(str(request.url)),
                    "path": request.url.path,
                    "query_params": redact_query_params(request.query_params),
                    "headers": sanitized_headers,
                    "client_host": client_host,
                    "user_agent": user_agent,
                },
                ip_address=client_host,
                user_agent=user_agent,
                request_id=request_id,
            )

        # Process request
        try:
            response = await call_next(request)

            # Calculate processing time
            process_time = time.time() - start_time

            # Log response
            api_logger.log_action(
                "api.request_complete",
                message=f"{request.method} {request.url.path} - {response.status_code}",
                extra_data={
                    "method": request.method,
                    "url": redact_url_query(str(request.url)),
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "process_time_seconds": round(process_time, 3),
                    # The header dump is the bulky half; an evidence image
                    # answers the same three fixed headers every time.
                    **({} if slim else
                       {"response_headers": dict(response.headers)}),
                },
                ip_address=client_host,
                user_agent=user_agent,
                request_id=request_id,
            )

            # Add request ID to response headers
            response.headers["X-Request-ID"] = request_id

            return response

        except Exception as exc:
            # Calculate processing time
            process_time = time.time() - start_time

            # Log error
            api_logger.error(
                f"Request failed: {request.method} {request.url.path}",
                extra={
                    "method": request.method,
                    "url": redact_url_query(str(request.url)),
                    "path": request.url.path,
                    "process_time_seconds": round(process_time, 3),
                    "exception_type": type(exc).__name__,
                    "ip_address": client_host,
                    "user_agent": user_agent,
                    "request_id": request_id,
                },
                exc_info=True,
            )

            # Re-raise the exception
            raise exc

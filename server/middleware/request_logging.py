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


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all incoming HTTP requests and responses."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Generate unique request ID
        request_id = str(uuid.uuid4())

        # Get client info
        client_host = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")

        # Start timer
        start_time = time.time()

        # Sanitize headers to remove sensitive information
        sanitized_headers = dict(request.headers)
        sensitive_headers = {"authorization", "cookie", "x-api-key", "x-auth-token"}
        for header in sanitized_headers:
            if header.lower() in sensitive_headers:
                sanitized_headers[header] = "[REDACTED]"

        # Log incoming request
        api_logger.log_action(
            "api.request_start",
            message=f"{request.method} {request.url.path}",
            extra_data={
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "query_params": dict(request.query_params),
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
                    "url": str(request.url),
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "process_time_seconds": round(process_time, 3),
                    "response_headers": dict(response.headers),
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
                    "url": str(request.url),
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

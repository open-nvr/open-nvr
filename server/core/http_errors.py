# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The JSON response for an HTTPException (main.py's handler)."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def http_exception_response(exc: HTTPException) -> JSONResponse:
    """``{"detail": ...}`` with the exception's own headers kept
    (WWW-Authenticate on a 401, X-OpenNVR-Error on a refused API token):
    clients act on them."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))

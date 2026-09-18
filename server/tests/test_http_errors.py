# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""main.py's HTTPException handler used to drop the exception's headers, so
no 401 carried WWW-Authenticate and a refused API token lost X-OpenNVR-Error."""

from fastapi import HTTPException

from core.http_errors import http_exception_response


def test_headers_survive():
    r = http_exception_response(HTTPException(403, "nope",
                                              headers={"X-OpenNVR-Error": "token_address"}))
    assert r.status_code == 403 and r.headers["X-OpenNVR-Error"] == "token_address"
    assert r.body == b'{"detail":"nope"}'


def test_no_headers():
    r = http_exception_response(HTTPException(404, "gone"))
    assert r.status_code == 404 and "x-opennvr-error" not in r.headers

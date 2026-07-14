# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Access-log token redaction: MediaMTX streaming JWTs ride in ?jwt= query
params, so any logged request URL would otherwise print a live read token.
_RedactTokensFilter blanks them in both uvicorn-style %-args records and
plain-message records."""
from __future__ import annotations

import logging

from camera_agent import _RedactTokensFilter


def _record(msg: str, args: tuple = ()) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, msg, args, None)


def test_redacts_jwt_in_uvicorn_access_args() -> None:
    # uvicorn.access logs '%s - "%s %s HTTP/%s" %d' with the URL as an arg.
    rec = _record(
        '%s - "%s %s HTTP/%s" %d',
        ("172.28.0.1:55484", "DELETE",
         "/cam-1/whep/ca5ec995?jwt=eyJhbGciOiJSUzI1NiJ9.payload.sig&x=1",
         "1.1", 404),
    )
    assert _RedactTokensFilter().filter(rec) is True   # never drops records
    out = rec.getMessage()
    assert "eyJ" not in out
    assert "jwt=[redacted]" in out
    assert "&x=1" in out                # non-sensitive params survive
    assert "172.28.0.1:55484" in out    # non-string / other args untouched


def test_redacts_tokens_in_plain_messages() -> None:
    rec = _record("fetching http://mmtx/whep?token=SECRET&access_token=ALSO&y=2")
    _RedactTokensFilter().filter(rec)
    out = rec.getMessage()
    assert "SECRET" not in out and "ALSO" not in out
    assert "token=[redacted]" in out and "y=2" in out


def test_leaves_tokenless_lines_alone() -> None:
    msg = '%s - "%s %s HTTP/%s" %d'
    args = ("172.28.0.1:1", "GET", "/alarms", "1.1", 200)
    rec = _record(msg, args)
    _RedactTokensFilter().filter(rec)
    assert rec.getMessage() == msg % args

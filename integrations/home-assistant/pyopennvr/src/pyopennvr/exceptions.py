# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Errors raised by pyopennvr.

Callers (the Home Assistant integration) map them to HA's own: auth errors
start a re-auth flow, connection errors mark entities unavailable, a
contract error raises a Repair telling the user what to upgrade.
"""

from __future__ import annotations


class OpenNVRError(Exception):
    """Base class for every pyopennvr error."""


class OpenNVRConnectionError(OpenNVRError):
    """The server could not be reached, timed out, or answered 5xx."""


class OpenNVRSSLError(OpenNVRConnectionError):
    """TLS failed: typically a self-signed certificate while verifying."""


class OpenNVRAuthError(OpenNVRError):
    """401/403: the token is wrong, revoked, expired, or lacks a scope.

    ``code`` is the server's ``X-OpenNVR-Error`` when it names the reason,
    e.g. ``token_address``: the token may not be used from this address, so
    a new token with the same settings would not help.
    """

    def __init__(self, message: str, status: int | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class OpenNVRNotFoundError(OpenNVRError):
    """404: the camera, event or entity does not exist (or is not visible)."""


class OpenNVRRequestError(OpenNVRError):
    """Any other 4xx: the request was refused as sent (422, 409, ...)."""

    def __init__(self, message: str, status: int, detail: object = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class OpenNVRContractError(OpenNVRError):
    """The server speaks a contract version this library does not support."""

    def __init__(self, message: str, server_version: str | None) -> None:
        super().__init__(message)
        self.server_version = server_version

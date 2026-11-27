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

"""Remember logins a camera refused, so they are not sent again at once.

Cameras lock an account after a few wrong passwords (Hikvision's "illegal
login lock": a handful of failures, then the account is locked for a while,
for every client). One settings-page Refresh used to send the same refused
login three times — ONVIF while picking the driver, the ISAPI fingerprint,
ONVIF again for the device info — and every tab then sent it once more.
An operator who had the password right on the second try still found the
device locked (Discord report, 2026-11-27).

So a refusal is remembered for ``HOLD_S`` seconds per device, protocol and
credential pair, and during that window the camera is not asked again; the
caller gets the same refusal without a request. A different username or
password is a different key, so correcting the password is tried at once.
The record is per process, like the other per-camera caches in the control
layer, and a stale entry costs at most one refused call.

``protocol`` separates the device's ONVIF accounts from its web (ISAPI/CGI)
accounts: Hikvision keeps them apart, so ONVIF refusing a login says nothing
about the web login.
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import urlparse

#: How long a refused login is not retried. Long enough to cover one probe
#: and the burst of calls a settings page makes; short enough that an
#: operator who fixed the camera side (enabled ONVIF, added the user) can
#: retry within a minute.
HOLD_S = 60.0

ONVIF = "onvif"
WEB = "web"

_REFUSED: dict[tuple[str, str, str, str], float] = {}


def _host(url_or_host: str) -> str:
    """The device, without scheme or port: a lockout is per device."""
    if "://" in url_or_host:
        return (urlparse(url_or_host).hostname or url_or_host).lower()
    return url_or_host.lower()


def _key(protocol: str, url_or_host: str, username: str | None, password: str | None):
    digest = hashlib.sha256((password or "").encode()).hexdigest()
    return (protocol, _host(url_or_host), username or "", digest)


def refused_for(
    protocol: str, url_or_host: str, username: str | None, password: str | None
) -> float | None:
    """Seconds left before this login may be sent again, or None if it may now."""
    key = _key(protocol, url_or_host, username, password)
    until = _REFUSED.get(key)
    if until is None:
        return None
    left = until - time.monotonic()
    if left <= 0:
        _REFUSED.pop(key, None)
        return None
    return left


def note_refused(
    protocol: str, url_or_host: str, username: str | None, password: str | None
) -> None:
    _REFUSED[_key(protocol, url_or_host, username, password)] = time.monotonic() + HOLD_S


def note_accepted(
    protocol: str, url_or_host: str, username: str | None, password: str | None
) -> None:
    _REFUSED.pop(_key(protocol, url_or_host, username, password), None)


def digest_login_refused(status_code: int, www_authenticate: str | None) -> bool:
    """A 401 that still carries a Digest challenge after httpx's DigestAuth
    answered one: the login was sent and refused. A 401 with no Digest
    challenge (Basic only, or none) means the login was never sent, so it
    cost the device's lockout counter nothing and is not remembered."""
    return status_code == 401 and "digest" in (www_authenticate or "").lower()


def held_suffix(left_s: float) -> str:
    """Sentence appended to a refusal served from memory."""
    return (
        f" Not tried again for {max(1, round(left_s))} s, so the camera does not "
        "lock the account after repeated wrong passwords."
    )


def clear() -> None:
    """Forget every refusal (tests)."""
    _REFUSED.clear()

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

"""Reuse a short-lived signed token without outliving it (issue #1023).

Core signs a few-minute JWT for MediaMTX and reuses it rather than putting
an RSA signature on every request. Those caches used to time the reuse
with ``time.monotonic()`` alone, while the token's ``exp``/``iat`` are wall
clock. When the wall clock jumped forward (NTP stepping a board with no RTC
50 minutes, or an admin setting the time) MediaMTX rejected the cached token
as expired for up to four minutes: the dashboard said "Media server Down"
and every Control API call failed. A backward jump leaves a token issued
"in the future".

A token here is reusable only while BOTH clocks agree it is: the monotonic
clock bounds the reuse window as before, and the wall clock must still be
inside [minted, minted + reuse]. A jump either way re-mints on the next call.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class ShortLivedTokenCache:
    """Hold one token for at most ``reuse_s`` seconds on both clocks."""

    def __init__(self, reuse_s: float) -> None:
        self.reuse_s = reuse_s
        self._lock = threading.Lock()
        self._token: str | None = None
        self._mono_until = 0.0
        self._wall_from = 0.0
        self._wall_until = 0.0

    def _usable(self, mono: float, wall: float) -> bool:
        return (
            self._token is not None
            and mono < self._mono_until
            and self._wall_from <= wall < self._wall_until
        )

    def get(self, mint: Callable[[], str]) -> str:
        """The cached token, or a fresh one from ``mint``.

        ``mint`` exceptions propagate and leave the cache empty, so each
        caller keeps its own "could not mint" handling.
        """
        with self._lock:
            # Clocks read before minting, so the reuse window never ends
            # later than the token's own lifetime, however long signing took.
            mono, wall = time.monotonic(), time.time()
            if self._usable(mono, wall):
                return self._token  # type: ignore[return-value]
            self._token = None
            token = mint()
            self._token = token
            self._mono_until = mono + self.reuse_s
            self._wall_from = wall
            self._wall_until = wall + self.reuse_s
            return token

    def clear(self) -> None:
        with self._lock:
            self._token = None

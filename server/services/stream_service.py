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
MediaMTX integration utilities.

This module provides helpers to build WebRTC (WHEP) URLs for live playback from MediaMTX.
You typically configure MediaMTX with an HTTP listener (default 8889). Clients fetch
WHEP endpoints at /whep/<stream>.

Integration notes:
- Path naming can be based on camera IP or camera ID (configurable)
- Protect MediaMTX via reverse proxy and forward an Authorization header
"""

from urllib.parse import urlencode

from core.config import settings


def _build_stream_name(prefix: str, camera_id: int, camera_ip: str) -> str:
    # Fallback to "ip" if setting not present
    mode = getattr(settings, "mediamtx_path_mode", "ip")
    try:
        if str(mode).lower() == "id":
            return f"{prefix}{camera_id}"
    except Exception:
        # If anything odd happens, default to IP-based
        pass
    sanitized = (camera_ip or "").replace(".", "_")
    return f"{prefix}{sanitized}"


def build_whep_url(
    camera_id: int, camera_ip: str, token: str | None = None, extra: dict | None = None
) -> str:
    """Build a WHEP URL for a given camera.

    MediaMTX default WHEP path: {base}/whep/{stream}
    """
    base = settings.mediamtx_base_url.rstrip("/")
    stream = _build_stream_name(settings.mediamtx_stream_prefix, camera_id, camera_ip)
    url = f"{base}/{stream}/whep"
    params = {}
    if extra:
        params.update(extra)
    if token:
        params["token"] = token
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


def build_secure_whep_url_for_user(camera_id: int, camera_ip: str, user_id: int) -> str:
    token = settings.mediamtx_token
    return build_whep_url(camera_id, camera_ip, token=token)

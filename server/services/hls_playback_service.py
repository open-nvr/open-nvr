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
HLS Playback Service

Generates HLS VOD manifests from MediaMTX recordings.
Since MediaMTX does not support HLS VOD natively, this service:
1. Queries MediaMTX /list endpoint for segment info
2. Generates HLS .m3u8 manifests with configurable segment duration
3. Manages playback sessions with expiry
4. Proxies segment requests to MediaMTX /get endpoint

Security:
- Session-based authentication (session_id is auth token)
- Sessions are time-limited and tied to user
- All MediaMTX access is proxied (localhost only)
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from core.config import settings
from core.logging_config import recording_logger


@dataclass
class PlaybackSession:
    """Represents an active HLS playback session."""

    session_id: str
    user_id: int
    username: str
    camera_id: int
    camera_path: str
    start_time: datetime  # Recording start time
    end_time: datetime  # Recording end time
    created_at: float  # Session creation timestamp
    expires_at: float  # Session expiry timestamp
    segments: list[dict[str, Any]] = field(default_factory=list)
    total_duration: float = 0.0


class HlsPlaybackService:
    """
    Service for generating HLS VOD manifests from MediaMTX recordings.

    Architecture:
    - Backend generates HLS manifests from MediaMTX segment list
    - Segments are proxied from MediaMTX /get endpoint
    - Sessions provide authentication without per-request JWT
    """

    # Configuration
    SEGMENT_DURATION: float = 5.0  # Target segment duration in seconds
    SESSION_TTL_SECONDS: int = 7200  # 2 hours default session lifetime
    MAX_SESSIONS_PER_USER: int = 10  # Prevent session leaks
    CLEANUP_INTERVAL: int = 300  # Cleanup expired sessions every 5 minutes

    # In-memory session storage (use Redis in production for scaling)
    _sessions: dict[str, PlaybackSession] = {}
    _user_sessions: dict[int, list[str]] = {}  # user_id -> [session_ids]
    _cleanup_task: asyncio.Task | None = None
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def start_cleanup_task(cls) -> None:
        """Start background task to cleanup expired sessions."""
        if cls._cleanup_task is None or cls._cleanup_task.done():
            cls._cleanup_task = asyncio.create_task(cls._cleanup_loop())
            recording_logger.info("[HLS] Started session cleanup task")

    @classmethod
    async def _cleanup_loop(cls) -> None:
        """Periodically cleanup expired sessions."""
        while True:
            try:
                await asyncio.sleep(cls.CLEANUP_INTERVAL)
                await cls._cleanup_expired_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                recording_logger.error(f"[HLS] Cleanup error: {e}")

    @classmethod
    async def _cleanup_expired_sessions(cls) -> None:
        """Remove expired sessions from storage."""
        now = time.time()
        expired = []

        async with cls._lock:
            for session_id, session in cls._sessions.items():
                if session.expires_at < now:
                    expired.append(session_id)

            for session_id in expired:
                session = cls._sessions.pop(session_id, None)
                if session:
                    # Remove from user sessions list
                    user_sessions = cls._user_sessions.get(session.user_id, [])
                    if session_id in user_sessions:
                        user_sessions.remove(session_id)

        if expired:
            recording_logger.info(f"[HLS] Cleaned up {len(expired)} expired sessions")

    @classmethod
    async def create_session(
        cls,
        user_id: int,
        username: str,
        camera_id: int,
        camera_path: str,
        start_time: datetime,
        end_time: datetime,
        ttl_seconds: int | None = None,
    ) -> PlaybackSession:
        """
        Create a new HLS playback session.

        Args:
            user_id: Authenticated user ID
            username: Username for logging
            camera_id: Camera ID
            camera_path: MediaMTX path (e.g., "cam-57")
            start_time: Recording start time
            end_time: Recording end time
            ttl_seconds: Session TTL (default: SESSION_TTL_SECONDS)

        Returns:
            PlaybackSession with segment info populated
        """
        # Ensure cleanup task is running
        await cls.start_cleanup_task()

        ttl = ttl_seconds or cls.SESSION_TTL_SECONDS
        now = time.time()

        # Limit sessions per user
        async with cls._lock:
            user_sessions = cls._user_sessions.get(user_id, [])
            if len(user_sessions) >= cls.MAX_SESSIONS_PER_USER:
                # Remove oldest session
                oldest_id = user_sessions.pop(0)
                cls._sessions.pop(oldest_id, None)
                recording_logger.debug(
                    f"[HLS] Removed oldest session for user {user_id}"
                )

        # Generate session ID
        session_id = str(uuid.uuid4())

        # Query MediaMTX for segment info
        segments, total_duration = await cls._fetch_segments(
            camera_path, start_time, end_time
        )

        # Create session
        session = PlaybackSession(
            session_id=session_id,
            user_id=user_id,
            username=username,
            camera_id=camera_id,
            camera_path=camera_path,
            start_time=start_time,
            end_time=end_time,
            created_at=now,
            expires_at=now + ttl,
            segments=segments,
            total_duration=total_duration,
        )

        # Store session
        async with cls._lock:
            cls._sessions[session_id] = session
            if user_id not in cls._user_sessions:
                cls._user_sessions[user_id] = []
            cls._user_sessions[user_id].append(session_id)

        recording_logger.info(
            f"[HLS] Created session {session_id[:8]}... for user={username}, "
            f"camera={camera_id}, duration={total_duration:.1f}s"
        )

        return session

    @classmethod
    async def get_session(cls, session_id: str) -> PlaybackSession | None:
        """Get session by ID, returns None if expired or not found."""
        session = cls._sessions.get(session_id)
        if session and session.expires_at > time.time():
            return session
        return None

    @classmethod
    async def invalidate_session(cls, session_id: str) -> bool:
        """Invalidate a specific session."""
        async with cls._lock:
            session = cls._sessions.pop(session_id, None)
            if session:
                user_sessions = cls._user_sessions.get(session.user_id, [])
                if session_id in user_sessions:
                    user_sessions.remove(session_id)
                return True
        return False

    @classmethod
    async def invalidate_user_sessions(cls, user_id: int) -> int:
        """Invalidate all sessions for a user (e.g., on logout). Returns count."""
        async with cls._lock:
            session_ids = cls._user_sessions.pop(user_id, [])
            for sid in session_ids:
                cls._sessions.pop(sid, None)
            return len(session_ids)

    @classmethod
    async def _fetch_segments(
        cls, camera_path: str, start_time: datetime, end_time: datetime
    ) -> tuple[list[dict[str, Any]], float]:
        """
        Fetch segment info from MediaMTX /list endpoint.

        Returns:
            Tuple of (segments list, total duration)
        """
        try:
            # Format times for MediaMTX
            start_str = start_time.isoformat()
            end_str = end_time.isoformat()

            url = f"{settings.mediamtx_playback_url}/list"
            params = {"path": camera_path, "start": start_str, "end": end_str}

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, params=params)

                if response.status_code != 200:
                    recording_logger.warning(
                        f"[HLS] MediaMTX /list returned {response.status_code}"
                    )
                    return [], 0.0

                segments = response.json()
                if not segments:
                    return [], 0.0

                # Calculate total duration
                total_duration = sum(seg.get("duration", 0) for seg in segments)

                return segments, total_duration

        except Exception as e:
            recording_logger.error(f"[HLS] Failed to fetch segments: {e}")
            return [], 0.0

    @classmethod
    def generate_manifest(cls, session: PlaybackSession) -> str:
        """
        Generate HLS VOD manifest (.m3u8) for a session.

        Creates a playlist with SEGMENT_DURATION second segments,
        pointing to the segment proxy endpoint.
        """
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            f"#EXT-X-TARGETDURATION:{int(cls.SEGMENT_DURATION) + 1}",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-MEDIA-SEQUENCE:0",
            # fMP4 initialization segment info
            '#EXT-X-MAP:URI="init.mp4"',
        ]

        if session.total_duration <= 0:
            # Empty playlist
            lines.append("#EXT-X-ENDLIST")
            return "\n".join(lines)

        # Generate segment entries
        # We divide the total duration into SEGMENT_DURATION chunks
        current_time = 0.0
        segment_index = 0

        while current_time < session.total_duration:
            remaining = session.total_duration - current_time
            segment_duration = min(cls.SEGMENT_DURATION, remaining)

            # Ensure minimum segment duration
            if segment_duration < 0.5:
                break

            lines.append(f"#EXTINF:{segment_duration:.3f},")
            lines.append(f"segment-{segment_index}.m4s")

            current_time += segment_duration
            segment_index += 1

        lines.append("#EXT-X-ENDLIST")

        return "\n".join(lines)

    @classmethod
    async def get_init_segment(cls, session: PlaybackSession) -> bytes | None:
        """
        Get the fMP4 initialization segment.

        Fetches a tiny portion of the recording to extract the init segment.
        """
        try:
            # Request first 0.1 seconds to get init data
            start_str = session.start_time.isoformat()

            url = f"{settings.mediamtx_playback_url}/get"
            params = {
                "path": session.camera_path,
                "start": start_str,
                "duration": "0.1",
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, params=params)

                if response.status_code != 200:
                    return None

                # The fMP4 contains init data at the start
                # Return the full response - HLS.js will parse it
                return response.content

        except Exception as e:
            recording_logger.error(f"[HLS] Failed to get init segment: {e}")
            return None

    @classmethod
    async def get_segment(
        cls, session: PlaybackSession, segment_index: int
    ) -> bytes | None:
        """
        Get a specific segment by index.

        Proxies the request to MediaMTX /get endpoint with calculated time range.
        """
        try:
            # Calculate segment time offset
            start_offset = segment_index * cls.SEGMENT_DURATION

            if start_offset >= session.total_duration:
                return None

            # Calculate actual segment duration
            remaining = session.total_duration - start_offset
            segment_duration = min(cls.SEGMENT_DURATION, remaining)

            # Calculate absolute start time
            segment_start = session.start_time + timedelta(seconds=start_offset)
            start_str = segment_start.isoformat()

            url = f"{settings.mediamtx_playback_url}/get"
            params = {
                "path": session.camera_path,
                "start": start_str,
                "duration": str(segment_duration),
            }

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(url, params=params)

                if response.status_code != 200:
                    recording_logger.warning(
                        f"[HLS] MediaMTX /get returned {response.status_code} for segment {segment_index}"
                    )
                    return None

                return response.content

        except Exception as e:
            recording_logger.error(f"[HLS] Failed to get segment {segment_index}: {e}")
            return None

    @classmethod
    async def stream_segment(cls, session: PlaybackSession, segment_index: int):
        """
        Stream a segment with chunked transfer.

        Yields chunks for streaming response.
        """
        try:
            # Calculate segment time offset
            start_offset = segment_index * cls.SEGMENT_DURATION

            if start_offset >= session.total_duration:
                return

            # Calculate actual segment duration
            remaining = session.total_duration - start_offset
            segment_duration = min(cls.SEGMENT_DURATION, remaining)

            # Calculate absolute start time
            segment_start = session.start_time + timedelta(seconds=start_offset)
            start_str = segment_start.isoformat()

            url = f"{settings.mediamtx_playback_url}/get"
            params = {
                "path": session.camera_path,
                "start": start_str,
                "duration": str(segment_duration),
            }

            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("GET", url, params=params) as response:
                    if response.status_code != 200:
                        recording_logger.warning(
                            f"[HLS] MediaMTX stream returned {response.status_code}"
                        )
                        return

                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        yield chunk

        except Exception as e:
            recording_logger.error(
                f"[HLS] Failed to stream segment {segment_index}: {e}"
            )

    @classmethod
    def get_session_count(cls) -> int:
        """Get total active session count."""
        return len(cls._sessions)

    @classmethod
    def get_user_session_count(cls, user_id: int) -> int:
        """Get session count for a specific user."""
        return len(cls._user_sessions.get(user_id, []))

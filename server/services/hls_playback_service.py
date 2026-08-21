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

Generates HLS VOD manifests for MediaMTX recordings.

Two playback paths exist; a session prefers the first and falls back to the
second automatically:

1. Byte-range (fast seek, default): the recording window resolves (via the
   recordings DB index) to a run of CONTIGUOUS fragmented-MP4 clip files on
   disk — with 1-minute segments, up to ~60 per one-hour session window. Each
   file's atom headers are scanned once (cached across sessions) to map every
   moof/mdat fragment to a byte offset+length, then ONE playlist is emitted
   covering the whole run: per-file #EXT-X-MAP + #EXT-X-BYTERANGE entries with
   #EXT-X-DISCONTINUITY at file boundaries (each clip restarts its timestamps).
   hls.js plays straight through file boundaries with no session churn, and a
   seek anywhere in the window is a single HTTP Range request.
2. MediaMTX proxy (fallback): when no on-disk files can be resolved/indexed,
   segments are proxied from MediaMTX's /get endpoint (correct, but re-seeks
   the un-indexed fMP4 on every request).

Sessions are created per CONTINUOUS recording run (the client anchors them
inside one merged timeline segment), so a session never needs to represent a
recording gap.

Security:
- Session-based authentication (session_id is auth token)
- Sessions are time-limited and tied to user
- On-disk file access is confined to the recordings base via V-005 path checks
"""

import asyncio
import os
import struct
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from core.config import settings
from core.logging_config import recording_logger


@dataclass
class SessionFile:
    """One on-disk clip file inside a session's playback window."""

    path: str
    start_offset: float  # seconds from the session timeline start
    duration: float
    init_length: int  # bytes [0, init_length) = ftyp+moov init segment
    byte_segments: list[dict[str, Any]] = field(default_factory=list)


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

    # Byte-range playback (path 1): the ordered run of contiguous clip files
    # covering the session window. Empty -> MediaMTX proxy path (path 2).
    files: list[SessionFile] = field(default_factory=list)

    # Legacy single-file mirrors of files[0] — kept so the plain ``media``
    # endpoint and the H.265 remux path (which is per-file) work unchanged.
    file_path: str | None = None
    init_length: int = 0
    byte_segments: list[dict[str, Any]] = field(default_factory=list)
    # Video sample-entry fourcc of the resolved file (hev1/hvc1/avc1). Drives
    # browser-compat handling: an ``hev1`` (H.265) recording is served through
    # the pure-Python remux (see hevc_remux_service) so it plays in the browser.
    video_codec: str | None = None
    # Seconds from the on-disk file's start to this session window's start. The
    # remux serves the WHOLE physical file, so the native player must offset the
    # timeline by this to map wall-clock time <-> video time (and to seek
    # natively across the whole file without re-loading).
    file_offset_seconds: float = 0.0
    # Frozen H.265 remux recipe for this session (see hevc_remux_service).
    # Pinned at session creation ON PURPOSE: a still-recording file grows, and
    # re-indexing mid-playback would move every byte offset under the player —
    # the browser would then stitch together two different versions of the same
    # URL and stall. A session therefore serves one consistent snapshot; picking
    # up newly finished footage happens when the client starts a new session.
    remux_index: Any = None


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
    # A session covers at most this much media. Bounds manifest size (an
    # unbounded day over 1-min clips would be a multi-MB playlist) while still
    # letting playback run a full hour with zero session churn; the client
    # opens a new session when the playhead exits the window.
    MAX_SESSION_WINDOW_SECONDS: float = 3600.0
    # Clips whose gap is at most this are one continuous run (mirrors
    # recording_query_service.MERGE_TOLERANCE_SECONDS).
    CONTIGUITY_TOLERANCE_SECONDS: float = 2.0
    # Prevent session leaks. Sized for the synchronized multi-camera playback
    # grid: up to 4 tiles each holding one live session, plus churn headroom
    # while tiles cross clip boundaries (old sessions are deleted async).
    MAX_SESSIONS_PER_USER: int = 24
    CLEANUP_INTERVAL: int = 300  # Cleanup expired sessions every 5 minutes

    # In-memory session storage (use Redis in production for scaling)
    _sessions: dict[str, PlaybackSession] = {}
    _user_sessions: dict[int, list[str]] = {}  # user_id -> [session_ids]
    _cleanup_task: asyncio.Task | None = None
    _lock: asyncio.Lock = asyncio.Lock()

    # fMP4 scan cache: closed clip files are immutable, so their fragment maps
    # are reusable across sessions and users. Keyed by (path, size, mtime) so
    # a still-growing newest clip never gets a stale entry.
    _scan_cache: OrderedDict[tuple[str, int, int], tuple[int, list[tuple[int, int]]]] = (
        OrderedDict()
    )
    _SCAN_CACHE_MAX = 4096

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
        db: Any = None,
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
            db: DB session, used to resolve the on-disk recording file for the
                fast byte-range path. When omitted, only the MediaMTX proxy
                path is available.

        Returns:
            PlaybackSession with segment info populated
        """
        # Ensure cleanup task is running
        await cls.start_cleanup_task()

        # Bound the window: caps manifest size at ~1h of 1-min clips; the
        # client opens a new session when the playhead exits the window.
        max_end = start_time + timedelta(seconds=cls.MAX_SESSION_WINDOW_SECONDS)
        if end_time > max_end:
            end_time = max_end

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

        # Identity guard: when the camera's on-disk dir is marked as another
        # camera's (post-DB-wipe window before the reconciler quarantines),
        # NEITHER the byte-range path nor the MediaMTX proxy may serve it —
        # both read the conflicted tree straight off disk.
        conflicted = False
        if db is not None:
            try:
                from models import Camera as _Camera
                from services.camera_identity import dir_conflicts_with_camera
                from services.storage_service import (
                    get_effective_recordings_base_path,
                )

                cam = db.query(_Camera).filter(_Camera.id == camera_id).first()
                if cam is not None:
                    conflicted = await asyncio.to_thread(
                        dir_conflicts_with_camera,
                        cam,
                        Path(get_effective_recordings_base_path(db)),
                    )
            except Exception:
                conflicted = False

        # Query MediaMTX for segment info
        if conflicted:
            recording_logger.warning(
                f"[HLS] Camera {camera_id}: dir identity conflict — serving "
                "empty session instead of another camera's footage"
            )
            segments, total_duration = [], 0.0
        else:
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

        # Try the fast byte-range path: resolve the single on-disk file covering
        # this window and index its fragments. Any failure leaves the session on
        # the MediaMTX proxy path (byte_segments stays empty). Runs off the event
        # loop since it does blocking file I/O.
        try:
            if not conflicted:
                await asyncio.to_thread(
                    cls._attach_byte_index, session, camera_id, db, total_duration
                )
        except Exception as e:
            recording_logger.warning(
                f"[HLS] Byte-range indexing failed, using MediaMTX fallback: {e}"
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
    def _resolve_recording_rows(
        cls, camera_id: int, start_time: datetime, end_time: datetime, db: Any
    ) -> list[tuple[Path, datetime, float]]:
        """Clip files overlapping [start_time, end_time), from the DB index.

        One indexed range query on (camera_id, start_time) — no directory
        walk. Returns path-checked ``(path, start_utc, duration)`` tuples
        sorted by start. Falls back to the legacy filesystem resolution when
        the camera has no indexed rows.
        """
        from models import Recording
        from services.storage_service import (
            get_effective_recordings_base_path,
            resolve_under_root,
        )

        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=UTC)
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=UTC)

        # Legacy 1h files mean the file containing start_time may have begun
        # up to ~2h earlier; the window filter below discards non-overlaps.
        lookback = start_time - timedelta(hours=2)
        rows = (
            db.query(Recording.file_path, Recording.start_time, Recording.duration)
            .filter(
                Recording.camera_id == camera_id,
                Recording.start_time >= lookback,
                Recording.start_time < end_time,
            )
            .order_by(Recording.start_time.asc())
            .all()
        )
        if not rows:
            legacy = cls._resolve_recording_file(camera_id, start_time, end_time, db)
            if legacy is None:
                return []
            path, file_start = legacy
            if file_start.tzinfo is None:
                file_start = file_start.replace(tzinfo=UTC)
            return [(path, file_start, 0.0)]

        root = Path(get_effective_recordings_base_path(db))
        out: list[tuple[Path, datetime, float]] = []
        for rel, row_start, row_dur in rows:
            if row_start.tzinfo is None:
                row_start = row_start.replace(tzinfo=UTC)
            dur = float(row_dur or 0.0)
            row_end = row_start + timedelta(seconds=dur) if dur else end_time
            # Keep only files overlapping the window (allowing 1s clock skew).
            if row_end <= start_time - timedelta(seconds=1) or row_start >= end_time:
                continue
            try:
                resolved = resolve_under_root(root, rel)
            except Exception:
                continue
            if resolved.is_file():
                out.append((resolved, row_start, dur))
        return out

    @classmethod
    def _stored_codec(
        cls, camera_id: int, first_start: datetime, db: Any
    ) -> str | None:
        """The codec recorded for the session's first clip, or None.

        Populated by the segment-complete webhook via the same
        ``probe_video_codec`` used at playback, so the value is directly
        comparable. NULL for reconciler/legacy rows -> caller probes.
        """
        try:
            from models import Recording

            return (
                db.query(Recording.codec)
                .filter(
                    Recording.camera_id == camera_id,
                    Recording.start_time == first_start,
                )
                .order_by(Recording.start_time.asc())
                .limit(1)
                .scalar()
            )
        except Exception:
            return None

    @classmethod
    def _attach_byte_index(
        cls,
        session: PlaybackSession,
        camera_id: int,
        db: Any,
        total_duration: float,
    ) -> None:
        """Resolve the on-disk clip run and populate the byte-range index.

        Blocking (file I/O). On success populates ``session.files`` (plus the
        legacy single-file mirrors from files[0]). On any failure it leaves
        them unset so the session falls back to the MediaMTX proxy.
        """
        if db is None:
            return

        rows = cls._resolve_recording_rows(
            camera_id, session.start_time, session.end_time, db
        )
        if not rows:
            recording_logger.debug(
                f"[HLS] No on-disk files for camera {camera_id}; "
                "using MediaMTX fallback"
            )
            return

        # Codec of the first file decides the path for the whole session: the
        # H.265 remux is per-file, so an HEVC session is truncated to its
        # first clip (the client's boundary logic opens the next one).
        first_path = rows[0][0]
        first_start = rows[0][1]
        try:
            from services.hevc_remux_service import (
                is_browser_incompatible_video,
                probe_video_codec,
            )

            # Prefer the codec recorded at segment-complete (same probe, same
            # fourcc vocabulary) — a webhook-indexed row carries it, so we skip
            # re-opening the file. Reconciler/legacy rows have NULL codec; fall
            # back to probing the first clip.
            session.video_codec = cls._stored_codec(
                camera_id, first_start, db
            ) or probe_video_codec(first_path)
            needs_remux = is_browser_incompatible_video(session.video_codec)
        except Exception:
            session.video_codec = None
            needs_remux = False
        if needs_remux:
            rows = rows[:1]

        session_start = session.start_time
        if session_start.tzinfo is None:
            session_start = session_start.replace(tzinfo=UTC)

        files: list[SessionFile] = []
        timeline = 0.0  # seconds of media accumulated on the session timeline
        prev_end: datetime | None = None
        for path, file_start, row_dur in rows:
            if timeline >= cls.MAX_SESSION_WINDOW_SECONDS:
                break
            # Stop at the first real gap: a session models one continuous run.
            if (
                prev_end is not None
                and (file_start - prev_end).total_seconds()
                > cls.CONTIGUITY_TOLERANCE_SECONDS
            ):
                break

            scan = cls._scan_fmp4_cached(path)
            if scan is None:
                if not files:
                    recording_logger.debug(
                        f"[HLS] {path.name} is not an indexable fMP4; "
                        "using MediaMTX fallback"
                    )
                    return  # first file unusable -> proxy path for the window
                break  # mid-run unusable file: serve what we have
            init_length, fragments = scan

            n = len(fragments)
            file_duration = row_dur
            if file_duration <= 0:
                # Legacy row without duration: assume 1s parts.
                file_duration = float(n)
            avg_part = file_duration / n if n else 1.0
            frags_per_seg = max(1, round(cls.SEGMENT_DURATION / avg_part))

            byte_segments: list[dict[str, Any]] = []
            for i in range(0, n, frags_per_seg):
                group = fragments[i : i + frags_per_seg]
                first_off = group[0][0]
                last_off, last_len = group[-1]
                byte_segments.append(
                    {
                        "offset": first_off,
                        "length": (last_off + last_len) - first_off,
                        "duration": len(group) * avg_part,
                    }
                )
            if not byte_segments:
                break

            files.append(
                SessionFile(
                    path=str(path),
                    start_offset=timeline,
                    duration=file_duration,
                    init_length=init_length,
                    byte_segments=byte_segments,
                )
            )
            timeline += file_duration
            prev_end = file_start + timedelta(seconds=file_duration)

        if not files:
            return

        session.files = files
        session.total_duration = timeline
        # Legacy single-file mirrors (plain `media` endpoint + remux path).
        session.file_path = files[0].path
        session.init_length = files[0].init_length
        session.byte_segments = files[0].byte_segments
        # Seconds from the first file's start to the session window start.
        first_file_start = rows[0][1]
        try:
            session.file_offset_seconds = max(
                0.0, (session_start - first_file_start).total_seconds()
            )
        except Exception:
            session.file_offset_seconds = 0.0

        # For H.265, freeze the remux recipe NOW (see PlaybackSession.remux_index):
        # the file may still be growing, and re-indexing mid-playback would shift
        # every byte offset under the player.
        if needs_remux:
            try:
                from services.hevc_remux_service import build_remux_index

                session.remux_index = build_remux_index(first_path)
            except Exception as e:
                session.remux_index = None
                recording_logger.warning(
                    f"[HLS] Could not pre-index {first_path.name} for remux: {e}"
                )
        recording_logger.info(
            f"[HLS] Indexed {len(files)} file(s) for camera {camera_id}: "
            f"{timeline:.0f}s of media (codec={session.video_codec})"
        )

    @classmethod
    def _scan_fmp4_cached(
        cls, path: Path
    ) -> tuple[int, list[tuple[int, int]]] | None:
        """``_scan_fmp4`` with a cross-session LRU keyed by (path, size, mtime).

        Closed clip files never change, so their fragment maps are shared by
        every session that touches them; a still-growing file naturally misses
        (its size/mtime moved) and is re-scanned.
        """
        try:
            st = path.stat()
        except OSError:
            return None
        key = (str(path), st.st_size, int(st.st_mtime))
        cached = cls._scan_cache.get(key)
        if cached is not None:
            cls._scan_cache.move_to_end(key)
            return cached
        scan = cls._scan_fmp4(path)
        if scan is not None:
            cls._scan_cache[key] = scan
            if len(cls._scan_cache) > cls._SCAN_CACHE_MAX:
                cls._scan_cache.popitem(last=False)
        return scan

    @classmethod
    def _resolve_recording_file(
        cls, camera_id: int, start_time: datetime, end_time: datetime, db: Any
    ) -> tuple[Path, datetime] | None:
        """Find the single on-disk recording file that contains ``start_time``.

        Returns ``(path, file_start)`` — the file whose start timestamp is the
        latest at or before the session start (i.e. the file the session begins
        inside, and that file's naive start datetime), path-checked against the
        recordings base (V-005). Returns None if nothing matches.

        Matching is done on the tz-stripped wall clock: the recording filename,
        MediaMTX's /list start (which the frontend echoes back as ``start``) and
        list_recordings' timestamps all describe the same server-local instant,
        so comparing naive wall-clock times matches on any server timezone.
        """
        from services.storage_service import (
            get_effective_recordings_base_path,
            resolve_under_root,
            storage_service,
        )

        # Identity guard: this filesystem fallback bypasses the recordings
        # index, so it must not serve a directory whose marker belongs to a
        # different camera (post-DB-wipe window before quarantine).
        try:
            from models import Camera as _Camera
            from services.camera_identity import dir_conflicts_with_camera

            cam = db.query(_Camera).filter(_Camera.id == camera_id).first()
            if cam is not None and dir_conflicts_with_camera(
                cam, Path(get_effective_recordings_base_path(db))
            ):
                return None
        except Exception:
            pass

        start_naive = start_time.replace(tzinfo=None)

        # Filter ourselves on the naive clock; list_recordings' own start/end
        # filtering assumes UTC-labelled timestamps and would misfire off-UTC.
        listing = storage_service.list_recordings(
            db, camera_id=camera_id, start=None, end=None, limit=1000
        )
        best_rel: str | None = None
        best_ts: datetime | None = None
        for item in listing.get("items", []):
            ts_str = item.get("start_time")
            rel = item.get("relpath")
            if not ts_str or not rel:
                continue
            try:
                ts = datetime.fromisoformat(ts_str).replace(tzinfo=None)
            except ValueError:
                continue
            # The file that contains the session start begins at or (allowing a
            # small clock skew) just before it; keep the latest such.
            if ts <= start_naive + timedelta(seconds=1) and (
                best_ts is None or ts > best_ts
            ):
                best_ts, best_rel = ts, rel

        if best_rel is None:
            return None

        root = Path(get_effective_recordings_base_path(db))
        try:
            resolved = resolve_under_root(root, best_rel)
        except Exception:
            return None
        return (resolved, best_ts) if resolved.is_file() else None

    @classmethod
    def _scan_fmp4(
        cls, path: Path
    ) -> tuple[int, list[tuple[int, int]]] | None:
        """Walk the top-level atoms of a fragmented-MP4 file.

        Reads only box headers (8-16 bytes each), seeking over mdat payloads,
        so a 1h/~3600-fragment file costs a few thousand tiny reads. Returns
        ``(init_length, [(fragment_offset, fragment_length), ...])`` where
        ``init_length`` is the byte length of the ftyp+moov init segment, or
        None if the file isn't a usable fMP4.
        """
        fragments: list[tuple[int, int]] = []
        init_length = 0
        frag_start: int | None = None

        with open(path, "rb") as f:
            total = os.fstat(f.fileno()).st_size
            pos = 0
            while pos + 8 <= total:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                box_size = struct.unpack(">I", hdr[:4])[0]
                box_type = hdr[4:8]
                header_len = 8
                if box_size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    box_size = struct.unpack(">Q", ext)[0]
                    header_len = 16
                elif box_size == 0:
                    # Box extends to EOF.
                    box_size = total - pos
                if box_size < header_len or pos + box_size > total:
                    break  # malformed / truncated

                if box_type in (b"ftyp", b"moov"):
                    # ftyp then moov are the init segment; its end is the
                    # boundary the #EXT-X-MAP byte range points at.
                    init_length = pos + box_size
                elif box_type == b"moof":
                    frag_start = pos
                elif box_type == b"mdat" and frag_start is not None:
                    fragments.append((frag_start, (pos + box_size) - frag_start))
                    frag_start = None

                pos += box_size

        if init_length <= 0 or not fragments:
            return None
        return init_length, fragments

    @classmethod
    def generate_manifest(cls, session: PlaybackSession) -> str:
        """
        Generate HLS VOD manifest (.m3u8) for a session.

        Uses the byte-range playlist when the recording was indexed on disk
        (fast-seek path), otherwise the MediaMTX-proxied segment playlist.
        """
        if session.files:
            return cls._generate_byterange_manifest(session)

        return cls._generate_proxy_manifest(session)

    @classmethod
    def _generate_byterange_manifest(cls, session: PlaybackSession) -> str:
        """Multi-file byte-range playlist (fast-seek path).

        One VOD playlist over the whole contiguous clip run: each file gets
        its own #EXT-X-MAP (its ftyp+moov head) and #EXT-X-BYTERANGE entries
        against ``media/{n}``, with #EXT-X-DISCONTINUITY between files (each
        clip file restarts its timestamps). hls.js plays straight through the
        boundaries; every fetch is a direct HTTP Range read.
        """
        max_dur = max(
            (s["duration"] for f in session.files for s in f.byte_segments),
            default=1.0,
        )
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{int(max_dur) + 1}",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        for idx, fe in enumerate(session.files):
            if idx > 0:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(
                f'#EXT-X-MAP:URI="media/{idx}",BYTERANGE="{fe.init_length}@0"'
            )
            for seg in fe.byte_segments:
                lines.append(f"#EXTINF:{seg['duration']:.3f},")
                lines.append(f"#EXT-X-BYTERANGE:{seg['length']}@{seg['offset']}")
                lines.append(f"media/{idx}")
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines)

    @classmethod
    def _generate_proxy_manifest(cls, session: PlaybackSession) -> str:
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

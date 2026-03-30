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
Recording Session Service

Groups recording segments into logical sessions for better UX.
Detects continuous recordings and provides timeline metadata.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
from sqlalchemy.orm import Session

from models import Camera
from services.storage_service import get_effective_recordings_base_path


@dataclass
class RecordingSegment:
    """Represents a single recording segment file."""

    path: str  # Relative path from recordings base
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    size_bytes: int
    camera_id: int
    camera_name: str


@dataclass
class RecordingSession:
    """Represents a continuous recording session (grouped segments)."""

    session_id: str  # Unique identifier for this session
    camera_id: int
    camera_name: str
    start_time: datetime
    end_time: datetime
    total_duration_seconds: float
    total_size_bytes: int
    segment_count: int
    segments: list[RecordingSegment]
    date: str  # YYYY-MM-DD for grouping


class RecordingSessionService:
    """Service for grouping recordings into sessions."""

    # Gap threshold: if segments are more than 5 minutes apart, consider them separate sessions
    SESSION_GAP_THRESHOLD_SECONDS = 300

    def __init__(self):
        self.cv2_available = True
        try:
            import cv2
        except ImportError:
            self.cv2_available = False

    def _get_video_duration(self, video_path: Path) -> float:
        """Get video duration in seconds using cv2."""
        if not self.cv2_available:
            return 0.0

        try:
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                return 0.0

            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.release()

            if fps > 0:
                return frame_count / fps
            return 0.0
        except Exception:
            return 0.0

    def _parse_segment_from_path(
        self, relative_path: str, full_path: Path, camera_map: dict[int, dict]
    ) -> RecordingSegment | None:
        """
        Parse recording segment from file path.
        Format: cam-XX/YYYY/MM/DD/HH-MM-SS-ffffff.mp4
        or: cam-XX/YYYY/MM/DD/HH-MM-SS-ffffff/cam-XX.mp4
        """
        try:
            parts = relative_path.split("/")
            if len(parts) < 5:
                return None

            # Extract camera ID
            cam_folder = parts[0]  # cam-XX
            if not cam_folder.startswith("cam-"):
                return None

            cam_id = int(cam_folder.split("-")[1])

            # Extract date components
            year = int(parts[1])
            month = int(parts[2])
            day = int(parts[3])

            # Extract time from filename or folder name
            if len(parts) == 6:
                # New format: cam-XX/YYYY/MM/DD/HH-MM-SS-ffffff/cam-XX.mp4
                timestamp_folder = parts[4]
            else:
                # Old format: cam-XX/YYYY/MM/DD/HH-MM-SS-ffffff.mp4
                timestamp_folder = parts[4].replace(".mp4", "")

            # Parse timestamp: HH-MM-SS-ffffff
            time_parts = timestamp_folder.split("-")
            if len(time_parts) < 4:
                return None

            hour = int(time_parts[0])
            minute = int(time_parts[1])
            second = int(time_parts[2])
            microsecond = int(time_parts[3]) if len(time_parts) > 3 else 0

            start_time = datetime(year, month, day, hour, minute, second, microsecond)

            # Get video duration
            duration = self._get_video_duration(full_path)
            end_time = start_time + timedelta(seconds=duration)

            # Get file size
            size_bytes = full_path.stat().st_size if full_path.exists() else 0

            # Get camera name
            camera_info = camera_map.get(cam_id, {})
            camera_name = camera_info.get("name", f"Camera {cam_id}")

            return RecordingSegment(
                path=relative_path,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                size_bytes=size_bytes,
                camera_id=cam_id,
                camera_name=camera_name,
            )

        except (ValueError, IndexError):
            return None

    def _group_segments_into_sessions(
        self, segments: list[RecordingSegment]
    ) -> list[RecordingSession]:
        """
        Group segments into continuous recording sessions.
        Segments within SESSION_GAP_THRESHOLD_SECONDS are grouped together.
        """
        if not segments:
            return []

        # Sort segments by camera and start time
        sorted_segments = sorted(segments, key=lambda s: (s.camera_id, s.start_time))

        sessions: list[RecordingSession] = []
        current_session_segments: list[RecordingSegment] = []

        for segment in sorted_segments:
            if not current_session_segments:
                # Start new session
                current_session_segments = [segment]
            else:
                last_segment = current_session_segments[-1]

                # Check if same camera and within gap threshold
                time_gap = (segment.start_time - last_segment.end_time).total_seconds()

                if (
                    segment.camera_id == last_segment.camera_id
                    and time_gap <= self.SESSION_GAP_THRESHOLD_SECONDS
                ):
                    # Continue current session
                    current_session_segments.append(segment)
                else:
                    # Finalize current session and start new one
                    sessions.append(self._create_session(current_session_segments))
                    current_session_segments = [segment]

        # Add last session
        if current_session_segments:
            sessions.append(self._create_session(current_session_segments))

        return sessions

    def _create_session(self, segments: list[RecordingSegment]) -> RecordingSession:
        """Create a RecordingSession from a list of segments."""
        if not segments:
            raise ValueError("Cannot create session from empty segments list")

        first_segment = segments[0]
        last_segment = segments[-1]

        session_id = f"{first_segment.camera_id}_{first_segment.start_time.strftime('%Y%m%d_%H%M%S')}"

        total_duration = sum(s.duration_seconds for s in segments)
        total_size = sum(s.size_bytes for s in segments)

        return RecordingSession(
            session_id=session_id,
            camera_id=first_segment.camera_id,
            camera_name=first_segment.camera_name,
            start_time=first_segment.start_time,
            end_time=last_segment.end_time,
            total_duration_seconds=total_duration,
            total_size_bytes=total_size,
            segment_count=len(segments),
            segments=segments,
            date=first_segment.start_time.strftime("%Y-%m-%d"),
        )

    def list_recording_sessions(
        self,
        db: Session,
        camera_id: int | None = None,
        date: str | None = None,  # YYYY-MM-DD format
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        List recording sessions grouped by camera and date.
        Returns hierarchical structure: camera -> date -> sessions.
        """
        recordings_base = Path(get_effective_recordings_base_path(db))

        if not recordings_base.exists():
            return {"recordings_base": str(recordings_base), "cameras": []}

        # Build camera map
        camera_map = {}
        cameras = db.query(Camera).all()
        for cam in cameras:
            camera_map[cam.id] = {
                "id": cam.id,
                "name": cam.name,
                "location": cam.location,
            }

        # Collect all segments
        all_segments: list[RecordingSegment] = []

        # Scan recordings directory
        for cam_dir in recordings_base.iterdir():
            if not cam_dir.is_dir() or not cam_dir.name.startswith("cam-"):
                continue

            try:
                cam_id = int(cam_dir.name.split("-")[1])

                # Filter by camera if specified
                if camera_id is not None and cam_id != camera_id:
                    continue

                # Scan year/month/day structure
                for year_dir in cam_dir.iterdir():
                    if not year_dir.is_dir():
                        continue

                    for month_dir in year_dir.iterdir():
                        if not month_dir.is_dir():
                            continue

                        for day_dir in month_dir.iterdir():
                            if not day_dir.is_dir():
                                continue

                            # Filter by date if specified
                            if date:
                                dir_date = (
                                    f"{year_dir.name}-{month_dir.name}-{day_dir.name}"
                                )
                                if dir_date != date:
                                    continue

                            # Find video files
                            for item in day_dir.iterdir():
                                video_file = None

                                if item.is_file() and item.suffix == ".mp4":
                                    # Old format: direct mp4 file
                                    video_file = item
                                elif item.is_dir():
                                    # New format: folder with mp4 inside
                                    for sub_item in item.iterdir():
                                        if (
                                            sub_item.is_file()
                                            and sub_item.suffix == ".mp4"
                                        ):
                                            video_file = sub_item
                                            break

                                if video_file:
                                    relative_path = str(
                                        video_file.relative_to(recordings_base)
                                    ).replace("\\", "/")
                                    segment = self._parse_segment_from_path(
                                        relative_path, video_file, camera_map
                                    )
                                    if segment:
                                        all_segments.append(segment)

            except (ValueError, IndexError):
                continue

        # Group segments into sessions
        sessions = self._group_segments_into_sessions(all_segments)

        # Build hierarchical structure
        camera_date_map: dict[int, dict[str, list[RecordingSession]]] = {}

        for session in sessions:
            if session.camera_id not in camera_date_map:
                camera_date_map[session.camera_id] = {}

            if session.date not in camera_date_map[session.camera_id]:
                camera_date_map[session.camera_id][session.date] = []

            camera_date_map[session.camera_id][session.date].append(session)

        # Format response
        cameras_list = []
        for cam_id, dates_dict in camera_date_map.items():
            camera_info = camera_map.get(
                cam_id, {"id": cam_id, "name": f"Camera {cam_id}"}
            )

            dates_list = []
            for date_str, date_sessions in sorted(dates_dict.items(), reverse=True):
                # Sort sessions by start time
                sorted_sessions = sorted(date_sessions, key=lambda s: s.start_time)

                sessions_data = []
                for session in sorted_sessions:
                    # Separate complete and incomplete segments
                    complete_segments = [
                        seg
                        for seg in session.segments
                        if seg.duration_seconds > 0 and seg.size_bytes > 0
                    ]
                    incomplete_segments = [
                        seg
                        for seg in session.segments
                        if seg.duration_seconds == 0 or seg.size_bytes == 0
                    ]

                    # Calculate stats for complete segments only
                    complete_duration = sum(
                        s.duration_seconds for s in complete_segments
                    )
                    complete_size = sum(s.size_bytes for s in complete_segments)

                    sessions_data.append(
                        {
                            "session_id": session.session_id,
                            "start_time": session.start_time.isoformat(),
                            "end_time": session.end_time.isoformat(),
                            "duration_seconds": session.total_duration_seconds,
                            "duration_formatted": self._format_duration(
                                session.total_duration_seconds
                            ),
                            "size_bytes": session.total_size_bytes,
                            "size_formatted": self._format_size(
                                session.total_size_bytes
                            ),
                            "segment_count": session.segment_count,
                            "complete_segment_count": len(complete_segments),
                            "incomplete_segment_count": len(incomplete_segments),
                            "is_in_progress": len(incomplete_segments) > 0,
                            "complete_duration_seconds": complete_duration,
                            "complete_duration_formatted": self._format_duration(
                                complete_duration
                            ),
                            "segments": [
                                {
                                    "path": seg.path,
                                    "start_time": seg.start_time.isoformat(),
                                    "end_time": seg.end_time.isoformat(),
                                    "duration_seconds": seg.duration_seconds,
                                    "size_bytes": seg.size_bytes,
                                    "is_complete": seg.duration_seconds > 0
                                    and seg.size_bytes > 0,
                                }
                                for seg in session.segments
                            ],
                        }
                    )

                dates_list.append(
                    {
                        "date": date_str,
                        "session_count": len(sorted_sessions),
                        "total_duration_seconds": sum(
                            s.total_duration_seconds for s in sorted_sessions
                        ),
                        "sessions": sessions_data,
                    }
                )

            cameras_list.append(
                {
                    "camera_id": cam_id,
                    "camera_name": camera_info["name"],
                    "camera_location": camera_info.get("location"),
                    "dates": dates_list,
                }
            )

        # Sort cameras by ID
        cameras_list.sort(key=lambda c: c["camera_id"])

        return {
            "recordings_base": str(recordings_base),
            "camera_count": len(cameras_list),
            "total_sessions": sum(
                len(dates_dict) for dates_dict in camera_date_map.values()
            ),
            "cameras": cameras_list,
        }

    def _format_duration(self, seconds: float) -> str:
        """Format duration as human-readable string."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        if hours > 0:
            return f"{hours}h {minutes}m {secs}s"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{secs}s"

    def _format_size(self, bytes: int) -> str:
        """Format file size as human-readable string."""
        for unit in ["B", "KB", "MB", "GB"]:
            if bytes < 1024.0:
                return f"{bytes:.1f} {unit}"
            bytes /= 1024.0
        return f"{bytes:.1f} TB"


# Singleton instance
_recording_session_service = None


def get_recording_session_service() -> RecordingSessionService:
    """Get or create the recording session service singleton."""
    global _recording_session_service
    if _recording_session_service is None:
        _recording_session_service = RecordingSessionService()
    return _recording_session_service

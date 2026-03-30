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
Compliance & Reports router.

Provides compliance reporting endpoints including:
- System-wide compliance summary
- Per-camera recording coverage statistics
- Access audit logs
- CSV export functionality
"""

import csv
from datetime import date, datetime, timedelta
from io import StringIO

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import Date, cast, func
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser
from core.database import get_db
from core.logging_config import main_logger
from models import AuditLog, Camera, Recording, User

router = APIRouter(prefix="/compliance", tags=["compliance"])


@router.get("/summary")
async def get_compliance_summary(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """
    Get overall compliance summary including:
    - Total cameras
    - Online cameras (streaming data > 0 bytes)
    - Degraded cameras (unused in simplified logic, or specific error states)
    - Offline cameras (no data streaming)
    - Recording enabled count
    - Retention period (Configured Policy)
    - Storage usage (calculated from filesystem)
    """
    import json
    import os
    from datetime import datetime

    from core.config import settings
    from models import SecuritySetting
    from services.mediamtx_admin_service import MediaMtxAdminService
    from services.stream_service import _build_stream_name

    try:
        # Total active cameras in DB
        cameras = db.query(Camera).filter(Camera.is_active == True).all()
        total_cameras = len(cameras)

        # 1. Fetch active paths (Online Status)
        active_paths_map = {}
        try:
            active_paths_result = await MediaMtxAdminService.list_active_paths()
            if active_paths_result.get("status") == "ok":
                items = active_paths_result.get("details", {}).get("items", [])
                for item in items:
                    name = item.get("name")
                    if name:
                        active_paths_map[name] = item
        except Exception as e:
            main_logger.error(f"Failed to fetch active paths from MediaMTX: {e}")

        online_count = 0
        offline_count = 0
        degraded_count = 0
        recording_enabled = 0
        all_segments = []

        for cam in cameras:
            # Check recording status from DB config
            if cam.config and cam.config.recording_enabled:
                recording_enabled += 1

            stream_name = _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            is_online = False

            if stream_name in active_paths_map:
                path_data = active_paths_map[stream_name]
                bytes_received = path_data.get("bytesReceived", 0)
                # Strict online check: must have received data
                if bytes_received > 0:
                    online_count += 1
                    is_online = True
                else:
                    offline_count += 1
            else:
                offline_count += 1

            # Fetch recordings (still need for total_recordings count)
            if settings.mediamtx_playback_url:
                try:
                    import requests as http_client

                    url = f"{settings.mediamtx_playback_url}/list?path={stream_name}"
                    resp = http_client.get(url, timeout=0.5)
                    if resp.status_code == 200:
                        segs = resp.json()
                        if segs:
                            for s in segs:
                                s["camera_id"] = cam.id
                                all_segments.append(s)
                except Exception:
                    pass

        # Retention Days (Use Configured Policy per user request)
        retention_days = 0
        try:
            row = (
                db.query(SecuritySetting)
                .filter(SecuritySetting.key == "recordings_retention")
                .first()
            )
            if row and row.json_value:
                val = json.loads(row.json_value)
                retention_days = val.get("retention_days", 0)
        except Exception as e:
            main_logger.error(f"Error fetching retention setting: {e}")

        # Calculate Total Recordings (Sessions)
        total_sessions = 0
        from collections import defaultdict

        cam_segments = defaultdict(list)
        for s in all_segments:
            cam_segments[s["camera_id"]].append(s)

        for cid, segs in cam_segments.items():
            if not segs:
                continue
            segs.sort(key=lambda x: x.get("start", ""))

            # Simple session logic: count continuous blocks (> 60s gap = new session)
            current_session_count = 1
            try:
                dt = datetime.fromisoformat(segs[0]["start"].replace("Z", "+00:00"))
                last_end_score = dt.timestamp() + segs[0].get("duration", 0)

                for i in range(1, len(segs)):
                    s = segs[i]
                    dt = datetime.fromisoformat(s["start"].replace("Z", "+00:00"))
                    start_score = dt.timestamp()
                    if start_score - last_end_score > 60:
                        current_session_count += 1
                    last_end_score = start_score + s.get("duration", 0)
            except:
                pass
            total_sessions += current_session_count

        # Calculate Total Storage (Filesystem)
        total_storage_bytes = 0
        try:
            # Use configurable recording path or default
            rec_path = settings.recordings_base_path
            if not os.path.isabs(rec_path):
                rec_path = os.path.join(os.getcwd(), rec_path)

            if os.path.exists(rec_path):
                for dirpath, dirnames, filenames in os.walk(rec_path):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        if f.endswith(".mp4") or f.endswith(".m4s"):
                            try:
                                total_storage_bytes += os.path.getsize(fp)
                            except OSError:
                                pass
        except Exception as e:
            main_logger.error(f"Error calculating storage size: {e}")

        total_storage_gb = (
            float(total_storage_bytes) / (1024 * 1024 * 1024)
            if total_storage_bytes
            else 0
        )

        return {
            "total_cameras": total_cameras,
            "online_cameras": online_count,
            "degraded_cameras": degraded_count,
            "offline_cameras": offline_count,
            "recording_enabled": recording_enabled,
            "retention_days": retention_days,
            "total_storage_mb": round(
                total_storage_gb * 1024, 2
            ),  # Keep API response name but value is correct size in MB
            "storage_gb": round(total_storage_gb, 2),  # Add explicit GB field
            "total_recordings": total_sessions,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        main_logger.error(f"Error fetching compliance summary: {e}", exc_info=True)
        raise


@router.get("/recording-coverage")
async def get_recording_coverage(
    days: int = Query(30, ge=1, le=90, description="Number of days to analyze"),
    camera_id: int | None = Query(None, description="Filter by specific camera ID"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """
    Get per-camera recording coverage for the last N days.
    Returns daily recording counts and total duration per camera per day.
    """
    try:
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)

        # Build query
        query = (
            db.query(
                Camera.id.label("camera_id"),
                Camera.name.label("camera_name"),
                cast(Recording.start_time, Date).label("date"),
                func.count(Recording.id).label("recording_count"),
                func.coalesce(func.sum(Recording.duration), 0).label(
                    "total_duration_seconds"
                ),
            )
            .join(Recording, Camera.id == Recording.camera_id)
            .filter(
                Camera.is_active == True,
                Recording.start_time >= start_date,
                Recording.start_time <= end_date,
            )
        )

        if camera_id:
            query = query.filter(Camera.id == camera_id)

        query = query.group_by(
            Camera.id, Camera.name, cast(Recording.start_time, Date)
        ).order_by(Camera.id, cast(Recording.start_time, Date).desc())

        results = query.all()

        # Format results
        coverage = []
        for row in results:
            coverage.append(
                {
                    "camera_id": row.camera_id,
                    "camera_name": row.camera_name,
                    "date": row.date.isoformat()
                    if isinstance(row.date, date)
                    else str(row.date),
                    "recording_count": row.recording_count,
                    "total_duration_seconds": float(row.total_duration_seconds),
                    "total_duration_hours": round(
                        float(row.total_duration_seconds) / 3600, 2
                    ),
                }
            )

        return {
            "start_date": start_date.date().isoformat(),
            "end_date": end_date.date().isoformat(),
            "days": days,
            "coverage": coverage,
        }
    except Exception as e:
        main_logger.error(f"Error fetching recording coverage: {e}", exc_info=True)
        raise


@router.get("/access-audit")
async def get_access_audit(
    limit: int = Query(
        100, ge=1, le=500, description="Maximum number of logs to return"
    ),
    camera_id: int | None = Query(None, description="Filter by camera ID"),
    action_filter: str | None = Query(
        None, description="Filter by action (e.g., 'camera.view', 'stream.start')"
    ),
    days: int = Query(7, ge=1, le=90, description="Number of days to look back"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """
    Get recent camera access audit logs.
    Filters for camera-related actions (view, stream, playback).
    """
    try:
        start_date = datetime.utcnow() - timedelta(days=days)

        # Query audit logs related to camera access
        query = (
            db.query(
                AuditLog.id,
                AuditLog.timestamp,
                AuditLog.action,
                AuditLog.entity_type,
                AuditLog.entity_id,
                AuditLog.details,
                AuditLog.ip,
                AuditLog.user_id,
                User.username,
            )
            .outerjoin(User, AuditLog.user_id == User.id)
            .filter(AuditLog.timestamp >= start_date)
        )

        # Filter by camera-related actions
        if action_filter:
            query = query.filter(AuditLog.action == action_filter)
        else:
            # Default: show camera access/stream related actions
            camera_actions = [
                "camera.view",
                "stream.start",
                "stream.stop",
                "camera.provision",
                "recording.playback",
                "camera.create",
                "camera.update",
                "camera.delete",
            ]
            query = query.filter(AuditLog.action.in_(camera_actions))

        # Filter by specific camera if provided
        if camera_id:
            query = query.filter(
                AuditLog.entity_type == "camera", AuditLog.entity_id == str(camera_id)
            )

        query = query.order_by(AuditLog.timestamp.desc()).limit(limit)

        results = query.all()

        # Format results
        logs = []
        for row in results:
            logs.append(
                {
                    "id": row.id,
                    "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                    "action": row.action,
                    "entity_type": row.entity_type,
                    "entity_id": row.entity_id,
                    "details": row.details,
                    "ip": row.ip,
                    "user_id": row.user_id,
                    "username": row.username,
                }
            )

        return {
            "logs": logs,
            "total": len(logs),
            "days": days,
        }
    except Exception as e:
        main_logger.error(f"Error fetching access audit: {e}", exc_info=True)
        raise


@router.get("/export")
async def export_compliance_csv(
    days: int = Query(30, ge=1, le=90, description="Days of coverage to export"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),  # Only admins can export
):
    """
    Export compliance data as CSV file.
    Includes summary stats and recording coverage for the last N days.
    """
    try:
        # Get summary data
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)

        # Get per-camera coverage
        coverage_query = (
            db.query(
                Camera.id,
                Camera.name,
                Camera.location,
                cast(Recording.start_time, Date).label("date"),
                func.count(Recording.id).label("recording_count"),
                func.coalesce(func.sum(Recording.duration), 0).label("total_duration"),
            )
            .join(Recording, Camera.id == Recording.camera_id)
            .filter(
                Camera.is_active == True,
                Recording.start_time >= start_date,
                Recording.start_time <= end_date,
            )
            .group_by(
                Camera.id,
                Camera.name,
                Camera.location,
                cast(Recording.start_time, Date),
            )
            .order_by(Camera.id, cast(Recording.start_time, Date))
        )

        results = coverage_query.all()

        # Create CSV in memory
        output = StringIO()
        writer = csv.writer(output)

        # Write header
        writer.writerow(
            [
                "Camera ID",
                "Camera Name",
                "Location",
                "Date",
                "Recording Count",
                "Total Duration (hours)",
            ]
        )

        # Write data rows
        for row in results:
            duration_hours = (
                round(float(row.total_duration) / 3600, 2) if row.total_duration else 0
            )
            date_str = (
                row.date.isoformat() if isinstance(row.date, date) else str(row.date)
            )
            writer.writerow(
                [
                    row.id,
                    row.name,
                    row.location or "",
                    date_str,
                    row.recording_count,
                    duration_hours,
                ]
            )

        # Get CSV content
        csv_content = output.getvalue()
        output.close()

        # Return as downloadable file
        filename = f"compliance_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception as e:
        main_logger.error(f"Error exporting compliance CSV: {e}", exc_info=True)
        raise

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
Cloud Streaming Service

Manages external stream publishing (republishing) to platforms like YouTube, Twitch,
or custom RTMP/RTMPS servers. Uses FFmpeg to read from MediaMTX and push to external destinations.

Architecture:
- Each camera can have its own cloud streaming target
- FFmpeg processes are managed per-camera
- Supports RTMP, RTMPS, and custom protocols
- Handles reconnection and failure recovery
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models import SecuritySetting

logger = logging.getLogger(__name__)


class StreamStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"
    RECONNECTING = "reconnecting"


@dataclass
class CloudStreamTarget:
    """Configuration for a cloud streaming target (custom server)."""
    target_id: str  # Unique identifier for this target
    camera_id: int
    enabled: bool = False
    server_url: str = ""  # e.g., rtmp://myserver.com/live or rtmps://secure.server.com/app
    stream_key: str = ""  # Stream key or path identifier
    protocol: str = "rtmp"  # rtmp, rtmps, srt
    use_tls: bool = False  # Use TLS encryption
    use_custom_ca: bool = False  # Use BYOK CA certificate for TLS verification
    video_codec: str = "copy"  # copy, libx264, libx265
    audio_codec: str = "aac"  # copy, aac
    video_bitrate: str | None = None  # e.g., "4000k"
    audio_bitrate: str | None = "128k"
    max_reconnect_attempts: int = 5
    reconnect_delay_seconds: int = 5
    # Runtime-only BYOK temp file paths (not persisted)
    tls_ca_file: str | None = None
    tls_cert_file: str | None = None
    tls_key_file: str | None = None


@dataclass
class StreamProcess:
    """Tracks a running FFmpeg stream process."""
    target_id: str  # Unique target identifier
    camera_id: int
    target: CloudStreamTarget
    process: subprocess.Popen | None = None
    status: StreamStatus = StreamStatus.STOPPED
    started_at: datetime | None = None
    error_message: str | None = None
    reconnect_attempts: int = 0
    stats: dict = field(default_factory=dict)


class CloudStreamingService:
    """Service for managing cloud streaming (republishing) of camera streams."""
    
    _instance = None
    _streams: dict[str, StreamProcess] = {}  # Key is target_id
    _ca_temp_files: list[str] = []
    _monitor_task: asyncio.Task | None = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._streams = {}
        return cls._instance
    
    @classmethod
    def get_instance(cls) -> "CloudStreamingService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def _get_mediamtx_stream_url(self, camera_id: int) -> str:
        """Get the MediaMTX RTSP URL for a camera."""
        prefix = settings.mediamtx_stream_prefix or "cam-"
        if not settings.mediamtx_rtsp_url:
            raise ValueError("MEDIAMTX_RTSP_URL must be configured in environment")
        return f"{settings.mediamtx_rtsp_url}/{prefix}{camera_id}"
    
    def _get_output_url(self, target: CloudStreamTarget) -> str:
        """Construct the full output URL with stream key."""
        if not target.server_url:
            raise ValueError("Server URL is required")
        
        url = target.server_url.rstrip("/")
        key = target.stream_key
        
        # Append stream key if provided and not already in URL
        if key and key not in url:
            return f"{url}/{key}"
        return url
    
    def _generate_stream_token(self, camera_id: int) -> str:
        """Generate a JWT token for internal MediaMTX stream access."""
        from services.mediamtx_jwt_service import MediaMtxJwtService
        
        token = MediaMtxJwtService.create_stream_token(
            user_id=0,  # System service account
            username="cloud-streaming-service",
            camera_id=camera_id,
            actions=["read"],
            expiry_minutes=60 * 24,  # 24 hours
        )
        return token
    
    def _build_ffmpeg_command(
        self,
        camera_id: int,
        target: CloudStreamTarget,
        ca_cert_path: str | None = None,
        cert_file_path: str | None = None,
        key_file_path: str | None = None,
    ) -> list[str]:
        """Build FFmpeg command for republishing stream to custom server."""
        input_url = self._get_mediamtx_stream_url(camera_id)
        output_url = self._get_output_url(target)
        
        # Generate JWT token for MediaMTX authentication
        jwt_token = self._generate_stream_token(camera_id)
        
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
        ]
        
        # Add JWT auth header for HTTP/HLS input (MediaMTX requires authentication)
        # FFmpeg requires \r\n at the end of each header
        if input_url.startswith("http"):
            cmd.extend(["-headers", f"Authorization: Bearer {jwt_token}\r\n"])
        
        # For RTSP, append token to URL as query parameter (RTSP doesn't support HTTP headers)
        if input_url.startswith("rtsp"):
            input_url = f"{input_url}?jwt={jwt_token}"
            cmd.extend(["-rtsp_transport", "tcp"])
        
        cmd.extend(["-i", input_url])
        
        # Video codec settings
        if target.video_codec == "copy":
            cmd.extend(["-c:v", "copy"])
        else:
            cmd.extend(["-c:v", target.video_codec])
            if target.video_bitrate:
                cmd.extend(["-b:v", target.video_bitrate])
            # Add preset for encoding
            if target.video_codec in ("libx264", "libx265"):
                cmd.extend(["-preset", "fast"])
        
        # Audio codec settings
        if target.audio_codec == "copy":
            cmd.extend(["-c:a", "copy"])
        else:
            cmd.extend(["-c:a", target.audio_codec])
            if target.audio_bitrate:
                cmd.extend(["-b:a", target.audio_bitrate])
        
        # Determine output format based on protocol
        protocol = target.protocol.lower()
        if protocol == "srt":
            cmd.extend(["-f", "mpegts"])
        else:
            # RTMP/RTMPS use FLV format
            cmd.extend(["-f", "flv", "-flvflags", "no_duration_filesize"])
        
        # Add TLS options for RTMPS/BYOK.
        # Use FFmpeg-compatible option names (ca_file/cert_file/key_file).
        if protocol == "rtmps":
            cmd.extend(["-tls_verify", "1"])
            if target.use_custom_ca and ca_cert_path:
                cmd.extend(["-ca_file", ca_cert_path])
            if cert_file_path and key_file_path:
                cmd.extend(["-cert_file", cert_file_path, "-key_file", key_file_path])
        
        cmd.append(output_url)
        
        return cmd
    
    def _cleanup_ca_files(self):
        """Clean up temporary CA bundle files."""
        for path in self._ca_temp_files:
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except Exception:
                pass
        self._ca_temp_files = []
    
    def _get_byok_tls_files(self, db: Session | None) -> tuple[str | None, str | None, str | None]:
        """
        Load BYOK cert/key/CA from SecuritySetting 'media_source' and write to temp files.
        Returns (ca_file_path, cert_file_path, key_file_path).
        """
        if db is None:
            return None, None, None
        
        try:
            row = db.query(SecuritySetting).filter(
                SecuritySetting.key == "media_source"
            ).first()
            if not row or not row.json_value:
                return None, None, None
            
            cfg = json.loads(row.json_value)
            ca_bundle_pem = cfg.get("tls_ca_bundle_pem")
            cert_pem = cfg.get("tls_cert_pem")
            key_pem = cfg.get("tls_key_pem")
            
            # Clean previous CA files so we don't leak temp files
            self._cleanup_ca_files()

            ca_path = None
            cert_path = None
            key_path = None

            if ca_bundle_pem:
                ca_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".ca", delete=False
                )
                ca_file.write(ca_bundle_pem)
                ca_file.close()
                self._ca_temp_files.append(ca_file.name)
                ca_path = ca_file.name

            if cert_pem:
                cert_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".crt", delete=False
                )
                cert_file.write(cert_pem)
                cert_file.close()
                self._ca_temp_files.append(cert_file.name)
                cert_path = cert_file.name

            if key_pem:
                key_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".key", delete=False
                )
                key_file.write(key_pem)
                key_file.close()
                self._ca_temp_files.append(key_file.name)
                key_path = key_file.name

            return ca_path, cert_path, key_path
        except Exception:
            # If BYOK material cannot be loaded, fall back to default trust
            return None, None, None
    
    async def start_stream(self, target_id: str, target: CloudStreamTarget, db: Session | None = None) -> dict[str, Any]:
        """Start streaming a camera to cloud target."""
        if target_id in self._streams and self._streams[target_id].status == StreamStatus.RUNNING:
            return {
                "status": "already_running",
                "target_id": target_id,
                "camera_id": target.camera_id,
                "message": "Stream is already running"
            }
        
        try:
            # For RTMPS, always load BYOK client cert/key if present.
            # CA bundle remains optional and controlled by use_custom_ca.
            ca_cert_path: str | None = None
            cert_file_path: str | None = None
            key_file_path: str | None = None
            if target.protocol.lower() == "rtmps":
                if db is not None:
                    ca_cert_path, cert_file_path, key_file_path = self._get_byok_tls_files(db)
                    target.tls_ca_file = ca_cert_path
                    target.tls_cert_file = cert_file_path
                    target.tls_key_file = key_file_path
                else:
                    ca_cert_path = target.tls_ca_file
                    cert_file_path = target.tls_cert_file
                    key_file_path = target.tls_key_file

            cmd = self._build_ffmpeg_command(
                target.camera_id,
                target,
                ca_cert_path=ca_cert_path,
                cert_file_path=cert_file_path,
                key_file_path=key_file_path,
            )
            logger.info(f"Starting cloud stream for target {target_id} (camera {target.camera_id}): {' '.join(cmd[:10])}...")
            
            # Start FFmpeg process
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
            
            stream_process = StreamProcess(
                target_id=target_id,
                camera_id=target.camera_id,
                target=target,
                process=process,
                status=StreamStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
            
            self._streams[target_id] = stream_process
            
            # Start monitoring if not already running
            if self._monitor_task is None or self._monitor_task.done():
                self._monitor_task = asyncio.create_task(self._monitor_streams())
            
            return {
                "status": "started",
                "target_id": target_id,
                "camera_id": target.camera_id,
                "pid": process.pid,
                "server_url": target.server_url,
            }
            
        except Exception as e:
            logger.error(f"Failed to start cloud stream for target {target_id}: {e}")
            return {
                "status": "error",
                "target_id": target_id,
                "camera_id": target.camera_id,
                "message": str(e)
            }
    
    async def stop_stream(self, target_id: str) -> dict[str, Any]:
        """Stop streaming a target to cloud."""
        if target_id not in self._streams:
            return {
                "status": "not_running",
                "target_id": target_id,
                "message": "No stream running for this target"
            }
        
        stream = self._streams[target_id]
        camera_id = stream.camera_id
        
        if stream.process and stream.process.poll() is None:
            try:
                stream.process.terminate()
                try:
                    stream.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    stream.process.kill()
                    stream.process.wait()
            except Exception as e:
                logger.error(f"Error stopping stream for target {target_id}: {e}")
        
        stream.status = StreamStatus.STOPPED
        stream.process = None
        del self._streams[target_id]
        
        return {
            "status": "stopped",
            "target_id": target_id,
            "camera_id": camera_id,
        }
    
    async def get_stream_status(self, target_id: str) -> dict[str, Any]:
        """Get status of a target's cloud stream."""
        if target_id not in self._streams:
            return {
                "target_id": target_id,
                "status": StreamStatus.STOPPED,
                "running": False,
            }
        
        stream = self._streams[target_id]
        running = stream.process is not None and stream.process.poll() is None
        
        return {
            "target_id": target_id,
            "camera_id": stream.camera_id,
            "status": stream.status,
            "running": running,
            "started_at": stream.started_at.isoformat() if stream.started_at else None,
            "server_url": stream.target.server_url,
            "error_message": stream.error_message,
            "reconnect_attempts": stream.reconnect_attempts,
        }
    
    async def get_all_stream_statuses(self) -> list[dict[str, Any]]:
        """Get status of all cloud streams."""
        return [await self.get_stream_status(tid) for tid in self._streams.keys()]
    
    async def _monitor_streams(self):
        """Background task to monitor stream health and handle reconnection."""
        while self._streams:
            for target_id, stream in list(self._streams.items()):
                if stream.status == StreamStatus.STOPPED:
                    continue
                
                # Check if process is still running
                if stream.process and stream.process.poll() is not None:
                    exit_code = stream.process.returncode
                    stderr = ""
                    try:
                        _, stderr_bytes = stream.process.communicate(timeout=1)
                        stderr = stderr_bytes.decode("utf-8", errors="ignore")[-500:]
                    except Exception:
                        pass
                    
                    logger.warning(
                        f"Cloud stream for target {target_id} (camera {stream.camera_id}) exited with code {exit_code}. "
                        f"Stderr: {stderr}"
                    )
                    
                    # Attempt reconnection
                    if stream.reconnect_attempts < stream.target.max_reconnect_attempts:
                        stream.status = StreamStatus.RECONNECTING
                        stream.reconnect_attempts += 1
                        stream.error_message = f"Reconnecting (attempt {stream.reconnect_attempts})..."
                        
                        await asyncio.sleep(stream.target.reconnect_delay_seconds)
                        
                        # Restart the stream
                        result = await self.start_stream(target_id, stream.target)
                        if result.get("status") == "started":
                            logger.info(f"Successfully reconnected cloud stream for target {target_id}")
                    else:
                        stream.status = StreamStatus.ERROR
                        stream.error_message = f"Max reconnection attempts reached. Last error: {stderr}"
                        logger.error(f"Cloud stream for target {target_id} failed permanently")
            
            await asyncio.sleep(5)  # Check every 5 seconds
    
    async def shutdown(self):
        """Stop all streams and cleanup."""
        logger.info("Shutting down cloud streaming service...")
        for target_id in list(self._streams.keys()):
            await self.stop_stream(target_id)
        
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass


# Database helpers for persisting cloud stream configurations

def get_cloud_stream_targets(db: Session) -> dict[str, CloudStreamTarget]:
    """Load cloud stream targets from database. Key is target_id."""
    row = db.query(SecuritySetting).filter(SecuritySetting.key == "cloud_stream_targets").first()
    if not row or not row.json_value:
        return {}
    
    try:
        data = json.loads(row.json_value)
        targets = {}
        for target_id, target_data in data.items():
            targets[target_id] = CloudStreamTarget(target_id=target_id, **target_data)
        return targets
    except Exception as e:
        logger.error(f"Failed to load cloud stream targets: {e}")
        return {}


def save_cloud_stream_target(db: Session, target: CloudStreamTarget) -> None:
    """Save a cloud stream target to database."""
    row = db.query(SecuritySetting).filter(SecuritySetting.key == "cloud_stream_targets").first()
    
    if not row:
        row = SecuritySetting(key="cloud_stream_targets", json_value="{}")
        db.add(row)
    
    try:
        data = json.loads(row.json_value or "{}")
    except Exception:
        data = {}
    
    # Convert target to dict (excluding target_id which is the key)
    target_dict = {
        "camera_id": target.camera_id,
        "enabled": target.enabled,
        "server_url": target.server_url,
        "stream_key": target.stream_key,
        "protocol": target.protocol,
        "use_tls": target.use_tls,
        "use_custom_ca": target.use_custom_ca,
        "video_codec": target.video_codec,
        "audio_codec": target.audio_codec,
        "video_bitrate": target.video_bitrate,
        "audio_bitrate": target.audio_bitrate,
        "max_reconnect_attempts": target.max_reconnect_attempts,
        "reconnect_delay_seconds": target.reconnect_delay_seconds,
    }
    
    data[target.target_id] = target_dict
    row.json_value = json.dumps(data)
    db.commit()


def delete_cloud_stream_target(db: Session, target_id: str) -> None:
    """Delete a cloud stream target from database."""
    row = db.query(SecuritySetting).filter(SecuritySetting.key == "cloud_stream_targets").first()
    if not row:
        return
    
    try:
        data = json.loads(row.json_value or "{}")
        if target_id in data:
            del data[target_id]
            row.json_value = json.dumps(data)
            db.commit()
    except Exception as e:
        logger.error(f"Failed to delete cloud stream target: {e}")


# Server type presets (for custom streaming servers)
SERVER_PRESETS = {
    "rtmp": {
        "name": "RTMP Server",
        "description": "Standard RTMP server (AntMedia, Nginx-RTMP, etc.)",
        "protocol": "rtmp",
        "default_port": 1935,
        "video_codec": "copy",
        "audio_codec": "copy",
    },
    "rtmps": {
        "name": "RTMPS Server (TLS)",
        "description": "Secure RTMP with TLS encryption",
        "protocol": "rtmps",
        "default_port": 443,
        "video_codec": "copy",
        "audio_codec": "copy",
    },
    "srt": {
        "name": "SRT Server",
        "description": "Secure Reliable Transport (low latency)",
        "protocol": "srt",
        "default_port": 9000,
        "video_codec": "copy",
        "audio_codec": "copy",
    },
}

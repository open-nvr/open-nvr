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
Cloud Recording Service

Handles uploading recordings to cloud storage (S3, another NVR, or custom endpoints).
Supports BYOK TLS certificates for secure uploads over HTTPS.

Features:
- S3-compatible storage upload (AWS S3, MinIO, DigitalOcean Spaces, etc.)
- Secure HTTPS uploads with custom certificates (BYOK)
- Automatic retry with exponential backoff
- Upload queue management
"""

import asyncio
import hashlib
import json
import logging
import os
import ssl
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO

import httpx
from sqlalchemy.orm import Session

from core.config import settings
from models import SecuritySetting

logger = logging.getLogger(__name__)


class UploadStatus(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass
class UploadTask:
    """Represents a file upload task."""
    file_path: str
    camera_id: int | None
    destination_key: str
    status: UploadStatus = UploadStatus.PENDING
    attempts: int = 0
    error_message: str | None = None
    created_at: datetime = None
    completed_at: datetime | None = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()


class CloudRecordingService:
    """Service for uploading recordings to cloud storage."""
    
    _instance = None
    _upload_queue: asyncio.Queue = None
    _worker_task: asyncio.Task | None = None
    _ssl_context: ssl.SSLContext | None = None
    _cert_temp_files: list[str] = []
    _byok_cert_file: str | None = None
    _byok_key_file: str | None = None
    _byok_ca_file: str | None = None
    _stats: dict[str, Any] = {}
    _active_file: str | None = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._upload_queue = asyncio.Queue()
            cls._cert_temp_files = []
            cls._stats = {
                "queued_total": 0,
                "completed_total": 0,
                "failed_total": 0,
                "retrying_total": 0,
                "last_error": None,
                "last_success": None,
                "updated_at": None,
            }
            cls._active_file = None
        return cls._instance
    
    @classmethod
    def get_instance(cls) -> "CloudRecordingService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def _load_byok_certificates(self, db: Session) -> ssl.SSLContext | None:
        """Load BYOK certificates from database and create SSL context."""
        try:
            row = db.query(SecuritySetting).filter(
                SecuritySetting.key == "media_source"
            ).first()
            
            if not row or not row.json_value:
                return None
            
            cfg = json.loads(row.json_value)
            cert_pem = cfg.get("tls_cert_pem")
            key_pem = cfg.get("tls_key_pem")
            ca_bundle_pem = cfg.get("tls_ca_bundle_pem")
            
            if not cert_pem or not key_pem:
                return None
            
            # Clean up old temp files
            self._cleanup_temp_files()
            
            # Write certificates to temporary files (SSL context needs file paths)
            cert_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".crt", delete=False
            )
            cert_file.write(cert_pem)
            cert_file.close()
            self._cert_temp_files.append(cert_file.name)
            self._byok_cert_file = cert_file.name
            
            key_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".key", delete=False
            )
            key_file.write(key_pem)
            key_file.close()
            self._cert_temp_files.append(key_file.name)
            self._byok_key_file = key_file.name
            
            # Create SSL context
            ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            ssl_context.load_cert_chain(cert_file.name, key_file.name)
            
            # Load CA bundle if provided
            if ca_bundle_pem:
                ca_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".ca", delete=False
                )
                ca_file.write(ca_bundle_pem)
                ca_file.close()
                self._cert_temp_files.append(ca_file.name)
                self._byok_ca_file = ca_file.name
                ssl_context.load_verify_locations(ca_file.name)
            
            logger.info("Loaded BYOK certificates for cloud uploads")
            return ssl_context
            
        except Exception as e:
            logger.error(f"Failed to load BYOK certificates: {e}")
            return None
    
    def _cleanup_temp_files(self):
        """Clean up temporary certificate files."""
        for path in self._cert_temp_files:
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except Exception:
                pass
        self._cert_temp_files = []
        self._byok_cert_file = None
        self._byok_key_file = None
        self._byok_ca_file = None
    
    def _get_cloud_settings(self, db: Session) -> dict[str, Any]:
        """Load cloud recording settings from database."""
        row = db.query(SecuritySetting).filter(SecuritySetting.key == "cloud").first()
        if not row or not row.json_value:
            return {}
        
        try:
            data = json.loads(row.json_value)
            return data.get("recording", {})
        except Exception:
            return {}
    
    def _get_media_source_settings(self, db: Session) -> dict[str, Any]:
        """Load media source settings (includes cloud_recording_server_ip)."""
        row = db.query(SecuritySetting).filter(
            SecuritySetting.key == "media_source"
        ).first()
        if not row or not row.json_value:
            return {}
        
        try:
            return json.loads(row.json_value)
        except Exception:
            return {}
    
    async def upload_to_nvr(
        self,
        db: Session,
        file_path: str,
        camera_id: int | None,
        relative_path: str,
    ) -> dict[str, Any]:
        """Upload a recording file to another NVR instance with BYOK certificate support."""
        media_cfg = self._get_media_source_settings(db)
        cloud_cfg = self._get_cloud_settings(db)
        cloud_ip = media_cfg.get("cloud_recording_server_ip")
        
        if not cloud_ip:
            return {"status": "error", "message": "No cloud recording server configured"}
        
        if not os.path.exists(file_path):
            return {"status": "error", "message": f"File not found: {file_path}"}
        
        # Determine protocol based on BYOK toggle + available certificates
        use_byok = cloud_cfg.get("use_byok", True)
        ssl_context = self._load_byok_certificates(db) if use_byok else None
        protocol = "https" if ssl_context else "http"
        
        url = f"{protocol}://{cloud_ip}:{settings.port}{settings.api_prefix}/recordings/ingest"
        
        try:
            # Prepare multipart upload
            data = {
                "camera_id": str(camera_id) if camera_id else "",
                "rel": relative_path,
            }
            
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0),
                verify=ssl_context if ssl_context else True,
            ) as client:
                with open(file_path, "rb") as f:
                    files = {
                        "file": (os.path.basename(file_path), f, "video/mp4")
                    }
                    params = {}
                    if settings.mediamtx_webhook_token:
                        params["t"] = settings.mediamtx_webhook_token
                    
                    response = await client.post(
                        url,
                        params=params,
                        data=data,
                        files=files,
                    )
            
            if response.is_success:
                logger.info(f"Uploaded {file_path} to cloud NVR at {cloud_ip}")
                return {
                    "status": "success",
                    "message": f"Uploaded to {cloud_ip}",
                    "protocol": protocol,
                }
            else:
                return {
                    "status": "error",
                    "message": f"Upload failed: {response.status_code} - {response.text}",
                }
                
        except Exception as e:
            logger.error(f"Cloud NVR upload failed: {e}")
            return {"status": "error", "message": str(e)}
    
    async def upload_to_s3(
        self,
        db: Session,
        file_path: str,
        destination_key: str,
    ) -> dict[str, Any]:
        """Upload a recording file to S3-compatible storage."""
        cloud_cfg = self._get_cloud_settings(db)
        
        if not cloud_cfg.get("enabled"):
            return {"status": "error", "message": "Cloud recording not enabled"}
        
        server_url = cloud_cfg.get("server_url")
        bucket = cloud_cfg.get("bucket")
        access_key = cloud_cfg.get("access_key")
        secret_key = cloud_cfg.get("secret_key")
        region = cloud_cfg.get("region", "us-east-1")
        
        if not all([server_url, bucket, access_key, secret_key]):
            return {"status": "error", "message": "Incomplete S3 configuration"}
        
        if not os.path.exists(file_path):
            return {"status": "error", "message": f"File not found: {file_path}"}
        
        try:
            # Load BYOK certs/CA only when enabled in cloud recording settings.
            use_byok = cloud_cfg.get("use_byok", True)
            if use_byok:
                self._load_byok_certificates(db)
            else:
                self._cleanup_temp_files()

            # Use boto3 if available, otherwise fall back to httpx with AWS4 signing
            try:
                import boto3
                from botocore.config import Config

                is_aws_s3 = "amazonaws.com" in (server_url or "")

                # AWS S3 public endpoints should use system trust.
                # BYOK CA bundles are primarily for private/custom S3-compatible servers.
                if is_aws_s3:
                    verify_param: bool | str = True
                else:
                    verify_param = self._byok_ca_file if self._byok_ca_file else True

                config_kwargs: dict[str, Any] = {}
                if not is_aws_s3:
                    config_kwargs["signature_version"] = "s3v4"

                # Optional mTLS client cert support (available in newer botocore).
                if not is_aws_s3 and self._byok_cert_file and self._byok_key_file:
                    config_kwargs["client_cert"] = (
                        self._byok_cert_file,
                        self._byok_key_file,
                    )

                try:
                    boto_cfg = Config(**config_kwargs)
                except TypeError:
                    # Older botocore may not support client_cert.
                    config_kwargs.pop("client_cert", None)
                    boto_cfg = Config(**config_kwargs)
                
                # Determine if this is AWS S3 or S3-compatible
                if is_aws_s3:
                    s3_client = boto3.client(
                        "s3",
                        region_name=region,
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                        verify=verify_param,
                        config=boto_cfg,
                    )
                else:
                    # S3-compatible endpoint (MinIO, DigitalOcean, etc.)
                    s3_client = boto3.client(
                        "s3",
                        endpoint_url=server_url,
                        region_name=region,
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                        verify=verify_param,
                        config=boto_cfg,
                    )
                
                # Determine storage class
                storage_class = cloud_cfg.get("storage_class", "STANDARD")
                extra_args = {"StorageClass": storage_class} if storage_class else {}
                
                # Upload is blocking I/O; run it in a thread so API remains responsive.
                await asyncio.to_thread(
                    s3_client.upload_file,
                    file_path,
                    bucket,
                    destination_key,
                    ExtraArgs=extra_args,
                )
                
                logger.info(f"Uploaded {file_path} to s3://{bucket}/{destination_key}")
                return {
                    "status": "success",
                    "message": f"Uploaded to s3://{bucket}/{destination_key}",
                    "bucket": bucket,
                    "key": destination_key,
                }
                
            except ImportError:
                return {
                    "status": "error",
                    "message": "boto3 not installed. Run: uv add boto3",
                }
                
        except Exception as e:
            logger.error(f"S3 upload failed: {e}")
            return {"status": "error", "message": str(e)}
    
    async def queue_upload(
        self,
        file_path: str,
        camera_id: int | None,
        destination_key: str,
    ) -> dict[str, Any]:
        """Queue a file for upload to cloud storage."""
        task = UploadTask(
            file_path=file_path,
            camera_id=camera_id,
            destination_key=destination_key,
        )
        
        await self._upload_queue.put(task)
        self._stats["queued_total"] += 1
        self._stats["updated_at"] = datetime.utcnow().isoformat()
        
        # Start worker if not running
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._process_upload_queue())
        
        return {
            "status": "queued",
            "file_path": file_path,
            "destination_key": destination_key,
        }
    
    async def _process_upload_queue(self):
        """Background worker to process upload queue."""
        from core.database import SessionLocal
        
        while True:
            try:
                task = await asyncio.wait_for(
                    self._upload_queue.get(),
                    timeout=60.0
                )
            except asyncio.TimeoutError:
                if self._upload_queue.empty():
                    logger.debug("Upload queue empty, worker stopping")
                    break
                continue
            
            task.status = UploadStatus.UPLOADING
            task.attempts += 1
            self._active_file = task.file_path
            self._stats["updated_at"] = datetime.utcnow().isoformat()
            
            db = SessionLocal()
            try:
                # Try S3 first if configured
                cloud_cfg = self._get_cloud_settings(db)
                if cloud_cfg.get("enabled") and cloud_cfg.get("server_url"):
                    result = await self.upload_to_s3(
                        db,
                        task.file_path,
                        task.destination_key,
                    )
                else:
                    # Fall back to NVR-to-NVR upload
                    result = await self.upload_to_nvr(
                        db,
                        task.file_path,
                        task.camera_id,
                        task.destination_key,
                    )
                
                if result.get("status") == "success":
                    task.status = UploadStatus.COMPLETED
                    task.completed_at = datetime.utcnow()
                    logger.info(f"Upload completed: {task.file_path}")
                    self._stats["completed_total"] += 1
                    self._stats["last_success"] = {
                        "file": task.file_path,
                        "message": result.get("message"),
                    }
                else:
                    # Retry logic with exponential backoff
                    if task.attempts < 3:
                        task.status = UploadStatus.RETRYING
                        task.error_message = result.get("message")
                        self._stats["retrying_total"] += 1
                        self._stats["last_error"] = {
                            "file": task.file_path,
                            "message": task.error_message,
                        }
                        delay = 2 ** task.attempts  # 2, 4, 8 seconds
                        await asyncio.sleep(delay)
                        await self._upload_queue.put(task)
                    else:
                        task.status = UploadStatus.FAILED
                        task.error_message = result.get("message")
                        self._stats["failed_total"] += 1
                        self._stats["last_error"] = {
                            "file": task.file_path,
                            "message": task.error_message,
                        }
                        logger.error(
                            f"Upload failed permanently: {task.file_path} - {task.error_message}"
                        )
                        
            except Exception as e:
                logger.error(f"Upload error: {e}")
                task.status = UploadStatus.FAILED
                task.error_message = str(e)
                self._stats["failed_total"] += 1
                self._stats["last_error"] = {
                    "file": task.file_path,
                    "message": task.error_message,
                }
            finally:
                db.close()
                self._active_file = None
                self._stats["updated_at"] = datetime.utcnow().isoformat()
            
            self._upload_queue.task_done()
    
    def get_queue_status(self) -> dict[str, Any]:
        """Get current upload queue status."""
        return {
            "queue_size": self._upload_queue.qsize() if self._upload_queue else 0,
            "worker_running": self._worker_task is not None and not self._worker_task.done(),
            "active_file": self._active_file,
            "stats": dict(self._stats),
        }
    
    async def shutdown(self):
        """Cleanup resources."""
        self._cleanup_temp_files()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass


def upload_recording_to_cloud_sync(
    db: Session,
    file_path: str,
    camera_id: int | None,
    relative_path: str,
) -> dict[str, Any]:
    """Synchronous wrapper for cloud upload (for use in webhooks)."""
    service = CloudRecordingService.get_instance()
    
    # Queue the upload for async processing
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            service.queue_upload(file_path, camera_id, relative_path)
        )
    finally:
        loop.close()

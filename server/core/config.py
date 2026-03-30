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
Configuration module for the FastAPI application.
Handles environment variables and application settings.
"""

import base64
import binascii
import os

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _get_default_recordings_path() -> str:
    """
    Auto-detect default recordings path based on environment.
    
    Returns:
        - Docker: /app/recordings (if RECORDINGS_BASE_PATH env var or /.dockerenv exists)
        - Non-Docker: ./recordings (relative to backend working directory)
    """
    # Check env var first (explicit override)
    env_path = os.getenv("RECORDINGS_BASE_PATH")
    if env_path:
        return env_path
    
    # Check if running in Docker
    if os.path.exists("/.dockerenv"):
        return "/app/recordings"
    
    # Non-Docker: use relative path
    return "./recordings"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Security settings
    # Dummy hash for timing attack mitigation (pre-computed bcrypt hash)
    # Default is the hash of "timing_attack_mitigation" with cost 12
    dummy_password_hash: str = (
        "$2b$12$UnGgF7H6Qt4bO4VWTo/dd.U6Wloatx58kEOT3EQo7hkvQlVTQQSTm"
    )

    # Database settings
    database_url: str

    # JWT settings
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    # Application settings
    debug: bool = False  # Never enable debug in production
    host: str = "127.0.0.1"  # Localhost only - blocks network access from other devices
    port: int = 8000
    application_url: str | None = None  # Auto-detected from host:port if not set
    api_prefix: str = "/api/v1"  # API route prefix

    # CORS settings - localhost only for single-machine deployment
    cors_origins: str = "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173"  # Comma-separated list

    # MediaMTX playback settings (WHEP)
    mediamtx_base_url: str = "http://localhost:8889"
    mediamtx_token: str | None = None
    mediamtx_stream_prefix: str = "cam-"
    mediamtx_path_mode: str = "id"  # id | ip (case-insensitive)

    # MediaMTX admin API v3 (reverse proxy or direct)
    mediamtx_admin_api: str | None = None
    mediamtx_admin_token: str | None = None
    mediamtx_auto_provision: bool = True  # Enable/disable auto-provisioning on startup

    # MediaMTX service URLs (internal - for backend to MediaMTX communication)
    mediamtx_hls_url: str | None = "http://localhost:8888"  # HLS streaming endpoint
    mediamtx_rtsp_url: str | None = "rtsp://localhost:8554"  # RTSP streaming endpoint
    mediamtx_playback_url: str = (
        "http://localhost:9996"  # Playback server for recordings
    )

    # MediaMTX external URLs (for browser access - falls back to internal URLs if not set)
    mediamtx_external_base_url: str | None = (
        None  # External WebRTC endpoint for browsers
    )
    mediamtx_external_hls_url: str | None = None  # External HLS endpoint for browsers
    mediamtx_external_playback_url: str | None = (
        None  # External playback endpoint for browsers
    )

    # MediaMTX internal port addresses for configuration generation
    mediamtx_api_port: int = 9997  # Admin API port
    mediamtx_rtsp_port: int = 8554  # RTSP port
    mediamtx_webrtc_port: int = 8889  # WebRTC port
    mediamtx_hls_port: int = 8888  # HLS port

    # MediaMTX webhook settings
    mediamtx_webhook_token: str | None = None  # Token for webhook verification (legacy)

    # MediaMTX security secret - used for hook verification via X-MTX-Secret header
    # LOCAL DEV: Set MEDIAMTX_SECRET in .env file (must match mediamtx.yml runOnInit/runOnRecordSegmentComplete webhooks)
    # DOCKER: Uses hardcoded secret embedded in Docker image (not visible in source code)
    # WARNING: In local mode, this MUST match the X-MTX-Secret query parameter in mediamtx.yml webhooks!
    # Generate with: openssl rand -hex 32
    mediamtx_secret: str = "change-this-mediamtx-secret-use-openssl-rand-hex-32"

    # Recording settings
    # Auto-detected default path:
    # - Docker: /app/recordings (if RECORDINGS_BASE_PATH env var or /.dockerenv exists)
    # - Non-Docker: ./recordings (relative to backend working directory)
    # User can override this in UI (Configuration > Storage)
    recordings_base_path: str = _get_default_recordings_path()
    
    # Docker volume mount paths for path mapping (only used in Docker deployments)
    # These map between host filesystem paths and container filesystem paths
    recordings_host_base: str | None = None  # Host filesystem path (e.g., D:/opennvr/Recordings)
    recordings_container_base: str = "/app/recordings"  # Container mount point

    # Default admin user settings (created on startup if not exists)
    default_admin_username: str = "admin"
    default_admin_password: str = "admin123"
    default_admin_email: str = "admin@opennvr.local"
    default_admin_first_name: str = "System"
    default_admin_last_name: str = "Administrator"

    # Logging settings
    log_level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    log_file_enabled: bool = True
    log_file_path: str = "logs/server.log"
    log_file_max_size_mb: int = 50  # Maximum log file size in MB
    log_file_backup_count: int = 10  # Number of backup files to keep
    log_console_enabled: bool = True
    log_json_format: bool = True  # Use JSON format for structured logging

    # Suricata log file paths (WSL-friendly defaults)
    suricata_eve_path: str = r"\\wsl$\\Ubuntu\\var\\log\\suricata\\eve.json"
    suricata_fastlog_path: str = r"\\wsl$\\Ubuntu\\var\\log\\suricata\\fast.log"

    # FFmpeg-based RTSP proxy and local disk recordings are disabled/removed.
    # The application now relies solely on MediaMTX for streaming.

    # Cloud provider settings
    credential_encryption_key: str  # Base64-encoded Fernet key
    internal_api_key: str  # For adapter authentication
    kai_c_url: str = "http://localhost:8100"  # KAI-C orchestrator URL
    kai_c_ip: str = "127.0.0.1"  # KAI-C IP for whitelisting

    @field_validator("secret_key", "mediamtx_secret", "internal_api_key")
    @classmethod
    def validate_strong_secrets(cls, v: str, info: ValidationInfo) -> str:
        if not v:
            raise ValueError(f"{info.field_name} cannot be empty")

        # Enforce minimum length and check for weak defaults
        key_name = info.field_name
        weak_passwords = [
            "secret",
            "password",
            "123456",
            "changeme",
            "admin",
            "default",
            "topsecret",
        ]

        if v.lower() in weak_passwords:
            raise ValueError(
                f"{key_name} is set to a weak default value ('{v}'). Please change it immediately."
            )

        if len(v) < 12:
            raise ValueError(
                f"{key_name} is too short (minimum 12 characters required)."
            )

        return v

    @field_validator("credential_encryption_key")
    @classmethod
    def validate_fernet_key(cls, v: str) -> str:
        try:
            # Check if it's valid base64
            decoded = base64.urlsafe_b64decode(v)
            # Check if it decodes to 32 bytes (required for Fernet)
            if len(decoded) != 32:
                raise ValueError("Key must decode to exactly 32 bytes.")
        except (binascii.Error, ValueError):
            raise ValueError(
                "Invalid base64 encoding for credential_encryption_key. Must be a valid Fernet key."
            )
        return v

    def get_application_url(self) -> str:
        """Get the application URL, auto-detecting if not configured."""
        if self.application_url:
            return self.application_url.rstrip("/")

        # Auto-detect based on host and port
        if self.host == "0.0.0.0":
            host = "localhost"
        else:
            host = self.host

        return f"http://{host}:{self.port}"

    # Pydantic v2 settings config
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


# Create global settings instance
settings = Settings()


def get_settings() -> Settings:
    """Get the application settings instance."""
    return settings

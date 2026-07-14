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
import ipaddress
import os
import socket
from typing import Literal
from urllib.parse import urlparse

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Owned by core.secret_policy so the Makefile's check-secrets target can import
# it without instantiating Settings(). Single source of truth. See V-002.
from core.secret_policy import PLACEHOLDER_FRAGMENTS as _PLACEHOLDER_FRAGMENTS  # noqa: F401

# Bare hostnames treated as internal (fast path before DNS resolution). IP
# literals are classified in _host_is_internal. 0.0.0.0 is NOT internal — it's
# the wildcard bind that the MediaMTX trust-zone check must refuse.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# How long the resolver is allowed to spend on getaddrinfo before we give up
# and fail-closed. Broken DNS at boot must not hang startup.
_DNS_RESOLVE_TIMEOUT_SECONDS = 2.0


def _ip_is_internal(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``addr`` is inside the MediaMTX trust zone: loopback, RFC1918,
    IPv6 ULA, or link-local. Public addresses and the 0.0.0.0 wildcard are
    rejected. See V-015.
    """
    # is_private also matches 0.0.0.0/8, so exclude the wildcard explicitly.
    if addr.is_unspecified:
        return False
    return bool(
        addr.is_loopback
        or addr.is_private          # covers RFC1918 + IPv6 ULA
        or addr.is_link_local       # covers 169.254.0.0/16 + fe80::/10
    )


def _host_is_internal(host: str | None) -> bool:
    """True if ``host`` (hostname or IP literal) resolves entirely inside the
    trust zone. Hostnames are resolved with a short timeout and fail closed;
    every resolved address must be internal.
    """
    if not host:
        return False
    h = host.strip("[]").lower()
    if h in _LOOPBACK_HOSTS:
        return True
    try:
        # IP-literal fast path.
        return _ip_is_internal(ipaddress.ip_address(h))
    except ValueError:
        pass
    # Hostname path, time-bounded so a broken resolver can't hang boot.
    saved_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(_DNS_RESOLVE_TIMEOUT_SECONDS)
        try:
            infos = socket.getaddrinfo(h, None)
        except (socket.gaierror, socket.timeout, OSError):
            return False
    finally:
        socket.setdefaulttimeout(saved_timeout)
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            if not _ip_is_internal(ipaddress.ip_address(addr)):
                return False
        except ValueError:
            return False
    return bool(infos)


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
    refresh_token_expire_days: int = 30

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

    # Default recording segment length (seconds) the backend sends to MediaMTX
    # when provisioning a camera that has no explicit value of its own. Env var:
    # RECORDING_SEGMENT_SECONDS. Default 3600 (1h) to match the MediaMTX
    # pathDefaults `recordSegmentDuration: 1h` — keep them in sync.
    recording_segment_seconds: int = 3600

    # MediaMTX service URLs (internal - for backend to MediaMTX communication)
    mediamtx_hls_url: str | None = "http://localhost:8888"  # HLS streaming endpoint
    mediamtx_rtsp_url: str | None = "rtsp://localhost:8554"  # RTSP streaming endpoint
    # TLS RTSP (RTSPS) endpoint the backend uses to reach MediaMTX (port 8322).
    # The *_external_ variant is the URL handed to browsers instead. See V-019.
    mediamtx_rtsps_url: str | None = "rtsps://localhost:8322"
    mediamtx_external_rtsps_url: str | None = None
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

    # Read inference frames from MediaMTX's plaintext loopback listener instead
    # of a second RTSP session to the camera. Turn off for distributed setups
    # where MediaMTX and KAI-C are on different hosts. See V-019.
    inference_use_mediamtx_tap: bool = True

    # Give the camera-agent a low-res substream (derived from vendor URL
    # conventions) instead of the full-res feed, to save CPU on a single box.
    # Off by default since not every camera exposes a substream.
    agent_live_use_substream: bool = False

    # MediaMTX webhook settings
    mediamtx_webhook_token: str | None = None  # Token for webhook verification (legacy)

    # Shared secret for verifying MediaMTX webhooks (X-MTX-Secret header); must
    # match the runOn* hooks in mediamtx.yml. Required, no default.
    # Generate with: openssl rand -hex 32 (or `make secrets`). See V-002.
    mediamtx_secret: str

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

    # Default admin, created on first boot. There is NO default password: the
    # account starts with password_set=False and requires the token-gated
    # first-time-setup flow. See V-001.
    default_admin_username: str = "admin"
    default_admin_password: str | None = None
    default_admin_email: str = "admin@opennvr.local"
    default_admin_first_name: str = "System"
    default_admin_last_name: str = "Administrator"

    # Deployment posture (env-only, not runtime-mutable):
    #   offline (default) - cloud routes 403, cloud callsites refuse outbound
    #   hybrid            - cloud allowed, each crossing audit-logged
    #   cloud             - unrestricted
    # See V-009 / V-022.
    deployment_mode: Literal["offline", "hybrid", "cloud"] = "offline"

    # AI egress posture (env-only):
    #   local_only (default) - KAI-C refuses non-local adapters; cloud infer 403
    #   federated            - cross-org training, anonymised params only
    #   cloud_allowed        - both checks off
    ai_sovereignty: Literal[
        "local_only", "federated", "cloud_allowed"
    ] = "local_only"

    # Informational only: records the operator's acknowledgement (boot audit +
    # /system/posture) when running the permissive mediamtx.local.yml without
    # TLS. Does not change MediaMTX behaviour. See V-019.
    mediamtx_allow_plaintext_outputs: bool = False

    # One-click app install, opt-in (default off = air-gapped posture). Even
    # when on, the web app never runs Docker: it writes a desired-state row and
    # a separate reconciler applies it. See docs/APPS_INSTALL.md.
    apps_install_enabled: bool = False

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
        """Reject empty, weak, placeholder, or <32-char secrets at startup.
        See V-002.
        """
        key_name = info.field_name
        if not v:
            raise ValueError(
                f"{key_name} must be set. Run `make secrets` to generate "
                f"cryptographically random values for all required secrets."
            )

        v_lower = v.lower()

        weak_passwords = {
            "secret",
            "password",
            "123456",
            "changeme",
            "admin",
            "default",
            "topsecret",
            "test",
            "dev",
        }
        if v_lower in weak_passwords:
            raise ValueError(
                f"{key_name} is set to a weak value. Run `make secrets` "
                f"to generate a strong replacement."
            )

        for fragment in _PLACEHOLDER_FRAGMENTS:
            if fragment in v_lower:
                raise ValueError(
                    f"{key_name} still looks like the placeholder shipped in "
                    f"env.example (matched fragment '{fragment}'). Run "
                    f"`make secrets` to generate a real value."
                )

        # 32 chars covers both `openssl rand -hex 32` (64 hex chars) and the
        # urlsafe-base64 form of a 32-byte random value (~43 chars).
        if len(v) < 32:
            raise ValueError(
                f"{key_name} is too short ({len(v)} chars; minimum 32 "
                f"required). Run `make secrets` to generate a strong value."
            )

        return v

    @field_validator("credential_encryption_key")
    @classmethod
    def validate_fernet_key(cls, v: str) -> str:
        # Run the placeholder check first, then verify Fernet structure — a
        # shape-only check would accept a publicly-known test key.
        if not v:
            raise ValueError(
                "credential_encryption_key must be set. Run `make secrets` "
                "to generate one."
            )
        v_lower = v.lower()
        for fragment in _PLACEHOLDER_FRAGMENTS:
            if fragment in v_lower:
                raise ValueError(
                    f"credential_encryption_key still looks like a placeholder "
                    f"(matched fragment '{fragment}'). Run `make secrets` to "
                    f"generate a real Fernet key."
                )
        try:
            # Check if it's valid base64
            decoded = base64.urlsafe_b64decode(v)
            # Check if it decodes to 32 bytes (required for Fernet)
            if len(decoded) != 32:
                raise ValueError("Key must decode to exactly 32 bytes.")
        except (binascii.Error, ValueError):
            raise ValueError(
                "Invalid base64 encoding for credential_encryption_key. "
                "Must be a valid Fernet key."
            )
        return v

    @model_validator(mode="after")
    def _enforce_mediamtx_internal(self) -> "Settings":
        """Refuse to start if any ingress-side MediaMTX URL resolves outside
        the trust zone (loopback / RFC1918 / ULA / link-local). Browser-facing
        egress uses the MEDIAMTX_EXTERNAL_* settings, which are exempt.
        See V-015.
        """
        # URLs to check (None = use default, always localhost). The
        # MEDIAMTX_EXTERNAL_* egress URLs are intentionally excluded.
        candidates: list[tuple[str, str | None]] = [
            ("MEDIAMTX_BASE_URL", self.mediamtx_base_url),
            ("MEDIAMTX_ADMIN_API", self.mediamtx_admin_api),
            ("MEDIAMTX_HLS_URL", self.mediamtx_hls_url),
            ("MEDIAMTX_RTSP_URL", self.mediamtx_rtsp_url),
            ("MEDIAMTX_RTSPS_URL", self.mediamtx_rtsps_url),
            ("MEDIAMTX_PLAYBACK_URL", self.mediamtx_playback_url),
        ]

        offending: list[str] = []
        for name, raw in candidates:
            if not raw:
                continue
            try:
                parsed = urlparse(raw)
            except (ValueError, TypeError):
                offending.append(f"{name}={raw!r} (unparseable URL)")
                continue
            host = parsed.hostname
            # A scheme-less value (e.g. "192.168.1.5:8889") parses with
            # hostname=None; reject it instead of letting it slip through.
            if host is None:
                offending.append(
                    f"{name}={raw!r} (unparseable host — did you forget the "
                    f"http:// scheme?)"
                )
                continue
            # 0.0.0.0 is the wildcard bind, not an internal address — reject
            # it with a specific message.
            if host == "0.0.0.0":
                offending.append(
                    f"{name}={raw!r} (host is 0.0.0.0 — that is the "
                    f"bind-everywhere wildcard, not an internal address; "
                    f"MediaMTX is almost certainly exposed on every NIC "
                    f"including the public uplink. Bind MediaMTX to the "
                    f"camera-LAN address instead, or front it with TLS and "
                    f"use MEDIAMTX_EXTERNAL_* for the public URL.)"
                )
                continue
            if not _host_is_internal(host):
                offending.append(f"{name}={raw!r} (host={host})")

        if offending:
            details = "\n  - ".join(offending)
            raise ValueError(
                "V-015: MediaMTX ingress endpoints resolve to a host outside "
                "OpenNVR's trust zone (loopback / RFC1918 / IPv6 ULA / "
                "link-local). MediaMTX speaks plaintext RTSP and HTTP on "
                "this path, so a public-internet-reachable address would "
                "void the Secure-by-Design guarantee. Bind MediaMTX to your "
                "camera-LAN / Docker-bridge / VPN-overlay interface, or, "
                "for browser-facing access, terminate TLS in a reverse "
                "proxy and publish the public URL via MEDIAMTX_EXTERNAL_* "
                "(which is intentionally outside this check). "
                f"Offending settings:\n  - {details}"
            )
        return self

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

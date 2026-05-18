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

# The placeholder-fragment list is owned by core.secret_policy so that it
# is also importable from the Makefile's `check-secrets` target without
# triggering the full Settings() instantiation at import time
# (M0 followup H-3 — single source of truth between runtime and tooling).
from core.secret_policy import PLACEHOLDER_FRAGMENTS as _PLACEHOLDER_FRAGMENTS  # noqa: F401

# Hosts that count as "loopback" for the purposes of V-015 MediaMTX bind
# enforcement. Any other host requires ALLOW_REMOTE_MEDIAMTX=true.
#
# NOTE: 0.0.0.0 is intentionally NOT loopback — it is the bind-everywhere /
# wildcard address. A URL written against 0.0.0.0 almost always means the
# corresponding MediaMTX listener is also bound to 0.0.0.0, which is exactly
# the public exposure V-015 must refuse. We treat it as the most obvious form
# of misconfiguration and emit a specific error message in
# _enforce_mediamtx_loopback below.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# How long the loopback resolver is allowed to spend on getaddrinfo before we
# give up and fail-closed. Broken DNS at boot must not hang startup.
_DNS_RESOLVE_TIMEOUT_SECONDS = 2.0


def _host_is_loopback(host: str | None) -> bool:
    """Return True if ``host`` resolves to a loopback address.

    Accepts bare hostnames, IPv4 literals, and IPv6 literals (with/without
    brackets). For non-literal hostnames we resolve through getaddrinfo and
    require *every* result to be loopback so a poisoned hosts file can't sneak
    a routable address past us.

    Fails closed on DNS timeout (treats as non-loopback) so a broken
    /etc/resolv.conf at boot cannot mask a misconfigured public binding.
    """
    if not host:
        return False
    h = host.strip("[]").lower()
    if h in _LOOPBACK_HOSTS:
        return True
    try:
        # IP literal path — covers 127.0.0.1, 127.x.y.z, ::1, etc. Does NOT
        # match 0.0.0.0 because ipaddress.ip_address("0.0.0.0").is_loopback
        # is False (which is correct).
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        pass
    # Hostname resolution path, bounded by a timeout so a broken resolver at
    # boot doesn't hang the entire process.
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
            if not ipaddress.ip_address(addr).is_loopback:
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

    # MediaMTX service URLs (internal - for backend to MediaMTX communication)
    mediamtx_hls_url: str | None = "http://localhost:8888"  # HLS streaming endpoint
    mediamtx_rtsp_url: str | None = "rtsp://localhost:8554"  # RTSP streaming endpoint
    # M1b-fixup C-3 / v2 F-1: explicit TLS-required RTSP endpoint. Defaults
    # to the mediamtx.docker.yml rtspsAddress port (8322). Subject to the
    # V-015 loopback validator below.
    #
    # The "external" companion follows the same convention as
    # mediamtx_external_hls_url and mediamtx_external_base_url: this is
    # what the backend hands to *browser/external* clients (resolvable on
    # their network). The non-external value is for internal probes /
    # backend-to-MediaMTX connections (where the Docker hostname `mediamtx`
    # is resolvable). See server/routers/streams.py for the fallback chain.
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

    # MediaMTX webhook settings
    mediamtx_webhook_token: str | None = None  # Token for webhook verification (legacy)

    # MediaMTX security secret - used for hook verification via X-MTX-Secret header
    # LOCAL DEV: Set MEDIAMTX_SECRET in .env file (must match mediamtx.yml
    # runOnInit/runOnRecordSegmentComplete webhooks).
    # MUST be explicitly set; no default is provided to satisfy the paper's
    # "Secure-by-Design" defaults (Zenodo 17261761 §4.1).
    # Generate with: openssl rand -hex 32  (or `make secrets`)
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

    # Default admin user settings (created on startup if not exists).
    #
    # V-001: There is NO default password. On first boot, if
    # `default_admin_password` is unset, a cryptographically random initial
    # password is generated and printed to stdout + the audit log exactly once.
    # The admin account is created with `password_set=False`, forcing the
    # first-time-setup flow (see routers/auth.py) before the account can be
    # used. This aligns with the Zenodo paper (DOI 10.5281/zenodo.17261761)
    # §3.1 (eliminating default/weak credentials) and ETSI EN 303 645 §5.1-1
    # (unique per-device credentials).
    default_admin_username: str = "admin"
    default_admin_password: str | None = None
    default_admin_email: str = "admin@opennvr.local"
    default_admin_first_name: str = "System"
    default_admin_last_name: str = "Administrator"

    # V-015: MediaMTX bind enforcement. By default OpenNVR refuses to start if
    # MediaMTX endpoint URLs resolve to anything other than loopback, because
    # the paper's three-tier architecture (Zenodo 17261761 §4.2) requires the
    # middleware to be the only edge exposed to operators. Set this to True
    # only if you know you are intentionally proxying MediaMTX behind a
    # separate TLS-terminating reverse proxy on the management NIC.
    allow_remote_mediamtx: bool = False

    # V-009 (M1a): Deployment-mode policy. The paper (Zenodo 17261761 §3.4 /
    # §4.1 Principle "Customer Sovereignty") treats vendor-controlled cloud
    # pipelines as a primary systemic weakness — the offline-first design is
    # the differentiator. So the default is *offline*, and every router that
    # initiates an outbound HTTP call to a non-loopback host is gated on
    # this setting via core.policy.require_outbound_allowed().
    #
    #   offline       - default. Cloud-touching routes return 403; cloud
    #                   service callsites refuse outbound. Operator can still
    #                   read stored cloud metadata for cleanup.
    #   hybrid        - opt-in: cloud features available, but each call is
    #                   audit-logged so the operator can see when the
    #                   sovereignty boundary is crossed.
    #   cloud         - everything allowed; suitable for development or for
    #                   deployments that have explicitly accepted the
    #                   sovereignty trade-off.
    #
    # This is intentionally env-only / non-mutable-at-runtime: changing the
    # deployment posture is an infrastructure decision, not a UI toggle.
    deployment_mode: Literal["offline", "hybrid", "cloud"] = "offline"

    # V-022 (M1a): AI sovereignty policy. The paper (§3.4, §4.2 Tier 3,
    # NIST AI RMF) calls out vendor AI inference pipelines as a sovereignty
    # risk because they require frame decryption outside customer control.
    # The default is *local_only*: KAI-C refuses to forward to any adapter
    # that is not on a loopback URL, and the cloud_inference router returns
    # 403. Federated mode allows participation in cross-organisation model
    # training with anonymised parameters only. Cloud_allowed disables both
    # the boundary check and the federation guard.
    ai_sovereignty: Literal[
        "local_only", "federated", "cloud_allowed"
    ] = "local_only"

    # V-019 (M1b): MediaMTX plaintext-output acknowledgement.
    #
    # The MediaMTX YAML templates ship with `rtspEncryption: "yes"` so the
    # operator-facing RTSP server refuses plaintext and only accepts TLS
    # (RFC 7826). Some development environments cannot provision TLS certs
    # (no PKI, no domain, devs streaming locally with VLC) and need the
    # permissive `mediamtx.local.yml`. Set this to True there so the
    # operator's acknowledgement is recorded in the boot audit log and
    # surfaced via /system/posture. OpenNVR cannot enforce MediaMTX's
    # config — MediaMTX is a separate process — so this setting is
    # *informational only*: it doesn't change MediaMTX's behaviour, but
    # it makes the deviation from the hardened default auditable.
    mediamtx_allow_plaintext_outputs: bool = False

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
        """V-002: Reject empty/weak/placeholder secrets at startup.

        Catches the exact placeholder strings shipped in ``env.example``
        (e.g. ``change-this-...``, ``your-secret-here-...``) as well as
        common weak values. Enforces a 32-character minimum, matching the
        output of ``openssl rand -hex 32`` / ``secrets.token_urlsafe(32)``.

        Paper alignment: Zenodo 17261761 §3.1 (credential abuse) and
        §4.1 Principle "Secure-by-Design" defaults (CISA, ETSI EN 303 645).
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
        # M-2 reviewer finding: the Fernet-shape check is necessary but not
        # sufficient — anyone who pastes a real-but-publicly-known Fernet test
        # key passes it. Run the same placeholder/weakness check we use for
        # the symmetric secrets, then verify Fernet structure on top.
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
    def _enforce_mediamtx_loopback(self) -> "Settings":
        """V-015: Refuse to start if MediaMTX endpoints are bound to a routable
        interface unless ``ALLOW_REMOTE_MEDIAMTX=true`` is explicitly set.

        The paper's three-tier architecture (Zenodo 17261761 §4.2) treats the
        middleware as the single hardened edge between cameras and operators;
        exposing MediaMTX directly defeats that property. Loopback-only is the
        Secure-by-Design default; the override exists for the case where a
        separate TLS-terminating reverse proxy sits on the management NIC.
        """
        if self.allow_remote_mediamtx:
            return self

        # (env_var_name, value) pairs we need to check. None values are skipped
        # because they mean "use default" which is always localhost.
        #
        # MEDIAMTX_EXTERNAL_* URLs are intentionally NOT in this list —
        # they are the browser-facing endpoints behind your TLS-terminating
        # reverse proxy and may legitimately resolve to a routable host.
        # See docs/SECURITY_ARCHITECTURE.md §2.2 (V-015 scope note).
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
            # M-1 reviewer finding: a scheme-less value like "192.168.1.5:8889"
            # parses with hostname=None, which would silently slip past the
            # loopback check. Treat that as an offense in its own right so the
            # operator sees a clear error rather than a downstream connect
            # failure.
            if host is None:
                offending.append(
                    f"{name}={raw!r} (unparseable host — did you forget the "
                    f"http:// scheme?)"
                )
                continue
            # C-3 reviewer finding: 0.0.0.0 is the wildcard bind, not a
            # loopback. Refuse it with a specific message so the operator
            # understands the semantic, not just the syntactic, problem.
            if host == "0.0.0.0":
                offending.append(
                    f"{name}={raw!r} (host is 0.0.0.0 — that is the "
                    f"bind-everywhere wildcard, not localhost; MediaMTX is "
                    f"almost certainly exposed on every NIC. Set the URL to "
                    f"127.0.0.1 and bind MediaMTX to 127.0.0.1 too.)"
                )
                continue
            if not _host_is_loopback(host):
                offending.append(f"{name}={raw!r} (host={host})")

        if offending:
            details = "\n  - ".join(offending)
            raise ValueError(
                "V-015: MediaMTX endpoints are bound to a non-loopback host, "
                "which violates the Secure-by-Design default. Either set them "
                "back to localhost/127.0.0.1, or, if you are intentionally "
                "fronting MediaMTX with a TLS-terminating reverse proxy on "
                "the management NIC, set ALLOW_REMOTE_MEDIAMTX=true. "
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

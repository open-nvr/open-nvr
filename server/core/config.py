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

    # Device firewall (app-layer access control).
    # Break-glass HARD-OFF. The admin toggles enforcement from the UI (stored in
    # the DB); this env override forces it off regardless, for recovery from a
    # lockout: set DEVICE_FIREWALL_KILL=true and restart, then fix the device
    # list, then clear it. Loopback is always allowed even when enforcing.
    device_firewall_kill: bool = False
    # Comma-separated CIDRs whose X-Forwarded-For header is trusted (the reverse
    # proxy in front of us). MUST be the proxy only — never 0.0.0.0/0 — or a
    # client can spoof its source IP. Default = loopback + the compose network
    # PINNED in docker-compose.yml (OPENNVR_DOCKER_SUBNET, 172.28.0.0/16) —
    # deliberately NOT all of 172.16.0.0/12: on a bare-metal LAN that uses
    # 172.16/12 for clients, the /12 default would let every such client spoof
    # XFF and bypass the device firewall. If you changed the compose subnet or
    # run bare-metal behind a proxy, set TRUSTED_PROXY_CIDRS to your proxy.
    trusted_proxy_cidrs: str = "127.0.0.1/32,::1/128,172.28.0.0/16"
    # CIDRs treated as internal services (MediaMTX, KAI-C, adapters) that must
    # never be firewalled. Same narrow default (loopback + pinned compose
    # subnet) for the same reason: a broad range makes the device firewall a
    # no-op for every client inside it.
    internal_service_cidrs: str = "127.0.0.1/32,::1/128,172.28.0.0/16"

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

    # Seed for the ICE host addresses MediaMTX advertises to browsers
    # (``webrtcAdditionalHosts``). Comma-separated. This is only a seed: the
    # authoritative list lives in the DB and is learned from the address
    # browsers actually reach this server on. See
    # services/webrtc_ice_host_service.py for why the env var alone is not
    # enough (it is baked in at container-create time and silently lost).
    mediamtx_webrtc_hosts: str = ""

    # Default recording segment length (seconds) the backend sends to MediaMTX
    # when provisioning a camera that has no explicit value of its own. Env var:
    # RECORDING_SEGMENT_SECONDS. Default 60 (1-minute clips) to match the
    # MediaMTX pathDefaults `recordSegmentDuration: 60s` — keep them in sync.
    # Short clips give minute-granular retention, cheap per-file indexing,
    # and a precise timeline; playback sessions span many clips (Phase 4).
    recording_segment_seconds: int = 60

    # IANA timezone name (e.g. "Asia/Kolkata") governing how NEW-LAYOUT
    # recording paths (cam-N/YYYY-MM-DD/HH/MM-SS-ffffff.mp4) are interpreted.
    # None = the process's local timezone (the TZ env var, which docker-compose
    # also passes to MediaMTX so both sides name/parse identically). Legacy
    # layouts (cam-N/YYYY/MM/DD/...) are always parsed as UTC — they were
    # written while the containers ran UTC.
    recording_timezone: str | None = None

    # Serve recording listings/timelines from the recordings DB index (fast,
    # SQL-backed) instead of per-request MediaMTX /list fan-outs. The MediaMTX
    # path remains as automatic fallback for cameras with no indexed rows, and
    # this flag is the instant rollback switch for the cutover.
    use_db_recordings_index: bool = True

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

    # Which MediaMTX stream Tier-0's detect-pipeline (and the agent's frame
    # grabs) tap when a camera HAS a substream:
    #   auto  (default) — substream on CPU-only decode, MAIN stream when
    #           hardware decode is configured (detect_hwaccel != cpu): a
    #           GPU box can afford full-res decode and gets full-res
    #           evidence crops; a CPU box gets the ~5x saving. Detection
    #           accuracy is equal either way (the model input is a fixed
    #           square; what matters is an object's fraction of frame).
    #   sub   — always the substream when one exists.
    #   main  — always the main stream (full-res evidence crops).
    # Cameras with no substream use the main stream regardless.
    inference_tap_stream: str = "auto"

    # Mirror of the detect-pipeline's DETECT_HWACCEL (compose passes the
    # same env to both containers) — the 'auto' signal above. "cpu" (the
    # default) means software decode.
    detect_hwaccel: str = "cpu"

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
    # detect-pipeline Tier-0 Prometheus /metrics (compute-gated inference).
    # Polled read-only for the app's Compute-gated panel; empty disables it.
    detect_pipeline_metrics_url: str = "http://localhost:9109"
    # PR-C: OCR the best frame of vehicle visits (fast_plate_ocr via KAI-C)
    # and store plate_text on the event row. Best-effort; off = rows only.
    events_plate_enrichment: bool = True
    # RFC-0002 Phase 0: NATS URL for core's domain-event consumers
    # (plate.recognized.v1 today). Compose sets NATS_URL=nats://nats:4222;
    # empty disables consumption (enrichment's synchronous fallback still
    # writes plate_text).
    nats_url: str = ""

    @field_validator("trusted_proxy_cidrs", "internal_service_cidrs")
    @classmethod
    def validate_trust_cidrs(cls, v: str, info: ValidationInfo) -> str:
        """The XFF/internal trust zone must stay narrow.

        A /0 means every client on earth can spoof X-Forwarded-For (and the
        device firewall gates nothing) — hard error. Anything broader than a
        /16 (e.g. all of 172.16.0.0/12) is legal but dangerous on bare-metal
        LANs that use the range for clients, so it warns loudly at startup.
        """
        import sys

        for part in (v or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                net = ipaddress.ip_network(part, strict=False)
            except ValueError:
                raise ValueError(
                    f"{info.field_name}: '{part}' is not a valid CIDR"
                ) from None
            if net.prefixlen == 0:
                raise ValueError(
                    f"{info.field_name} must never include '{part}': every "
                    "client could spoof X-Forwarded-For and the device "
                    "firewall would gate nothing. List only the proxy/"
                    "internal network (e.g. 172.28.0.0/16)."
                )
            if net.version == 4 and net.prefixlen < 16:
                print(
                    f"[SECURITY WARNING] {info.field_name} includes the broad "
                    f"range '{part}'. If clients on your LAN fall inside it, "
                    "they can spoof X-Forwarded-For / bypass the device "
                    "firewall. Prefer the exact proxy or Docker subnet "
                    "(default 172.28.0.0/16).",
                    file=sys.stderr,
                )
        return v

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

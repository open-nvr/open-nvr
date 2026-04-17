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
Pydantic schemas for request/response models and data validation.
Defines the structure of data exchanged between the API and clients.
"""

import html
import ipaddress
import os
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, EmailStr, Field, field_validator


# Base schemas
class RoleBase(BaseModel):
    """Base role schema with common fields."""

    name: str = Field(..., min_length=1, max_length=50)
    description: str | None = None

    @field_validator("name", "description")
    @classmethod
    def sanitize_html(cls, v: str | None) -> str | None:
        if v:
            return html.escape(v)
        return v


class PermissionBase(BaseModel):
    """Base permission schema with common fields."""

    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None

    @field_validator("name", "description")
    @classmethod
    def sanitize_html(cls, v: str | None) -> str | None:
        if v:
            return html.escape(v)
        return v


class UserBase(BaseModel):
    """Base user schema with common fields."""

    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    first_name: str | None = Field(None, max_length=50)
    last_name: str | None = Field(None, max_length=50)
    is_active: bool = True

    @field_validator("username", "first_name", "last_name")
    @classmethod
    def sanitize_html(cls, v: str | None) -> str | None:
        if v:
            return html.escape(v)
        return v


class CameraBase(BaseModel):
    """Base camera schema with common fields."""

    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    ip_address: str = Field(..., max_length=45)
    port: int = Field(554, ge=1, le=65535)
    username: str | None = Field(None, max_length=50)
    # password removed to prevent leak in Response
    rtsp_url: str | None = Field(None, max_length=500)
    location: str | None = Field(None, max_length=200)
    vlan: str | None = Field(None, max_length=50)
    status: str | None = Field("unknown", max_length=20)
    # ONVIF device metadata
    manufacturer: str | None = Field(None, max_length=100)
    model: str | None = Field(None, max_length=100)
    firmware_version: str | None = Field(None, max_length=100)
    serial_number: str | None = Field(None, max_length=100)
    hardware_id: str | None = Field(None, max_length=100)

    @field_validator(
        "name",
        "description",
        "location",
        "vlan",
        "manufacturer",
        "model",
        "firmware_version",
    )
    @classmethod
    def sanitize_html(cls, v: str | None) -> str | None:
        if v:
            return html.escape(v)
        return v

    @field_validator("rtsp_url")
    @classmethod
    def validate_rtsp_url(cls, v: str | None) -> str | None:
        if not v:
            return v

        # Basic injection check
        # Allow ';' as it is a valid URL sub-delimiter and appears in HTML entities (like &amp;)
        if any(c in v for c in ["$", "`", "|"]):
            raise ValueError("Invalid characters in URL")

        try:
            parsed = urlparse(v)
            if parsed.scheme not in ("rtsp", "rtsps", "http", "https", "onvif"):
                raise ValueError(
                    "Invalid URL scheme. Must be rtsp://, rtsps://, http://, https://, or onvif://"
                )
        except Exception:
            raise ValueError("Invalid URL format")

        return v

    @field_validator("ip_address")
    @classmethod
    def validate_ip(cls, v: str) -> str:
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            raise ValueError(
                "Invalid IP address format. Must be a valid IPv4 or IPv6 address."
            )


class RecordingBase(BaseModel):
    """Base recording schema with common fields."""

    filename: str = Field(..., max_length=255)
    file_path: str = Field(..., max_length=500)
    file_size: float | None = Field(None, ge=0)
    duration: float | None = Field(None, ge=0)
    recording_type: str = Field("motion", max_length=50)
    start_time: datetime
    end_time: datetime | None = None


# Create schemas
class RoleCreate(RoleBase):
    """Schema for creating a new role."""

    pass


class PermissionCreate(PermissionBase):
    """Schema for creating a new permission."""

    pass


class UserCreate(UserBase):
    """Schema for creating a new user."""

    password: str = Field(..., min_length=8)
    role_id: int

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one number")
        return v


class UserRegister(BaseModel):
    """Public registration schema."""

    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8)

    @field_validator("username")
    @classmethod
    def sanitize_html(cls, v: str | None) -> str | None:
        if v:
            return html.escape(v)
        return v

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one number")
        return v


class CameraCreate(CameraBase):
    """Schema for creating a new camera."""

    password: str | None = Field(None, max_length=255)


class RecordingCreate(RecordingBase):
    """Schema for creating a new recording."""

    camera_id: int


class CameraConfigBase(BaseModel):
    """Base config schema."""

    stream_protocol: str = Field("rtsp", pattern="^(rtsp|rtmp|webrtc)$")
    source_url: str | None = None
    recording_enabled: bool = False
    recording_path: str | None = None
    recording_segment_seconds: int = 60
    webrtc_publisher: bool = False
    rtmp_publisher: bool = False
    rtsp_transport: str | None = Field(None, pattern="^(udp|tcp|auto)?$")
    extra_options: str | None = None  # JSON string
    # Proxy config (exposed defaults)
    proxy_enabled: bool = False
    auto_restart: bool = True
    max_restart_attempts: int = 3


class CameraConfigCreate(CameraConfigBase):
    camera_id: int


class CameraConfigUpdate(BaseModel):
    stream_protocol: str | None = Field(None, pattern="^(rtsp|rtmp|webrtc)$")
    source_url: str | None = None
    recording_enabled: bool | None = None
    recording_path: str | None = None
    recording_segment_seconds: int | None = None
    webrtc_publisher: bool | None = None
    rtmp_publisher: bool | None = None
    rtsp_transport: str | None = Field(None, pattern="^(udp|tcp|auto)?$")
    extra_options: str | None = None
    proxy_enabled: bool | None = None
    auto_restart: bool | None = None
    max_restart_attempts: int | None = None


class CameraConfigResponse(CameraConfigBase):
    id: int
    camera_id: int
    last_provisioned_at: datetime | None = None
    stream_active: bool = False
    last_stream_start: datetime | None = None
    stream_failures: int = 0

    class Config:
        from_attributes = True


class ProvisionResult(BaseModel):
    """Response for provisioning to MediaMTX."""

    camera_id: int
    path: str
    status: str
    details: dict[str, Any] | None = None


# Update schemas
class RoleUpdate(BaseModel):
    """Schema for updating a role."""

    name: str | None = Field(None, min_length=1, max_length=50)
    description: str | None = None


class PermissionUpdate(BaseModel):
    """Schema for updating a permission."""

    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None


class UserUpdate(BaseModel):
    """Schema for updating a user."""

    username: str | None = Field(None, min_length=3, max_length=50)
    email: EmailStr | None = None
    first_name: str | None = Field(None, max_length=50)
    last_name: str | None = Field(None, max_length=50)
    is_active: bool | None = None
    role_id: int | None = None


class CameraUpdate(BaseModel):
    """Schema for updating a camera."""

    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    ip_address: str | None = Field(None, max_length=45)
    port: int | None = Field(None, ge=1, le=65535)
    username: str | None = Field(None, max_length=50)
    password: str | None = Field(None, max_length=255)
    rtsp_url: str | None = Field(None, max_length=500)
    location: str | None = Field(None, max_length=200)
    vlan: str | None = Field(None, max_length=50)
    status: str | None = Field(None, max_length=20)
    is_active: bool | None = None

    @field_validator("ip_address")
    @classmethod
    def validate_ip(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            raise ValueError(
                "Invalid IP address format. Must be a valid IPv4 or IPv6 address."
            )


class RecordingUpdate(BaseModel):
    """Schema for updating a recording."""

    filename: str | None = Field(None, max_length=255)
    file_path: str | None = Field(None, max_length=500)
    file_size: float | None = Field(None, ge=0)
    duration: float | None = Field(None, ge=0)
    recording_type: str | None = Field(None, max_length=50)
    start_time: datetime | None = None
    end_time: datetime | None = None
    is_processed: bool | None = None


# Permission schemas
class CameraPermissionAssign(BaseModel):
    """Schema to assign camera permissions to a user."""

    user_id: int
    can_view: bool = True
    can_manage: bool = False


class RolePermissionsSet(BaseModel):
    """Schema to set permissions for a role (replace assignment)."""

    permission_ids: list[int]


class CameraPermissionResponse(BaseModel):
    """Schema for camera permission response."""

    user_id: int
    camera_id: int
    can_view: bool
    can_manage: bool

    class Config:
        from_attributes = True


# MFA schemas
class MfaSetupResponse(BaseModel):
    otpauth_url: str
    secret: str


class MfaVerifyRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=8)


# Response schemas
class RoleResponse(RoleBase):
    id: int
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class PermissionResponse(PermissionBase):
    id: int
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


# Password policy schemas
class PasswordPolicyBase(BaseModel):
    min_length: int = Field(8, ge=4, le=128)
    min_classes: int = Field(3, ge=1, le=4)
    disallow_username_email: bool = True
    passphrase_enabled: bool = True
    passphrase_min_length: int = Field(16, ge=8, le=256)
    history_count: int = Field(5, ge=0, le=50)
    expiration_days: int | None = Field(None, ge=0, le=3650)
    max_failed_attempts: int = Field(5, ge=0, le=50)
    lockout_minutes: int = Field(15, ge=0, le=1440)
    reset_token_ttl_minutes: int = Field(15, ge=1, le=1440)
    require_mfa_for_privileged: bool = True


class PasswordPolicyUpdate(PasswordPolicyBase):
    pass


class PasswordPolicyResponse(PasswordPolicyBase):
    id: int
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class UserResponse(UserBase):
    # Allow returning local/reserved-domain emails (e.g., opennvr.local) without validation errors
    email: str
    id: int
    role_id: int
    is_superuser: bool
    password_set: bool
    mfa_enabled: bool
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class FirstTimeSetupRequest(BaseModel):
    username: str
    password: str = Field(..., min_length=8)


class FirstTimeSetupResponse(BaseModel):
    message: str
    mfa_required: bool
    mfa_secret: str | None = None
    mfa_qr_uri: str | None = None


class FirstTimeSetupCheckResponse(BaseModel):
    setup_required: bool
    username: str | None = None


class CameraResponse(CameraBase):
    id: int
    owner_id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime | None = None
    # Optional MediaMTX provisioning info (populated at creation time)
    mediamtx_provisioned: bool | None = None
    recording_enabled: bool | None = None

    class Config:
        from_attributes = True


class RecordingResponse(RecordingBase):
    id: int
    camera_id: int
    created_by_id: int
    is_processed: bool
    created_at: datetime

    class Config:
        from_attributes = True


# Authentication schemas
class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


class TokenData(BaseModel):
    username: str | None = None


class UserLogin(BaseModel):
    username: str
    password: str
    code: str | None = None


# List response schemas
class RoleList(BaseModel):
    roles: list[RoleResponse]
    total: int


class PermissionList(BaseModel):
    permissions: list[PermissionResponse]
    total: int


class PasswordPolicyStatus(BaseModel):
    enabled: bool


# Security schemas
class FirewallRuleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    direction: Literal["inbound", "outbound"] = "inbound"
    protocol: Literal["tcp", "udp", "any"] = "tcp"
    port_from: int | None = Field(None, ge=1, le=65535)
    port_to: int | None = Field(None, ge=1, le=65535)
    sources: str | None = None
    action: Literal["allow", "deny"] = "allow"
    enabled: bool = True
    priority: int = Field(100, ge=0, le=100000)


class FirewallRuleCreate(FirewallRuleBase):
    pass


class FirewallRuleUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    direction: Literal["inbound", "outbound"] | None = None
    protocol: Literal["tcp", "udp", "any"] | None = None
    port_from: int | None = Field(None, ge=1, le=65535)
    port_to: int | None = Field(None, ge=1, le=65535)
    sources: str | None = None
    action: Literal["allow", "deny"] | None = None
    enabled: bool | None = None
    priority: int | None = Field(None, ge=0, le=100000)


class FirewallRuleResponse(FirewallRuleBase):
    id: int
    hit_count: int
    created_at: datetime
    updated_at: datetime | None

    class Config:
        from_attributes = True


class FirewallRuleList(BaseModel):
    rules: list[FirewallRuleResponse]
    total: int


class SecuritySettingPayload(BaseModel):
    key: str
    value: dict


class SecuritySettingResponse(BaseModel):
    key: str
    value: dict


class UserList(BaseModel):
    users: list[UserResponse]
    total: int


class CameraList(BaseModel):
    cameras: list[CameraResponse]
    total: int


class RecordingList(BaseModel):
    recordings: list[RecordingResponse]
    total: int


# Audit log schemas
class AuditLogResponse(BaseModel):
    id: int
    timestamp: datetime
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    user_id: int | None = None
    username: str | None = None
    details: dict | str | None = None
    ip: str | None = None
    user_agent: str | None = None

    class Config:
        from_attributes = True


class AuditLogList(BaseModel):
    logs: list[AuditLogResponse]
    total: int


# WebRTC schemas
class TurnServer(BaseModel):
    url: str = Field(..., min_length=3, max_length=255)
    username: str | None = Field(None, max_length=255)
    credential: str | None = Field(None, max_length=255)


class WebRTCICE(BaseModel):
    transport_policy: Literal["all", "relay"] = "all"
    candidate_pool_size: int = Field(0, ge=0, le=10)
    trickle: bool = True


class ResolutionCap(BaseModel):
    width: int = Field(1920, ge=160, le=7680)
    height: int = Field(1080, ge=120, le=4320)


class WebRTCBandwidth(BaseModel):
    video_max_bitrate_kbps: int = Field(2500, ge=64, le=200000)
    audio_max_bitrate_kbps: int = Field(64, ge=8, le=1024)
    max_fps: int = Field(30, ge=1, le=240)
    resolution_cap: ResolutionCap = ResolutionCap()


AllowedVideoCodec = Literal["h264", "vp8", "vp9", "av1"]
AllowedAudioCodec = Literal["opus", "pcmu", "pcma", "aac"]


class WebRTCCodecs(BaseModel):
    video_preferred: list[AllowedVideoCodec] = ["h264", "vp9", "vp8", "av1"]
    audio_preferred: list[AllowedAudioCodec] = ["opus"]


class WebRTCSettings(BaseModel):
    stun_servers: list[str] = ["stun:stun.l.google.com:19302"]
    turn_servers: list[TurnServer] = []
    ice: WebRTCICE = WebRTCICE()
    bandwidth: WebRTCBandwidth = WebRTCBandwidth()
    codecs: WebRTCCodecs = WebRTCCodecs()


class WebRTCSettingsUpdate(BaseModel):
    stun_servers: list[str] | None = None
    turn_servers: list[TurnServer] | None = None
    ice: WebRTCICE | None = None
    bandwidth: WebRTCBandwidth | None = None
    codecs: WebRTCCodecs | None = None


class WebRTCClientConfig(BaseModel):
    """Client-friendly config for RTCPeerConnection and negotiation hints."""

    iceServers: list[dict[str, Any]]
    iceTransportPolicy: Literal["all", "relay"]
    codecPreferences: dict[str, list[str]]
    bandwidth: dict[str, Any]


# Media Source (MediaMTX) settings schemas
class MediaSourceSettings(BaseModel):
    mediamtx_base_url: str = Field("http://localhost:8889", min_length=4)
    mediamtx_token: str | None = None
    mediamtx_stream_prefix: str = Field("cam-", min_length=1, max_length=50)
    mediamtx_path_mode: Literal["id", "ip"] = "id"
    mediamtx_admin_api: str | None = None
    mediamtx_admin_token: str | None = None
    # FFmpeg-based RTSP publish settings removed
    # UI-only toggles; can inform how URLs are built/displayed in app
    hls_enabled: bool = True
    ll_hls_enabled: bool = False
    # Transcoding configuration (not yet wired to runtime pipeline)
    transcoding_enabled: bool = False
    transcode_video_codec: str | None = None  # e.g., h264, hevc
    transcode_audio_codec: str | None = None  # e.g., aac, opus
    video_bitrate_kbps: int | None = Field(None, ge=64, le=200000)
    audio_bitrate_kbps: int | None = Field(None, ge=8, le=1024)
    max_fps: int | None = Field(None, ge=1, le=240)
    scale_width: int | None = Field(None, ge=160, le=7680)
    scale_height: int | None = Field(None, ge=120, le=4320)
    # Uplink / external servers
    cloud_recording_server_ip: str | None = None
    uplink_streaming_server_ip: str | None = None
    # TLS certificates (PEM contents)
    tls_cert_pem: str | None = None
    tls_key_pem: str | None = None
    tls_ca_bundle_pem: str | None = None


class MediaSourceSettingsUpdate(BaseModel):
    mediamtx_base_url: str | None = None
    mediamtx_token: str | None = None
    mediamtx_stream_prefix: str | None = None
    mediamtx_path_mode: Literal["id", "ip"] | None = None
    mediamtx_admin_api: str | None = None
    mediamtx_admin_token: str | None = None
    # FFmpeg-based RTSP publish settings removed
    hls_enabled: bool | None = None
    ll_hls_enabled: bool | None = None
    transcoding_enabled: bool | None = None
    transcode_video_codec: str | None = None
    transcode_audio_codec: str | None = None
    video_bitrate_kbps: int | None = None
    audio_bitrate_kbps: int | None = None
    max_fps: int | None = None
    scale_width: int | None = None
    scale_height: int | None = None
    cloud_recording_server_ip: str | None = None
    uplink_streaming_server_ip: str | None = None
    tls_cert_pem: str | None = None
    tls_key_pem: str | None = None
    tls_ca_bundle_pem: str | None = None


# Recordings settings schemas
class RecordingScheduleWindow(BaseModel):
    day: int = Field(..., ge=0, le=6)  # 0=Mon ... 6=Sun
    start: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    end: str = Field(..., pattern=r"^\d{2}:\d{2}$")


class RecordingScheduleSettings(BaseModel):
    mode: Literal["continuous", "motion", "schedule"] = "continuous"
    windows: list[RecordingScheduleWindow] = []


class RecordingStorageSettings(BaseModel):
    recordings_base_path: str | None = Field(
        None, min_length=1
    )  # None = use settings.recordings_base_path
    segment_seconds: int = Field(60, ge=5, le=3600)
    filename_template: str = Field("%camera/%Y/%m/%d/%H/%M/%S.mp4", min_length=1)
    max_storage_gb: int | None = Field(None, ge=1, le=100000)
    min_free_space_gb: int | None = Field(None, ge=1, le=100000)
    # External storage devices configuration
    # If configured, the application can use active_device_id's mount_path as the effective root
    # (runtime logic to honor this can be implemented in recording services)

    class StorageDevice(BaseModel):
        id: str = Field(..., min_length=1)  # unique id (e.g., dev-<timestamp>)
        name: str = Field(..., min_length=1)
        type: Literal["hdd", "ssd", "usb", "nas"] = "hdd"
        mount_path: str = Field(..., min_length=1)
        enabled: bool = True
        quota_gb: int | None = Field(None, ge=1, le=1000000)
        # telemetry fields can be added later (total_gb, used_gb) if a scanner is implemented

        @field_validator("mount_path")
        @classmethod
        def validate_path(cls, v: str) -> str:
            # Prevent path traversal
            if ".." in v:
                raise ValueError("Invalid path: Directory traversal not allowed")

            # Additional checks for dangerous paths (e.g. strict whitelist or blacklist)
            # For now, just ensuring it's an absolute path and not traversing
            if not os.path.isabs(v):
                raise ValueError("Path must be absolute")

            forbidden = [
                "/etc",
                "/bin",
                "/usr",
                "/var/lib/postgresql",
                "/proc",
                "/sys",
                "/boot",
                "C:\\Windows",
                "C:\\Program Files",
            ]
            for f in forbidden:
                if v.startswith(f):
                    raise ValueError(f"Path conflicts with system directory: {f}")

            return v

    devices: list[StorageDevice] = []
    active_device_id: str | None = None

    @field_validator("recordings_base_path")
    @classmethod
    def validate_root_path(cls, v: str | None) -> str | None:
        if not v:
            return v

        if ".." in v:
            raise ValueError("Invalid path: Directory traversal not allowed")

        if not os.path.isabs(v):
            # Relative paths might be allowed relative to app root, but safer to enforce absolute
            # However, existing default "Recordings" is relative.
            # If strictly requiring absolute, we break default.
            # Only check for traversal if relative.
            pass
        else:
            forbidden = [
                "/etc",
                "/bin",
                "/usr",
                "/var/lib/postgresql",
                "/proc",
                "/sys",
                "/boot",
                "C:\\Windows",
                "C:\\Program Files",
            ]
            for f in forbidden:
                if v.startswith(f):
                    raise ValueError(f"Path conflicts with system directory: {f}")

        return v


class RecordingRetentionSettings(BaseModel):
    retention_days: int | None = Field(30, ge=0, le=3650)
    protect_flagged: bool = True
    min_free_space_gb: int | None = Field(None, ge=1, le=100000)


class RecordingExportRequest(BaseModel):
    camera_id: int | None = None
    start_time: datetime
    end_time: datetime
    format: Literal["original", "zip"] = "zip"


# General settings schemas
class GeneralSystemSettings(BaseModel):
    device_name: str = Field("OpenNVR", min_length=1)
    timezone: str = Field("UTC", min_length=1)
    ntp_enabled: bool = True
    ntp_server: str = Field("pool.ntp.org", min_length=1)


class GeneralNetworkSettings(BaseModel):
    dhcp_enabled: bool = True
    ipv4_address: str | None = None
    ipv4_subnet_mask: str | None = None
    ipv4_gateway: str | None = None
    preferred_dns: str | None = None
    alternate_dns: str | None = None
    mtu: int = Field(1500, ge=576, le=9000)


class GeneralAlarmSettings(BaseModel):
    motion_alarm_enabled: bool = False
    motion_sensitivity: int = Field(3, ge=1, le=5)
    tamper_alarm_enabled: bool = False
    notify_email: str | None = None


class GeneralRs232Settings(BaseModel):
    baud_rate: int = Field(9600, ge=110, le=115200)
    data_bits: Literal[5, 6, 7, 8] = 8
    stop_bits: Literal[1, 2] = 1
    parity: Literal["none", "even", "odd"] = "none"


class GeneralLiveViewSettings(BaseModel):
    default_layout: Literal[
        "1x1", "2x2", "3x3", "4x4", "1+5", "1+7", "2+8", "1+12", "4+9", "1+1+10"
    ] = "2x2"
    show_osd: bool = True
    low_latency_mode: bool = False


# Custom Window Layout schema for "More Settings" > "Window Settings"
class CustomWindowLayout(BaseModel):
    id: str = Field(..., min_length=1, max_length=50)  # e.g. "1+7", "2+6"
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    enabled: bool = True
    # Grid definition: list of tile positions
    # Each tile: {row, col, rowSpan, colSpan}
    grid_columns: int = Field(4, ge=1, le=8)  # Total columns in the grid
    grid_rows: int = Field(4, ge=1, le=8)  # Total rows in the grid
    tiles: list[dict[str, int]] = []  # [{row, col, rowSpan, colSpan}, ...]


class WindowDivisionSettings(BaseModel):
    # Built-in layouts enabled/disabled
    layouts_enabled: dict[str, bool] = {
        "1x1": True,
        "2x2": True,
        "3x3": True,
        "4x4": True,
        "1+5": True,
        "1+7": True,
        "2+8": True,
        "1+12": True,
        "4+9": True,
        "1+1+10": True,
    }
    # Custom user-defined layouts
    custom_layouts: list[CustomWindowLayout] = []
    # Default layout for live view
    default_layout: str = "2x2"


class GeneralExceptionsSettings(BaseModel):
    email_on_motion: bool = False
    email_on_stream_failure: bool = True
    webhook_url: str | None = None


class GeneralUserSettings(BaseModel):
    session_timeout_minutes: int = Field(30, ge=1, le=1440)
    password_expiry_days: int = Field(90, ge=0, le=3650)


class GeneralPosSettings(BaseModel):
    enabled: bool = False
    tcp_host: str = "127.0.0.1"
    tcp_port: int = Field(9000, ge=1, le=65535)


# Cloud settings schemas
class CloudStreamingSettings(BaseModel):
    enabled: bool = False
    server_url: str | None = None
    auth_token: str | None = None
    protocol: Literal["webrtc", "rtmp", "hls"] = "webrtc"
    video_codec: Literal["h264", "h265", "vp9", "av1"] = "h264"
    encryption: Literal["none", "dtls-srtp", "aes-128", "sample-aes"] = "dtls-srtp"


class CloudRecordingSettings(BaseModel):
    enabled: bool = False
    use_byok: bool = True
    server_url: str | None = None
    bucket: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    region: str | None = None
    storage_class: str | None = None


class CloudSettings(BaseModel):
    streaming: CloudStreamingSettings = CloudStreamingSettings()
    recording: CloudRecordingSettings = CloudRecordingSettings()


# Cloud Provider Schemas
class CloudProviderCredentialCreate(BaseModel):
    """Schema for creating a new cloud provider credential."""

    provider: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Provider name (e.g., 'huggingface')",
    )
    token: str = Field(..., min_length=1, description="API token (will be encrypted)")
    account_info: dict[str, Any] | None = Field(None, description="Optional metadata")


class CloudProviderCredentialResponse(BaseModel):
    """Schema for cloud provider credential response (without token)."""

    id: str
    user_id: int
    provider: str
    account_info: str | None  # JSON string, not dict
    created_at: datetime
    updated_at: datetime | None  # Can be None on create

    class Config:
        from_attributes = True


class CloudProviderModelCreate(BaseModel):
    """Schema for creating a cloud provider model configuration."""

    name: str = Field(..., min_length=1, max_length=100, description="Friendly name")
    provider: str = Field(..., description="Provider name (e.g., 'huggingface')")
    credential_id: str = Field(..., description="UUID of the credential to use")
    model_id: str = Field(
        ...,
        description="Model identifier (e.g., 'Salesforce/blip-image-captioning-base')",
    )
    task: str = Field(
        ..., description="Task type (e.g., 'image-classification', 'object-detection')"
    )
    config: str | None = Field(None, description="Model configuration as JSON string")
    enabled: bool | None = Field(True, description="Whether model is enabled")


class CloudProviderModelResponse(BaseModel):
    """Schema for cloud provider model response."""

    id: int
    user_id: int
    name: str
    provider: str
    credential_id: str
    model_id: str
    task: str
    config: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class CloudInferenceRequest(BaseModel):
    """Schema for synchronous cloud inference request."""

    model_id: int = Field(..., description="CloudProviderModel ID to use")
    inputs: dict[str, Any] = Field(
        ..., description="Inference inputs (image URL, text, etc.)"
    )
    parameters: dict[str, Any] | None = Field(None, description="Inference parameters")


class CloudInferenceResponse(BaseModel):
    """Schema for cloud inference result."""

    id: str
    user_id: int
    model_id: int | None  # May be null for async jobs
    provider: str
    model_identifier: str  # The actual model string (e.g., 'Salesforce/blip-...')
    task: str
    status: str
    result_json: str | None  # JSON string
    error_message: str | None
    latency_ms: int | None
    executed_at: datetime

    class Config:
        from_attributes = True
        protected_namespaces = ()  # Allow model_ prefix


class AIInferenceJobCreate(BaseModel):
    """Schema for creating async inference job."""

    model_id: int = Field(..., description="CloudProviderModel ID to use")
    inputs: dict[str, Any] = Field(..., description="Inference inputs")
    parameters: dict[str, Any] | None = Field(None, description="Inference parameters")


class AIInferenceJobResponse(BaseModel):
    """Schema for async inference job response."""

    id: str
    user_id: int
    model_id: str  # Model identifier string, not FK
    provider: str
    task: str
    status: str
    result_id: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    class Config:
        from_attributes = True
        protected_namespaces = ()  # Allow model_ prefix

    result_id: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    class Config:
        from_attributes = True


class TenantQuotaResponse(BaseModel):
    """Schema for tenant quota response."""

    provider: str
    daily_usage: int
    daily_quota: int
    daily_remaining: int
    monthly_usage: int
    monthly_quota: int
    monthly_remaining: int
    concurrent_usage: int
    concurrent_limit: int
    circuit_state: str
    circuit_failure_count: int


class TenantQuotaUpdate(BaseModel):
    """Schema for updating tenant quotas."""

    daily_quota: int | None = Field(None, ge=0)
    monthly_quota: int | None = Field(None, ge=0)

    concurrent_limit: int | None = Field(None, ge=1)


class NetworkConfig(BaseModel):
    interface_name: str
    dhcp_enabled: bool = True
    ipv4_address: str | None = None
    ipv4_subnet_mask: str | None = None
    ipv4_gateway: str | None = None
    preferred_dns: str | None = None
    alternate_dns: str | None = None
    mtu: int = 1500
    description: str | None = None
    subnet_cidr: str | None = None
    blacklisted_ips: list[str] | None = None

    @field_validator("ipv4_address", "ipv4_gateway", "preferred_dns", "alternate_dns")
    @classmethod
    def validate_ip(cls, v: str | None, info: Any) -> str | None:
        if v is None or v == "":
            return None
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            raise ValueError(
                f"Invalid IP address format for field {info.field_name}: {v}"
            )

    @field_validator("blacklisted_ips")
    @classmethod
    def validate_blacklisted_ips(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for ip in v:
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                raise ValueError(f"Invalid IP address in blacklist: {ip}")
        return v


# Integration Schemas
class IntegrationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    type: str  # Use str to accept both Enum and string, validated by business logic or modify to Enum if shared
    enabled: bool = True
    config: dict[str, Any]


class IntegrationCreate(IntegrationBase):
    pass


class IntegrationUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class IntegrationRead(IntegrationBase):
    id: int
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True

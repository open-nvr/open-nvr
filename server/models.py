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
SQLAlchemy database models.
Defines the database schema and table structures for the application.
"""

import enum
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.config import settings
from core.database import Base


class Role(Base):
    """Role model for user permissions and access control."""

    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    users = relationship("User", back_populates="role")
    role_permissions = relationship(
        "RolePermission", back_populates="role", cascade="all, delete-orphan"
    )
    permissions = relationship(
        "Permission", secondary="role_permissions", back_populates="roles"
    )


class User(Base):
    """User model for authentication and user management."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    first_name = Column(String(50), nullable=True)
    last_name = Column(String(50), nullable=True)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    password_set = Column(
        Boolean, default=False
    )  # Track if initial password setup is complete
    # True only once the user has enrolled a TOTP secret (/auth/mfa/verify).
    # New accounts start False; the client blocks app access until enrollment
    # completes (MFA wall), so MFA is still mandatory — just set up by the
    # account owner on first login, not assumed at creation.
    mfa_enabled = Column(Boolean, default=False)

    # Store encrypted MFA secret
    encrypted_mfa_secret = Column(String(500), nullable=True)

    @property
    def mfa_secret(self):
        """Return decrypted MFA secret."""
        from utils.encryption import decrypt_value

        if self.encrypted_mfa_secret:
            return decrypt_value(self.encrypted_mfa_secret)
        return None

    @mfa_secret.setter
    def mfa_secret(self, value):
        """Encrypt MFA secret on set."""
        from utils.encryption import encrypt_value

        if value:
            self.encrypted_mfa_secret = encrypt_value(value)
        else:
            self.encrypted_mfa_secret = None

    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Foreign keys
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False)

    # Relationships
    role = relationship("Role", back_populates="users")

    @property
    def role_name(self) -> str | None:
        """The role's name, for API responses (clients — e.g. the camera
        agent's auth delegation — map permission tiers by NAME; the raw
        role_id would force every client to also read the roles table,
        which is superuser-only)."""
        return self.role.name if self.role is not None else None
    cameras = relationship("Camera", back_populates="owner")
    recordings = relationship("Recording", back_populates="created_by")
    cloud_credentials = relationship("CloudProviderCredential", back_populates="user")
    cloud_models = relationship("CloudProviderModel", back_populates="user")
    quotas = relationship("TenantQuota", back_populates="user")


class IntegrationType(str, enum.Enum):
    WEBHOOK = "webhook"
    SLACK = "slack"
    TEAMS = "teams"
    EMAIL = "email"
    MQTT = "mqtt"
    S3 = "s3"
    SYSLOG = "syslog"
    PROMETHEUS = "prometheus"


class Integration(Base):
    """External integration configurations."""

    __tablename__ = "integrations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    type = Column(SAEnum(IntegrationType), nullable=False)
    enabled = Column(Boolean, default=True)
    config = Column(
        JSON, nullable=False
    )  # Stores type-specific settings and event subscriptions

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class Camera(Base):
    """Camera model for surveillance camera management."""

    __tablename__ = "cameras"
    __table_args__ = (
        # Note: Removed UniqueConstraint on (name, owner_id) to allow duplicate camera names
        Index("ix_camera_owner", "owner_id"),
        Index("ix_camera_ip", "ip_address"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # Stable identity that survives a DB wipe/rebuild (the numeric id is a
    # sequence that restarts at 1 on a fresh DB). Stamped into each camera's
    # on-disk recordings directory (.camera-identity.json) so footage can
    # never be silently re-attributed to a different camera that later reuses
    # the same numeric id. Nullable at the DB level only so the additive
    # column self-heal can add it to old create_all databases; code always
    # populates it (default here + ensure_camera_uuids at startup).
    uuid = Column(
        String(36),
        nullable=True,
        unique=True,
        index=True,
        default=lambda: str(uuid.uuid4()),
    )
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=False)
    port = Column(Integer, default=554)  # RTSP streaming port
    # ONVIF/HTTP control port, discovered when the camera is added (Hikvision 80,
    # Secureye/Tiandy 8088, others vary). Persisted so the driver layer never has
    # to re-guess from a fixed list — works for any port on any camera.
    onvif_port = Column(Integer, nullable=True)
    # Control-plane URL scheme for the ONVIF/HTTP API: "http" (default) or
    # "https" for cameras whose control API is TLS-only. Resolved with the port
    # and persisted so a TLS-only device is reached without re-probing.
    control_scheme = Column(String(8), nullable=True)
    username = Column(String(50), nullable=True)
    # password = Column(String(255), nullable=True)  # Legacy plaintext
    encrypted_password = Column(String(500), nullable=True)  # Store encrypted password

    @property
    def password(self):
        """Return decrypted password."""
        from utils.encryption import decrypt_value

        if self.encrypted_password:
            return decrypt_value(self.encrypted_password)
        return None

    @password.setter
    def password(self, value):
        """Encrypt password on set."""
        from utils.encryption import encrypt_value

        if value:
            self.encrypted_password = encrypt_value(value)
        else:
            self.encrypted_password = None

    rtsp_url = Column(String(500), nullable=True)
    # Optional low-res secondary RTSP profile. When set, the camera-agent's
    # live view (AGENT_LIVE_USE_SUBSTREAM) uses it instead of the derived
    # vendor default — covers cameras whose substream path isn't a known
    # Hikvision/Dahua convention.
    substream_url = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)
    # Tombstone for irreversible soft delete. NULL = live (active or paused);
    # set = camera is in the bin: hidden from every normal list, not editable,
    # never provisioned, recordings viewable only through the bin until
    # retention ages them out. Distinct from is_active, which is a reversible
    # pause. Nullable so the additive column self-heal can add it to old
    # create_all databases.
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    location = Column(String(200), nullable=True)
    vlan = Column(String(50), nullable=True)
    status = Column(String(20), nullable=False, default="unknown")
    # Per-camera capability assignment — the ONE source of truth for
    # "what is this camera for" (docs/design/per-camera-assignment.md).
    # A list of {"skill": "<capability>", "labels": [..]?} entries, e.g.
    # [{"skill": "license_plate_recognition"},
    #  {"skill": "object_detection", "labels": ["person", "truck"]}].
    # Written ONLY by the camera settings surface; served additively on the
    # internal camera-agent endpoint so consumers (Tier-0 reconcile, the
    # App SDK's cameras_for_skill, the catalog UI) can opt in one by one.
    # NULL/[] = nothing assigned — every consumer must treat that as
    # "no restriction declared", never as "do nothing" (back-compat).
    # Nullable so the additive column self-heal can add it to old
    # create_all databases.
    assignments = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # ONVIF device metadata
    manufacturer = Column(String(100), nullable=True)
    model = Column(String(100), nullable=True)
    firmware_version = Column(String(100), nullable=True)
    serial_number = Column(String(100), nullable=True)
    hardware_id = Column(String(100), nullable=True)

    # Foreign keys
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Relationships
    owner = relationship("User", back_populates="cameras")
    recordings = relationship("Recording", back_populates="camera")
    permissions = relationship(
        "CameraPermission", back_populates="camera", cascade="all, delete-orphan"
    )
    config = relationship(
        "CameraConfig",
        back_populates="camera",
        uselist=False,
        cascade="all, delete-orphan",
    )
    capability = relationship(
        "CameraCapability",
        back_populates="camera",
        uselist=False,
        cascade="all, delete-orphan",
    )


class CameraPermission(Base):
    """Mapping table for user-to-camera permissions."""

    __tablename__ = "camera_permissions"
    __table_args__ = (
        UniqueConstraint("user_id", "camera_id", name="uq_user_camera_perm"),
        Index("ix_perm_user", "user_id"),
        Index("ix_perm_camera", "camera_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False, index=True)
    can_view = Column(Boolean, default=True)
    can_manage = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    camera = relationship("Camera", back_populates="permissions")


class CameraConfig(Base):
    """Per-camera streaming/recording configuration stored in NVR (OpenNVR Surveillance)."""

    __tablename__ = "camera_configs"

    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False, unique=True)

    stream_protocol = Column(String(16), nullable=False, default="rtsp")
    source_url = Column(String(500), nullable=True)
    recording_enabled = Column(Boolean, default=False)
    recording_path = Column(String(500), nullable=True)
    # Fallback default for rows created without an explicit value. Reads the
    # configured RECORDING_SEGMENT_SECONDS (default 1h) at insert time, so
    # there is one source of truth — not a separate hardcoded number here.
    recording_segment_seconds = Column(
        Integer, default=lambda: settings.recording_segment_seconds
    )
    webrtc_publisher = Column(Boolean, default=False)
    rtmp_publisher = Column(Boolean, default=False)
    rtsp_transport = Column(String(16), nullable=True)
    extra_options = Column(Text, nullable=True)

    last_provisioned_at = Column(DateTime(timezone=True), nullable=True)

    # V-003: per-camera RTSPS policy (Zenodo 17261761 §3.2 / §4.2 Tier 1).
    #   rtsps_required    — stream service refuses plaintext fallback.
    #   rtsps_preferred   — try RTSPS first, fall back to RTSP. Default.
    #   plaintext_allowed — operator explicitly accepted legacy plaintext
    #                       camera (paper-consistent on the isolated
    #                       camera VLAN; middleware-side TLS still applies).
    # See services/transport_probe_service.py for the probe that informs
    # this value at camera-add time, and routers/cameras.py for the
    # re-probe endpoint.
    transport_security = Column(
        String(20),
        nullable=False,
        default="rtsps_preferred",
        server_default="rtsps_preferred",
    )
    # True iff `transport_security` was set by an explicit operator action
    # (vs probe-driven). A re-probe won't overwrite an operator-set policy
    # unless ?reset_policy=true. See V-003.
    transport_security_operator_set = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    # Latest probe outcome. Decoupled from `transport_security` so the
    # operator's policy choice survives a transient probe failure.
    #   supported       — TLS handshake completed on the probed port.
    #   not_supported   — TLS handshake refused / TCP rejected.
    #   inconclusive    — DNS / timeout / unreachable; informational only.
    #   not_probed      — never probed (migration default for back-fill).
    transport_security_probe_result = Column(
        String(20),
        nullable=False,
        default="not_probed",
        server_default="not_probed",
    )
    transport_security_probed_at = Column(
        DateTime(timezone=True), nullable=True
    )

    # RTSP proxy fields
    proxy_enabled = Column(Boolean, default=False, nullable=False)
    stream_active = Column(Boolean, default=False, nullable=False)
    last_stream_start = Column(DateTime(timezone=True), nullable=True)
    stream_failures = Column(Integer, default=0, nullable=False)
    auto_restart = Column(Boolean, default=True, nullable=False)
    max_restart_attempts = Column(Integer, default=3, nullable=False)

    camera = relationship("Camera", back_populates="config")


class CameraCapability(Base):
    """Cached snapshot of what a camera's device driver can read/drive.

    Populated by a capability probe (services/camera_drivers/capabilities.py)
    that talks to the camera over ONVIF (+ vendor APIs) and records which
    settings areas are supported, the resolved ONVIF service endpoints, and
    refreshed device metadata. The settings UI reads this to render only the
    tabs/controls a given device actually supports. Semantics mirror the
    transport-security probe on CameraConfig (result + probed_at).

    Capability data is an open, nested, per-vendor document, so it lives as
    JSON here rather than as dozens of columns on CameraConfig.
    """

    __tablename__ = "camera_capabilities"

    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(
        Integer, ForeignKey("cameras.id"), nullable=False, unique=True
    )

    driver_name = Column(String(50), nullable=True)  # onvif | hikvision | ...
    manufacturer = Column(String(100), nullable=True)
    model = Column(String(100), nullable=True)
    firmware_version = Column(String(100), nullable=True)

    onvif_endpoints = Column(JSON, nullable=True)  # {device, media, imaging, ...}
    supported_areas = Column(JSON, nullable=True)  # {imaging: true, ptz: false, ...}
    capabilities = Column(JSON, nullable=True)  # nested detail (ranges/tokens)

    # 'ok' | 'partial' | 'unreachable' | 'error' | 'not_probed'
    probe_result = Column(
        String(20), nullable=False, default="not_probed", server_default="not_probed"
    )
    probed_at = Column(DateTime(timezone=True), nullable=True)
    probe_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    camera = relationship("Camera", back_populates="capability")


class SkillAssignment(Base):
    """RFC-0002 Phase 2: the declarative camera-assignment table.

    Desired state with union semantics (decision 8): one row per
    (skill, camera, **consumer**) claim. A skill runs on the UNION of
    its consumers' cameras; releasing one consumer's row shrinks the
    union, and releasing the last makes the skill dormant (gap 7).

    ``Camera.assignments`` (the JSON column Tier-0 reconcile, the SDK's
    ``cameras_for_skill`` and the internal camera-agent endpoint already
    read) becomes the PROJECTION of this table — recomputed by
    ``services/skill_assignments.py`` on every write, so no existing
    consumer changes to keep working.

    ``skill`` is an open-vocabulary string on purpose (the tasks.yml
    canonical names and app-derived skills both land here; annotate,
    never gate — the per-camera-assignment design rule). ``consumer``
    identifies who wants it: ``operator`` (the camera-settings editor),
    ``app:<id>``, ``agent``. ``params`` is the per-claim narrowing
    (e.g. ``{"labels": [...]}``); claims merge additively in the
    projection.
    """

    __tablename__ = "skill_assignments"
    __table_args__ = (
        Index("uq_skill_assignment", "skill", "camera_id", "consumer",
              unique=True),
        Index("ix_skill_assignments_camera", "camera_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    skill = Column(String(100), nullable=False, index=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False)
    consumer = Column(String(100), nullable=False)
    params = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class TimelineEvent(Base):
    """Canonical event & evidence store (RFC-0001 Challenge 1) — one row per
    object VISIT (a Tier-0 track lifecycle), alarm, or app alert.

    The question-shaped store: "who came between 3 and 4pm?" is a range scan
    here, each row carrying its best-frame evidence JPEG (selected at capture
    time — the sharpest look Tier-0 had at the object) and, later, the
    recording anchor and LPR plate text. All producers share this table; apps
    query it instead of keeping private stores."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_cam_start", "camera_id", "started_at"),
        # One visit = one (camera, track, start): ingest retries are
        # idempotent. NULLs (alarm/alert rows) never collide by SQL semantics.
        Index("uq_events_visit", "camera_id", "track_id", "started_at",
              unique=True),
    )

    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False)
    source = Column(String(30), nullable=False)        # tier0 | camera | app | adapter
    event_type = Column(String(30), nullable=False)    # track | alarm | alert
    label = Column(String(60), nullable=True, index=True)
    score = Column(Float, nullable=True)
    track_id = Column(String(40), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    recording_ref = Column(String(500), nullable=True)
    evidence_path = Column(String(500), nullable=True)
    plate_text = Column(String(32), nullable=True)
    payload = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CameraEvent(Base):
    """A camera-native alarm (motion / tamper / video-loss / IO) received from
    the device's event stream. History store parallel to AIDetectionResult; the
    live copy is fanned out on the in-process event bus for the dashboard."""

    __tablename__ = "camera_events"
    __table_args__ = (
        Index("ix_camera_event_cam_time", "camera_id", "occurred_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(
        Integer, ForeignKey("cameras.id"), nullable=False, index=True
    )
    event_type = Column(String(50), nullable=False)  # VMD | tamperdetection | ...
    event_state = Column(String(20), nullable=True)  # active | inactive
    description = Column(String(200), nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SystemEvent(Base):
    """A host-level alert (disk pressure, CPU/RAM, purge outcome) — history
    store parallel to CameraEvent for events with no camera scope. The live
    copy is fanned out on the in-process event bus as ``system_alert``."""

    __tablename__ = "system_events"
    __table_args__ = (
        Index("ix_system_event_type_time", "event_type", "occurred_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # cpu_high | memory_high | disk_low | disk_pressure_purge |
    # disk_purge_exhausted | disk_stat_error
    event_type = Column(String(50), nullable=False)
    event_state = Column(String(20), nullable=True)  # active | inactive | None
    severity = Column(String(10), nullable=False, default="warning")
    description = Column(String(300), nullable=True)
    data = Column(Text, nullable=True)  # JSON metric snapshot / purge stats
    occurred_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Recording(Base):
    """One recorded segment file — the indexed source of truth for listings.

    Fed by the MediaMTX segment-complete webhook and converged with the
    filesystem by the recording reconciler. ``start_time``/``end_time`` are
    tz-aware UTC derived from the segment filename (never wall-clock at
    webhook receipt). ``(camera_id, file_path)`` is unique so webhook retries
    and reconciler overlap upsert instead of duplicating.
    """

    __tablename__ = "recordings"
    __table_args__ = (
        Index("ix_recordings_camera_start", "camera_id", "start_time"),
        Index("uq_recordings_camera_file", "camera_id", "file_path", unique=True),
        Index("ix_recordings_start_time", "start_time"),
    )

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_size = Column(Float, nullable=True)
    duration = Column(Float, nullable=True)
    recording_type = Column(String(50), default="motion")
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=True)
    is_processed = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Video codec tag recorded at segment-complete (e.g. 'h264', 'hevc') so
    # playback can pick the H.265 remux path without probing the file.
    codec = Column(String(20), nullable=True)
    # Flagged recordings survive retention (protect_flagged).
    is_flagged = Column(Boolean, nullable=False, default=False, server_default="false")
    # How the row entered the index: 'webhook' | 'reconciler'.
    source = Column(String(10), nullable=True)

    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    camera = relationship("Camera", back_populates="recordings")
    created_by = relationship("User", back_populates="recordings")


class Permission(Base):
    """Permission model representing a granular capability that can be assigned to roles."""

    __tablename__ = "permissions"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    role_permissions = relationship(
        "RolePermission", back_populates="permission", cascade="all, delete-orphan"
    )
    roles = relationship(
        "Role", secondary="role_permissions", back_populates="permissions"
    )


class RolePermission(Base):
    """Mapping table between roles and permissions."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
        Index("ix_role_permissions_role", "role_id"),
        Index("ix_role_permissions_perm", "permission_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False)
    permission_id = Column(Integer, ForeignKey("permissions.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    role = relationship("Role", back_populates="role_permissions")
    permission = relationship("Permission", back_populates="role_permissions")


class PasswordPolicy(Base):
    """Configurable password policy stored in DB (single-row)."""

    __tablename__ = "password_policy"

    id = Column(Integer, primary_key=True, index=True)
    # Requirements
    min_length = Column(Integer, nullable=False, default=8)
    min_classes = Column(
        Integer, nullable=False, default=3
    )  # number of character classes required (1-4)
    disallow_username_email = Column(Boolean, nullable=False, default=True)
    passphrase_enabled = Column(Boolean, nullable=False, default=True)
    passphrase_min_length = Column(Integer, nullable=False, default=16)
    # Lifecycle
    history_count = Column(Integer, nullable=False, default=5)
    expiration_days = Column(Integer, nullable=True)  # null or 0 to disable
    # Lockout / reset
    max_failed_attempts = Column(Integer, nullable=False, default=5)
    lockout_minutes = Column(Integer, nullable=False, default=3)
    reset_token_ttl_minutes = Column(Integer, nullable=False, default=15)
    # Privileged
    require_mfa_for_privileged = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class FirewallDirection(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class FirewallAction(str, enum.Enum):
    allow = "allow"
    deny = "deny"


class DeviceStatus(str, enum.Enum):
    approved = "approved"
    pending = "pending"
    blocked = "blocked"


class TrustedDevice(Base):
    """A browser known to the OpenNVR app-layer firewall.

    ``approved`` may use OpenNVR; ``pending`` has logged in but awaits an admin
    decision (blocked meanwhile); ``blocked`` is explicitly denied. The first
    browser to authenticate on a fresh install is auto-approved so the installer
    is never locked out.

    Identity is ``device_token_hash`` — the SHA-256 of a long random token the
    server issues at login and stores in an HttpOnly cookie (it outlives the
    session, so logging out never costs an approval). Identity is deliberately
    NOT the client IP: NAT collapses every device behind one address (Docker
    Desktop's port forwarding makes all LAN clients look like the bridge
    gateway), so an IP can neither distinguish nor reliably re-identify a
    device. ``ip_address`` is retained as metadata for the admin UI only.
    Granularity is therefore per browser profile: a second profile, another
    browser, or cleared cookies enroll as a new device needing approval.
    """

    __tablename__ = "trusted_devices"

    id = Column(Integer, primary_key=True, index=True)
    device_token_hash = Column(String(64), nullable=True, unique=True, index=True)
    # Metadata ("last seen from"), NOT identity — see the class docstring.
    ip_address = Column(String(45), nullable=True, index=True)
    label = Column(String(100), nullable=True)
    status = Column(
        SAEnum(DeviceStatus), nullable=False, default=DeviceStatus.pending
    )
    user_agent = Column(String(400), nullable=True)
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), server_default=func.now())
    attempt_count = Column(Integer, nullable=False, default=1)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    auto_enrolled = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FirewallRule(Base):
    """Firewall rule with simple fields and prioritization."""

    __tablename__ = "firewall_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    direction = Column(
        SAEnum(FirewallDirection), nullable=False, default=FirewallDirection.inbound
    )
    protocol = Column(String(8), nullable=False, default="tcp")  # tcp/udp/any
    port_from = Column(Integer, nullable=True)
    port_to = Column(Integer, nullable=True)
    sources = Column(Text, nullable=True)  # comma-separated CIDRs
    action = Column(
        SAEnum(FirewallAction), nullable=False, default=FirewallAction.allow
    )
    enabled = Column(Boolean, nullable=False, default=True)
    priority = Column(Integer, nullable=False, default=100)
    hit_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class SecuritySetting(Base):
    """Generic key->JSON security settings storage (ports/platform_access/nat)."""

    __tablename__ = "security_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(50), unique=True, nullable=False, index=True)
    json_value = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class AuditLog(Base):
    """Audit log of significant user and system actions.

    Typical actions: login, logout, user.create, user.update, camera.create, camera.update,
    camera.delete, camera.permission.assign, camera.permission.revoke, settings.update,
    camera_config.update, camera.provision, camera.unprovision, stream.start, stream.stop, stream.restart, mfa.enable, mfa.disable
    """

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    action = Column(String(64), nullable=False, index=True)
    entity_type = Column(String(64), nullable=True, index=True)
    entity_id = Column(String(128), nullable=True, index=True)
    details = Column(Text, nullable=True)  # JSON string or plain text
    ip = Column(String(64), nullable=True)
    user_agent = Column(Text, nullable=True)

    # Actor
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)


class AIModel(Base):
    """AI Model configuration for inference tasks."""

    __tablename__ = "ai_models"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    model_name = Column(
        String(50), nullable=False
    )  # yolov8, yolov11, blip, insightface
    task = Column(
        String(50), nullable=False, index=True
    )  # person_detection, person_counting, etc.
    config = Column(Text, nullable=True)  # JSON string for additional options
    enabled = Column(Boolean, default=True)

    # Source configuration - supports both live cameras and recordings
    source_type = Column(
        String(20), nullable=False, default="live", index=True
    )  # "live" or "recording"
    assigned_camera_id = Column(
        Integer, nullable=True, index=True
    )  # For live: camera ID
    recording_path = Column(
        Text, nullable=True
    )  # For recording: relative path to video file

    inference_interval = Column(
        Integer, nullable=True, default=2
    )  # Seconds between inference runs (live only)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    detection_results = relationship(
        "AIDetectionResult", back_populates="model", cascade="all, delete-orphan"
    )


class AIDetectionResult(Base):
    """AI detection/inference results storage."""

    __tablename__ = "ai_detection_results"

    id = Column(Integer, primary_key=True, index=True)
    model_id = Column(Integer, ForeignKey("ai_models.id"), nullable=False, index=True)
    camera_id = Column(Integer, nullable=True, index=True)
    task = Column(String(50), nullable=False, index=True)

    # Detection data
    label = Column(String(100), nullable=True)
    confidence = Column(Float, nullable=True)
    bbox_x = Column(Integer, nullable=True)
    bbox_y = Column(Integer, nullable=True)
    bbox_width = Column(Integer, nullable=True)
    bbox_height = Column(Integer, nullable=True)
    count = Column(Integer, nullable=True)  # For counting tasks
    caption = Column(Text, nullable=True)  # For captioning tasks

    # Metadata
    latency_ms = Column(Integer, nullable=True)
    annotated_image_uri = Column(Text, nullable=True)
    executed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    # Relationships
    model = relationship("AIModel", back_populates="detection_results")


# Cloud Provider Support Models


class CloudProviderCredential(Base):
    """Encrypted cloud provider credentials with tenant isolation."""

    __tablename__ = "cloud_provider_credentials"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String(50), nullable=False, index=True)
    encrypted_token = Column(Text, nullable=False)
    token_hash = Column(String(64), nullable=False, index=True)
    encryption_key_id = Column(String(50), nullable=False)
    name = Column(String(100), nullable=True)
    account_info = Column(
        Text, nullable=True
    )  # JSON string, not JSON type - matches actual DB
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="cloud_credentials")
    models = relationship(
        "CloudProviderModel", back_populates="credential", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_cloud_cred_user_provider", "user_id", "provider"),
        UniqueConstraint("user_id", "token_hash", name="uq_user_token"),
    )


class CloudProviderModel(Base):
    """User-configured cloud AI models with allowlist support."""

    __tablename__ = "cloud_provider_models"

    model_config = {"protected_namespaces": ()}  # Allow model_ prefix

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    credential_id = Column(
        String(36), ForeignKey("cloud_provider_credentials.id"), nullable=False
    )
    provider = Column(String(50), nullable=False)
    model_id = Column(String(200), nullable=False)
    task = Column(String(50), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    config = Column(
        Text, nullable=True
    )  # Actual DB has 'config', not 'default_parameters'
    enabled = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user = relationship("User", back_populates="cloud_models")
    credential = relationship("CloudProviderCredential", back_populates="models")
    inference_results = relationship(
        "CloudInferenceResult", back_populates="model", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_cloud_model_user_task", "user_id", "task"),
        UniqueConstraint("user_id", "model_id", name="uq_user_model_id"),
    )


class CloudInferenceResult(Base):
    """Inference results from cloud providers."""

    __tablename__ = "cloud_inference_results"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    model_id = Column(Integer, ForeignKey("cloud_provider_models.id"), nullable=False)
    provider = Column(String(50), nullable=False)
    model_name = Column(String(200), nullable=False)
    task = Column(String(50), nullable=False, index=True)
    status = Column(String(20), nullable=False)
    result_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    provider_request_id = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User")
    model = relationship("CloudProviderModel", back_populates="inference_results")

    __table_args__ = (Index("ix_cloud_result_user_date", "user_id", "created_at"),)


class AIInferenceJob(Base):
    """Async inference job tracking."""

    __tablename__ = "ai_inference_jobs"

    model_config = {"protected_namespaces": ()}  # Allow model_ prefix

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    model_id = Column(Integer, ForeignKey("cloud_provider_models.id"), nullable=False)
    provider = Column(String(50), nullable=False)
    model_name = Column(String(200), nullable=False)
    task = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, index=True)
    input_json = Column(Text, nullable=False)
    parameters_json = Column(Text, nullable=True)
    result_id = Column(
        String(36), ForeignKey("cloud_inference_results.id"), nullable=True
    )
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User")
    model = relationship("CloudProviderModel")
    result = relationship("CloudInferenceResult")

    __table_args__ = (Index("ix_job_status_created", "status", "created_at"),)


class TenantQuota(Base):
    """Rate limiting and quota enforcement per user per provider."""

    __tablename__ = "tenant_quotas"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider = Column(String(50), nullable=False)
    daily_quota = Column(Integer, nullable=False)
    monthly_quota = Column(Integer, nullable=False)
    concurrent_limit = Column(Integer, nullable=False)
    daily_usage = Column(Integer, nullable=False, default=0)
    monthly_usage = Column(Integer, nullable=False, default=0)
    concurrent_usage = Column(Integer, nullable=False, default=0)
    daily_reset_at = Column(DateTime(timezone=True), nullable=True)
    monthly_reset_at = Column(DateTime(timezone=True), nullable=True)
    circuit_state = Column(String(20), nullable=False, default="closed")
    circuit_failure_count = Column(Integer, nullable=False, default=0)
    circuit_last_failure = Column(DateTime(timezone=True), nullable=True)
    circuit_half_open_successes = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user = relationship("User", back_populates="quotas")

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_provider_quota"),
    )


class InstalledApp(Base):
    """One registered vertical-detector app (App SDK spec §05).

    Apps self-register on boot via ``POST /api/v1/apps/register`` —
    the same shape adapters use against KAI-C. ``manifest_json`` is the
    ``AppManifest.to_dict()`` snapshot; ``config_json`` is operator
    config validated against ``manifest_json["params"]``.
    """

    __tablename__ = "installed_apps"

    # The manifest id, e.g. "loitering-detection" — apps upsert by it.
    id = Column(String(100), primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    category = Column(String(100), nullable=True)
    version = Column(String(50), nullable=False)
    url = Column(String(500), nullable=False)  # e.g. http://loitering:9200
    manifest_json = Column(JSON, nullable=False)
    config_json = Column(JSON, nullable=False, default=dict)
    enabled = Column(Boolean, default=False, nullable=False)
    # registered | ok | unreachable
    status = Column(String(20), nullable=False, default="registered")
    last_seen = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class AppInstallIntent(Base):
    """Desired-state record for the opt-in one-click app installer.

    This is the seam that keeps the web app out of Docker. The
    ``POST /apps/index/{id}/install`` and ``/uninstall`` endpoints do
    exactly three things — validate the id against the curated index,
    upsert one row here, and audit — and nothing else. They never spawn
    a subprocess, hold the docker socket, or run compose.

    A separate, minimally-privileged reconciler
    (``scripts/app-installer``) is the ONLY component that touches
    Docker: it polls this table, and for each row drives ``docker
    compose`` up (``desired="installed"``) or down (``desired="absent"``),
    then writes back ``status``/``message``. The primary key is the
    curated app id (one intent per app, upsert on re-request), mirroring
    ``installed_apps``.

    Security notes:
    * ``image`` / ``image_digest`` are copied from the curated index at
      write time, never from user input — an id not in the index is
      rejected before a row is ever created.
    * ``image_digest`` is the sha256 the reconciler pins to. When NULL
      the image is unpinned (dev only); the reconciler logs a loud
      warning and the operator is told not to run it in production.
    """

    __tablename__ = "app_install_intents"

    # The curated app id, e.g. "loitering-detection" (must exist in
    # apps_index.yml). Upsert on re-request, same as installed_apps.
    id = Column(String(100), primary_key=True, index=True)
    # Canonical image ref + optional sha256 digest, both copied from the
    # curated index entry at write time (never from the request body).
    image = Column(String(500), nullable=False)
    image_digest = Column(String(100), nullable=True)  # sha256:... or NULL
    # What the operator wants: installed | absent.
    desired = Column(String(20), nullable=False, default="installed")
    # Where the reconciler is: pending | applied | failed.
    status = Column(String(20), nullable=False, default="pending")
    # Human-readable last-reconcile note (compose stderr on failure, etc).
    message = Column(Text, nullable=True)
    # Actor bookkeeping — who requested the current desired state.
    requested_by = Column(String(100), nullable=True)
    requested_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

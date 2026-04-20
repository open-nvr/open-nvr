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
    mfa_enabled = Column(Boolean, default=True)  # MFA enabled by default for security

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
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=False)
    port = Column(Integer, default=554)
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
    is_active = Column(Boolean, default=True)
    location = Column(String(200), nullable=True)
    vlan = Column(String(50), nullable=True)
    status = Column(String(20), nullable=False, default="unknown")
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
    recording_segment_seconds = Column(Integer, default=60)
    webrtc_publisher = Column(Boolean, default=False)
    rtmp_publisher = Column(Boolean, default=False)
    rtsp_transport = Column(String(16), nullable=True)
    extra_options = Column(Text, nullable=True)

    last_provisioned_at = Column(DateTime(timezone=True), nullable=True)

    # RTSP proxy fields
    proxy_enabled = Column(Boolean, default=False, nullable=False)
    stream_active = Column(Boolean, default=False, nullable=False)
    last_stream_start = Column(DateTime(timezone=True), nullable=True)
    stream_failures = Column(Integer, default=0, nullable=False)
    auto_restart = Column(Boolean, default=True, nullable=False)
    max_restart_attempts = Column(Integer, default=3, nullable=False)

    camera = relationship("Camera", back_populates="config")


class Recording(Base):
    """Recording model for storing video recording metadata."""

    __tablename__ = "recordings"

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

    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)

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

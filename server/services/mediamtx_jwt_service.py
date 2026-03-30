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
MediaMTX JWT Service

Provides JWT token generation for MediaMTX authentication.
Uses RSA key pair for signing - MediaMTX fetches public key via JWKS endpoint.

Security:
- RSA-256 signing (asymmetric - private key never exposed)
- Short-lived tokens (configurable expiry)
- Per-user/per-camera permission scoping
- JWKS endpoint for public key distribution
"""

import base64
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from core.logging_config import main_logger


class MediaMtxJwtService:
    """Service for generating MediaMTX-compatible JWT tokens with JWKS support."""

    # Key storage paths
    KEYS_DIR = Path("keys")
    PRIVATE_KEY_PATH = KEYS_DIR / "mediamtx_private.pem"
    PUBLIC_KEY_PATH = KEYS_DIR / "mediamtx_public.pem"

    # JWT settings
    ALGORITHM = "RS256"
    DEFAULT_EXPIRY_MINUTES = 60  # 1 hour default
    ISSUER = "opennvr"

    _private_key: rsa.RSAPrivateKey | None = None
    _public_key: rsa.RSAPublicKey | None = None
    _kid: str | None = None  # Key ID for JWKS

    @classmethod
    def _ensure_keys_exist(cls) -> None:
        """Generate RSA key pair if not exists."""
        cls.KEYS_DIR.mkdir(exist_ok=True)

        if not cls.PRIVATE_KEY_PATH.exists() or not cls.PUBLIC_KEY_PATH.exists():
            main_logger.info("[MediaMTX JWT] Generating new RSA key pair...")

            # Generate 2048-bit RSA key pair
            private_key = rsa.generate_private_key(
                public_exponent=65537, key_size=2048, backend=default_backend()
            )

            # Save private key (PEM format)
            private_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            cls.PRIVATE_KEY_PATH.write_bytes(private_pem)

            # Save public key (PEM format)
            public_key = private_key.public_key()
            public_pem = public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            cls.PUBLIC_KEY_PATH.write_bytes(public_pem)

            # Set restrictive permissions on private key
            try:
                os.chmod(cls.PRIVATE_KEY_PATH, 0o600)
            except Exception:
                pass  # Windows doesn't support chmod

            main_logger.info("[MediaMTX JWT] RSA key pair generated successfully")

    @classmethod
    def _load_keys(cls) -> None:
        """Load RSA keys from files."""
        if cls._private_key is not None:
            return

        cls._ensure_keys_exist()

        # Load private key
        private_pem = cls.PRIVATE_KEY_PATH.read_bytes()
        cls._private_key = serialization.load_pem_private_key(
            private_pem, password=None, backend=default_backend()
        )

        # Load public key
        public_pem = cls.PUBLIC_KEY_PATH.read_bytes()
        cls._public_key = serialization.load_pem_public_key(
            public_pem, backend=default_backend()
        )

        # Generate Key ID from public key hash
        cls._kid = hashlib.sha256(public_pem).hexdigest()[:16]

        main_logger.info(f"[MediaMTX JWT] Keys loaded, kid={cls._kid}")

    @classmethod
    def get_jwks(cls) -> dict[str, Any]:
        """
        Get JWKS (JSON Web Key Set) containing public key.

        This is served at /.well-known/jwks.json for MediaMTX to fetch.
        """
        cls._load_keys()

        # Get public key numbers
        public_numbers = cls._public_key.public_numbers()

        # Convert to base64url encoding (no padding)
        def int_to_base64url(num: int, length: int) -> str:
            """Convert integer to base64url encoded string."""
            num_bytes = num.to_bytes(length, byteorder="big")
            return base64.urlsafe_b64encode(num_bytes).rstrip(b"=").decode("ascii")

        # RSA modulus (n) - 256 bytes for 2048-bit key
        n = int_to_base64url(public_numbers.n, 256)
        # RSA exponent (e) - typically 3 bytes
        e = int_to_base64url(public_numbers.e, 3)

        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": cls.ALGORITHM,
                    "kid": cls._kid,
                    "n": n,
                    "e": e,
                }
            ]
        }

    @classmethod
    def create_stream_token(
        cls,
        user_id: int,
        username: str,
        camera_id: int | None = None,
        camera_path: str | None = None,
        actions: list[str] | None = None,
        expiry_minutes: int | None = None,
    ) -> str:
        """
        Create a JWT token for MediaMTX stream access.

        Args:
            user_id: User ID for audit trail
            username: Username for identification
            camera_id: Optional specific camera ID
            camera_path: Optional specific path (e.g., "cam-57")
            actions: List of allowed actions ["read", "publish", "playback"]
            expiry_minutes: Token expiry in minutes (default: 60)

        Returns:
            Signed JWT token string
        """
        cls._load_keys()

        if actions is None:
            actions = ["read"]  # Default: read-only

        if expiry_minutes is None:
            expiry_minutes = cls.DEFAULT_EXPIRY_MINUTES

        now = datetime.now(UTC)
        expiry = now + timedelta(minutes=expiry_minutes)

        # Build MediaMTX permissions claim
        permissions = []
        for action in actions:
            perm = {"action": action}
            if camera_path:
                perm["path"] = camera_path
            elif camera_id:
                # Use regex to match camera path pattern
                perm["path"] = f"~^cam-{camera_id}$"
            # Empty path = all paths (requires explicit grant)
            permissions.append(perm)

        # JWT payload - use timezone-aware datetime for correct timestamps
        payload = {
            "iss": cls.ISSUER,
            "sub": username,
            "aud": "mediamtx",
            "iat": int(now.timestamp()),
            "exp": int(expiry.timestamp()),
            "jti": f"{user_id}-{camera_id or 'all'}-{int(now.timestamp())}",
            # MediaMTX permission claim
            "mediamtx_permissions": permissions,
            # Custom claims for audit
            "user_id": user_id,
            "camera_id": camera_id,
        }

        # Get private key in PEM format for jose
        private_pem = cls._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        # Sign token
        token = jwt.encode(
            payload, private_pem, algorithm=cls.ALGORITHM, headers={"kid": cls._kid}
        )

        main_logger.info(
            f"[MediaMTX JWT] Token created: user={username}, camera={camera_id}, "
            f"actions={actions}, expiry={expiry_minutes}m"
        )

        return token

    @classmethod
    def create_publish_token(
        cls,
        camera_id: int,
        camera_path: str,
        expiry_minutes: int = 1440,  # 24 hours for cameras
    ) -> str:
        """
        Create a JWT token for camera publishing.

        Used internally for camera RTSP source connections.

        Args:
            camera_id: Camera ID
            camera_path: MediaMTX path name (e.g., "cam-57")
            expiry_minutes: Token expiry (default: 24 hours)

        Returns:
            Signed JWT token string
        """
        cls._load_keys()

        now = datetime.now(UTC)
        expiry = now + timedelta(minutes=expiry_minutes)

        payload = {
            "iss": cls.ISSUER,
            "sub": f"camera-{camera_id}",
            "aud": "mediamtx",
            "iat": int(now.timestamp()),
            "exp": int(expiry.timestamp()),
            "jti": f"cam-{camera_id}-{int(now.timestamp())}",
            "mediamtx_permissions": [{"action": "publish", "path": camera_path}],
            "camera_id": camera_id,
            "type": "camera_publish",
        }

        private_pem = cls._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        token = jwt.encode(
            payload, private_pem, algorithm=cls.ALGORITHM, headers={"kid": cls._kid}
        )

        main_logger.info(
            f"[MediaMTX JWT] Publish token created: camera={camera_id}, path={camera_path}"
        )

        return token

    @classmethod
    def create_playback_token(
        cls,
        user_id: int,
        username: str,
        camera_id: int | None = None,
        expiry_minutes: int = 30,
    ) -> str:
        """
        Create a JWT token for recording playback access.

        Args:
            user_id: User ID
            username: Username
            camera_id: Optional specific camera ID
            expiry_minutes: Token expiry (default: 30 minutes)

        Returns:
            Signed JWT token string
        """
        return cls.create_stream_token(
            user_id=user_id,
            username=username,
            camera_id=camera_id,
            actions=["playback", "read"],
            expiry_minutes=expiry_minutes,
        )

    @classmethod
    def create_admin_token(cls, expiry_minutes: int = 5) -> str:
        """
        Create a short-lived JWT token for MediaMTX admin API access.

        Args:
            expiry_minutes: Token expiry (default: 5 minutes)

        Returns:
            Signed JWT token string
        """
        cls._load_keys()

        now = datetime.now(UTC)
        expiry = now + timedelta(minutes=expiry_minutes)

        payload = {
            "iss": cls.ISSUER,
            "sub": "opennvr-backend",
            "aud": "mediamtx",
            "iat": int(now.timestamp()),
            "exp": int(expiry.timestamp()),
            "jti": f"admin-{int(now.timestamp())}",
            "mediamtx_permissions": [
                {"action": "api"},
                {"action": "publish"},
                {"action": "read"},
                {"action": "playback"},
            ],
            "type": "admin",
        }

        private_pem = cls._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        token = jwt.encode(
            payload, private_pem, algorithm=cls.ALGORITHM, headers={"kid": cls._kid}
        )

        return token

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
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:  # POSIX only; on Windows the in-process lock is all we have
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from core.logging_config import main_logger


def _harden_key_file_permissions(path: Path) -> None:
    """Restrict a private-key file to the current owner only.

    POSIX: ``chmod 0600``. Windows: ``os.chmod`` only toggles the read-only
    bit and does NOT restrict other users, so use ``icacls`` to drop inherited
    ACEs and grant the current user Full control alone. Best-effort — logged,
    never fatal (the key still works; this is defence-in-depth against another
    local account reading it).
    """
    try:
        if os.name == "nt":
            import getpass
            import subprocess

            user = os.environ.get("USERNAME") or getpass.getuser()
            # /inheritance:r removes inherited ACEs; /grant:r replaces the
            # user's ACE with Full control. List args -> no shell.
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                check=True,
                capture_output=True,
                timeout=10,
            )
        else:
            os.chmod(path, 0o600)
    except Exception as exc:
        main_logger.warning(
            "[MediaMTX JWT] Could not harden private-key permissions on %s: %s",
            path,
            exc,
        )


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

    # Key creation is serialised: the first start used to race itself.
    # MediaMTX fetches the JWKS while the first stream token is being
    # minted, both land in the threadpool, both saw no key files, both
    # generated a pair and the two plain ``write_bytes`` calls interleaved —
    # leaving a public key that did not belong to the private key and every
    # stream token rejected until someone rewrote the file by hand. The
    # thread lock covers one process; the flock covers a second process on
    # the same volume (uvicorn --reload, a one-off management command).
    _lock = threading.RLock()
    LOCK_FILE_NAME = ".keys.lock"

    @classmethod
    @contextmanager
    def _keys_dir_lock(cls) -> Iterator[None]:
        """Hold the thread lock and (POSIX) an exclusive lock on the keys dir."""
        with cls._lock:
            if fcntl is None:
                yield
                return
            with open(cls.KEYS_DIR / cls.LOCK_FILE_NAME, "a+b") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _write_atomic(path: Path, data: bytes, *, private: bool) -> None:
        """Write ``data`` to a sibling temp file and rename it over ``path``.

        A reader never sees a half-written key, and a crash between the two
        key files leaves either the old pair or a complete new file — never
        a torn one. The private key is created 0600 from the first byte.
        """
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if private:
                _harden_key_file_permissions(tmp)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _public_pem_of(private_key: rsa.RSAPrivateKey) -> bytes:
        return private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    @classmethod
    def _ensure_keys_exist(cls) -> None:
        """Generate the RSA key pair if it does not exist (locked)."""
        cls.KEYS_DIR.mkdir(parents=True, exist_ok=True)
        with cls._keys_dir_lock():
            cls._ensure_keys_exist_locked()

    @classmethod
    def _ensure_keys_exist_locked(cls) -> None:
        """Body of :meth:`_ensure_keys_exist`; the caller holds the lock.

        A private key without its public half (a start interrupted between
        the two writes) keeps the private key and derives the public file
        from it, so tokens already minted stay valid and the kid is stable.
        """
        if cls.PRIVATE_KEY_PATH.exists() and cls.PUBLIC_KEY_PATH.exists():
            return

        private_key: rsa.RSAPrivateKey | None = None
        if cls.PRIVATE_KEY_PATH.exists():
            try:
                loaded = serialization.load_pem_private_key(
                    cls.PRIVATE_KEY_PATH.read_bytes(),
                    password=None,
                    backend=default_backend(),
                )
                if isinstance(loaded, rsa.RSAPrivateKey):
                    private_key = loaded
                    main_logger.warning(
                        "[MediaMTX JWT] Public key file missing; deriving it from the existing private key"
                    )
            except (ValueError, TypeError) as exc:
                main_logger.warning(
                    "[MediaMTX JWT] Existing private key is unreadable (%s); generating a new pair",
                    exc,
                )

        if private_key is None:
            main_logger.info("[MediaMTX JWT] Generating new RSA key pair...")
            # Generate 2048-bit RSA key pair
            private_key = rsa.generate_private_key(
                public_exponent=65537, key_size=2048, backend=default_backend()
            )
            private_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            cls._write_atomic(cls.PRIVATE_KEY_PATH, private_pem, private=True)

        cls._write_atomic(
            cls.PUBLIC_KEY_PATH, cls._public_pem_of(private_key), private=False
        )
        main_logger.info("[MediaMTX JWT] RSA key pair ready")

    @classmethod
    def _load_keys(cls) -> None:
        """Load RSA keys from files (once per process, under the lock)."""
        if cls._private_key is not None:
            return

        cls.KEYS_DIR.mkdir(parents=True, exist_ok=True)
        with cls._keys_dir_lock():
            if cls._private_key is not None:  # another thread got here first
                return

            cls._ensure_keys_exist_locked()

            private_pem = cls.PRIVATE_KEY_PATH.read_bytes()
            private_key = serialization.load_pem_private_key(
                private_pem, password=None, backend=default_backend()
            )
            if not isinstance(private_key, rsa.RSAPrivateKey):
                raise TypeError("[MediaMTX JWT] private key is not RSA")

            # The public file must be the private key's own half. A pair
            # torn by the old unlocked first start (or a hand-edited file)
            # is repaired here rather than minting tokens MediaMTX rejects.
            public_pem = cls.PUBLIC_KEY_PATH.read_bytes()
            expected_numbers = private_key.public_key().public_numbers()
            try:
                public_key = serialization.load_pem_public_key(
                    public_pem, backend=default_backend()
                )
                matches = (
                    isinstance(public_key, rsa.RSAPublicKey)
                    and public_key.public_numbers() == expected_numbers
                )
            except (ValueError, TypeError):
                matches = False
            if not matches:
                main_logger.warning(
                    "[MediaMTX JWT] %s does not match the private key; rewriting it from the private key",
                    cls.PUBLIC_KEY_PATH,
                )
                public_pem = cls._public_pem_of(private_key)
                cls._write_atomic(cls.PUBLIC_KEY_PATH, public_pem, private=False)
                public_key = serialization.load_pem_public_key(
                    public_pem, backend=default_backend()
                )

            cls._public_key = public_key  # type: ignore[assignment]
            # Generate Key ID from public key hash
            cls._kid = hashlib.sha256(public_pem).hexdigest()[:16]
            # Published last: readers that skip the lock check this attribute.
            cls._private_key = private_key

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
        extra_paths: list[str] | None = None,
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
            # One publisher may feed more than one path: a webcam's ffmpeg
            # publishes the main path and its -sub in one process.
            "mediamtx_permissions": [{"action": "publish", "path": p}
                                     for p in [camera_path, *(extra_paths or [])]],
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

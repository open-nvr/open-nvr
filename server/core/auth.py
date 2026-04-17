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
Authentication and authorization utilities.
Handles JWT token creation, validation, and password hashing.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pyotp
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from core.config import settings
from core.database import get_db
from core.logging_config import auth_logger
from models import PasswordPolicy, User
from schemas import TokenData

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT token bearer scheme
security = HTTPBearer()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Generate password hash from plain password."""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Create JWT access token with enhanced security claims (jti, iat, nbf)."""
    to_encode = data.copy()
    now = datetime.now(UTC)

    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.access_token_expire_minutes)

    to_encode.update({"exp": expire, "iat": now, "nbf": now, "jti": str(uuid.uuid4()), "type": "access"})

    encoded_jwt = jwt.encode(
        to_encode, settings.secret_key, algorithm=settings.algorithm
    )

    auth_logger.log_action(
        "auth.token_created",
        message=f"Access token created for user: {data.get('sub', 'unknown')}",
        extra_data={
            "username": data.get("sub"),
            "expires_in_minutes": settings.access_token_expire_minutes,
            "algorithm": settings.algorithm,
        },
    )

    return encoded_jwt


def create_refresh_token(data: dict) -> str:
    """Create a long-lived JWT refresh token used to obtain new access tokens."""
    to_encode = data.copy()
    now = datetime.now(UTC)
    expire = now + timedelta(days=settings.refresh_token_expire_days)
    to_encode.update({"exp": expire, "iat": now, "jti": str(uuid.uuid4()), "type": "refresh"})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def verify_token(token: str) -> TokenData | None:
    """Verify and decode JWT token."""
    try:
        # Enforce algorithm validation to prevent "none" algorithm attacks
        # Only allow the specifically configured algorithm (e.g., HS256)
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.algorithm]
        )
        username: str = payload.get("sub")
        if username is None:
            auth_logger.log_action(
                "auth.token_verify_failed",
                message="Token verification failed: missing username in payload",
                extra_data={"reason": "missing_username"},
            )
            return None

        token_data = TokenData(username=username)
        auth_logger.log_action(
            "auth.token_verify_success",
            message=f"Token verified successfully for user: {username}",
            extra_data={"username": username},
        )
        return token_data

    except JWTError as e:
        auth_logger.log_action(
            "auth.token_verify_failed",
            message=f"Token verification failed: {type(e).__name__}",
            extra_data={"error": str(e), "error_type": type(e).__name__},
        )
        return None


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Get current authenticated user from JWT token."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = credentials.credentials
    token_data = verify_token(token)
    if token_data is None:
        auth_logger.log_action(
            "auth.user_auth_failed",
            message="User authentication failed: invalid token",
            extra_data={"reason": "invalid_token"},
        )
        raise credentials_exception

    user = db.query(User).filter(User.username == token_data.username).first()
    if user is None:
        auth_logger.log_action(
            "auth.user_auth_failed",
            message=f"User authentication failed: user not found - {token_data.username}",
            extra_data={"username": token_data.username, "reason": "user_not_found"},
        )
        raise credentials_exception

    if not user.is_active:
        auth_logger.log_action(
            "auth.user_auth_failed",
            user_id=user.id,
            message=f"User authentication failed: inactive user - {user.username}",
            extra_data={"username": user.username, "reason": "user_inactive"},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user"
        )

    auth_logger.log_action(
        "auth.user_auth_success",
        user_id=user.id,
        message=f"User authenticated successfully: {user.username}",
        extra_data={"username": user.username, "user_id": user.id},
    )

    return user


def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    """Get current active user."""
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user"
        )
    return current_user


def get_current_superuser(current_user: User = Depends(get_current_user)) -> User:
    """Get current superuser."""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    return current_user


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    """
    Authenticate user with username and password.
    Mitigates timing attacks by always performing password verification.
    Enforces account lockout policy.
    """
    user = db.query(User).filter(User.username == username).first()

    if not user:
        # TIMING ATTACK PREVENTION:
        # Always verify against a dummy hash so the response time is identical
        # to when a user exists. This prevents username enumeration.
        verify_password(password, settings.dummy_password_hash)
        return None

    # Check for account lockout
    if user.locked_until:
        if user.locked_until > datetime.now(UTC):
            # Account is locked.
            # To prevent enumeration via timing analysis, we perform a dummy check
            verify_password(password, settings.dummy_password_hash)
            return None
        else:
            # Lock expired, reset
            user.locked_until = None
            user.failed_login_attempts = 0
            db.add(user)
            db.commit()

    if not verify_password(password, user.hashed_password):
        # Handle failed attempt
        user.failed_login_attempts += 1

        # Get policy for lockout rules
        policy = db.query(PasswordPolicy).first()
        if not policy:
            policy = PasswordPolicy()  # Use defaults if not set

        max_attempts = (
            policy.max_failed_attempts if policy.max_failed_attempts is not None else 5
        )
        lockout_mins = (
            policy.lockout_minutes if policy.lockout_minutes is not None else 15
        )

        if user.failed_login_attempts >= max_attempts:
            user.locked_until = datetime.now(UTC) + timedelta(minutes=lockout_mins)
            user.failed_login_attempts = 0  # Reset counter after locking? Or keep it?
            # Usually we can keep it or reset. Let's reset so they start fresh after lockout expires.
            # Or keep it to show they were locked? The lockout mechanism relies on locked_until.
            # Resetting attempts ensures that after lock expires, they have full attempts again.

        db.add(user)
        db.commit()
        return None

    # Authentication successful (Password Verified)
    # Note: We do NOT reset failed_login_attempts here immediately if MFA is enabled.
    # The caller (router) is responsible for resetting the counter upon full successful login.

    return user


# MFA helpers


def generate_mfa_secret() -> str:
    """Generate a base32 secret for TOTP."""
    return pyotp.random_base32()


def get_mfa_provisioning_uri(
    username: str, secret: str, issuer: str = "OpenNVR Surveillance"
) -> str:
    """Build otpauth provisioning URI for QR code apps (Google Authenticator, etc.)."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=username, issuer_name=issuer)


def verify_totp_code(secret: str, code: str) -> bool:
    """Verify a TOTP code against a secret."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)

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
Authentication router for login and token management.
Handles user authentication and JWT token generation.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from core.auth import (
    authenticate_user,
    create_access_token,
    create_refresh_token,
    generate_mfa_secret,
    get_current_active_user,
    get_mfa_provisioning_uri,
    get_password_hash,
    verify_totp_code,
)
from core.config import settings
from core.database import get_db
from core.logging_config import auth_logger
from models import PasswordPolicy, Role, User
from schemas import (
    FirstTimeSetupCheckResponse,
    FirstTimeSetupRequest,
    FirstTimeSetupResponse,
    MfaSetupResponse,
    MfaVerifyRequest,
    Token,
    UserCreate,
    UserLogin,
    UserRegister,
    UserResponse,
)
from services.audit_service import write_audit_log
from services.user_service import UserService

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/check-setup", response_model=FirstTimeSetupCheckResponse)
async def check_first_time_setup(db: Session = Depends(get_db)):
    """Check if first-time setup is required for admin user."""
    admin_user = (
        db.query(User).filter(User.username == settings.default_admin_username).first()
    )

    if not admin_user:
        return FirstTimeSetupCheckResponse(
            setup_required=True, username=settings.default_admin_username
        )

    if not admin_user.password_set:
        return FirstTimeSetupCheckResponse(
            setup_required=True, username=admin_user.username
        )

    return FirstTimeSetupCheckResponse(setup_required=False)


@router.post("/first-time-setup", response_model=FirstTimeSetupResponse)
async def first_time_setup(
    payload: FirstTimeSetupRequest,
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Complete first-time setup: set password and enable MFA."""
    # Find user that needs setup
    user = (
        db.query(User)
        .filter(User.username == payload.username, User.password_set == False)
        .first()
    )

    if not user:
        raise HTTPException(
            status_code=400, detail="User not found or setup already completed"
        )

    # Enforce password policy
    UserService._enforce_password_policy(
        db, user.username, user.email, payload.password
    )

    # Set password
    user.hashed_password = get_password_hash(payload.password)
    user.password_set = True

    # Generate MFA secret
    mfa_secret = generate_mfa_secret()
    user.mfa_secret = mfa_secret
    user.mfa_enabled = True

    db.commit()

    # Generate QR code URI for MFA setup
    mfa_qr_uri = get_mfa_provisioning_uri(user.username, mfa_secret)

    auth_logger.log_action(
        "auth.first_time_setup_complete",
        user_id=user.id,
        message=f"First-time setup completed for user: {user.username}",
        extra_data={"username": user.username, "mfa_enabled": True},
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )

    return FirstTimeSetupResponse(
        message="Setup complete. Please scan QR code with authenticator app.",
        mfa_required=True,
        mfa_secret=mfa_secret,
        mfa_qr_uri=mfa_qr_uri,
    )


@router.post("/register", response_model=UserResponse)
async def register_user(
    payload: UserRegister, db: Session = Depends(get_db), request: Request = None
):
    """Public registration for viewer role by default."""
    viewer = db.query(Role).filter(Role.name == "viewer").first()
    if not viewer:
        raise HTTPException(status_code=400, detail="Viewer role is not set up")
    user_create = UserCreate(
        username=payload.username,
        email=payload.email,
        password=payload.password,
        first_name=None,
        last_name=None,
        is_active=True,
        role_id=viewer.id,
    )
    user = UserService.create_user(db, user_create)
    try:
        write_audit_log(
            db,
            action="user.register",
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={"username": user.username, "email": user.email},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return user


@router.post("/login", response_model=Token)
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Login endpoint to get access token (supports MFA if enabled)."""

    # Check if user needs first-time setup
    user_check = db.query(User).filter(User.username == form_data.username).first()
    if user_check and not user_check.password_set:
        auth_logger.log_action(
            "auth.setup_required",
            message=f"Setup required for user: {form_data.username}",
            extra_data={"username": form_data.username, "reason": "password_not_set"},
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
        raise HTTPException(
            status_code=403,
            detail="First-time setup required. Please complete setup before logging in.",
            headers={"X-Setup-Required": "true"},
        )

    auth_logger.log_action(
        "auth.login_attempt",
        message=f"Login attempt for user: {form_data.username}",
        extra_data={
            "username": form_data.username,
            "method": "form",
            "mfa_required": False,
        },
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )

    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        auth_logger.log_action(
            "auth.login_failed",
            message=f"Login failed for user: {form_data.username} - invalid credentials",
            extra_data={
                "username": form_data.username,
                "reason": "invalid_credentials",
                "method": "form",
            },
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        auth_logger.log_action(
            "auth.login_failed",
            user_id=user.id,
            message=f"Login failed for user: {form_data.username} - inactive user",
            extra_data={
                "username": form_data.username,
                "reason": "inactive_user",
                "method": "form",
            },
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.mfa_enabled:
        auth_logger.log_action(
            "auth.login_mfa_required",
            user_id=user.id,
            message=f"MFA required for user: {form_data.username}",
            extra_data={
                "username": form_data.username,
                "method": "form",
                "mfa_enabled": True,
            },
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
        raise HTTPException(
            status_code=401, detail="MFA required. Use /auth/login-json with code"
        )

    # Reset failed attempts on successful login
    if user.failed_login_attempts > 0 or user.locked_until:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.add(user)
        db.commit()

    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    refresh_token = create_refresh_token(data={"sub": user.username})

    auth_logger.log_action(
        "auth.login_success",
        user_id=user.id,
        message=f"Login successful for user: {user.username}",
        extra_data={
            "username": user.username,
            "method": "form",
            "token_expires_minutes": settings.access_token_expire_minutes,
            "user_role": user.role.name if user.role else None,
        },
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )

    # Legacy audit log
    try:
        write_audit_log(
            db,
            action="login",
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={"username": user.username, "method": "form"},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        auth_logger.error(f"Failed to write audit log: {e}", exc_info=True)

    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer"}


@router.post("/login-json", response_model=Token)
async def login_with_json(
    user_credentials: UserLogin,
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Alternative login endpoint using JSON body with optional TOTP code."""

    # Check if user needs first-time setup
    user_check = (
        db.query(User).filter(User.username == user_credentials.username).first()
    )
    if user_check and not user_check.password_set:
        raise HTTPException(
            status_code=403,
            detail="First-time setup required. Please complete setup before logging in.",
            headers={"X-Setup-Required": "true"},
        )

    user = authenticate_user(db, user_credentials.username, user_credentials.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.mfa_enabled:
        if not user_credentials.code or not verify_totp_code(
            user.mfa_secret, user_credentials.code
        ):
            # Handle MFA failure as a failed login attempt
            user.failed_login_attempts += 1

            policy = db.query(PasswordPolicy).first()
            if not policy:
                policy = PasswordPolicy()

            max_attempts = (
                policy.max_failed_attempts
                if policy.max_failed_attempts is not None
                else 5
            )
            lockout_mins = (
                policy.lockout_minutes if policy.lockout_minutes is not None else 15
            )

            if user.failed_login_attempts >= max_attempts:
                user.locked_until = datetime.now(UTC) + timedelta(minutes=lockout_mins)

            db.add(user)
            db.commit()

            raise HTTPException(status_code=401, detail="Invalid or missing MFA code")

    # Reset failed attempts on successful login
    if user.failed_login_attempts > 0 or user.locked_until:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.add(user)
        db.commit()

    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    refresh_token = create_refresh_token(data={"sub": user.username})
    try:
        write_audit_log(
            db,
            action="login",
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={
                "username": user.username,
                "method": "json",
                "mfa": bool(user_credentials.code),
            },
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer"}


@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def mfa_setup(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """Generate and return provisioning URI + secret for TOTP; store secret disabled until verify."""
    secret = generate_mfa_secret()
    current_user.mfa_secret = secret
    current_user.mfa_enabled = False
    db.commit()
    otpauth_url = get_mfa_provisioning_uri(
        current_user.username, secret, issuer="OpenNVR Surveillance"
    )
    return MfaSetupResponse(otpauth_url=otpauth_url, secret=secret)


@router.post("/mfa/verify")
async def mfa_verify(
    payload: MfaVerifyRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Verify user-provided TOTP code to enable MFA."""
    if not current_user.mfa_secret:
        raise HTTPException(status_code=400, detail="MFA not initialized")
    if not verify_totp_code(current_user.mfa_secret, payload.code):
        raise HTTPException(status_code=400, detail="Invalid code")
    current_user.mfa_enabled = True
    db.commit()
    try:
        write_audit_log(
            db,
            action="mfa.enable",
            user_id=current_user.id,
            entity_type="user",
            entity_id=current_user.id,
            details={"username": current_user.username},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"message": "MFA enabled"}


@router.post("/mfa/disable")
async def mfa_disable(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Disable MFA for current user."""
    current_user.mfa_enabled = False
    current_user.mfa_secret = None
    db.commit()
    try:
        write_audit_log(
            db,
            action="mfa.disable",
            user_id=current_user.id,
            entity_type="user",
            entity_id=current_user.id,
            details={"username": current_user.username},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"message": "MFA disabled"}


@router.post("/refresh", response_model=Token)
async def refresh_access_token(
    refresh_token: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    """Exchange a valid refresh token for a new access token and refresh token pair."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            refresh_token, settings.secret_key, algorithms=[settings.algorithm]
        )
        if payload.get("type") != "refresh":
            raise credentials_exception
        username: str = payload.get("sub")
        if not username:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user:
        raise credentials_exception

    new_access_token = create_access_token(data={"sub": user.username})
    new_refresh_token = create_refresh_token(data={"sub": user.username})
    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
    }


@router.post("/logout")
async def logout(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Stateless logout endpoint. Frontend can call this to record a logout event."""
    try:
        write_audit_log(
            db,
            action="logout",
            user_id=current_user.id,
            entity_type="user",
            entity_id=current_user.id,
            details={"username": current_user.username},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"message": "Logged out"}


@router.get("/me", response_model=UserResponse)
async def read_users_me(current_user: User = Depends(get_current_active_user)):
    """Get current user information."""
    try:
        print(f"DEBUG: /me endpoint called, user: {current_user.username}")
        return current_user
    except Exception as e:
        print(f"ERROR in /me endpoint: {e}")
        print(f"ERROR type: {type(e)}")
        import traceback

        traceback.print_exc()
        raise

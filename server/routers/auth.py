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

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from core.auth import (
    authenticate_user,
    build_account_locked_detail,
    build_invalid_credentials_detail,
    create_access_token,
    create_refresh_token,
    generate_mfa_secret,
    get_lockout_policy,
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
from services.first_time_setup_service import (
    verify_and_consume as _verify_setup_token,
)
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

    return FirstTimeSetupCheckResponse(
        setup_required=False,
        registration_open=bool(settings.public_registration_enabled))


@router.post("/first-time-setup", response_model=FirstTimeSetupResponse)
async def first_time_setup(
    payload: FirstTimeSetupRequest,
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Complete first-time setup: set password and enable MFA."""
    # Gate on the one-time setup token (constant-time check + consume inside the
    # service). Done BEFORE the username lookup so this can't be used as a
    # user-existence oracle. See V-001.
    if not _verify_setup_token(payload.setup_token):
        try:
            auth_logger.log_action(
                "auth.first_time_setup_token_rejected",
                message="First-time setup attempted with missing or invalid token",
                extra_data={"username": payload.username},
                ip_address=request.client.host if request and request.client else None,
                user_agent=request.headers.get("user-agent") if request else None,
            )
        except Exception:
            pass
        # Match the post-success "wrong user" 4xx code surface so an attacker
        # cannot distinguish "no token armed" from "wrong token" from
        # "wrong username" — all three look the same.
        raise HTTPException(
            status_code=403,
            detail="First-time setup token missing or invalid.",
        )

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
    """Self-service registration (viewer role). Off unless the operator
    opted in with ``PUBLIC_REGISTRATION_ENABLED`` — accounts on an NVR
    are otherwise the administrator's to create (``POST /users``)."""
    if not settings.public_registration_enabled:
        raise HTTPException(
            status_code=403,
            detail="Self-service registration is disabled on this server; "
                   "ask an administrator for an account")
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


def _enroll_device(db, request, response, user_id: int | None) -> str | None:
    """Device-firewall enrollment on successful login.

    The BROWSER is identified by a long-lived device token, not by the client IP
    (behind NAT every client shares one address, so an IP can neither
    distinguish nor re-identify a device). A browser presenting no/unknown token
    is issued a fresh one: the first on a fresh install is auto-approved, later
    ones are recorded pending for an admin to approve.

    Returns the NEW token when one was minted, so the caller can put it in the
    login response — the SPA's fetch client uses ``credentials: 'omit'``, so a
    cookie alone would be ignored by the browser and every device would look
    unenrolled. The cookie is set too, for non-SPA clients. Neither is ever
    cleared on logout: signing out must not cost an approval.

    Best-effort: enrollment failure must not fail the login.
    """
    if request is None:
        return None
    try:
        from core.client_ip import get_client_ip
        from services import device_firewall_service as _dfw

        _dev, issued = _dfw.register_authenticated_browser(
            db,
            _dfw.token_from_request(request),
            get_client_ip(request),
            request.headers.get("user-agent"),
            user_id,
        )
        if issued and response is not None:
            # Mark Secure only when the request really arrived over TLS: a plain
            # http:// dev server would silently drop a Secure cookie, leaving the
            # browser permanently unenrollable.
            proto = (
                request.headers.get("x-forwarded-proto") or request.url.scheme or ""
            ).split(",")[0].strip().lower()
            response.set_cookie(
                _dfw.DEVICE_COOKIE_NAME,
                issued,
                max_age=_dfw.DEVICE_COOKIE_MAX_AGE,
                httponly=True,  # unreadable by JS, so XSS cannot exfiltrate it
                secure=proto == "https",
                samesite="lax",
                path="/",
            )
        return issued
    except Exception:
        return None


def _mfa_login_required(db: Session, user: User) -> bool:
    """Whether this login must present a TOTP code.

    Accounts can carry mfa_enabled=True without a secret (rows created before
    the default changed, or a provisioned bootstrap admin). No valid code can
    exist for them, so demanding one locks the account forever. Normalize the
    flag to False so the client shows the MFA-setup wall after this password
    login; /auth/mfa/verify turns it back on once enrollment completes.
    """
    if user.mfa_enabled and not user.mfa_secret:
        user.mfa_enabled = False
        db.add(user)
        db.commit()
        auth_logger.log_action(
            "auth.mfa_enrollment_pending",
            user_id=user.id,
            message=(
                f"User {user.username} flagged for MFA without an enrolled "
                "secret; allowing password login, enrollment enforced on client"
            ),
            extra_data={"username": user.username},
        )
        return False
    return user.mfa_enabled


@router.post("/login", response_model=Token)
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    request: Request = None,
    response: Response = None,
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

    user, auth_error = authenticate_user(db, form_data.username, form_data.password)
    if auth_error:
        failure_reason = auth_error.get("reason", "invalid_credentials")
        failure_status = int(auth_error.get("status_code", status.HTTP_401_UNAUTHORIZED))
        failure_detail = auth_error.get("detail", build_invalid_credentials_detail())

        if failure_reason == "account_locked":
            auth_logger.log_action(
                "auth.login_blocked_locked",
                message=f"Login blocked for user: {form_data.username} - account locked",
                extra_data={
                    "username": form_data.username,
                    "reason": "account_locked",
                    "method": "form",
                    "retry_after_seconds": failure_detail.get("retry_after_seconds", 0),
                },
                ip_address=request.client.host if request and request.client else None,
                user_agent=request.headers.get("user-agent") if request else None,
            )
        else:
            auth_logger.log_action(
                "auth.login_failed",
                message=f"Login failed for user: {form_data.username} - invalid credentials",
                extra_data={
                    "username": form_data.username,
                    "reason": "invalid_credentials",
                    "method": "form",
                    "remaining_attempts": failure_detail.get("remaining_attempts"),
                },
                ip_address=request.client.host if request and request.client else None,
                user_agent=request.headers.get("user-agent") if request else None,
            )

        raise HTTPException(
            status_code=failure_status,
            detail=failure_detail,
            headers={"WWW-Authenticate": "Bearer"}
            if failure_status == status.HTTP_401_UNAUTHORIZED
            else None,
        )

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
            detail=build_invalid_credentials_detail(),
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
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "forbidden",
                "message": "Account is inactive.",
            },
        )

    if _mfa_login_required(db, user):
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

    # Device firewall: enroll this BROWSER on successful login (issues the
    # device cookie when it has none). See _enroll_device.
    device_token = _enroll_device(db, request, response, user.id)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "device_token": device_token,
    }


@router.post("/login-json", response_model=Token)
async def login_with_json(
    user_credentials: UserLogin,
    db: Session = Depends(get_db),
    request: Request = None,
    response: Response = None,
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

    user, auth_error = authenticate_user(db, user_credentials.username, user_credentials.password)
    if auth_error:
        failure_status = int(auth_error.get("status_code", status.HTTP_401_UNAUTHORIZED))
        failure_detail = auth_error.get("detail", build_invalid_credentials_detail())
        raise HTTPException(
            status_code=failure_status,
            detail=failure_detail,
            headers={"WWW-Authenticate": "Bearer"}
            if failure_status == status.HTTP_401_UNAUTHORIZED
            else None,
        )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=build_invalid_credentials_detail(),
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "forbidden",
                "message": "Account is inactive.",
            },
        )

    if _mfa_login_required(db, user):
        if not user_credentials.code or not verify_totp_code(
            user.mfa_secret, user_credentials.code
        ):
            # Handle MFA failure as a failed login attempt
            user.failed_login_attempts += 1

            max_attempts, lockout_mins = get_lockout_policy(db)

            if user.failed_login_attempts >= max_attempts:
                user.locked_until = datetime.now(UTC) + timedelta(minutes=lockout_mins)
                user.failed_login_attempts = 0

            db.add(user)
            db.commit()

            if user.locked_until and user.locked_until > datetime.now(UTC):
                auth_logger.log_action(
                    "auth.login_blocked_locked",
                    user_id=user.id,
                    message=f"MFA login blocked for user: {user.username} - account locked",
                    extra_data={
                        "username": user.username,
                        "reason": "account_locked",
                        "method": "json",
                        "retry_after_seconds": max(0, int((user.locked_until - datetime.now(UTC)).total_seconds())),
                    },
                    ip_address=request.client.host if request and request.client else None,
                    user_agent=request.headers.get("user-agent") if request else None,
                )
                raise HTTPException(
                    status_code=status.HTTP_423_LOCKED,
                    detail=build_account_locked_detail(user.locked_until),
                )

            remaining_attempts = max(0, max_attempts - user.failed_login_attempts)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=build_invalid_credentials_detail(
                    message="Invalid or missing MFA code",
                    remaining_attempts=remaining_attempts,
                ),
                headers={"WWW-Authenticate": "Bearer"},
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
    # Device firewall: enroll this BROWSER on successful login (issues the
    # device cookie when it has none). See _enroll_device.
    device_token = _enroll_device(db, request, response, user.id)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "device_token": device_token,
    }


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
        auth_logger.debug("/me endpoint called for user_id=%s", current_user.id)
        return current_user
    except Exception:
        # Don't print() to stdout (pollutes the audit/stdout stream and can
        # leak detail); log at ERROR with a stack trace via the logger.
        auth_logger.error("error in /me endpoint", exc_info=True)
        raise

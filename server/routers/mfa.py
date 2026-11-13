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

"""The signed-in user's own second factors (Settings > Two-factor sign-in).

Typical use: moving to a new authenticator app without an administrator —
add the new app (account password + a code from the current one), check it,
then remove the old one. The last working authenticator can't be removed or
turned off; losing it is what an administrator's reset is for.

Every change to an account that already has a working authenticator needs
the account password and a current code (``X-MFA-Code``); while the account
has none (the enrolment screen) the session alone is enough, as before.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_mfa_provisioning_uri
from core.database import get_db
from core.logging_config import main_logger
from models import User
from schemas import (
    MfaBeginRequest,
    MfaBeginResponse,
    MfaConfirmRequest,
    MfaConfirmResponse,
    MfaFactorDelete,
    MfaFactorList,
    MfaFactorOut,
    MfaFactorPatch,
    MfaRecoveryCodesRequest,
    MfaRecoveryCodesResponse,
)
from services import mfa_service
from services.audit_service import write_audit_log
from utils.mfa_guard import require_reauth

router = APIRouter(prefix="/auth/mfa", tags=["authentication"])

ISSUER = "OpenNVR Surveillance"


def factor_out(f) -> MfaFactorOut:
    return MfaFactorOut(id=f.id, kind=f.kind, label=f.label, enabled=bool(f.enabled),
                        confirmed=f.confirmed_at is not None, created_at=f.created_at,
                        last_used_at=f.last_used_at)


def _refuse_sso(db: Session, user: User) -> None:
    if mfa_service.is_sso_account(user) and not mfa_service.has_active_factor(db, user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This account signs in through single sign-on; its identity provider owns two-factor sign-in.",
        )


def _step_up(db: Session, user: User, code: str | None, password: str | None) -> None:
    """Password + code once the account has a working authenticator. A
    recovery code stands in for the code here (and is used up): it is how
    someone whose phone is gone adds the new one."""
    if mfa_service.has_active_factor(db, user):
        require_reauth(user, code, password=password, need_password=True, allow_recovery=True)


def _audit(db, request, user: User, action: str, details: dict) -> None:
    try:
        write_audit_log(
            db, action=action, user_id=user.id, entity_type="user", entity_id=user.id,
            details={"username": user.username, **details},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        main_logger.warning(f"Failed to write audit log ({action}): {e}")


@router.get("/factors", response_model=MfaFactorList)
def list_factors(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Your authenticators (enrolments in progress included, marked unconfirmed)."""
    rows = mfa_service.factors(db, current_user, include_pending=True)
    db.commit()     # a legacy secret adopted on the way stays adopted
    return MfaFactorList(factors=[factor_out(f) for f in rows],
                         recovery_codes_remaining=mfa_service.recovery_codes_remaining(db, current_user))


@router.post("/factors/totp", response_model=MfaBeginResponse)
def begin_totp(
    body: MfaBeginRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    mfa_code: str | None = Header(None, alias="X-MFA-Code"),
):
    """Start adding an authenticator app: returns its QR code (otpauth URL)
    and secret. It does nothing until confirmed with a code from it."""
    _refuse_sso(db, current_user)
    _step_up(db, current_user, mfa_code, body.current_password)
    f, secret = mfa_service.begin_totp(db, current_user, body.label)
    return MfaBeginResponse(
        factor_id=f.id, secret=secret,
        otpauth_url=get_mfa_provisioning_uri(current_user.username, secret, issuer=ISSUER),
    )


@router.post("/factors/{factor_id}/confirm", response_model=MfaConfirmResponse)
def confirm_totp(
    factor_id: int,
    body: MfaConfirmRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Finish adding an authenticator app with a code from it. For the
    account's first app, the answer also carries a new set of recovery codes,
    shown this once."""
    first = not mfa_service.has_active_factor(db, current_user)
    f = mfa_service.confirm_totp(db, current_user, body.code, factor_id=factor_id, label=body.label)
    if f is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="That code does not match. Check the app and try the current code.")
    _audit(db, request, current_user, "mfa.factor_added", {"factor_id": f.id, "label": f.label})
    codes = mfa_service.generate_recovery_codes(db, current_user) if first else None
    return MfaConfirmResponse(**factor_out(f).model_dump(), recovery_codes=codes)


@router.post("/recovery-codes", response_model=MfaRecoveryCodesResponse)
def regenerate_recovery_codes(
    body: MfaRecoveryCodesRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    mfa_code: str | None = Header(None, alias="X-MFA-Code"),
):
    """A new set of recovery codes, replacing the old set (shown this once).
    Needs your password and a current code."""
    if not mfa_service.has_active_factor(db, current_user):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Set up an authenticator app first.")
    _step_up(db, current_user, mfa_code, body.current_password)
    codes = mfa_service.generate_recovery_codes(db, current_user)
    _audit(db, request, current_user, "mfa.recovery_codes_generated", {"count": len(codes)})
    return MfaRecoveryCodesResponse(recovery_codes=codes)


@router.patch("/factors/{factor_id}", response_model=MfaFactorOut)
def update_factor(
    factor_id: int,
    body: MfaFactorPatch,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    mfa_code: str | None = Header(None, alias="X-MFA-Code"),
):
    """Rename an authenticator, or turn it off / on (not your last working one)."""
    _step_up(db, current_user, mfa_code, body.current_password)
    try:
        f = None
        if body.label is not None:
            f = mfa_service.rename(db, current_user, factor_id, body.label)
        if body.enabled is not None:
            f = mfa_service.set_enabled(db, current_user, factor_id, body.enabled)
            _audit(db, request, current_user, "mfa.factor_enabled" if body.enabled else "mfa.factor_disabled",
                   {"factor_id": f.id, "label": f.label})
        if f is None:
            f = mfa_service.get_factor(db, current_user, factor_id)
    except mfa_service.MfaChangeRefused as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return factor_out(f)


@router.delete("/factors/{factor_id}")
def delete_factor(
    factor_id: int,
    request: Request,
    body: MfaFactorDelete | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    mfa_code: str | None = Header(None, alias="X-MFA-Code"),
):
    """Remove an authenticator (not your last working one)."""
    _step_up(db, current_user, mfa_code, body.current_password if body else None)
    try:
        f = mfa_service.get_factor(db, current_user, factor_id)
        label = f.label
        mfa_service.remove(db, current_user, factor_id)
    except mfa_service.MfaChangeRefused as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    _audit(db, request, current_user, "mfa.factor_removed", {"factor_id": factor_id, "label": label})
    return {"message": "Authenticator removed"}

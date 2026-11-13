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

"""Two-factor sign-in: the one place that reads or changes second factors.

An account holds zero or more factors (``models.UserMfaFactor``), today
authenticator apps (TOTP). A factor is *active* once confirmed (a code from
it was verified) and while enabled; only active factors verify codes.

Invariants every write here keeps (``_sync``):

- ``users.mfa_enabled`` is true exactly when the account has an active
  factor. The SPA's enrolment screen and the API's enrolment wall
  (``core.auth``) read it.
- ``users.encrypted_mfa_secret`` holds the oldest active factor's ciphertext,
  or NULL. Nothing here verifies against it; it is there so an older core
  started on this database (rollback) checks the right authenticator.

A secret found in that column with no factor rows at all (a row written by
an older core or a script) is adopted as the account's first factor the
first time it is needed, so nothing is ever lost between versions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta

import pyotp
from sqlalchemy.orm import Session

from models import User, UserMfaFactor, UserRecoveryCode

DEFAULT_LABEL = "Authenticator"
RECOVERY_CODE_COUNT = 10
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
# What people mistype for base32: digits that look like letters.
_LOOKALIKE = str.maketrans({"0": "O", "1": "I", "8": "B"})
PENDING_TTL = timedelta(minutes=15)
_STEP = 30


class MfaResetRefused(ValueError):
    """The account has no second factor here to reset."""


class MfaChangeRefused(ValueError):
    """A change that would leave the account without a working factor, or
    that names a factor the account does not have."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:    # a backend that drops the zone (SQLite)
        return dt.replace(tzinfo=UTC)
    return dt


def is_sso_account(user: User) -> bool:
    return (user.auth_provider or "local") == "oidc"


# ── reading ───────────────────────────────────────────────────────────

def _rows(db: Session, user: User) -> list[UserMfaFactor]:
    return (db.query(UserMfaFactor).filter(UserMfaFactor.user_id == user.id)
            .order_by(UserMfaFactor.id).all())


def _adopt_legacy(db: Session, user: User) -> None:
    """A secret in the old column and no factor rows: make it the first factor."""
    if not user.encrypted_mfa_secret or not user.mfa_enabled:
        return
    if db.query(UserMfaFactor.id).filter(UserMfaFactor.user_id == user.id).first():
        return
    db.add(UserMfaFactor(user_id=user.id, kind="totp", label=DEFAULT_LABEL,
                         encrypted_secret=user.encrypted_mfa_secret, enabled=True,
                         confirmed_at=_now()))
    db.flush()


def factors(db: Session, user: User, *, include_pending: bool = False) -> list[UserMfaFactor]:
    _adopt_legacy(db, user)
    rows = _rows(db, user)
    return rows if include_pending else [f for f in rows if f.confirmed_at is not None]


def active_factors(db: Session, user: User) -> list[UserMfaFactor]:
    return [f for f in factors(db, user) if f.enabled]


def has_active_factor(db: Session, user: User) -> bool:
    return bool(active_factors(db, user))


def _sync(db: Session, user: User) -> None:
    active = [f for f in _rows(db, user) if f.confirmed_at is not None and f.enabled]
    user.mfa_enabled = bool(active)
    user.encrypted_mfa_secret = active[0].encrypted_secret if active else None
    db.add(user)


# ── verifying codes ───────────────────────────────────────────────────

def _matching_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The TOTP time step ``code`` belongs to (now ±1 step), or None."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = int((time.time() if now is None else now) // _STEP)
    for step in (current, current - 1, current + 1):
        if hmac.compare_digest(totp.at(step * _STEP), code):
            return step
    return None


def verify_login_code(db: Session, user: User, code: str | None) -> str | None:
    """A sign-in code: from any active factor ("totp"), or an unused recovery
    code ("recovery", used up by this). None when neither matches. A TOTP code
    is accepted for one sign-in only (per factor, the step must move forward).
    Commits on success."""
    if not code:
        return None
    if _use_recovery_code(db, user, code):
        db.commit()
        return "recovery"
    for f in active_factors(db, user):
        step = _matching_step(f.secret, code)
        if step is None:
            continue
        if f.last_used_step is not None and step <= f.last_used_step:
            return None                      # replayed, or an older code after a newer one
        f.last_used_step, f.last_used_at = step, _now()
        db.commit()
        return "totp"
    return None


def verify_step_up(db: Session, user: User, code: str | None, *, allow_recovery: bool = False) -> bool:
    """A code from any active factor confirming a sensitive action. The same
    code may confirm several actions inside its 30 s window (deleting two
    cameras in a row), so there is no replay check here.

    ``allow_recovery`` also accepts (and uses up) a recovery code. Only the
    account's own two-factor settings pass it: someone who signed in with a
    recovery code because the phone is gone must be able to add a new app,
    but a recovery code never confirms deleting cameras or users."""
    if not code:
        return False
    for f in active_factors(db, user):
        if _matching_step(f.secret, code) is not None:
            f.last_used_at = _now()
            db.flush()
            return True
    return allow_recovery and _use_recovery_code(db, user, code)


# ── recovery codes ────────────────────────────────────────────────────

def normalize_recovery_code(code: str | None) -> str | None:
    """The 16 base32 characters of a recovery code as typed (any case, with
    or without dashes and spaces, 0/1/8 for O/I/B), or None if it isn't one."""
    raw = "".join((code or "").split()).replace("-", "").upper().translate(_LOOKALIKE)
    return raw if len(raw) == 16 and all(c in _B32 for c in raw) else None


def _hash_code(normalized: str) -> str:
    return hashlib.sha256(normalized.encode()).hexdigest()


def _use_recovery_code(db: Session, user: User, code: str) -> bool:
    norm = normalize_recovery_code(code)
    if norm is None:
        return False
    digest = _hash_code(norm)
    for rc in (db.query(UserRecoveryCode)
               .filter(UserRecoveryCode.user_id == user.id, UserRecoveryCode.used_at.is_(None)).all()):
        if hmac.compare_digest(rc.code_hash, digest):
            rc.used_at = _now()
            db.flush()
            return True
    return False


def recovery_codes_remaining(db: Session, user: User) -> int:
    return (db.query(UserRecoveryCode.id)
            .filter(UserRecoveryCode.user_id == user.id, UserRecoveryCode.used_at.is_(None))
            .count())


def generate_recovery_codes(db: Session, user: User) -> list[str]:
    """A fresh set of recovery codes, replacing any earlier set. Returned once,
    formatted XXXX-XXXX-XXXX-XXXX; only hashes are kept. Commits."""
    db.query(UserRecoveryCode).filter(UserRecoveryCode.user_id == user.id).delete(
        synchronize_session=False)
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_B32) for _ in range(16))
        db.add(UserRecoveryCode(user_id=user.id, code_hash=_hash_code(raw)))
        codes.append("-".join(raw[i:i + 4] for i in range(0, 16, 4)))
    db.commit()
    return codes


# ── enrolment ─────────────────────────────────────────────────────────

def _clean_label(label: str | None) -> str:
    label = " ".join((label or "").split())[:64]
    return label or DEFAULT_LABEL


def begin_totp(db: Session, user: User, label: str | None = None) -> tuple[UserMfaFactor, str]:
    """Start enrolling an authenticator app; returns (pending factor, secret).

    While an unexpired enrolment is pending it is returned again instead of a
    new one: reloading the enrolment screen shows the same QR code, so a code
    the user already scanned keeps working. Commits."""
    _adopt_legacy(db, user)
    pending = [f for f in _rows(db, user) if f.confirmed_at is None]
    keep = None
    for f in pending:
        created = _aware(f.created_at) or _now()
        if keep is None and _now() - created < PENDING_TTL:
            keep = f
        else:
            db.delete(f)
    if keep is None:
        keep = UserMfaFactor(user_id=user.id, kind="totp", label=_clean_label(label),
                             encrypted_secret=_encrypt(pyotp.random_base32()),
                             enabled=False, confirmed_at=None)
        db.add(keep)
    elif label:
        keep.label = _clean_label(label)
    db.commit()
    db.refresh(keep)
    return keep, keep.secret


def confirm_totp(db: Session, user: User, code: str, *, factor_id: int | None = None,
                 label: str | None = None) -> UserMfaFactor | None:
    """Verify a code from a pending enrolment and make it an active factor.
    ``factor_id`` None means the newest pending one. None when the code does
    not match (or nothing is pending). Commits on success."""
    pending = [f for f in _rows(db, user) if f.confirmed_at is None
               and (factor_id is None or f.id == factor_id)]
    if not pending:
        return None
    f = pending[-1]
    step = _matching_step(f.secret, code)
    if step is None:
        return None
    f.confirmed_at, f.enabled, f.last_used_step = _now(), True, step
    if label:
        f.label = _clean_label(label)
    _sync(db, user)
    db.commit()
    return f


def enrol_confirmed(db: Session, user: User, secret: str, label: str | None = None) -> UserMfaFactor:
    """First-time setup: the administrator's factor, confirmed at once (they
    scan the QR and then sign in with a code from it). Flushes; the caller
    commits."""
    f = UserMfaFactor(user_id=user.id, kind="totp", label=_clean_label(label),
                      encrypted_secret=_encrypt(secret), enabled=True, confirmed_at=_now())
    db.add(f)
    db.flush()
    _sync(db, user)
    return f


def _encrypt(secret: str) -> str:
    from utils.encryption import encrypt_value

    return encrypt_value(secret)


# ── the account owner's changes ───────────────────────────────────────

def get_factor(db: Session, user: User, factor_id: int) -> UserMfaFactor:
    f = next((x for x in factors(db, user, include_pending=True) if x.id == factor_id), None)
    if f is None:
        raise MfaChangeRefused("No such authenticator on this account.")
    return f


def _others_active(db: Session, user: User, factor: UserMfaFactor) -> bool:
    return any(x.id != factor.id for x in active_factors(db, user))


def rename(db: Session, user: User, factor_id: int, label: str) -> UserMfaFactor:
    f = get_factor(db, user, factor_id)
    f.label = _clean_label(label)
    db.commit()
    return f


def set_enabled(db: Session, user: User, factor_id: int, enabled: bool) -> UserMfaFactor:
    f = get_factor(db, user, factor_id)
    if f.confirmed_at is None:
        raise MfaChangeRefused("Finish setting up this authenticator first.")
    if not enabled and f.enabled and not _others_active(db, user, f):
        raise MfaChangeRefused(
            "This is your only working authenticator. Add another one before turning it off.")
    f.enabled = bool(enabled)
    _sync(db, user)
    db.commit()
    return f


def remove(db: Session, user: User, factor_id: int) -> None:
    f = get_factor(db, user, factor_id)
    if f.confirmed_at is not None and f.enabled and not _others_active(db, user, f):
        raise MfaChangeRefused(
            "This is your only working authenticator. Add another one before removing it.")
    db.delete(f)
    db.flush()
    _sync(db, user)
    db.commit()


# ── an administrator's changes ────────────────────────────────────────

def reset_mfa(db: Session, target: User) -> dict:
    """Remove every second factor so the account enrols afresh at next sign-in.

    - Factors and the legacy column go; the next password sign-in lands on the
      enrolment screen, and until it enrols the session reaches nothing else
      (``core.auth._refuse_until_enrolled``).
    - ``token_version`` is bumped, ending every browser session and refresh
      token the account holds (not its ``onvr_`` API tokens).
    - A single sign-on account whose provider owns MFA has nothing to reset.
      One marked single sign-on that nevertheless has TOTP is a password
      account linked by e-mail before linking was restricted: it becomes a
      local account, so it does not slip out of the enrolment requirement
      (and, for a superuser, stays refused by single sign-on).

    Commits. Returns what changed, for the audit entry.
    """
    rows = factors(db, target, include_pending=True)
    had = bool([f for f in rows if f.confirmed_at is not None and f.enabled])
    if is_sso_account(target) and not had and not target.mfa_enabled:
        raise MfaResetRefused(
            "This account signs in through single sign-on; its identity provider owns two-factor sign-in."
        )
    details = {
        "username": target.username,
        "had_mfa": had,
        "factors_removed": len([f for f in rows if f.confirmed_at is not None]),
        "recovery_codes_removed": recovery_codes_remaining(db, target),
        "converted_to_local": is_sso_account(target),
    }
    for f in rows:
        db.delete(f)
    db.query(UserRecoveryCode).filter(UserRecoveryCode.user_id == target.id).delete(
        synchronize_session=False)
    db.flush()
    if is_sso_account(target):
        target.auth_provider = None
    _sync(db, target)
    target.mfa_enabled = False
    target.token_version = int(target.token_version or 0) + 1
    db.add(target)
    db.commit()
    return details


def admin_set_enabled(db: Session, target: User, factor_id: int, enabled: bool) -> dict:
    """Turn one of another user's factors off or on. Turning off their last
    working one is a reset (sessions end) rather than a quiet lock-out-free
    state. Commits; returns what happened, for the audit entry."""
    f = get_factor(db, target, factor_id)
    if f.confirmed_at is None:
        raise MfaChangeRefused("That authenticator was never set up.")
    if not enabled and f.enabled and not _others_active(db, target, f):
        return {**reset_mfa(db, target), "via": "last_factor_disabled", "factor": f"{factor_id}"}
    f.enabled = bool(enabled)
    _sync(db, target)
    db.commit()
    return {"username": target.username, "factor_id": factor_id, "label": f.label,
            "enabled": bool(enabled)}


def admin_remove(db: Session, target: User, factor_id: int) -> dict:
    """Remove one of another user's factors; removing the last working one is
    a reset. Commits; returns what happened, for the audit entry."""
    f = get_factor(db, target, factor_id)
    if f.confirmed_at is not None and f.enabled and not _others_active(db, target, f):
        return {**reset_mfa(db, target), "via": "last_factor_removed", "factor": f"{factor_id}"}
    label = f.label
    db.delete(f)
    db.flush()
    _sync(db, target)
    db.commit()
    return {"username": target.username, "factor_id": factor_id, "label": label, "removed": True}

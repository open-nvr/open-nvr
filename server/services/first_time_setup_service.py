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

"""First-time-setup token service.

Gates POST /auth/first-time-setup with a one-time token minted at startup, so
nobody can race the operator to claim the freshly-bootstrapped admin account.
The token is a process-local singleton, printed to stdout + audit log once, and
consumed on first successful (constant-time) use.
See V-001 and DESIGN_NOTES: first-time-setup token.
"""

from __future__ import annotations

import hmac
import secrets
import threading
from dataclasses import dataclass

from sqlalchemy.orm import Session

from core.logging_config import auth_logger
from models import User

# 32 random bytes -> ~43 url-safe chars; long enough to defeat online guessing
# at any practical rate limit and short enough to be copy-pasted reliably.
_TOKEN_BYTES = 32

# Process-local state. The Lock is defensive: the FastAPI lifespan hook fires
# once on a single thread, but a forced re-arm via the admin API (planned for
# a later milestone) would race the consume path on the request worker.
_lock = threading.Lock()
_state: "_TokenState | None" = None


@dataclass
class _TokenState:
    """Bundle of the active setup token. Module-private."""

    token: str


def _is_first_time_setup_pending(db: Session) -> bool:
    """True iff at least one user still has ``password_set=False``.

    This is the only signal we use to decide whether to arm a setup token.
    It is intentionally narrow: even if e.g. the database has been wiped and
    no admin exists yet, we don't arm — the init path is responsible for
    creating the admin first, and only then do we decide to arm.
    """
    return (
        db.query(User).filter(User.password_set.is_(False)).first() is not None
    )


def maybe_arm(db: Session) -> str | None:
    """Mint a setup token if first-time setup is pending; otherwise no-op.

    Returns the freshly minted token (so the caller can print/log it once),
    or ``None`` if no token was needed. Called from the FastAPI lifespan
    startup hook in ``main.py``.

    Idempotent: a second call returns ``None`` if a token is already armed,
    so accidental double-bootstrap does not silently rotate the token out
    from under an operator who is mid-copy-paste.
    """
    global _state
    with _lock:
        if _state is not None:
            return None
        if not _is_first_time_setup_pending(db):
            return None
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        _state = _TokenState(token=token)
        # Audit-log the issuance event (NOT the token value). The token is
        # printed to stdout separately by the caller; mixing the value into
        # the audit log would persist it to disk needlessly.
        try:
            auth_logger.log_action(
                "auth.first_time_setup_token_armed",
                message=(
                    "First-time-setup token armed; share with the operator "
                    "via stdout/console only."
                ),
                extra_data={"length_chars": len(token)},
            )
        except Exception:
            # Logging must never block server startup. The token is still
            # returned to the caller for stdout emission.
            pass
        return token


def verify_and_consume(supplied: str | None) -> bool:
    """Constant-time compare against the armed token; consume on success.

    Returns ``True`` iff a token is currently armed and ``supplied`` matches.
    After a successful match the in-memory state is cleared, so the token
    cannot be replayed — even by the same caller — once setup is complete.

    Returns ``False`` for any of: no token armed, empty supplied value, or
    non-matching token. The endpoint should map ``False`` to a generic 403
    so it does not distinguish the three failure modes.
    """
    global _state
    if not supplied:
        return False
    with _lock:
        if _state is None:
            return False
        ok = hmac.compare_digest(_state.token, supplied)
        if ok:
            _state = None
        return ok


def is_armed() -> bool:
    """Test hook: return True iff a token is currently armed."""
    with _lock:
        return _state is not None


def clear() -> None:
    """Test hook: clear the armed token. Not exposed to API surface."""
    global _state
    with _lock:
        _state = None

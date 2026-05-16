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

M0 followup C-1 — closes the bootstrap-race admin-takeover window introduced
by V-001's ``password_set=False`` admin bootstrap. Without this gate, any
unauthenticated caller on the management network could race the legitimate
operator to ``POST /auth/first-time-setup`` and claim the admin account.

How it works
------------
At server startup, after the admin user is created/verified, ``main.py`` calls
:func:`maybe_arm` which:

* checks whether any user in the database is in the ``password_set=False``
  state — i.e. whether first-time setup is actually pending;
* if so, mints a cryptographically random token, stores it in a process-local
  singleton, and prints it to stdout + the audit log exactly once;
* otherwise does nothing.

The ``/auth/first-time-setup`` endpoint requires the token in its payload.
Verification is constant-time (``hmac.compare_digest``). On success the token
is consumed (cleared) so it cannot be replayed even if it leaks afterward.

Why an in-memory singleton (and not the DB / a file)
----------------------------------------------------
The token only needs to live as long as the bootstrap window. Operators that
miss the printed value can simply restart the server to re-arm. Keeping the
token out of the database avoids the migration burden and avoids ever
persisting a credential we want to be ephemeral. The Zenodo paper §4.1
"Secure-by-Design" defaults principle calls for the secure path to be the
default path; this matches.

Threat model coverage
---------------------
* Pre-patch: any LAN attacker who reaches OpenNVR before the operator wins
  the admin role. (Critical.)
* Post-patch: an attacker would additionally need the printed setup token,
  which is only emitted to the operator's own stdout/audit channels.

Residual: an attacker with read access to the server process's stdout or to
the audit log file is a higher-privilege threat already and is out of scope
for this gate; it should be addressed by tier-2/tier-3 file-permission
hardening in the deployment guide.
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

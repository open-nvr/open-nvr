# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Licensed apps — the platform side of ``manifest.entitlement``.

An app that declares ``entitlement: license_key`` cannot be enabled
until an administrator enters a licence key AND the app itself says
the key is valid. Core is deliberately not the judge: it stores the key
(Fernet-encrypted, never returned), asks the app over its contract
surface (``POST {app}/entitlement/verify``, site-key gated like
actions) and records the verdict — status, plan, expiry, message,
limits. The verdict rides the app's live config poll so the app can
feature-gate itself, and the catalog shows it to the administrator.

Free apps (``entitlement: none``) have status ``none`` and are never
asked. Re-verification happens on every key change and on demand
(``POST /apps/{id}/license/verify``); an app that is unreachable when
asked keeps its previous verdict with a message, so a restart never
silently un-licenses a site.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from core.config import settings

logger = logging.getLogger(__name__)

VERIFY_TIMEOUT_S = 10.0
STATUSES = ("none", "unverified", "valid", "invalid")


def entitlement_mode(row) -> str:
    manifest = row.manifest_json if isinstance(row.manifest_json, dict) else {}
    mode = str(manifest.get("entitlement") or "none")
    return mode if mode in ("none", "license_key") else "none"


def requires_license(row) -> bool:
    return entitlement_mode(row) == "license_key"


def entitlement_view(row) -> dict[str, Any]:
    """What the catalog, the app and ``GET /apps`` see — never the key."""
    return {
        "mode": entitlement_mode(row),
        "status": row.entitlement_status or "none",
        "plan": row.entitlement_plan,
        "expires_at": row.entitlement_expires_at.isoformat()
        if row.entitlement_expires_at else None,
        "message": row.entitlement_message or "",
        "limits": row.entitlement_limits or {},
        "checked_at": row.entitlement_checked_at.isoformat()
        if row.entitlement_checked_at else None,
        "has_license_key": bool(row.license_key_encrypted),
    }


def may_enable(row) -> tuple[bool, str]:
    """Whether the app may be switched on, and why not."""
    if not requires_license(row):
        return True, ""
    if row.entitlement_status == "valid":
        expires = row.entitlement_expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)   # SQLite hands back naive
        if expires is not None and expires < datetime.now(UTC):
            return False, "the licence has expired"
        return True, ""
    if not row.license_key_encrypted:
        return False, "this app requires a licence key — enter one in the catalog"
    return False, (row.entitlement_message
                   or "the app has not accepted the licence key")


def _vault():
    from services.credential_vault_service import CredentialVaultService

    return CredentialVaultService(settings)


def store_license_key(row, license_key: str | None) -> None:
    """Set (encrypted) or clear the key; the verdict resets to
    unverified / none until the app is asked."""
    if license_key:
        row.license_key_encrypted = _vault().encrypt_token(license_key.strip())
        row.entitlement_status = "unverified"
    else:
        row.license_key_encrypted = None
        row.entitlement_status = "none"
    row.entitlement_plan = None
    row.entitlement_expires_at = None
    row.entitlement_message = None
    row.entitlement_limits = None
    row.entitlement_checked_at = None


def _stored_key(row) -> str | None:
    if not row.license_key_encrypted:
        return None
    try:
        return _vault().decrypt_token(row.license_key_encrypted)
    except Exception:  # noqa: BLE001
        logger.error("app %s: stored licence key could not be decrypted", row.id)
        return None


async def verify_with_app(row) -> dict[str, Any]:
    """Ask the app; record and return its verdict.

    Transport failure keeps the previous status (an unreachable app is
    not an invalid licence) and records the failure in ``message``.
    """
    now = datetime.now(UTC)
    if not requires_license(row):
        row.entitlement_status = "none"
        row.entitlement_checked_at = now
        return entitlement_view(row)
    key = _stored_key(row)
    if not key:
        row.entitlement_status = "none"
        row.entitlement_message = "no licence key stored"
        row.entitlement_checked_at = now
        return entitlement_view(row)
    headers = ({"X-Internal-Api-Key": settings.internal_api_key}
               if settings.internal_api_key else {})
    try:
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT_S) as client:
            resp = await client.post(f"{row.url.rstrip('/')}/entitlement/verify",
                                     json={"license_key": key}, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        verdict = resp.json()
        if not isinstance(verdict, dict):
            raise RuntimeError("non-object verdict")
    except Exception as exc:  # noqa: BLE001
        logger.warning("app %s: licence verification unreachable: %s", row.id, exc)
        if row.entitlement_status in ("none", "unverified"):
            row.entitlement_status = "unverified"
        row.entitlement_message = f"could not reach the app to verify: {exc}"[:500]
        row.entitlement_checked_at = now
        return entitlement_view(row)

    row.entitlement_status = "valid" if verdict.get("valid") else "invalid"
    row.entitlement_plan = (str(verdict.get("plan") or "")[:100]) or None
    row.entitlement_message = (str(verdict.get("message") or "")[:500]) or None
    limits = verdict.get("limits")
    row.entitlement_limits = limits if isinstance(limits, dict) else None
    expires = verdict.get("expires_at")
    row.entitlement_expires_at = None
    if isinstance(expires, str) and expires:
        try:
            dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            row.entitlement_expires_at = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    row.entitlement_checked_at = now
    return entitlement_view(row)

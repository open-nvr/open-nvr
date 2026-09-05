# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-app credentials — one key per installed app, instead of the site key.

Every SDK app used to boot with the deployment's ``INTERNAL_API_KEY``:
the same secret the detect-pipeline and KAI-C hold. Any app therefore
read every camera, every app's config and live state, and revoking one
app meant rotating the key for the whole stack. That is fine for the
platform's own components and wrong for a catalog of third-party apps.

The model here:

* ``POST /apps/register`` mints an **app key** the first time an app
  registers (or whenever the app says it has none — ``wants_key``), and
  returns it exactly once. Format ``oak_<app-id>_<32 hex>`` — the id is
  in the clear so a key can be routed to its row without a table scan;
  the secret half is what is hashed (SHA-256) and stored.
* Presenting an app key authenticates AS THAT APP: it may read its own
  config and status, register itself again, and read the platform's
  internal camera/event routes **for its own roster** (the cameras the
  operator assigned it — ``Camera.assignments[].skill == app id`` —
  or every camera when no assignment names it, the additive rule of
  docs/CAMERA_ASSIGNMENTS.md). Never another app's config, never the
  detect-pipeline's write routes.
* A superuser can rotate or revoke a key from the registry; the site
  key keeps working for platform components and for onboarding.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

KEY_PREFIX = "oak_"
_KEY_RE = re.compile(r"^oak_([A-Za-z0-9][A-Za-z0-9._-]{0,99})_([0-9a-f]{32})$")


@dataclass(frozen=True)
class AppPrincipal:
    """The caller is an installed app, authenticated by its own key."""

    app_id: str
    #: Keeps ``principal.is_superuser`` checks in the registry routes
    #: honest without isinstance games everywhere: an app is never one.
    is_superuser: bool = False


def looks_like_app_key(value: str | None) -> bool:
    return bool(value) and str(value).startswith(KEY_PREFIX)


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def mint_key(app_id: str) -> tuple[str, str]:
    """``(plain, sha256)`` for a fresh key bound to ``app_id``."""
    plain = f"{KEY_PREFIX}{app_id}_{secrets.token_hex(16)}"
    return plain, hash_key(plain)


def issue_key(db: Session, row) -> str:
    """Mint, store the hash on the InstalledApp row, return the plain key
    (the only time it exists in the clear). Caller commits."""
    plain, digest = mint_key(row.id)
    row.api_key_hash = digest
    row.api_key_issued_at = datetime.now(UTC)
    return plain


def revoke_key(row) -> None:
    row.api_key_hash = None
    row.api_key_issued_at = None


def resolve_app_key(db: Session, supplied: str | None):
    """The InstalledApp a presented key belongs to, or ``None``.

    Constant-time on the hash compare; the app id parsed from the key is
    only a lookup hint — the stored hash is what authenticates."""
    if not supplied:
        return None
    m = _KEY_RE.match(str(supplied).strip())
    if not m:
        return None
    from models import InstalledApp

    row = db.query(InstalledApp).filter(InstalledApp.id == m.group(1)).first()
    if row is None or not row.api_key_hash:
        return None
    if not secrets.compare_digest(row.api_key_hash, hash_key(str(supplied).strip())):
        return None
    return row


def app_skills(row) -> set[str]:
    """The skill names an app answers to in ``Camera.assignments``: what
    its manifest ``provides`` (``license_plate_recognition``) plus its id
    in both spellings (``license-plate-recognition`` / ``_``)."""
    manifest = row.manifest_json or {}
    skills = {str(s) for s in (manifest.get("provides") or []) if s}
    skills.add(row.id)
    skills.add(row.id.replace("-", "_"))
    return skills


def app_camera_ids(db: Session, row) -> set[int] | None:
    """Cameras the operator assigned to this app (``assignments[].skill``
    in :func:`app_skills`), or ``None`` = every camera when no assignment
    names it — the additive rule of docs/CAMERA_ASSIGNMENTS.md, exactly
    as ``cameras_for_skill`` in the SDK reads it."""
    from models import Camera

    skills = app_skills(row)
    rows = (
        db.query(Camera.id, Camera.assignments)
        .filter(Camera.deleted_at.is_(None))
        .all()
    )
    named: set[int] = set()
    for cam_id, assignments in rows:
        for entry in assignments or []:
            if isinstance(entry, dict) and entry.get("skill") in skills:
                named.add(int(cam_id))
                break
    return named or None

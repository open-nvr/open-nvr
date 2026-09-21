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
  internal camera/event routes **for its own roster** — the cameras
  picked for it in its own configuration (``app:<id>`` claims), and
  only those: an app with nothing picked sees nothing
  (docs/CAMERA_ASSIGNMENTS.md). Never another app's config, never the
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
    # The same key opens the apps bus (user = app id); NATS wants bcrypt.
    import bcrypt

    row.nats_password_bcrypt = bcrypt.hashpw(plain.encode("utf-8"),
                                             bcrypt.gensalt(rounds=10)).decode("ascii")
    return plain


def revoke_key(row) -> None:
    row.api_key_hash = None
    row.api_key_issued_at = None
    row.nats_password_bcrypt = None


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
    """The names that identify an app as a skill: its manifest
    ``provides`` plus its id in both spellings
    (``license-plate-recognition`` / ``license_plate_recognition``).

    No longer what an app's roster is built from (that is its picks).
    Used to recognise an assignment row on the camera page that names an
    app, which is refused."""
    manifest = row.manifest_json or {}
    skills = {str(s) for s in (manifest.get("provides") or []) if s}
    skills.add(row.id)
    skills.add(row.id.replace("-", "_"))
    return skills


def app_camera_ids(db: Session, row) -> set[int] | None:
    """The app's roster: the live cameras picked for it in its own
    configuration (``app:<id>`` claims — see
    ``services.skill_assignments.picked_camera_ids``).

    Returns a SET — empty when nothing is picked, which scopes the app to
    nothing. Callers test ``roster is not None``, which still holds: an
    empty set is not None and correctly filters to zero cameras.

    A DISABLED app has no roster at all. The catalog's switch used to
    change nothing an app could feel: it kept its cameras, kept pulling
    their streams and kept driving its adapters, so the only way to stop
    one was ``docker stop``. Enforced here, the app reads no camera it
    was not switched on for — whether or not the app itself is polite
    enough to notice the flag on its config poll.

    It used to be "cameras whose assignment on the camera page names this
    app". That made the camera page the only way to point an app at a
    camera and made every such row both a restriction and a pick — so a
    fresh install's app had nothing, and nowhere in the app to fix it.
    """
    from services.skill_assignments import picked_camera_ids

    if not row.enabled:
        return set()
    return picked_camera_ids(db, row.id)

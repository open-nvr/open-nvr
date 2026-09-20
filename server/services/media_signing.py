# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Signed media URLs (HA-112).

A notification on a phone cannot carry an Authorization header, so Home
Assistant asks core to SIGN a URL for one piece of media (an event image,
an alert photo, a clip) and hands that URL on. The URL is the credential:

    /api/v1/media/s/m1.<payload>.<signature>

* the payload names exactly one resource, an expiry, the signing key id,
  the user who signed it and the API token used (if any);
* the signature is HMAC-SHA256 with a site key kept in site settings;
  rotating keeps the previous key, so rotating twice revokes every URL;
* on every fetch the signer is re-checked: the user must still be active,
  the API token not revoked, the kind's permission still held and the
  camera still visible to them. A URL never outlives the access that
  minted it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services import site_settings

KEYS_KEY = "media_signing_keys"
PREFIX = "m1"
DEFAULT_TTL_S = 24 * 3600
MIN_TTL_S = 60
MAX_TTL_S = 7 * 24 * 3600
MAX_CLIP_S = 3600

#: kind -> permission needed to sign it (besides seeing the camera).
KIND_PERMISSION = {
    "event": "recordings.view",
    "alert_image": "alerts.view",
    "clip": "recordings.view",
}
#: Which TimelineEvent image a signed "event" URL may name.
EVENT_IMAGES = {
    "evidence": "evidence_path",
    "scene": "scene_evidence_path",
    "plate": "plate_evidence_path",
    "plate_frame": "plate_frame_path",
}


class BadToken(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _keys(db: Session) -> dict[str, Any]:
    value = site_settings.get_json(db, KEYS_KEY)
    if isinstance(value, dict) and value.get("current") in (value.get("keys") or {}):
        return value
    fresh = {"current": secrets.token_hex(4), "keys": {}}
    fresh["keys"][fresh["current"]] = secrets.token_hex(32)
    try:
        site_settings.set_json(db, KEYS_KEY, fresh)
    except IntegrityError:
        # Another request created them first (unique key): theirs win.
        db.rollback()
        return site_settings.get_json(db, KEYS_KEY)
    return fresh


def rotate_keys(db: Session) -> str:
    """New current key; the previous one keeps verifying, older ones stop.
    Returns the new key id."""
    keys = _keys(db)
    kid = secrets.token_hex(4)
    kept = {keys["current"]: keys["keys"][keys["current"]], kid: secrets.token_hex(32)}
    site_settings.set_json(db, KEYS_KEY, {"current": kid, "keys": kept})
    return kid


def _mac(secret_hex: str, body: str) -> str:
    return _b64(hmac.new(bytes.fromhex(secret_hex), body.encode("ascii"),
                         hashlib.sha256).digest())


def sign(db: Session, claims: dict[str, Any], ttl_s: int = DEFAULT_TTL_S) -> tuple[str, int]:
    """``(token, expires_at)`` for *claims* (kind, resource, signer)."""
    keys = _keys(db)
    exp = int(time.time()) + max(MIN_TTL_S, min(MAX_TTL_S, int(ttl_s)))
    body = _b64(json.dumps({**claims, "x": exp, "v": keys["current"]},
                           separators=(",", ":"), sort_keys=True).encode())
    return f"{PREFIX}.{body}.{_mac(keys['keys'][keys['current']], body)}", exp


def verify(db: Session, token: str) -> dict[str, Any]:
    """The claims of a valid, unexpired token signed with a live key, or
    BadToken. Says nothing yet about whether the signer may still see it."""
    try:
        prefix, body, sig = token.split(".")
    except ValueError as exc:
        raise BadToken("malformed") from exc
    if prefix != PREFIX:
        raise BadToken("malformed")
    try:
        claims = json.loads(_unb64(body))
    except (ValueError, TypeError) as exc:
        raise BadToken("malformed") from exc
    if not isinstance(claims, dict):
        raise BadToken("malformed")
    # The payload is attacker-controlled JSON on an unauthenticated route:
    # a list or dict for "v" is unhashable (TypeError on the key lookup)
    # and a non-ASCII "sig" makes compare_digest(str, str) raise. Both are
    # just bad tokens (403), not server errors (500).
    kid = claims.get("v")
    if not isinstance(kid, str):
        raise BadToken("malformed")
    secret_hex = (_keys(db).get("keys") or {}).get(kid)
    if secret_hex is None:
        raise BadToken("bad signature")
    try:
        good = hmac.compare_digest(_mac(secret_hex, body), sig)
    except (TypeError, ValueError) as exc:
        raise BadToken("malformed") from exc
    if not good:
        raise BadToken("bad signature")
    if not isinstance(claims.get("x"), int) or claims["x"] < time.time():
        raise BadToken("expired")
    return claims


def signer_principal(db: Session, claims: dict[str, Any]):
    """The user (or API-token principal) that signed, as they are NOW, or
    None if they may no longer use anything."""
    from models import ApiToken, User
    from services import api_tokens

    user = db.query(User).filter(User.id == claims.get("u")).first()
    if user is None or not user.is_active:
        return None
    if claims.get("t") is None:
        return user
    row = db.query(ApiToken).filter(ApiToken.id == claims["t"]).first()
    if row is None or not api_tokens._row_live(row) or row.owner_user_id != user.id:
        return None
    return api_tokens._principal(user, row)


# ── fetch audit, rate-limited (a notification image is fetched repeatedly)

AUDIT_EVERY_S = 600.0
_audited: dict[str, float] = {}


def should_audit(token: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    key = hashlib.sha256(token.encode()).hexdigest()[:24]
    if now - _audited.get(key, -1e9) < AUDIT_EVERY_S:
        return False
    if len(_audited) > 4096:
        _audited.clear()
    _audited[key] = now
    return True

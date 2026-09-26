# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Secrets inside JSON configuration columns: encrypted at rest, masked
on the API.

``integrations.config`` holds an MQTT broker password, an SMTP password,
webhook secrets — and stored them in plain JSON, and ``GET
/api/v1/integrations`` returned them verbatim to the browser. Camera
passwords have been Fernet-encrypted with CREDENTIAL_ENCRYPTION_KEY all
along; these get the same treatment, without every reader of the
column having to know:

* :class:`SealedJSON` is the column type. Writing a dict seals every
  value under a secret-looking key (``password``, ``secret``, ``token``,
  …) as ``enc:<fernet>``; reading unseals it, so ``row.config`` is plain
  to every service exactly as before. A value that was written before
  this existed is left alone on read and sealed on its next write.
* :func:`redact` is what the API returns — every secret replaced by
  :data:`MASK` — and :func:`merge_masked` is how an update that echoes
  the mask back (the edit form does) keeps the stored secret.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)

#: Keys whose values are secrets, wherever they appear at the top level
#: of a config dict. Matching is on the exact key, lower-cased.
SECRET_KEYS = frozenset({
    "password", "secret", "secret_key", "token", "api_key", "access_key",
    "secret_access_key", "webhook_secret", "client_secret", "auth_token",
})
#: What the API shows in place of a secret. Bullets, so it cannot be
#: mistaken for a real value and a form that echoes it back is detected.
MASK = "••••••••"
_PREFIX = "enc:"


def _cipher():
    from cryptography.fernet import Fernet

    from core.config import settings
    return Fernet(settings.credential_encryption_key.encode())


def is_secret_key(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in SECRET_KEYS


def seal(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Encrypt every plain secret value; already-sealed ones stay."""
    if not isinstance(config, dict):
        return config
    out = dict(config)
    cipher = None
    for key, value in config.items():
        if is_secret_key(key) and isinstance(value, str) and value \
                and not value.startswith(_PREFIX):
            cipher = cipher or _cipher()
            out[key] = _PREFIX + cipher.encrypt(value.encode()).decode()
    return out


def unseal(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decrypt sealed values. One that will not decrypt (the key was
    rotated) is left as it is — unusable, but the row still loads and
    the operator can re-enter it — and logged."""
    if not isinstance(config, dict):
        return config
    out = dict(config)
    cipher = None
    for key, value in config.items():
        if isinstance(value, str) and value.startswith(_PREFIX):
            try:
                cipher = cipher or _cipher()
                out[key] = cipher.decrypt(value[len(_PREFIX):].encode()).decode()
            except Exception:  # noqa: BLE001
                logger.warning("integration config: %r could not be decrypted — was "
                               "CREDENTIAL_ENCRYPTION_KEY rotated? Re-enter it.", key)
    return out


def needs_sealing(config: dict[str, Any] | None) -> bool:
    """A stored (raw) config still carrying a plain secret."""
    return isinstance(config, dict) and any(
        is_secret_key(k) and isinstance(v, str) and v and not v.startswith(_PREFIX)
        for k, v in config.items())


def redact(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """The config as the API shows it: secrets masked, empties left."""
    if not isinstance(config, dict):
        return config
    return {k: (MASK if is_secret_key(k) and isinstance(v, str) and v else v)
            for k, v in config.items()}


def merge_masked(stored: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any] | None:
    """An update whose secret is the mask means "keep what you have"."""
    if not isinstance(incoming, dict):
        return incoming
    out = dict(incoming)
    stored = stored if isinstance(stored, dict) else {}
    for key, value in list(out.items()):
        if is_secret_key(key) and value == MASK:
            if key in stored:
                out[key] = stored[key]
            else:
                del out[key]
    return out


class SealedJSON(TypeDecorator):
    """A JSON column whose secret values are encrypted at rest."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return seal(value)

    def process_result_value(self, value, dialect):
        return unseal(value)

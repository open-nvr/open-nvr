# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Site-wide settings kept in the generic ``security_settings`` key/JSON table.

Several modules each hand-rolled their own get/set over ``SecuritySetting``.
New site-wide state (the site id, the recording-pause flag, site mode,
media-signing keys) goes through this one helper instead.

Values are JSON. Keys are at most 50 characters (the column width).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import SecuritySetting

MAX_KEY_LEN = 50

#: Stable identity of this OpenNVR deployment (a UUID string). Lets a client
#: that talks to several sites, such as Home Assistant, tell them apart even
#: when a site's URL changes.
SITE_ID_KEY = "site_id"
SITE_NAME_KEY = "site_name"
DEFAULT_SITE_NAME = "OpenNVR"

#: Site-wide opt-in allowing recording to be paused (HA-108). Off by default:
#: this is a recorder, and recording is always on unless an admin allows it.
RECORDING_PAUSE_KEY = "recording_pause_enabled"


def _check_key(key: str) -> None:
    if not key or len(key) > MAX_KEY_LEN:
        raise ValueError(f"setting key must be 1..{MAX_KEY_LEN} chars: {key!r}")


def get_json(db: Session, key: str, default: Any = None) -> Any:
    """The stored value for *key*, or *default* if unset or unreadable."""
    _check_key(key)
    row = db.query(SecuritySetting).filter(SecuritySetting.key == key).first()
    if row is None:
        return default
    try:
        return json.loads(row.json_value)
    except (TypeError, ValueError):
        return default


def set_json(db: Session, key: str, value: Any) -> None:
    """Store *value* (JSON-serialisable) under *key* and commit."""
    _check_key(key)
    encoded = json.dumps(value)
    row = db.query(SecuritySetting).filter(SecuritySetting.key == key).first()
    if row is None:
        db.add(SecuritySetting(key=key, json_value=encoded))
    else:
        row.json_value = encoded
    db.commit()


def get_site_id(db: Session) -> str:
    """This deployment's stable id, created on first use."""
    site_id = get_json(db, SITE_ID_KEY)
    if isinstance(site_id, str) and site_id:
        return site_id
    site_id = str(uuid.uuid4())
    try:
        set_json(db, SITE_ID_KEY, site_id)
    except IntegrityError:
        # Another request created it first (unique key): theirs wins.
        db.rollback()
        return get_json(db, SITE_ID_KEY)
    return site_id


def get_site_name(db: Session) -> str:
    name = get_json(db, SITE_NAME_KEY)
    return name if isinstance(name, str) and name.strip() else DEFAULT_SITE_NAME


def recording_pause_enabled(db: Session) -> bool:
    return get_json(db, RECORDING_PAUSE_KEY, False) is True

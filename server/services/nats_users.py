# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-app credentials on the event bus.

Apps used to join NATS with the deployment's ``INTERNAL_API_KEY`` — the
same token the platform components use — and could therefore publish
and subscribe to everything. Now every app connects to the **apps bus**
(the ``nats-apps`` leaf server, ``nats/apps.conf``) as its own user:

* user     = the app id
* password = the app's own key (``oak_<app-id>_…``), bcrypt-hashed here
* permissions = derived from the manifest, below

Core renders those users into one NATS config fragment
(``settings.nats_users_conf``, a file on a volume the ``nats-apps``
container includes and reloads when it changes — see
``nats/apps-entrypoint.sh``). Nothing here talks to NATS; it writes a
file, atomically, whenever a key is issued, rotated or revoked, and at
start-up.

Permissions (v1 — the manifest is the source of truth):

* subscribe: the platform's inference broadcasts (``opennvr.inference.>``,
  ``opennvr.tier0.>``) and the alert stream (``opennvr.alerts.>``) for
  every app; a domain-event family only when the manifest asked for it
  (``requires_scopes: ["events:plate.recognized"]`` →
  ``opennvr.events.plate.recognized.>``). This is where
  ``requires_scopes`` stops being an audit row and becomes a wall.
* publish: the app's OWN alert subjects (``opennvr.alerts.app.<id>.>``),
  and the domain-event tree only for apps that ``provides`` a skill
  (they are producers by declaration). ``_INBOX.>`` both ways for
  request/reply.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

APPS_BUS_USER_PREFIX = ""          # user name == app id
_SAFE_SUBJECT = re.compile(r"^[A-Za-z0-9_.*>-]+$")
_SCOPE_RE = re.compile(r"^events:([a-z0-9_]+(?:\.[a-z0-9_]+)*)$")

#: What every app may read: the platform's own broadcasts. Roster
#: scoping of frames/events for people lives on the HTTP routes; the
#: bus carries detections for all cameras, as it always has.
BASE_SUBSCRIBE = ("opennvr.inference.>", "opennvr.tier0.>", "opennvr.alerts.>", "_INBOX.>")
BASE_PUBLISH = ("_INBOX.>",)


def _subject_ok(subject: str) -> bool:
    return bool(subject) and bool(_SAFE_SUBJECT.match(subject)) and len(subject) <= 200


def _alert_token(value: str) -> str:
    """The SDK sanitises alert-subject tokens the same way (alerts.py)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", value) or "_"


def app_permissions(app_id: str, manifest: dict[str, Any] | None) -> dict[str, list[str]]:
    """``{"publish": [...], "subscribe": [...]}`` for one app."""
    manifest = manifest if isinstance(manifest, dict) else {}
    subscribe: list[str] = list(BASE_SUBSCRIBE)
    publish: list[str] = list(BASE_PUBLISH)

    # The subject pattern the manifest declares it consumes — honoured
    # when it stays inside the families above (a manifest cannot widen
    # itself onto the domain-event tree; scopes do that).
    declared = manifest.get("subscribes")
    for pattern in ([declared] if isinstance(declared, str) else declared or []):
        if not isinstance(pattern, str) or not _subject_ok(pattern):
            continue
        if pattern.startswith(("opennvr.inference.", "opennvr.tier0.", "opennvr.alerts.")):
            if pattern not in subscribe:
                subscribe.append(pattern)

    for scope in manifest.get("requires_scopes") or []:
        m = _SCOPE_RE.match(str(scope).strip())
        if m:
            subject = f"opennvr.events.{m.group(1)}.>"
            if subject not in subscribe:
                subscribe.append(subject)

    publish.append(f"opennvr.alerts.app.{_alert_token(app_id)}.>")
    if manifest.get("provides"):
        publish.append("opennvr.events.>")
    return {"publish": publish, "subscribe": subscribe}


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_users_conf(apps: Iterable[Any]) -> str:
    """The NATS ``authorization`` block for every app that holds a key.
    ``apps`` are InstalledApp rows (need ``id``, ``manifest_json``,
    ``nats_password_bcrypt``)."""
    entries: list[str] = []
    for row in apps:
        pw = getattr(row, "nats_password_bcrypt", None)
        if not pw or not re.match(r"^[A-Za-z0-9._-]{1,100}$", str(row.id)):
            continue
        perms = app_permissions(str(row.id), getattr(row, "manifest_json", None))
        entries.append(
            "    {\n"
            f"      user: {_q(str(row.id))}\n"
            f"      password: {_q(str(pw))}\n"
            "      permissions: {\n"
            f"        publish: {{ allow: [{', '.join(_q(s) for s in perms['publish'])}] }}\n"
            f"        subscribe: {{ allow: [{', '.join(_q(s) for s in perms['subscribe'])}] }}\n"
            "      }\n"
            "    }"
        )
    if not entries:
        # NATS rejects an empty users list; a user nobody can match keeps
        # the file valid until the first app is issued a key.
        entries.append(
            "    { user: \"_no_apps_yet\", password: \"$2b$10$"
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\", "
            "permissions: { publish: { deny: [\">\"] }, subscribe: { deny: [\">\"] } } }"
        )
    body = ",\n".join(entries)
    return (
        "# Generated by OpenNVR core (services/nats_users.py) — do not edit.\n"
        "# One user per installed app: user = app id, password = bcrypt(app key).\n"
        "authorization {\n"
        "  users: [\n"
        f"{body}\n"
        "  ]\n"
        "}\n"
    )


def write_users_conf(db, path: str | os.PathLike | None = None) -> bool:
    """Render every installed app into the users file, atomically.
    ``False`` (and a log line) when the path is unset or unwritable —
    the bus keeps its previous users; nothing else fails."""
    from core.config import settings
    from models import InstalledApp

    configured = str(path or settings.nats_users_conf or "").strip()
    if not configured:
        return False
    target = Path(configured)
    rows = db.query(InstalledApp).all()
    text = render_users_conf(rows)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".users.", suffix=".conf", dir=str(target.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o640)
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning("could not write the apps-bus users file %s: %s", target, exc)
        return False
    logger.info("apps-bus users file written: %d app(s) → %s",
                sum(1 for r in rows if getattr(r, "nats_password_bcrypt", None)), target)
    return True

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Plain-language footage search through installed apps (HA-502).

``GET /search?q=`` answers structured questions from core's own tables.
For descriptive ones ("a red truck at the dock yesterday") it also asks
every enabled app that declares a ``search`` action taking a ``query``:
today that is ``examples/footage-search``, which indexes detector labels
and captioner text off the bus. Reusing it keeps one index and one parser;
core adds scoping, so a caller only ever sees rows for its own cameras.

A provider answers ``{"results": [{"camera", "when", "labels", "caption"}]}``
(footage-search's action response). Each row becomes a ``footage`` result:
``{kind, at, camera_id, labels, caption, source}``. A provider that fails or
times out is skipped and named in ``errors``; search never fails because of
one.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: One provider gets this long; Assist waits on the answer.
PROVIDER_TIMEOUT_S = 8.0


def providers(db: Session) -> list[Any]:
    """Enabled apps declaring ``search(query, ...)``."""
    from models import InstalledApp

    found = []
    for row in db.query(InstalledApp).filter(InstalledApp.enabled.is_(True)).all():
        for action in (row.manifest_json or {}).get("actions") or []:
            if not isinstance(action, dict) or action.get("name") != "search":
                continue
            names = {p.get("name") for p in action.get("params") or [] if isinstance(p, dict)}
            if "query" in names:
                found.append((row, "limit" in names))
                break
    return found


def _when(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def to_results(app_id: str, answer: Any, *, scope: set[int] | None,
               camera_id: int | None, label: str | None,
               from_: datetime | None, to: datetime | None) -> list[dict]:
    """A provider's rows as scoped ``footage`` results; rows on cameras the
    caller can't see, or that name no camera, are dropped."""
    from services.camera_scope import camera_id_from_handle

    rows = answer.get("results") if isinstance(answer, dict) else None
    out: list[dict] = []
    start, end = _aware(from_), _aware(to)
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        cam = camera_id_from_handle(row.get("camera"))
        if cam is None or (scope is not None and cam not in scope):
            continue
        if camera_id is not None and cam != camera_id:
            continue
        at = _when(row.get("when"))
        if at is None or (start is not None and at < start) or (end is not None and at >= end):
            continue
        labels = row.get("labels")
        labels = labels.split() if isinstance(labels, str) else \
            [str(x) for x in labels] if isinstance(labels, list) else []
        if label and label.strip().lower() not in {x.lower() for x in labels}:
            continue
        caption = row.get("caption")
        out.append({"kind": "footage", "at": at.isoformat(), "camera_id": cam,
                    "labels": labels, "caption": str(caption)[:500] if caption else None,
                    "source": app_id})
    return out


async def search(db: Session, user, query: str, *, limit: int, scope: set[int] | None,
                 camera_id: int | None, label: str | None, from_: datetime | None,
                 to: datetime | None) -> tuple[list[dict], list[str], list[str]]:
    """``(results, sources asked, sources that failed)``. The audit row
    names the apps asked, never the query (operators' words stay out of
    the log, as with app actions)."""
    from routers.apps import call_app_action
    from services.audit_service import write_audit_log

    found = providers(db)
    if not found:
        return [], [], []
    if scope is None:  # every camera: every camera that still exists
        from models import Camera

        scope = {cid for (cid,) in db.query(Camera.id).filter(Camera.deleted_at.is_(None))}

    async def ask(row, takes_limit: bool):
        params: dict[str, Any] = {"query": query}
        if takes_limit:
            params["limit"] = min(max(limit, 1), 200)
        return await asyncio.wait_for(
            call_app_action(db, row, "search", params, user, timeout=PROVIDER_TIMEOUT_S),
            PROVIDER_TIMEOUT_S + 1)

    answers = await asyncio.gather(*(ask(r, lim) for r, lim in found), return_exceptions=True)
    results: list[dict] = []
    asked, failed = [], []
    for (row, _), answer in zip(found, answers, strict=True):
        asked.append(row.id)
        if isinstance(answer, BaseException):
            failed.append(row.id)
            logger.info("footage search: %s did not answer: %s", row.id,
                        getattr(answer, "detail", answer))
            continue
        results.extend(to_results(row.id, answer, scope=scope, camera_id=camera_id,
                                  label=label, from_=from_, to=to))
    write_audit_log(db, action="search.footage", user_id=user.id, entity_type="app",
                    details={"apps": asked, "failed": failed, "results": len(results)})
    return results, asked, failed

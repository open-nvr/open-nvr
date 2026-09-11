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

"""
The AI adapter catalog — which models a deployment can install.

Apps have had ``apps_index.yml`` and a one-click install since the
beginning. Adapters had neither: a third party with a conformant model
had nowhere to be listed, and an operator looking for a better plate
reader had nowhere to look. ``server/config/adapters_index.yml`` is that
catalog and this router serves it.

The organising idea is the one the contract is built on: **an app asks
for a TASK, never for an adapter by name.** An app's
``requires_tasks: [license_plate_recognition]`` is satisfied by ANY
adapter advertising that task, so the catalog's job is to let an
operator see the choices for a task and swap between them without
touching a single app.

Entries are generated from an adapter's own ``/capabilities``
(``opennvr-adapter listing``), so a listing cannot claim a task the
adapter does not advertise or a permission it does not request.
"""
import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/adapters", tags=["adapters"])

ADAPTERS_INDEX_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "adapters_index.yml"
)

#: How the listing describes its relationship to the project — not a
#: quality ranking. A community adapter that conforms is as installable
#: as a first-party one; the contract is what is checked either way.
KNOWN_TIERS = {"first_party", "community"}


class AdapterModelInfo(BaseModel):
    """What the model is, from the adapter's own ``/capabilities``."""

    framework: str = ""
    size_mb: float | None = None
    modalities_in: list[str] = []
    modalities_out: list[str] = []
    #: Whether the adapter computes a weights fingerprint at all. KAI-C
    #: skips drift detection without one, so an operator should be able
    #: to see that before installing.
    fingerprinted: bool = False


class AdapterPermissions(BaseModel):
    """What the adapter asks the operator to grant."""

    gpu: bool = False
    network_egress: list[str] = []
    host_filesystem: list[str] = []


class AdapterScheduling(BaseModel):
    max_inflight: int = 1
    fair_queuing: str = "per_camera"


class AdapterIndexEntry(BaseModel):
    """One installable adapter."""

    id: str
    name: str
    summary: str
    version: str
    image: str
    tasks_advertised: list[str]
    tier: str = "community"
    model: AdapterModelInfo = AdapterModelInfo()
    permissions: AdapterPermissions = AdapterPermissions()
    scheduling: AdapterScheduling = AdapterScheduling()
    supports_stream: bool = False
    license: str = ""
    vendor: str = ""
    model_card_url: str | None = None
    docs_url: str | None = None
    source: str | None = None
    contact: str | None = None


def load_adapters_index(path: Path | None = None) -> list[AdapterIndexEntry]:
    """Parse the catalog, skipping entries that do not validate.

    One malformed entry must degrade to "that card is missing", never to
    a 500 that takes the whole catalog down — the same rule the app
    index follows. ``scripts/validate_adapters_index.py`` is what makes
    a malformed entry fail in CI instead of silently disappearing here.
    """
    source = path or ADAPTERS_INDEX_PATH
    try:
        raw = yaml.safe_load(source.read_text()) or []
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("adapters_index.yml could not be read: %s", exc)
        return []
    if not isinstance(raw, list):
        logger.warning("adapters_index.yml: expected a list of entries")
        return []

    entries: list[AdapterIndexEntry] = []
    for item in raw:
        try:
            entries.append(AdapterIndexEntry.model_validate(item))
        except ValidationError as exc:
            logger.warning(
                "adapters_index.yml: skipping invalid entry %r — run "
                "scripts/validate_adapters_index.py (%s)",
                (item or {}).get("id") if isinstance(item, dict) else item,
                exc.errors()[0].get("msg") if exc.errors() else exc,
            )
    return entries


@router.get("/index")
async def get_adapters_index(
    task: str | None = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The adapter catalog — every model a deployment can install.

    ``?task=`` narrows to the adapters that satisfy one task convention,
    which is the question an operator actually has: *what can read
    plates on this deployment, and what would I be trading?*

    The response groups by task as well as listing the adapters, so the
    UI can show the choices for a capability side by side. Nothing here
    contacts an adapter; it is the curated index, not a live probe —
    ``GET /api/v1/ai-models/adapters-metrics`` is the live view of what
    is actually registered and running.
    """
    entries = load_adapters_index()
    if task:
        wanted = task.strip().lower()
        entries = [e for e in entries if wanted in
                   [t.lower() for t in e.tasks_advertised]]

    by_task: dict[str, list[str]] = {}
    for entry in entries:
        for advertised in entry.tasks_advertised:
            by_task.setdefault(advertised, []).append(entry.id)

    return {
        "adapters": [entry.model_dump(mode="json") for entry in entries],
        # An app names a task; this is the map from that task to the
        # adapters that can serve it.
        "tasks": {name: sorted(ids) for name, ids in sorted(by_task.items())},
        "count": len(entries),
    }

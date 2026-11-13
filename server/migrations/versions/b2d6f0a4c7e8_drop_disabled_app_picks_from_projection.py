# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""cameras.assignments — drop the picks held by DISABLED apps (#539).

Disabling an app flipped ``installed_apps.enabled`` and nothing else.
Its picks stayed in ``skill_assignments`` AND stayed in the
``cameras.assignments`` projection, so every consumer that reads the
projection kept working: a disabled ANPR app went on buying plate OCR
on every vehicle, both in Tier-1 dispatch and in core's visit
enrichment. The app went quiet; the compute it switched on did not.

``project_camera`` now skips a disabled app's claims, but a projection
is only recomputed when something writes to it — so an install that
disabled an app before this release would keep the stale union until
the operator happened to toggle that app again. This heals those rows
once, using the same union rules as the projection: group by skill,
union the per-claim labels, and a claim carrying no labels means "no
restriction" and wins.

Nothing is deleted. The picks themselves stay exactly where they were,
so enabling the app restores the operator's camera selection.

Revision ID: b2d6f0a4c7e8
Revises: a1c8f4b27d93
Create Date: 2026-09-23
"""
import json

from alembic import op
from sqlalchemy import bindparam, text

revision = "b2d6f0a4c7e8"
down_revision = "a1c8f4b27d93"
branch_labels = None
depends_on = None

APP_CONSUMER_PREFIX = "app:"


def _labels_of(params):
    """The claim's label narrowing, cleaned like the projection cleans
    it. ``None`` = this claim asked for no restriction."""
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (TypeError, ValueError):
            return None
    if not isinstance(params, dict):
        return None
    labels = params.get("labels")
    if not isinstance(labels, list):
        return None
    cleaned = sorted({str(s).strip().lower() for s in labels if str(s).strip()})
    return cleaned or None


def upgrade() -> None:
    bind = op.get_bind()
    disabled = {
        f"{APP_CONSUMER_PREFIX}{row[0]}"
        for row in bind.execute(
            text("SELECT id FROM installed_apps WHERE enabled = false")
        )
    }
    if not disabled:
        return

    # Only the cameras a disabled app actually PICKED can be wrong, so
    # ask for exactly those — and then read every claim on each of them
    # once, since a camera's union is the union of all its consumers.
    picked = bind.execute(
        text("SELECT DISTINCT camera_id FROM skill_assignments "
             "WHERE consumer IN :disabled").bindparams(
                 bindparam("disabled", expanding=True)),
        {"disabled": sorted(disabled)},
    ).fetchall()
    camera_ids = sorted({int(row[0]) for row in picked})
    if not camera_ids:
        return
    claims = bind.execute(
        text("SELECT camera_id, skill, consumer, params FROM skill_assignments "
             "WHERE camera_id IN :cids ORDER BY camera_id, skill").bindparams(
                 bindparam("cids", expanding=True)),
        {"cids": camera_ids},
    ).fetchall()
    by_camera: dict[int, list] = {cid: [] for cid in camera_ids}
    for camera_id, skill, consumer, params in claims:
        by_camera[int(camera_id)].append((skill, consumer, params))
    for camera_id in camera_ids:
        merged = {}
        for skill, consumer, params in by_camera[camera_id]:
            if consumer in disabled:
                continue
            labels = _labels_of(params)
            if skill not in merged:
                merged[skill] = set(labels) if labels is not None else None
            elif merged[skill] is not None:
                merged[skill] = (
                    None if labels is None else merged[skill] | set(labels)
                )
        projection = []
        for skill in sorted(merged):
            entry = {"skill": skill}
            if merged[skill] is not None:
                entry["labels"] = sorted(merged[skill])
            projection.append(entry)
        bind.execute(
            text("UPDATE cameras SET assignments = :value WHERE id = :cid"),
            {"value": json.dumps(projection) if projection else None,
             "cid": camera_id},
        )


def downgrade() -> None:
    # One-way: the pre-heal projection is not recorded, and rebuilding it
    # would switch plate OCR back on for apps the operator turned off.
    pass

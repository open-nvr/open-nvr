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

"""add skill assignments (RFC-0002 Phase 2)

The declarative camera-assignment table: one row per (skill, camera,
consumer) claim, union semantics per decision 8. ``Camera.assignments``
becomes this table's projection (recomputed on every write), so Tier-0
reconcile, the SDK's ``cameras_for_skill`` and the internal endpoint
keep reading exactly what they read today.

Backfill: every entry already in ``cameras.assignments`` becomes a
``consumer='operator'`` row (the camera-settings editor was the only
writer until now), so nothing an operator assigned is lost and the
projection recompute is a no-op for untouched cameras.

Revision ID: cc99dd00ee11
Revises: bb88cc99dd00
Create Date: 2026-08-29 00:00:00.000000

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cc99dd00ee11"
down_revision: str | None = "bb88cc99dd00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_assignments",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("skill", sa.String(length=100), nullable=False, index=True),
        sa.Column(
            "camera_id", sa.Integer(),
            sa.ForeignKey("cameras.id"), nullable=False,
        ),
        sa.Column("consumer", sa.String(length=100), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_skill_assignment", "skill_assignments",
        ["skill", "camera_id", "consumer"], unique=True,
    )
    op.create_index(
        "ix_skill_assignments_camera", "skill_assignments", ["camera_id"],
    )

    # Backfill: existing operator-written camera.assignments entries.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, assignments FROM cameras "
                "WHERE assignments IS NOT NULL")
    ).fetchall()
    for camera_id, raw in rows:
        entries = raw if isinstance(raw, list) else None
        if entries is None and isinstance(raw, (str, bytes)):
            try:
                entries = json.loads(raw)
            except ValueError:
                entries = None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("skill"):
                continue
            params = None
            labels = entry.get("labels")
            if isinstance(labels, list) and labels:
                params = json.dumps({"labels": labels})
            conn.execute(
                sa.text(
                    "INSERT INTO skill_assignments "
                    "(skill, camera_id, consumer, params) "
                    "VALUES (:skill, :camera_id, 'operator', :params)"
                ),
                {"skill": str(entry["skill"]), "camera_id": camera_id,
                 "params": params},
            )


def downgrade() -> None:
    op.drop_index("ix_skill_assignments_camera", table_name="skill_assignments")
    op.drop_index("uq_skill_assignment", table_name="skill_assignments")
    op.drop_table("skill_assignments")

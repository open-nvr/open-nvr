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

"""add events table — the canonical event & evidence store (RFC-0001 C1)

One row per object VISIT (a Tier-0 track lifecycle), not per frame: the
grain a human question uses ("who came between 3 and 4?" = a handful of
visits, not thousands of detections). Evidence (the track's best frame,
selected at capture time) is stored as a small JPEG on disk next to the
recordings; the row carries its path. Purely additive; existing stores
(camera_events, footage-search) migrate onto this in later phases.

Revision ID: aa11bb22cc33
Revises: d5e6f7a8b9c0
Create Date: 2026-08-07 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa11bb22cc33"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "camera_id", sa.Integer(), sa.ForeignKey("cameras.id"), nullable=False
        ),
        # Producer family: tier0 | camera | app | adapter. One table for all
        # of them is the point (RFC-0001 Challenge 1).
        sa.Column("source", sa.String(length=30), nullable=False),
        # track (an object's visit) | alarm | alert
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("label", sa.String(length=60), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("track_id", sa.String(length=40), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        # Filled when the recording resolver can anchor the visit to a file.
        sa.Column("recording_ref", sa.String(length=500), nullable=True),
        # Relative path (under the evidence root) of the best-frame JPEG.
        sa.Column("evidence_path", sa.String(length=500), nullable=True),
        # PR-C: fast-plate-ocr enrichment lands here.
        sa.Column("plate_text", sa.String(length=32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.create_index("ix_events_id", "events", ["id"])
    op.create_index("ix_events_cam_start", "events", ["camera_id", "started_at"])
    op.create_index("ix_events_label", "events", ["label"])
    op.create_index("ix_events_source", "events", ["source"])


def downgrade() -> None:
    op.drop_index("ix_events_source", table_name="events")
    op.drop_index("ix_events_label", table_name="events")
    op.drop_index("ix_events_cam_start", table_name="events")
    op.drop_index("ix_events_id", table_name="events")
    op.drop_table("events")

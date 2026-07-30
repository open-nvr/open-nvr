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

"""add camera_events table

Phase 5 of multi-vendor camera configuration. Stores camera-native alarms
(motion / tamper / video-loss / IO) pulled from a device's event stream, so
operators get a history alongside the live dashboard feed. Purely additive.

Revision ID: f2b8d3e6a9c1
Revises: e4a1c7b9d2f3
Create Date: 2026-07-04 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b8d3e6a9c1"
down_revision: str | None = "e4a1c7b9d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "camera_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "camera_id", sa.Integer(), sa.ForeignKey("cameras.id"), nullable=False
        ),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("event_state", sa.String(length=20), nullable=True),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.create_index("ix_camera_events_id", "camera_events", ["id"])
    op.create_index("ix_camera_events_camera_id", "camera_events", ["camera_id"])
    op.create_index(
        "ix_camera_event_cam_time", "camera_events", ["camera_id", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_camera_event_cam_time", table_name="camera_events")
    op.drop_index("ix_camera_events_camera_id", table_name="camera_events")
    op.drop_index("ix_camera_events_id", table_name="camera_events")
    op.drop_table("camera_events")

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

"""add system_events table

Host-level alert history (disk pressure, CPU/RAM, purge outcomes) parallel to
camera_events. A separate table rather than a nullable camera_id: camera_events
is hot and every consumer assumes camera scope. Purely additive.

Revision ID: ff66aa77bb88
Revises: ee55ff66aa77
Create Date: 2026-08-17 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ff66aa77bb88"
down_revision: str | None = "ee55ff66aa77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("event_state", sa.String(length=20), nullable=True),
        sa.Column(
            "severity", sa.String(length=10), nullable=False, server_default="warning"
        ),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("data", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.create_index("ix_system_events_id", "system_events", ["id"])
    op.create_index(
        "ix_system_event_type_time", "system_events", ["event_type", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_system_event_type_time", table_name="system_events")
    op.drop_index("ix_system_events_id", table_name="system_events")
    op.drop_table("system_events")

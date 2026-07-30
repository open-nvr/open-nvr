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

"""add camera_capabilities table

Phase 0 of multi-vendor camera configuration. Caches, per camera, what its
device driver can read/drive (ONVIF + vendor APIs): supported settings areas,
resolved ONVIF service endpoints, refreshed device metadata, and a probe
result/timestamp. The settings UI reads this to render only the tabs/controls
a given device actually supports. Purely additive; existing rows unaffected.

Revision ID: e4a1c7b9d2f3
Revises: a7c31e9f4d28
Create Date: 2026-07-04 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4a1c7b9d2f3"
down_revision: str | None = "e3a7c92d4f18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "camera_capabilities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "camera_id",
            sa.Integer(),
            sa.ForeignKey("cameras.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("driver_name", sa.String(length=50), nullable=True),
        sa.Column("manufacturer", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("firmware_version", sa.String(length=100), nullable=True),
        sa.Column("onvif_endpoints", sa.JSON(), nullable=True),
        sa.Column("supported_areas", sa.JSON(), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column(
            "probe_result",
            sa.String(length=20),
            nullable=False,
            server_default="not_probed",
        ),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_camera_capabilities_id", "camera_capabilities", ["id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_camera_capabilities_id", table_name="camera_capabilities"
    )
    op.drop_table("camera_capabilities")

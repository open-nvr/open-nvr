# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add occupancy_heatmaps (occupancy.heatmap.v1 history)

Revision ID: ff88aa99bb00
Revises: dd11ee22ff33
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa

revision = "ff88aa99bb00"
down_revision = "dd11ee22ff33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "occupancy_heatmaps",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("camera_id", sa.Integer(), nullable=False, index=True),
        sa.Column("hour_start", sa.DateTime(timezone=True), nullable=False,
                  index=True),
        sa.Column("cols", sa.Integer(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("cells", sa.JSON(), nullable=False),
        sa.Column("frames", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_occupancy_heatmaps_camera_hour", "occupancy_heatmaps",
        ["camera_id", "hour_start"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_occupancy_heatmaps_camera_hour",
                  table_name="occupancy_heatmaps")
    op.drop_table("occupancy_heatmaps")

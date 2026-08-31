# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add occupancy_samples (occupancy.changed.v1 history)

Revision ID: ee11ff22aa33
Revises: dd00ee11ff22
Create Date: 2026-08-31
"""
from alembic import op
import sqlalchemy as sa

revision = "ee11ff22aa33"
down_revision = "dd00ee11ff22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "occupancy_samples",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("camera_id", sa.Integer(), nullable=False, index=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False,
                  server_default="normal"),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False,
                  index=True),
    )


def downgrade() -> None:
    op.drop_table("occupancy_samples")

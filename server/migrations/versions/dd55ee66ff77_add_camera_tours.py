# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""camera_tours — Live View tours for display boards

A tour is an ordered list of steps (a camera set, a layout and how long
to show it) that a display board cycles through. Additive only: one new
table, nothing existing is touched.

Revision ID: dd55ee66ff77
Revises: cc44dd55ee66
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

revision = "dd55ee66ff77"
down_revision = "cc44dd55ee66"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    # create_all() may already have built this table from the model on a
    # stack that booted once; a bare create_table would then raise and
    # roll back every other migration in the same batch.
    if _has_table("camera_tours"):
        return

    op.create_table(
        "camera_tours",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        # sa.true(), not "1": Postgres refuses an integer default on a boolean.
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_camera_tours_id", "camera_tours", ["id"])


def downgrade() -> None:
    if _has_table("camera_tours"):
        op.drop_table("camera_tours")

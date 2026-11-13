# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""camera_masks table

Parts of a camera's picture Tier-0 must ignore: motion masks (the pixels
under them never count as motion) and object masks (a detection standing
inside one is dropped). Drawn by the operator, read by the detect-pipeline
from the camera-agent sources listing.

Revision ID: a7c3e9f1b2d4
Revises: ee66ff77aa88
Create Date: 2026-11-01
"""
from alembic import op
import sqlalchemy as sa

revision = "a7c3e9f1b2d4"
down_revision = "ee66ff77aa88"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: init_db's create_all may have created the table on a
    # database that booted with the new models.
    inspector = sa.inspect(op.get_bind())
    if "camera_masks" not in inspector.get_table_names():
        op.create_table(
            "camera_masks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("camera_id", sa.Integer(),
                      sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(60), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False, server_default="motion"),
            sa.Column("polygon", sa.JSON(), nullable=False),
            sa.Column("labels", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("camera_id", "name", name="uq_camera_mask_name"),
        )
        op.create_index("ix_camera_masks_id", "camera_masks", ["id"])
        op.create_index("ix_camera_masks_camera_id", "camera_masks", ["camera_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "camera_masks" in inspector.get_table_names():
        op.drop_table("camera_masks")

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""camera_zones table and events.zone_ids

Named areas of a camera's picture, and the zones each visit passed
through (computed at ingest from the detect-pipeline's path). Home
Assistant builds per-zone occupancy sensors from them.

Revision ID: f4b8d2a6c9e1
Revises: e2a9c4f7b1d5
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision = "f4b8d2a6c9e1"
down_revision = "e2a9c4f7b1d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: init_db's create_all and the additive-column self-heal may
    # have created these on a database that booted with the new models.
    inspector = sa.inspect(op.get_bind())
    if "camera_zones" not in inspector.get_table_names():
        op.create_table(
            "camera_zones",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("camera_id", sa.Integer(),
                      sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(60), nullable=False),
            sa.Column("polygon", sa.JSON(), nullable=False),
            sa.Column("labels", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("camera_id", "name", name="uq_camera_zone_name"),
        )
        op.create_index("ix_camera_zones_id", "camera_zones", ["id"])
        op.create_index("ix_camera_zones_camera_id", "camera_zones", ["camera_id"])
    if "zone_ids" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("events")}:
        with op.batch_alter_table("events") as batch:
            batch.add_column(sa.Column("zone_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "zone_ids" in {c["name"] for c in inspector.get_columns("events")}:
        with op.batch_alter_table("events") as batch:
            batch.drop_column("zone_ids")
    if "camera_zones" in inspector.get_table_names():
        op.drop_table("camera_zones")

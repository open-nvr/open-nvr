# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""cameras.detection_enabled: turn Tier-0 detection off per camera

Home Assistant's camera "motion detection" switch (and anyone else) can
stop object detection on one camera without pausing it: the camera keeps
streaming and recording, and detect-pipeline drops it from its workers
(the camera-agent roster sends ``analyze: false``).

Nullable, and NULL means ON, so every existing camera keeps detecting.

Revision ID: e2a9c4f7b1d5
Revises: d7f3a1b9c2e4
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision = "e2a9c4f7b1d5"
down_revision = "d7f3a1b9c2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: core/database._backfill_additive_columns may already have
    # added the column on a database that booted with the new models.
    inspector = sa.inspect(op.get_bind())
    if "detection_enabled" not in {c["name"] for c in inspector.get_columns("cameras")}:
        with op.batch_alter_table("cameras") as batch:
            batch.add_column(sa.Column("detection_enabled", sa.Boolean(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "detection_enabled" in {c["name"] for c in inspector.get_columns("cameras")}:
        with op.batch_alter_table("cameras") as batch:
            batch.drop_column("detection_enabled")

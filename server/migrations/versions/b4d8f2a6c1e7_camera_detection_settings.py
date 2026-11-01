# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""cameras.detection_settings

Per-camera overrides of the detect-pipeline's global DETECT_* knobs:
analysis fps, motion gate, spawn score, stationary interval and per-label
object filters. NULL = inherit everything.

Revision ID: b4d8f2a6c1e7
Revises: a7c3e9f1b2d4
Create Date: 2026-11-01
"""
from alembic import op
import sqlalchemy as sa

revision = "b4d8f2a6c1e7"
down_revision = "a7c3e9f1b2d4"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return insp.has_table(table) and column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    # create_all() or the additive self-heal may have added it already.
    if _has_column("cameras", "detection_settings"):
        return
    with op.batch_alter_table("cameras") as batch:
        batch.add_column(sa.Column("detection_settings", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _has_column("cameras", "detection_settings"):
        with op.batch_alter_table("cameras") as batch:
            batch.drop_column("detection_settings")

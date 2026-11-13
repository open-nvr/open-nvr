# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""camera_tours.settings — how a wall draws and reacts to alarms

Per-tour display settings (fit or fill the tiles) and the alarm takeover
rules (which alerts bring a camera to the front, for how long). Nullable:
NULL means the defaults, so existing tours are unchanged.

Revision ID: ee66ff77aa88
Revises: dd55ee66ff77
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = "ee66ff77aa88"
down_revision = "dd55ee66ff77"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return insp.has_table(table) and column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    # create_all() may have built camera_tours from the newer model already,
    # column included; adding it again would abort the whole batch.
    if _has_column("camera_tours", "settings"):
        return
    with op.batch_alter_table("camera_tours") as batch:
        batch.add_column(sa.Column("settings", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _has_column("camera_tours", "settings"):
        with op.batch_alter_table("camera_tours") as batch:
            batch.drop_column("settings")

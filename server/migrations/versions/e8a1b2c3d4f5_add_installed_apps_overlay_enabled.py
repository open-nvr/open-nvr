# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""installed_apps.overlay_enabled — may this app draw on the live video?

Apps can publish ``overlay.boxes.v1`` (plate boxes from ANPR, zones
from occupancy). Whether those boxes are DRAWN over the operator's live
view is the operator's call, per app, off by default: an app painting
on the screen is a privilege granted in the catalog, not a side effect
of being installed.

Revision ID: e8a1b2c3d4f5
Revises: d7f1c4b820ae
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa

revision = "e8a1b2c3d4f5"
down_revision = "d7f1c4b820ae"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("installed_apps") as batch:
        batch.add_column(
            sa.Column(
                "overlay_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("installed_apps") as batch:
        batch.drop_column("overlay_enabled")

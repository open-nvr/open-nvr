# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add installed_apps.sdk_version

Revision ID: e8a1c4d27b95
Revises: d7f3a2c9e1b4
Create Date: 2026-09-06
"""
from alembic import op
import sqlalchemy as sa

revision = "e8a1c4d27b95"
down_revision = "d7f3a2c9e1b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("installed_apps") as b:
        b.add_column(sa.Column("sdk_version", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("installed_apps") as b:
        b.drop_column("sdk_version")

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add installed_apps.egress_allow

Revision ID: b4d8e2a91c7f
Revises: f2b7d9e04c61
Create Date: 2026-09-06
"""
from alembic import op
import sqlalchemy as sa

revision = "b4d8e2a91c7f"
down_revision = "f2b7d9e04c61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("installed_apps") as b:
        b.add_column(sa.Column("egress_allow", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("installed_apps") as b:
        b.drop_column("egress_allow")

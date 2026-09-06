# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add installed_apps.nats_password_bcrypt

Revision ID: f2b7d9e04c61
Revises: e8a1c4d27b95
Create Date: 2026-09-06
"""
from alembic import op
import sqlalchemy as sa

revision = "f2b7d9e04c61"
down_revision = "e8a1c4d27b95"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("installed_apps") as b:
        b.add_column(sa.Column("nats_password_bcrypt", sa.String(80), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("installed_apps") as b:
        b.drop_column("nats_password_bcrypt")

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add installed_apps licence + entitlement columns

Revision ID: d7f3a2c9e1b4
Revises: c4e7a9d1f2b3
Create Date: 2026-09-05
"""
from alembic import op
import sqlalchemy as sa

revision = "d7f3a2c9e1b4"
down_revision = "c4e7a9d1f2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("installed_apps") as b:
        b.add_column(sa.Column("license_key_encrypted", sa.Text(), nullable=True))
        b.add_column(sa.Column("entitlement_status", sa.String(16), nullable=False,
                               server_default="none"))
        b.add_column(sa.Column("entitlement_plan", sa.String(100), nullable=True))
        b.add_column(sa.Column("entitlement_expires_at", sa.DateTime(timezone=True),
                               nullable=True))
        b.add_column(sa.Column("entitlement_message", sa.String(500), nullable=True))
        b.add_column(sa.Column("entitlement_limits", sa.JSON(), nullable=True))
        b.add_column(sa.Column("entitlement_checked_at", sa.DateTime(timezone=True),
                               nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("installed_apps") as b:
        for col in ("entitlement_checked_at", "entitlement_limits",
                    "entitlement_message", "entitlement_expires_at",
                    "entitlement_plan", "entitlement_status",
                    "license_key_encrypted"):
            b.drop_column(col)

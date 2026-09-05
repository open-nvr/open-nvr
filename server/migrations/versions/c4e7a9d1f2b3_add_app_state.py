# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add app_state (durable per-app key/value for the SDK's client.state)

Revision ID: c4e7a9d1f2b3
Revises: b3d26b58dc7e
Create Date: 2026-09-05
"""
from alembic import op
import sqlalchemy as sa

revision = "c4e7a9d1f2b3"
down_revision = "b3d26b58dc7e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_state",
        sa.Column("app_id", sa.String(100),
                  sa.ForeignKey("installed_apps.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("app_state")

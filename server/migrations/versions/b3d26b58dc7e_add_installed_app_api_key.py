# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add installed_apps.api_key_hash / api_key_issued_at (per-app credentials)

Revision ID: b3d26b58dc7e
Revises: ff88aa99bb00
Create Date: 2026-09-05
"""
from alembic import op
import sqlalchemy as sa

revision = "b3d26b58dc7e"
down_revision = "ff88aa99bb00"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("installed_apps",
                  sa.Column("api_key_hash", sa.String(64), nullable=True))
    op.add_column("installed_apps",
                  sa.Column("api_key_issued_at", sa.DateTime(timezone=True),
                            nullable=True))
    op.create_index("ix_installed_apps_api_key_hash", "installed_apps",
                    ["api_key_hash"])


def downgrade() -> None:
    op.drop_index("ix_installed_apps_api_key_hash", table_name="installed_apps")
    op.drop_column("installed_apps", "api_key_issued_at")
    op.drop_column("installed_apps", "api_key_hash")

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""api_tokens: long-lived, scoped API credentials for non-browser clients

The Home Assistant integration needs a credential that is not a user's
password or a 30-minute JWT: revocable, limited to what it needs, and never
able to exceed the user who created it. Only the SHA-256 of the secret is
stored.

Revision ID: d7f3a1b9c2e4
Revises: c4d8e2f1a9b3
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision = "d7f3a1b9c2e4"
down_revision = "c4d8e2f1a9b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: create_all builds a missing table on boot before migrations
    # run on a create_all-bootstrapped database.
    if "api_tokens" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("camera_ids", sa.JSON(), nullable=True),
        sa.Column("allowed_cidrs", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_tokens_id", "api_tokens", ["id"])
    op.create_index("ix_api_tokens_prefix", "api_tokens", ["prefix"], unique=True)
    op.create_index("ix_api_tokens_owner_user_id", "api_tokens", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_tokens_owner_user_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_prefix", table_name="api_tokens")
    op.drop_index("ix_api_tokens_id", table_name="api_tokens")
    op.drop_table("api_tokens")

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""site setup links, and approving a replaced site agent

A setup link (https://<server>/site-setup#<id>.<secret>) lets the person at
a site set the agent up without typing a code or a certificate fingerprint:
`site_setup_tokens` keeps, per link, only values derived from its secret.
Two nullable `sites` columns hold a re-enrolled agent until an admin
approves it, when the site already has cameras (their passwords go to the
new agent only after that).

Revision ID: e3b5d7f9a1c2
Revises: d8e0f2a4b6c8
Create Date: 2026-11-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e3b5d7f9a1c2"
down_revision: str | None = "d8e0f2a4b6c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_setup_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_id", sa.String(16), nullable=False),
        sa.Column("enrol_hash", sa.String(64), nullable=False),
        sa.Column("proof_key_enc", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_ip", sa.String(45), nullable=True),
        sa.Column("used_hostname", sa.String(200), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_site_setup_tokens_token_id", "site_setup_tokens", ["token_id"], unique=True)
    op.create_index("ix_site_setup_tokens_site_id", "site_setup_tokens", ["site_id"])
    with op.batch_alter_table("sites") as batch:
        batch.add_column(sa.Column("approval_pending_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("pending_agent", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sites") as batch:
        batch.drop_column("pending_agent")
        batch.drop_column("approval_pending_at")
    op.drop_index("ix_site_setup_tokens_site_id", table_name="site_setup_tokens")
    op.drop_index("ix_site_setup_tokens_token_id", table_name="site_setup_tokens")
    op.drop_table("site_setup_tokens")

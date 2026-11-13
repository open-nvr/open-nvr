"""add users.token_version, auth_provider, oidc_issuer, oidc_subject

token_version is bumped on every password change or reset and carried in
tokens as ``ver``, so a reset ends the old sessions. auth_provider /
oidc_issuer / oidc_subject identify an account that signs in through the
site's OpenID Connect provider.

Revision ID: cc44dd55ee66
Revises: bb33cc44dd55
Create Date: 2026-10-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cc44dd55ee66"
down_revision: str | None = "bb33cc44dd55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("auth_provider", sa.String(length=16), nullable=True))
    op.add_column("users", sa.Column("oidc_issuer", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("oidc_subject", sa.String(length=255), nullable=True))
    op.create_index("ix_users_oidc_subject", "users", ["oidc_subject"])


def downgrade() -> None:
    op.drop_index("ix_users_oidc_subject", table_name="users")
    op.drop_column("users", "oidc_subject")
    op.drop_column("users", "oidc_issuer")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "token_version")

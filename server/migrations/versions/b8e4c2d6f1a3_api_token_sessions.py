# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""api_tokens.parent_id: short-lived session tokens for dashboard cards

A token can mint a session token for a dashboard card: read-only, at most
ten minutes, never more cameras than its parent, revoked with the parent.
NULL for every existing token (none of them is a session).

Revision ID: b8e4c2d6f1a3
Revises: f4b8d2a6c9e1
Create Date: 2026-09-19
"""
from alembic import op
import sqlalchemy as sa

revision = "b8e4c2d6f1a3"
down_revision = "f4b8d2a6c9e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: core/database._backfill_additive_columns may already have
    # added the column (without its key and index) on a database that booted
    # with the new models, so each part is added only where missing.
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("api_tokens")}
    fks = {tuple(f["constrained_columns"]) for f in inspector.get_foreign_keys("api_tokens")}
    indexes = {i["name"] for i in inspector.get_indexes("api_tokens")}
    with op.batch_alter_table("api_tokens") as batch:
        if "parent_id" not in columns:
            batch.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        if ("parent_id",) not in fks:
            batch.create_foreign_key("fk_api_tokens_parent_id", "api_tokens",
                                     ["parent_id"], ["id"], ondelete="CASCADE")
        if "ix_api_tokens_parent_id" not in indexes:
            batch.create_index("ix_api_tokens_parent_id", ["parent_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "parent_id" in {c["name"] for c in inspector.get_columns("api_tokens")}:
        with op.batch_alter_table("api_tokens") as batch:
            names = {i["name"] for i in inspector.get_indexes("api_tokens")}
            if "ix_api_tokens_parent_id" in names:
                batch.drop_index("ix_api_tokens_parent_id")
            fks = {f["name"] for f in inspector.get_foreign_keys("api_tokens")}
            if "fk_api_tokens_parent_id" in fks:
                batch.drop_constraint("fk_api_tokens_parent_id", type_="foreignkey")
            batch.drop_column("parent_id")

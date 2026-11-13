# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""user_recovery_codes: one-time codes for signing in without the phone.

Only sha256 hashes are stored (the codes carry 80 random bits). Nobody has
any yet: users generate a set from Settings > Two-factor sign-in, and new
enrolments get one on the spot.

create_all runs before Alembic at startup, so the table is created only
when missing.

Revision ID: f8b4d0a2c6e3
Revises: e7a3c9f1b5d2
Create Date: 2026-11-13
"""
import sqlalchemy as sa
from alembic import op

revision = "f8b4d0a2c6e3"
down_revision = "e7a3c9f1b5d2"
branch_labels = None
depends_on = None

TABLE = "user_recovery_codes"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("code_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        )
    existing = {ix["name"] for ix in sa.inspect(bind).get_indexes(TABLE)}
    for col in ("id", "user_id"):
        name = f"ix_{TABLE}_{col}"
        if name not in existing:
            op.create_index(name, TABLE, [col])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)

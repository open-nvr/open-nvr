"""add trusted_devices (app-layer device firewall)

Revision ID: a1b2c3d4e5f6
Revises: f2b8d3e6a9c1
Create Date: 2026-07-21

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f2b8d3e6a9c1"
branch_labels = None
depends_on = None

_STATUS = sa.Enum("approved", "pending", "blocked", name="devicestatus")


def upgrade() -> None:
    bind = op.get_bind()
    _STATUS.create(bind, checkfirst=True)
    op.create_table(
        "trusted_devices",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column(
            "status",
            _STATUS,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("user_agent", sa.String(length=400), nullable=True),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "approved_by",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "auto_enrolled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_trusted_devices_ip_address",
        "trusted_devices",
        ["ip_address"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_trusted_devices_ip_address", table_name="trusted_devices")
    op.drop_table("trusted_devices")
    _STATUS.drop(op.get_bind(), checkfirst=True)

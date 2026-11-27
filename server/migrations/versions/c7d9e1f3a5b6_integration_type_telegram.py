# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""integration type: telegram

A Telegram bot (its token and a chat): alerts and workflow messages to a
chat. On PostgreSQL the type column is a native enum, so the value is
added to it; SQLite stores the name as text and needs nothing.

Revision ID: c7d9e1f3a5b6
Revises: b4e6a8c0d2f3
Create Date: 2026-11-27
"""
from __future__ import annotations

from alembic import op

revision: str = "c7d9e1f3a5b6"
down_revision: str | None = "b4e6a8c0d2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # ADD VALUE cannot run inside a transaction block before PostgreSQL 12.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE integrationtype ADD VALUE IF NOT EXISTS 'TELEGRAM'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum; an unused value is harmless.
    pass

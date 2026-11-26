# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""integration type: home_assistant

A Home Assistant integration (its URL and a long-lived token), what a
workflow's ``home_assistant`` action calls (RFC #620). On PostgreSQL the
type column is a native enum, so the value is added to it; SQLite stores
the name as text and needs nothing.

Revision ID: b4e6a8c0d2f3
Revises: c1a9e4f7b2d8
Create Date: 2026-11-25
"""
from __future__ import annotations

from alembic import op

revision: str = "b4e6a8c0d2f3"
down_revision: str | None = "c1a9e4f7b2d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # ADD VALUE cannot run inside a transaction block before PostgreSQL 12.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE integrationtype ADD VALUE IF NOT EXISTS 'HOME_ASSISTANT'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum; an unused value is harmless.
    pass

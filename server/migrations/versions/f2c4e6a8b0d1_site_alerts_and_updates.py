# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""sites: per-site alert and agent-update switches

Three booleans on ``sites``, all on by default (existing sites get them on
too): raise an alert when the site is offline, raise one when one of its
cameras is down, and let its agent update itself to signed releases.

Revision ID: f2c4e6a8b0d1
Revises: e3b5d7f9a1c2
Create Date: 2026-11-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f2c4e6a8b0d1"
down_revision: str | None = "e3b5d7f9a1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sites") as batch:
        batch.add_column(sa.Column("alert_offline", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("alert_camera_down", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("agent_updates", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("sites") as batch:
        batch.drop_column("agent_updates")
        batch.drop_column("alert_camera_down")
        batch.drop_column("alert_offline")

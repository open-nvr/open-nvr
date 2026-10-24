"""add cameras.analytics_source and cameras.native_event_types

Per-camera detection source: NULL/'opennvr' (OpenNVR's own detection,
native events are history only), 'camera' (the camera's analytics
become alerts, Tier-0 paused on it) or 'both'. native_event_types is an
optional allowlist of the alert types native events may become.

Revision ID: aa22bb33cc44
Revises: e4b9c2d6f0a3
Create Date: 2026-10-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa22bb33cc44"
down_revision: str | None = "e4b9c2d6f0a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column("analytics_source", sa.String(length=8), nullable=True))
    op.add_column("cameras", sa.Column("native_event_types", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("cameras", "native_event_types")
    op.drop_column("cameras", "analytics_source")

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""index events.evidence_path — the same-frame lookup of enrichment reuse

Revision ID: a7c3e9f1b5d2
Revises: d5e1f7a9c3b4
Create Date: 2026-11-13
"""
from alembic import op

revision = "a7c3e9f1b5d2"
down_revision = "d5e1f7a9c3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_events_evidence_path", "events", ["evidence_path"])


def downgrade() -> None:
    op.drop_index("ix_events_evidence_path", table_name="events")

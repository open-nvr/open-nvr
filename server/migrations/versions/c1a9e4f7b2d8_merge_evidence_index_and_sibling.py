# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""merge heads: the events.evidence_path index and its sibling

Two migrations landed on main in parallel on 2026-11-13, each revising
the same tip. This revision joins them so the graph has one head again.

Revision ID: c1a9e4f7b2d8
Revises: a7c3e9f1b5d2, f8b4d0a2c6e3
Create Date: 2026-11-13
"""

revision = "c1a9e4f7b2d8"
down_revision = ("a7c3e9f1b5d2", "f8b4d0a2c6e3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

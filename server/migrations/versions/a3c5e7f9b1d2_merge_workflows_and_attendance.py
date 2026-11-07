# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""merge heads: main's workflows and attendance

Both added a migration off the same base (b4d8f2a6c1e7): Agentic
Workflows (f1a7c3e9b2d4) and attendance (e7a1c3d5f9b2). A merge keeps
both parents, so a database that already ran either one runs only the
other on upgrade; re-parenting attendance onto workflows would have told
a database at e7a1c3d5f9b2 that it had the workflow tables too. They
touch different tables, so this revision only joins them.

Revision ID: a3c5e7f9b1d2
Revises: f1a7c3e9b2d4, e7a1c3d5f9b2
Create Date: 2026-11-05
"""

revision = "a3c5e7f9b1d2"
down_revision = ("f1a7c3e9b2d4", "e7a1c3d5f9b2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

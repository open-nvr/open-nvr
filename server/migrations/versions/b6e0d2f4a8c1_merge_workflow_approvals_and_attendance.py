# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""merge heads: main's workflow approvals and attendance

Main added workflow approvals (a2b8d4f0c6e1) on top of workflows
(f1a7c3e9b2d4), which attendance's own merge (a3c5e7f9b1d2) already
joins. A database at a3c5e7f9b1d2 has workflows but not approvals; this
merge keeps both parents, so on upgrade it runs only the approvals
migration. Re-parenting a3c5e7f9b1d2 instead would tell such a database
that it already has the approvals table. They touch different tables,
so this revision only joins them.

Revision ID: b6e0d2f4a8c1
Revises: a2b8d4f0c6e1, a3c5e7f9b1d2
Create Date: 2026-11-07
"""

revision = "b6e0d2f4a8c1"
down_revision = ("a2b8d4f0c6e1", "a3c5e7f9b1d2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

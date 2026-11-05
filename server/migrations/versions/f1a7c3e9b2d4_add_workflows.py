# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add workflows and workflow_versions

The workflow side of Agentic Workflows (RFC #620, section 8): a workflow,
its state, and every saved version of its file. Runs (workflow_runs) keep
the workflow id and version they ran as plain columns.

Revision ID: f1a7c3e9b2d4
Revises: b4d8f2a6c1e7
Create Date: 2026-11-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f1a7c3e9b2d4"
down_revision: str | None = "b4d8f2a6c1e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=8), nullable=False, server_default="test"),
        sa.Column("needs_attention", sa.String(length=300), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("moves_things", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("workflow_id", sa.Integer(),
                  sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("yaml", sa.Text(), nullable=False),
        sa.Column("needs", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("workflow_id", "version", name="uq_workflow_version"),
    )


def downgrade() -> None:
    op.drop_table("workflow_versions")
    op.drop_table("workflows")

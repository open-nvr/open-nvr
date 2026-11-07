# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add workflow_approvals

A workflow's Ask a person step, waiting for an answer or given one
(RFC #620): its own record and permission, so acknowledging alerts never
clears it, and where the run carries on, so a reply after a restart
resumes it.

Revision ID: a2b8d4f0c6e1
Revises: f1a7c3e9b2d4
Create Date: 2026-11-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a2b8d4f0c6e1"
down_revision: str | None = "f1a7c3e9b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_approvals",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("run_id", sa.Integer(),
                  sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("step_id", sa.Integer(),
                  sa.ForeignKey("workflow_run_steps.id", ondelete="CASCADE"), nullable=True),
        sa.Column("step_path", sa.String(length=120), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("who", sa.String(length=120), nullable=False),
        sa.Column("replies", sa.JSON(), nullable=False),
        sa.Column("default_reply", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="open",
                  index=True),
        sa.Column("reply", sa.String(length=120), nullable=True),
        sa.Column("answered_by", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resume", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("workflow_approvals")

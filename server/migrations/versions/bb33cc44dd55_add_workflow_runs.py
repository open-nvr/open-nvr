# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add workflow_runs, workflow_run_steps, workflow_tool_calls

The run side of Agentic Workflows (RFC #620, §8): a run, its steps
written one by one, and an AI step's model and tool calls. The
workflows / workflow_versions tables come with the engine.

Revision ID: bb33cc44dd55
Revises: aa22bb33cc44
Create Date: 2026-10-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "bb33cc44dd55"
down_revision: str | None = "aa22bb33cc44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("workflow_id", sa.Integer(), nullable=True, index=True),
        sa.Column("workflow_version", sa.Integer(), nullable=True),
        sa.Column("correlation_id", sa.String(length=32), nullable=False, unique=True),
        sa.Column("chain_depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("test", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("trigger", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="running", index=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "workflow_run_steps",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("run_id", sa.Integer(),
                  sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("step_path", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("inputs", sa.JSON(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("outcome", sa.String(length=64), nullable=True),
        sa.Column("fields", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("confidence_source", sa.String(length=32), nullable=True),
        sa.Column("self_reported", sa.Float(), nullable=True),
        sa.Column("unsure_reason", sa.String(length=32), nullable=True),
        sa.Column("proposed", sa.String(length=64), nullable=True),
        sa.Column("rule", sa.Integer(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("state", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", "seq", name="uq_workflow_run_step_seq"),
    )
    op.create_table(
        "workflow_tool_calls",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("run_step_id", sa.Integer(),
                  sa.ForeignKey("workflow_run_steps.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("args_asked", sa.JSON(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("workflow_tool_calls")
    op.drop_table("workflow_run_steps")
    op.drop_table("workflow_runs")

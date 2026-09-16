# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""guard_screenings — every entry screening, not only the failures

Compliance is complete scans over ALL screenings. A store of only the
alerts can say how many complaints there were but never what share of
the day went right, so the clean ones are rows here too.

Revision ID: b7e4a1c9d302
Revises: a3f19c7d2e60
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

revision = "b7e4a1c9d302"
down_revision = "a3f19c7d2e60"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    # create_all() runs before migrations at startup and builds any table
    # whose model exists, so on a stack that has booted once this table is
    # already here. A bare create_table then raises DuplicateTable, and
    # because the upgrade is ONE transaction that rollback takes every
    # other migration in the batch with it — which is how the alert
    # columns in a3f19c7d2e60 silently failed to apply.
    if _has_table("guard_screenings"):
        return

    op.create_table(
        "guard_screenings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(40), nullable=False),
        sa.Column("camera_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("coverage", sa.Float(), nullable=True),
        sa.Column("order_score", sa.Float(), nullable=True),
        sa.Column("steps_done", sa.Text(), nullable=True),
        sa.Column("steps_missing", sa.Text(), nullable=True),
        # sa.false(), not text("0"): Postgres refuses an integer default
        # on a boolean column (42804), and this file's own docstring
        # explains what a failed migration costs here — env.py runs the
        # whole batch in ONE transaction, so aborting takes the alerts
        # migration down with it, app_alerts never gains alert_type /
        # images, and every query against it errors until the next boot.
        sa.Column("flagged", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("ended_by", sa.String(30), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("engaged_s", sa.Float(), nullable=True),
        sa.Column("guard_key", sa.String(64), nullable=True),
        sa.Column("guard_name", sa.String(100), nullable=True),
        sa.Column("images", sa.Text(), nullable=True),
        sa.Column("alert_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.create_index("ix_guard_screenings_session_id", "guard_screenings",
                    ["session_id"], unique=True)
    op.create_index("ix_guard_screenings_camera_id", "guard_screenings",
                    ["camera_id"])
    op.create_index("ix_guard_screenings_ended_at", "guard_screenings",
                    ["ended_at"])
    op.create_index("ix_guard_screenings_verdict", "guard_screenings",
                    ["verdict"])
    op.create_index("ix_guard_screenings_guard_key", "guard_screenings",
                    ["guard_key"])
    op.create_index("ix_guard_screenings_cam_ts", "guard_screenings",
                    ["camera_id", "ended_at"])


def downgrade() -> None:
    if _has_table("guard_screenings"):
        op.drop_table("guard_screenings")

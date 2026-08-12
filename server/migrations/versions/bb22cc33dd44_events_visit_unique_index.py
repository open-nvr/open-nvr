# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""events: unique visit index — ingest retries become idempotent

Promised in PR #213's follow-ups: the tier0 poster retries on transient
failures, and without a uniqueness guarantee a retry that raced a success
creates a duplicate visit row. One visit = one (camera, track, start), so
that triple is the natural idempotency key. NULL track_ids (future alarm/
alert rows) are exempt by SQL semantics — NULLs never collide.

Revision ID: bb22cc33dd44
Revises: aa11bb22cc33
Create Date: 2026-08-12 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "bb22cc33dd44"
down_revision: str | None = "aa11bb22cc33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_events_visit",
        "events",
        ["camera_id", "track_id", "started_at"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_events_visit", table_name="events")

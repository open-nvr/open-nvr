# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""index events by when the read happened

The plate aggregations range over coalesce(observed_at, started_at) —
"when was this vehicle here" (#451). That expression cannot use
ix_events_cam_start: the planner matched camera_id and then tested the
range against every row the camera ever recorded, and events holds every
track, people included.

    before  SEARCH events USING INDEX ix_events_cam_start
                (camera_id=? AND started_at>?)
    after   SEARCH events USING INDEX ix_events_cam_start (camera_id=?)

An expression index restores the seek. Not partial (WHERE plate_text IS
NOT NULL) even though every caller filters on plate_text: two of them
filter by equality to a plate rather than IS NOT NULL, and whether a
planner infers one from the other is a per-dialect subtlety this does not
need to bet on.

Revision ID: d7f1c4b820ae
Revises: c5e9a3b7d104
Create Date: 2026-09-09
"""
from alembic import op
import sqlalchemy as sa

revision = "d7f1c4b820ae"
down_revision = "c5e9a3b7d104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_events_cam_seen", "events",
        ["camera_id", sa.text("coalesce(observed_at, started_at)")],
    )


def downgrade() -> None:
    op.drop_index("ix_events_cam_seen", table_name="events")

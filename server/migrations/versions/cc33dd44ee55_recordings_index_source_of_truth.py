# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""recordings: become the indexed source of truth for listings

The recordings table was write-only: every listing endpoint bypassed it for
MediaMTX's /list API or full filesystem walks. Making it the read path needs:

* ``(camera_id, start_time)`` — the workhorse index behind every day/timeline
  query and aggregate;
* a UNIQUE ``(camera_id, file_path)`` — webhook retries and the filesystem
  reconciler upsert on this key, so double-inserts are structurally impossible;
* ``start_time`` alone — retention deletes oldest-first across all cameras;
* ``codec`` — recorded once at segment-complete so playback never has to probe
  the file to learn it's H.265;
* ``is_flagged`` — retention's ``protect_flagged`` was a documented no-op
  because there was no column to check. Now there is;
* ``source`` — 'webhook' or 'reconciler', for observability of how a row
  entered the index;
* ``created_by_id`` becomes nullable — rows were attributed to an arbitrary
  ``User.first()`` because the column demanded one. System recordings have no
  meaningful owner.

Revision ID: cc33dd44ee55
Revises: bb22cc33dd44
Create Date: 2026-08-14 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cc33dd44ee55"
down_revision: str | None = "bb22cc33dd44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recordings",
        sa.Column("codec", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "recordings",
        sa.Column(
            "is_flagged",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "recordings",
        sa.Column("source", sa.String(length=10), nullable=True),
    )
    op.alter_column(
        "recordings",
        "created_by_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    # Duplicate (camera_id, file_path) rows would break the unique index; the
    # legacy webhook could produce them on retries. Keep the newest row.
    op.execute(
        """
        DELETE FROM recordings a
        USING recordings b
        WHERE a.camera_id = b.camera_id
          AND a.file_path = b.file_path
          AND a.id < b.id
        """
    )

    op.create_index(
        "ix_recordings_camera_start",
        "recordings",
        ["camera_id", "start_time"],
    )
    op.create_index(
        "uq_recordings_camera_file",
        "recordings",
        ["camera_id", "file_path"],
        unique=True,
    )
    op.create_index(
        "ix_recordings_start_time",
        "recordings",
        ["start_time"],
    )


def downgrade() -> None:
    op.drop_index("ix_recordings_start_time", table_name="recordings")
    op.drop_index("uq_recordings_camera_file", table_name="recordings")
    op.drop_index("ix_recordings_camera_start", table_name="recordings")
    op.alter_column(
        "recordings",
        "created_by_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_column("recordings", "source")
    op.drop_column("recordings", "is_flagged")
    op.drop_column("recordings", "codec")

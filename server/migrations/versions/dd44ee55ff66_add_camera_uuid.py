# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""add cameras.uuid (stable identity that survives a DB rebuild)

The numeric camera id is an auto-increment sequence that restarts at 1 on a
fresh database, so it cannot identify a camera across a DB wipe. The uuid is
stamped into each camera's on-disk recordings directory so footage is never
silently re-attributed to whichever camera later reuses the same numeric id.

Revision ID: dd44ee55ff66
Revises: cc33dd44ee55
Create Date: 2026-08-16

"""

from __future__ import annotations

import uuid as _uuid

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dd44ee55ff66"
down_revision: str | None = "cc33dd44ee55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column("uuid", sa.String(length=36), nullable=True))

    # Backfill existing rows one by one in Python — portable across Postgres
    # and SQLite (no gen_random_uuid() dependency).
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id FROM cameras WHERE uuid IS NULL")).fetchall()
    for (cam_id,) in rows:
        conn.execute(
            sa.text("UPDATE cameras SET uuid = :u WHERE id = :i"),
            {"u": str(_uuid.uuid4()), "i": cam_id},
        )

    op.create_index("ix_cameras_uuid", "cameras", ["uuid"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_cameras_uuid", table_name="cameras")
    op.drop_column("cameras", "uuid")

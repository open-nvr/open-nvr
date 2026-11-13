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

"""add events.plate_frame_path — the frame the plate crop was cut from

``plate_evidence_path`` (aa88bb99cc00) is the plate rectangle, cut out of
an OCR attempt and correct by construction: those pixels ARE the number
on the row. The rest of that attempt was thrown away, and the rest is a
crop of the vehicle the plate belongs to.

That matters because a visit is not reliably one vehicle. When track
association merges a departing car with the one arriving behind it, the
visit keeps reading plates from car A while its best-thumbnail frame
settles on car B — observed live on 2026-09-03, a 107-second "car" visit
on a road where cars pass in seconds, showing a black Audi captioned
with the number of the car in front of it. Neither evidence_path nor
scene_evidence_path can be trusted to show the right car; this can.

Additive and nullable: NULL means the attempt bytes were not kept (every
row read before this column) and readers fall back to the vehicle frame.
No backfill is possible — the attempt is discarded when enrichment
returns, which is the whole reason the column exists.

Revision ID: cc00dd11ee22
Revises: bb99cc00dd11
Create Date: 2026-09-03 11:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cc00dd11ee22"
down_revision: str | None = "bb99cc00dd11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("plate_frame_path", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("events", "plate_frame_path")

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

"""add events.scene_evidence_path — the whole frame behind the best crop

A visit stored exactly one image: a crop of the detection box plus a
quarter-box margin. That framing is deliberate and good at its job — the
subject stays dominant — and it is precisely why it cannot answer the
operator's second question. "Is that my car?" needs the car; "what lane
was it in, who was it next to, was the gate open?" needs the scene, and
no margin setting gives you both without ruining the first.

So Tier-0 now posts a SECOND JPEG per visit: the whole frame the best
crop was taken from, downscaled to ~1280px on its long edge and encoded
at the moment the best frame is chosen (retaining the BGR pixels instead
would be 6.2 MB x DETECT_MAX_TRACKS per camera). Same content-addressed
store, same retention sweep, same additive-and-nullable shape
plate_evidence_path took before it.

NULL means "this pipeline sends no scene frame, or this row predates the
column"; readers fall back to evidence_path. No backfill is possible —
the frame bytes were never persisted, which is the whole point.

Revision ID: bb99cc00dd11
Revises: aa88bb99cc00
Create Date: 2026-09-03 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb99cc00dd11"
down_revision: str | None = "aa88bb99cc00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("scene_evidence_path", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("events", "scene_evidence_path")

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

"""add events.plate_evidence_path — the crop the plate was READ from (#382)

A visit stores one image: the vehicle-best frame, chosen for thumbnail
quality (biggest, sharpest VEHICLE box). Multi-frame OCR does not read
that frame — it reads plate-candidate crops — and the two are
anti-correlated by construction: a car is biggest when closest, which is
exactly when its plate slides out of the crop. So the Vehicles page was
captioning a correct plate with a photo that does not show it.

Additive and nullable: NULL means "read from the evidence crop itself,
or enriched before this column existed", and readers fall back to
evidence_path. No backfill is possible — the winning candidate's bytes
were never persisted, which is the bug.

Revision ID: aa88bb99cc00
Revises: ff77bb88cc99
Create Date: 2026-09-02 06:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa88bb99cc00"
down_revision: str | None = "ff77bb88cc99"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("plate_evidence_path", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("events", "plate_evidence_path")

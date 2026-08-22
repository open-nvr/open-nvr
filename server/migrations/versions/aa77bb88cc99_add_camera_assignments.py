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

"""add camera assignments

Per-camera capability assignment (docs/design/per-camera-assignment.md,
slice 1): ONE place that answers "what is camera 4 for". A JSON list of
``{"skill": "<capability>", "labels": [...]?}`` entries on the camera
record, written by the camera settings surface, served additively on the
internal camera-agent endpoint. Nothing consumes it yet — Tier-0
per-camera labels, the SDK's ``cameras_for_skill()``, and the catalog UI
opt in in later slices.

Nullable, no default: existing rows keep NULL, which every consumer must
read as "no restriction declared" (back-compat), never as "do nothing".

Revision ID: aa77bb88cc99
Revises: ff66aa77bb88
Create Date: 2026-08-22 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa77bb88cc99"
down_revision: str | None = "ff66aa77bb88"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column(
            "assignments",
            sa.JSON(),
            nullable=True,
            comment=(
                "Per-camera capability assignment: list of "
                '{"skill": ..., "labels": [...]?} entries. NULL/[] = '
                "nothing assigned (no restriction declared)."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("cameras", "assignments")

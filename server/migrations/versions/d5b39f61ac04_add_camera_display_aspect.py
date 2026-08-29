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

"""add camera display aspect

Adds an optional per-camera ``display_aspect_ratio`` to ``cameras`` (#354).

Some encoders squash a dimension and expect the player to stretch it back:
a Dahua-family "1080N" channel sends a 16:9 scene as 960x1080. The bitstream
declares no sample aspect ratio, so OpenNVR's passthrough pipeline has nothing
to correct from and the picture renders as a narrow strip.

Detection of the known modes lives in the UI and needs no stored value; this
column is the operator's override for the cameras it doesn't recognise, and
always wins over it.

Nullable, no default: existing rows keep NULL, which means "detect".

Revision ID: d5b39f61ac04
Revises: bb88cc99dd00
Create Date: 2026-08-29 19:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5b39f61ac04"
down_revision: str | None = "bb88cc99dd00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column(
            "display_aspect_ratio",
            sa.String(length=16),
            nullable=True,
            comment=(
                'Operator display-aspect override: NULL (detect), "native", '
                'or a "W:H" ratio. Display only — never re-provisions a stream.'
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("cameras", "display_aspect_ratio")

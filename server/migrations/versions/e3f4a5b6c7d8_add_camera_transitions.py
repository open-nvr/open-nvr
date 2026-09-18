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

"""add camera_transitions — which camera follows which, and how fast

"Where did it go after the dock?" needs two things the event store does
not have on its own: which cameras can follow which, and how long that
trip takes. Surveying that by hand is work nobody does, and it goes stale
the moment a gate is chained shut or a camera is re-aimed.

So it is learned, from the trips the system is already certain about. A
plate read by the LPR adapter, or a face recognised by the face adapter
— both served through KAI-C — is an EXACT identity: the same string on
two cameras is one object, observed travelling. Every such pair is one
sample of an A→B edge, and a few thousand of them give a median and a
p90 with no labelling and no configuration.

That is what makes the general case tractable. An object with no exact
identity — a person in a crowd, an unplated van, a trolley — can then be
followed by asking the topology where it could have gone and in what
time, and asking the descriptors which of the candidates there fits.
Without the graph, "follow this" means scanning every camera for the
whole window; with it, a handful of plausible ones.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-18 11:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "camera_transitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "from_camera_id", sa.Integer(), sa.ForeignKey("cameras.id"), nullable=False
        ),
        sa.Column(
            "to_camera_id", sa.Integer(), sa.ForeignKey("cameras.id"), nullable=False
        ),
        # How many confirmed trips this edge is built from. One sample is
        # a coincidence, and the score has to be allowed to say so.
        sa.Column("samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("median_seconds", sa.Float(), nullable=True),
        # The slow tail: somebody who stopped to talk on the way is not
        # evidence that the trip is impossible.
        sa.Column("p90_seconds", sa.Float(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.UniqueConstraint("from_camera_id", "to_camera_id", name="uq_transition_pair"),
    )


def downgrade() -> None:
    op.drop_table("camera_transitions")

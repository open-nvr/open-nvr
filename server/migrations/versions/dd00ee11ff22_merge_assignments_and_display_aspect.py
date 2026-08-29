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

"""merge skill-assignments and camera-display-aspect heads

PR #355 (cc99dd00ee11, skill_assignments) and PR #356 (d5b39f61ac04,
camera display aspect) were developed in parallel and both chain from
bb88cc99dd00 — git merged the files without conflict, leaving the
revision graph with two heads. Alembic refuses to 'upgrade head' past
that, so an existing deployment would silently skip BOTH migrations
(the exact failure mode test_migration_graph.py documents; it caught
this one). This no-op merge revision joins the branches; deployments
that already applied either or both heads converge here cleanly.

Revision ID: dd00ee11ff22
Revises: cc99dd00ee11, d5b39f61ac04
Create Date: 2026-08-29 00:00:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "dd00ee11ff22"
down_revision: str | Sequence[str] | None = ("cc99dd00ee11", "d5b39f61ac04")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

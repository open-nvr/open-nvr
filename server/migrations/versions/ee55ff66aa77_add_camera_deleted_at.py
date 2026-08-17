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

"""add cameras.deleted_at (tombstone separating delete from deactivate)

Deleting a camera used to be identical to deactivating it (is_active=False),
so "deleted" cameras could be revived by editing them and their recordings
became unreachable while still on disk. deleted_at marks an irreversible
soft delete: the camera moves to the bin, is never provisioned or editable
again, and its recordings stay viewable from the bin until retention removes
them. is_active alone now means a reversible pause.

Existing rows are left untouched: cameras soft-deleted under the old
behavior (is_active=False, deleted_at NULL) become "inactive/paused", the
safe interpretation since nothing can distinguish an old delete from an old
deactivate.

Revision ID: ee55ff66aa77
Revises: dd44ee55ff66
Create Date: 2026-08-17

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ee55ff66aa77"
down_revision: str | None = "dd44ee55ff66"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cameras", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("cameras", "deleted_at")

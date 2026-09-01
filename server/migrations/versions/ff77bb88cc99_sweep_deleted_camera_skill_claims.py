# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""sweep skill_assignments rows pointing at deleted cameras (#372)

Camera deletion is a soft delete, and until this release it left the
camera's ``skill_assignments`` rows behind. Under the assignment table's
union semantics ("the assignment list for that skill is the whole
truth"), one stale row on a binned camera kept the restriction armed
while scoping consumers to a camera that no longer exists — the LPR app
ignored every live camera because a deleted one still claimed the skill.

The query side now filters tombstoned cameras and both delete paths
clean up their claims, so this migration is the one-time sweep for rows
that installs accumulated BEFORE the fix: claims on soft-deleted
cameras, plus any orphans whose camera row is gone entirely.

Data-only; no schema change. Downgrade is a no-op — the swept rows were
semantically dead, and resurrecting tombstone claims would re-create the
bug this fixes.

Revision ID: ff77bb88cc99
Revises: ee11ff22aa33
Create Date: 2026-09-01
"""
from alembic import op
import sqlalchemy as sa

revision = "ff77bb88cc99"
down_revision = "ee11ff22aa33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(sa.text(
        """
        DELETE FROM skill_assignments
        WHERE camera_id IN (
            SELECT id FROM cameras WHERE deleted_at IS NOT NULL
        )
        OR camera_id NOT IN (SELECT id FROM cameras)
        """
    ))
    if result.rowcount:
        print(f"#372 sweep: removed {result.rowcount} skill claim(s) "
              "referencing deleted cameras")


def downgrade() -> None:
    # Data-only cleanup of semantically-dead rows; nothing to restore.
    pass

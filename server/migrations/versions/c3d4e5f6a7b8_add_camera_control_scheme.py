"""add cameras.control_scheme (persisted control-plane URL scheme)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-23

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cameras", sa.Column("control_scheme", sa.String(length=8), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("cameras", "control_scheme")

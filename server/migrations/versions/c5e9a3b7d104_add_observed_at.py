# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""add events.observed_at and app_alerts.observed_at

One timestamp for "when was this vehicle here", separate from every
stamp the platform makes while processing the read (#451).

Both columns are nullable with no backfill: the capture time of a frame
that has already been swept is not recoverable, and inventing one would
put a processing time in the very column that exists to not be one.
Readers fall back to started_at / fired_at for those rows.

Revision ID: c5e9a3b7d104
Revises: b4d8e2a91c7f
Create Date: 2026-09-09
"""
from alembic import op
import sqlalchemy as sa

revision = "c5e9a3b7d104"
down_revision = "b4d8e2a91c7f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("events") as b:
        b.add_column(sa.Column("observed_at", sa.DateTime(timezone=True),
                               nullable=True))
    with op.batch_alter_table("app_alerts") as b:
        b.add_column(sa.Column("observed_at", sa.DateTime(timezone=True),
                               nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_alerts") as b:
        b.drop_column("observed_at")
    with op.batch_alter_table("events") as b:
        b.drop_column("observed_at")

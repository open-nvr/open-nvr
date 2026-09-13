# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""app_alerts.alert_type + app_alerts.images — what happened, and the photos of it

Two columns the inbox was missing to carry an alert an operator can act
on without opening the camera.

``alert_type`` is the producer's own name for the thing ("scanner_flag",
"no_scan"). Severity already says how loudly to ring; nothing said WHAT
happened, so a month of history could not be filtered by kind. Indexed,
because filtering by kind is the whole point of it.

``images`` holds relative paths into the evidence store, never bytes.
Apps that tried to attach a photo put base64 in ``evidence`` and lost
it: the column is clipped to 8000 chars, so the JSON came back
unparseable and the WHOLE evidence dict read as null. Photos now go to
the evidence store first and only their paths ride along.

Revision ID: a3f19c7d2e60
Revises: e8a1b2c3d4f5
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

revision = "a3f19c7d2e60"
down_revision = "e8a1b2c3d4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_alerts") as batch:
        batch.add_column(sa.Column("alert_type", sa.String(40), nullable=True))
        batch.add_column(sa.Column("images", sa.Text(), nullable=True))
    op.create_index("ix_app_alerts_alert_type", "app_alerts", ["alert_type"])


def downgrade() -> None:
    op.drop_index("ix_app_alerts_alert_type", table_name="app_alerts")
    with op.batch_alter_table("app_alerts") as batch:
        batch.drop_column("images")
        batch.drop_column("alert_type")

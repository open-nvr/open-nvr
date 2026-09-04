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

"""add app_alerts — the operator alert inbox

Apps have published §11.5 alerts onto ``opennvr.alerts.>`` since the
alert stack shipped, and the LPR config even promised "the operator-UI
alerts inbox ... pick[s] them up" — but nothing on the core ever
subscribed. Alerts reached stdout and the bus and stopped: an armed
"alarm on unknown vehicle" fired into a log nobody watches, with no
sound, no banner, no acknowledgement anywhere. This table is where the
new bus consumer lands them and where the UI's ring/acknowledge state
lives.

``alert_id`` is the producer's own id and is UNIQUE — NATS is
at-least-once and the consumer reconnects, so redelivery must be an
UPDATE-nothing, never a second row ringing twice.

Revision ID: dd11ee22ff33
Revises: cc00dd11ee22
Create Date: 2026-09-03 16:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "dd11ee22ff33"
down_revision = "cc00dd11ee22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_alerts",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("alert_id", sa.String(64), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.String(30), nullable=True),
        sa.Column("source_name", sa.String(100), nullable=True),
        sa.Column("camera_id", sa.String(60), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("acknowledged_by", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_app_alerts_alert_id", "app_alerts", ["alert_id"],
                    unique=True)
    op.create_index("ix_app_alerts_unacked", "app_alerts",
                    ["acknowledged_at", "fired_at"])
    op.create_index("ix_app_alerts_source_name", "app_alerts",
                    ["source_name"])
    op.create_index("ix_app_alerts_camera_id", "app_alerts", ["camera_id"])


def downgrade() -> None:
    op.drop_index("ix_app_alerts_camera_id", table_name="app_alerts")
    op.drop_index("ix_app_alerts_source_name", table_name="app_alerts")
    op.drop_index("ix_app_alerts_unacked", table_name="app_alerts")
    op.drop_index("ix_app_alerts_alert_id", table_name="app_alerts")
    op.drop_table("app_alerts")

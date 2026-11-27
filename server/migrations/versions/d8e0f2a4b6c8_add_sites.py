# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""sites: remote sites whose cameras a site agent pushes to this server

Issue #925 (cloud ingest). A `sites` table for the enrolled agents, and four
nullable camera columns linking a publisher camera to its site and to the
camera's address and RTSP URLs on that site's LAN. Existing cameras are
untouched (all NULL).

Revision ID: d8e0f2a4b6c8
Revises: c7d9e1f3a5b6
Create Date: 2026-11-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d8e0f2a4b6c8"
down_revision: str | None = "c7d9e1f3a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("enrol_code_hash", sa.String(64), nullable=True),
        sa.Column("enrol_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credential_hash", sa.String(64), nullable=True),
        sa.Column("credential_prefix", sa.String(16), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ip", sa.String(45), nullable=True),
        sa.Column("agent_version", sa.String(100), nullable=True),
        sa.Column("report", sa.Text(), nullable=True),
        sa.Column("uplink_cap_kbps", sa.Integer(), nullable=True),
        sa.Column("live_policy", sa.String(20), nullable=False, server_default="always"),
    )
    op.create_index("ix_sites_id", "sites", ["id"])
    op.create_index("ix_sites_enrol_code_hash", "sites", ["enrol_code_hash"])

    with op.batch_alter_table("cameras") as batch:
        batch.add_column(sa.Column("site_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("site_lan_host", sa.String(255), nullable=True))
        batch.add_column(sa.Column("site_main_url", sa.String(500), nullable=True))
        batch.add_column(sa.Column("site_sub_url", sa.String(500), nullable=True))
        batch.create_foreign_key(
            "fk_cameras_site_id_sites", "sites", ["site_id"], ["id"], ondelete="RESTRICT"
        )
        batch.create_index("ix_cameras_site_id", ["site_id"])


def downgrade() -> None:
    with op.batch_alter_table("cameras") as batch:
        batch.drop_index("ix_cameras_site_id")
        batch.drop_constraint("fk_cameras_site_id_sites", type_="foreignkey")
        batch.drop_column("site_sub_url")
        batch.drop_column("site_main_url")
        batch.drop_column("site_lan_host")
        batch.drop_column("site_id")
    op.drop_index("ix_sites_enrol_code_hash", table_name="sites")
    op.drop_index("ix_sites_id", table_name="sites")
    op.drop_table("sites")

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""cameras.source_type and cameras.source_device — webcam cameras.

A webcam camera is one whose MediaMTX path is fed by a publisher (a
browser over WHIP, or the host agent pushing a USB webcam) instead of
being pulled from ``rtsp_url``. ``source_type`` says which; NULL means
``rtsp``, so existing cameras need no backfill. ``source_device`` is the
host agent's stable id of the USB webcam that feeds the camera; NULL for
a browser webcam.

Both columns are nullable and may already exist: ``core.database`` adds
missing nullable columns on boot, so each is added only where missing.

Revision ID: c4d0e6a8b2f3
Revises: b3c9e5f7a1d2
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op

revision = "c4d0e6a8b2f3"
down_revision = "b3c9e5f7a1d2"
branch_labels = None
depends_on = None

TABLE = "cameras"
COLUMNS = (
    ("source_type", sa.String(length=16)),
    ("source_device", sa.String(length=300)),
)


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}
    for name, coltype in COLUMNS:
        if name not in existing:
            op.add_column(TABLE, sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, _ in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)

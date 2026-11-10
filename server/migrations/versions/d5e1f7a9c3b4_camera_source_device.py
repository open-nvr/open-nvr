# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""cameras.source_device; cameras.publish_key_hash goes.

``c4d0e6a8b2f3`` first shipped (on a test branch) with ``publish_key_hash``
for the webcam helper script. The host agent replaced that script, and
``c4d0e6a8b2f3`` now adds ``source_device`` instead — but a database already
stamped at ``c4d0e6a8b2f3`` never runs it again, and core's additive
self-heal only covers databases without Alembic. This revision brings such a
database to the same columns. Every step checks first, so it is a no-op on a
database that went through the new ``c4d0e6a8b2f3``.

Revision ID: d5e1f7a9c3b4
Revises: c4d0e6a8b2f3
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op

revision = "d5e1f7a9c3b4"
down_revision = "c4d0e6a8b2f3"
branch_labels = None
depends_on = None

TABLE = "cameras"


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    cols = _columns()
    if "source_device" not in cols:
        op.add_column(TABLE, sa.Column("source_device", sa.String(length=300), nullable=True))
    if "publish_key_hash" in cols:
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column("publish_key_hash")


def downgrade() -> None:
    # source_device belongs to c4d0e6a8b2f3; publish_key_hash is not
    # restored (nothing reads it any more).
    pass

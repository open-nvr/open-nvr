# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""audit_logs.correlation_id: join an external action to its audit row

The Home Assistant integration (and any other API client) can send
``X-Correlation-Id``. RequestLoggingMiddleware keeps a valid one, or falls
back to the request's own id, and write_audit_log stamps it on every row
written during that request. Without it an operator could not tell which
automation in Home Assistant caused a PTZ move or a recording pause.

Nullable and indexed. Rows written before this migration, or outside a
request, carry NULL.

Revision ID: c4d8e2f1a9b3
Revises: b7e4a1c9d302
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision = "c4d8e2f1a9b3"
down_revision = "b7e4a1c9d302"
branch_labels = None
depends_on = None

_INDEX = "ix_audit_logs_correlation_id"


def upgrade() -> None:
    # Idempotent: core/database._backfill_additive_columns may already have
    # added the column (and its index) on a database that booted with the
    # new models before this migration ran.
    inspector = sa.inspect(op.get_bind())
    if "correlation_id" not in {c["name"] for c in inspector.get_columns("audit_logs")}:
        with op.batch_alter_table("audit_logs") as batch:
            batch.add_column(sa.Column("correlation_id", sa.String(64), nullable=True))
    if _INDEX not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("audit_logs")}:
        op.create_index(_INDEX, "audit_logs", ["correlation_id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="audit_logs")
    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_column("correlation_id")

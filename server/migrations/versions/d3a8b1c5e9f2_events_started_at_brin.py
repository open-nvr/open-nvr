# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A BRIN index on events.started_at — the fleet-wide window query.

Every existing index on ``events`` leads with something: camera
(``ix_events_cam_start``, ``ix_events_cam_seen``) or class
(``ix_events_label_start``). A query that names NEITHER — "everything
between 2am and 4am", the shape an operator reaches for after an
incident and the shape a free-text search falls back to — has no index
to seek on and scans.

A btree on ``started_at`` would serve it and would also be the most
expensive index on the table: one entry per row, on the table that grows
fastest, maintained on every insert from the hottest write path in the
system.

BRIN is the right shape for this column specifically. ``events`` is
append-ordered by time — visits are written as they finish — so
``started_at`` correlates almost perfectly with physical row order,
which is the exact condition under which BRIN works. It stores a
min/max summary per block range rather than a pointer per row, so it
costs kilobytes where a btree costs hundreds of megabytes, and it
prunes a time-ranged scan to the blocks that can contain the range.

It is a coarse index and that is the trade: it narrows the scan, it does
not eliminate it, and on a table whose time-ordering has been scrambled
(a bulk backfill of old footage, say) it degrades to no help at all
rather than to a wrong answer. Cheap enough that this is an acceptable
worst case; a btree is the answer if fleet-wide window queries ever
become the dominant read.

POSTGRES ONLY. SQLite has no BRIN, and a developer's SQLite file is not
the thing this is for. The upgrade is a no-op elsewhere rather than an
error, because a migration that refuses to run on the dialect the test
suite uses is a migration nobody runs.

Revision ID: d3a8b1c5e9f2
Revises: c2f7a9d4e8b1
Create Date: 2026-09-24
"""
from alembic import op

revision = "d3a8b1c5e9f2"
down_revision = "c2f7a9d4e8b1"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_events_started_brin"

#: Table blocks summarised by one index entry. Stated explicitly rather
#: than left to the server default so the granularity is a decision
#: recorded in this file, not a property of whichever Postgres version
#: happened to run the migration.
PAGES_PER_RANGE = 128


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON events "
        f"USING brin (started_at) WITH (pages_per_range = {PAGES_PER_RANGE})"
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")

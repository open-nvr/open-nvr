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

"""add event_text, and the index a fleet-wide search seeks on

Search over the canonical event store needs something to match "red" or
"delivery van" against; the visit row carries a class label and a plate,
not a description. This is that text, in a sidecar rather than on the
events row for three reasons:

* Enrichment is optional and arrives LATER than the visit (a captioner
  may run seconds or minutes behind Tier-0, or not at all). A sidecar
  lets it be written, rewritten and re-run without touching the visit,
  which is the row alarms and reports already point at.
* Most visits never get text. A nullable sidecar costs nothing for them;
  a wide events row would carry the column on every row forever.
* The full-text index belongs on the text, not on the event table, so
  rebuilding or re-tuning it never rewrites event history.

The index is Postgres-only on purpose. Production is Postgres 15; the
test suite runs SQLite, where the same queries fall back to LIKE over
the same two columns (services/search_service.py picks per dialect).
``simple`` rather than ``english``: the vocabulary here is nouns,
colours and makes, where stemming buys little and surprises are
expensive — an operator who searches "vans" and is shown "van" is fine,
but one who cannot predict what the box does stops trusting it.

Revision ID: c1d2e3f4a5b6
Revises: b7e4a1c9d302
Create Date: 2026-09-18 06:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b7e4a1c9d302"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The expression the GIN index covers and the query must match EXACTLY,
#: or Postgres plans a sequential scan and the index is decorative.
FTS_EXPR = (
    "to_tsvector('simple', coalesce(caption, '') || ' ' || coalesce(attributes, ''))"
)


def upgrade() -> None:
    op.create_table(
        "event_text",
        # PK and FK in one: one row of text per visit, and it dies with
        # the visit — retention on events is the retention on search.
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # What a captioner said about the frame.
        sa.Column("caption", sa.Text(), nullable=True),
        # Space-joined attribute words ("red van rear-door") from any
        # enricher that classifies rather than describes.
        sa.Column("attributes", sa.Text(), nullable=True),
        # Which enricher wrote this, so a bad one can be identified and
        # its rows re-run.
        sa.Column("source", sa.String(length=60), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    # The shape every search without a camera makes: a class over a time
    # window, fleet-wide. ix_events_label matches the class and then the
    # range is filtered against every row that class ever produced —
    # which, for "person", is most of the table. ix_events_cam_start
    # cannot help a query that names no camera. This is the seek.
    op.create_index("ix_events_label_start", "events", ["label", "started_at"])
    # get_context(), not get_bind(): this must also render correctly in
    # alembic's offline (--sql) mode, where there is no connection to ask.
    if op.get_context().dialect.name == "postgresql":
        op.execute(f"CREATE INDEX ix_event_text_fts ON event_text USING GIN ({FTS_EXPR})")
    # SQLite has no GIN, and the fallback query is a LIKE scan — no index
    # would help it, so none is created rather than pretending otherwise.


def downgrade() -> None:
    op.drop_index("ix_events_label_start", table_name="events")
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_event_text_fts")
    op.drop_table("event_text")

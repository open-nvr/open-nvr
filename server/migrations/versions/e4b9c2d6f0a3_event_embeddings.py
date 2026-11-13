# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""event_embeddings — a visit's vector, beside the visit.

``search_service``'s own docstring has named this for a while: "The
place for vector similarity is the same query, fused with these ranks
(RRF), once something in the stack produces embeddings." This is the
table that lets something.

WHY IT IS A BLOB AND NOT A ``vector`` COLUMN
--------------------------------------------

Because OpenNVR has to install on a mini-PC with SQLite and on a server
with Postgres, and a migration that only runs where pgvector happens to
be compiled in is a migration that splits the project in two. float32
little-endian bytes load everywhere, dump and restore across
architectures, and need no extension.

The cost is honest and bounded: similarity is computed by scanning the
FILTERED candidate set rather than by an index. Every query in this
system names a camera or a window or a class, so that set is small, and
:mod:`services.embedding_store` caps it and REPORTS when it capped —
which matters more than the speed, because a truncated result and an
empty one are otherwise indistinguishable.

An ANN index is an addition on top of this column when a deployment is
big enough to want one. It is not a different schema.

NO BACKFILL
-----------

The table starts empty and that is a working state, not a pending one.
``capability()`` reads zero rows and reports that semantic ranking is
off; search matches words exactly as it did yesterday. Nothing about
this migration changes a single response until an adapter advertising
the ``embed`` task actually runs.

Revision ID: e4b9c2d6f0a3
Revises: d3a8b1c5e9f2
Create Date: 2026-09-24
"""
import sqlalchemy as sa
from alembic import op

revision = "e4b9c2d6f0a3"
down_revision = "d3a8b1c5e9f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_embeddings",
        sa.Column("event_id", sa.Integer(),
                  sa.ForeignKey("events.id", ondelete="CASCADE"),
                  primary_key=True, nullable=False),
        # NOT NULL: a row with no vector is a row that can never match,
        # and "embedded but empty" is not a state worth being able to
        # represent. An enricher with nothing to store writes no row,
        # which is also how "still needs embedding" stays answerable.
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    # "Re-run everything the old adapter produced" and "what is left to
    # embed" are the two maintenance questions, and both are this index.
    op.create_index("ix_event_embeddings_model", "event_embeddings", ["model"])


def downgrade() -> None:
    op.drop_index("ix_event_embeddings_model", table_name="event_embeddings")
    op.drop_table("event_embeddings")

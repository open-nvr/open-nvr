# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""event_embeddings.kind — one table, two things embedded per visit.

Until now ``event_embeddings`` held exactly one vector per visit: the
evidence frame, through the embedding adapter's image tower. Search now
also embeds the visit's CAPTION (and its projected attribute words)
through the same model's text tower and fuses that as a third arm, so a
visit can carry two vectors. ``kind`` tells them apart and joins the
primary key: ``(event_id, kind)``.

WHY A COLUMN AND NOT A SECOND TABLE

Because everything that manages this table — the model-change detector
(``embedding_store.stale_rows``), the re-embed walk, retention's
sidecar delete, the capability probe — would otherwise have to be
written twice and kept in step twice. One ``kind`` column costs one
predicate in the scan; a second table costs a second copy of every
maintenance question.

EXISTING ROWS

Every row that exists today is an image vector, and the column's
default says so: ``'image'`` is filled in for all of them, nothing is
re-embedded, and search over them is unchanged.

Idempotent, like ``b8e4c2d6f1a3``: ``core.database`` may have added the
bare column already on a database that booted with the new models, so
each part is applied only where missing.

Revision ID: b3c9e5f7a1d2
Revises: a2b8d4f0c6e1
Create Date: 2026-11-09
"""
import sqlalchemy as sa
from alembic import op

revision = "b3c9e5f7a1d2"
down_revision = "a2b8d4f0c6e1"
branch_labels = None
depends_on = None

TABLE = "event_embeddings"
PK_NAME = "pk_event_embeddings"


def _kind_column(*, primary_key: bool = False) -> sa.Column:
    # ``primary_key`` is only for batch mode, where the rebuilt table is
    # assembled from its columns and a key column not flagged as one
    # draws a SQLAlchemy warning; ALTER TABLE ADD COLUMN ignores it.
    return sa.Column("kind", sa.String(length=16), nullable=False,
                     server_default="image", primary_key=primary_key)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns(TABLE)}
    pk = inspector.get_pk_constraint(TABLE) or {}
    pk_cols = list(pk.get("constrained_columns") or [])

    if bind.dialect.name == "postgresql":
        if "kind" not in columns:
            op.add_column(TABLE, _kind_column())
        else:
            # The additive backfill adds a bare column: no default, no
            # NOT NULL. Finish what it started.
            op.execute(sa.text(f"UPDATE {TABLE} SET kind = 'image' WHERE kind IS NULL"))
            op.alter_column(TABLE, "kind", existing_type=sa.String(length=16),
                            nullable=False, server_default="image")
        if pk_cols != ["event_id", "kind"]:
            if pk.get("name"):
                op.drop_constraint(pk["name"], TABLE, type_="primary")
            op.create_primary_key(PK_NAME, TABLE, ["event_id", "kind"])
        return

    # SQLite cannot ALTER a primary key; batch mode rebuilds the table.
    # The reflected (unnamed) single-column key is replaced by the named
    # composite one when it is added — see alembic's ApplyBatchImpl.
    if "kind" in columns:
        op.execute(sa.text(f"UPDATE {TABLE} SET kind = 'image' WHERE kind IS NULL"))
    with op.batch_alter_table(TABLE, recreate="always") as batch:
        if "kind" not in columns:
            batch.add_column(_kind_column(primary_key=True))
        else:
            batch.alter_column("kind", existing_type=sa.String(length=16),
                               nullable=False, server_default="image")
        if pk_cols != ["event_id", "kind"]:
            batch.create_primary_key(PK_NAME, ["event_id", "kind"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    pk = inspector.get_pk_constraint(TABLE) or {}
    # Only image vectors fit a single-column key; caption vectors go.
    op.execute(sa.text(f"DELETE FROM {TABLE} WHERE kind <> 'image'"))
    if bind.dialect.name == "postgresql":
        if pk.get("name"):
            op.drop_constraint(pk["name"], TABLE, type_="primary")
        op.drop_column(TABLE, "kind")
        op.create_primary_key(f"{TABLE}_pkey", TABLE, ["event_id"])
        return
    with op.batch_alter_table(TABLE, recreate="always") as batch:
        batch.drop_column("kind")
        batch.create_primary_key(f"{TABLE}_pkey", ["event_id"])

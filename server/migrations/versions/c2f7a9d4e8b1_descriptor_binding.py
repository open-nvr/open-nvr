# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""visit_descriptors.binding — how a claim's SUBJECT was determined.

Every other column on a descriptor qualifies the CLAIM: ``confidence``
says what the skill thought of it, ``source_task`` and
``source_adapter`` say who made it, ``correlation_id`` joins back to the
inference behind it — "the difference between a descriptor being an
assertion and being evidence", as the model puts it.

Nothing qualified the SUBJECT. Nothing said how this claim came to be
attached to THIS visit rather than the one before it, because until now
there was only one way: the producer already held the ``event_id``.

That is what kept frame-polling apps out of the store entirely. A
``FrameApp`` has a camera, some bytes and an instant; it has no
``event_id``. smart-doorbell's own source argues the case for staying
out, and the argument is right as far as it goes: attaching a name by
matching timestamps would make a guessed identity indistinguishable,
afterwards, from a measured one.

Indistinguishable is the operative word, and it is a property of the
record, not of the matching. So the record gains the missing column:

  direct   the producer held the event_id (every row written so far)
  window   core matched camera + instant INSIDE a visit's own span
  nearest  no visit covered the instant; the closest within a bounded
           tolerance was used

``window`` is a lookup, not a guess: the visit's span is core's own
record of when that object was present, and the match is made by the
component that owns the data. ``nearest`` IS a guess, which is exactly
why it is a different value and why a reader can refuse it in one
filter.

Every existing row is ``direct``, and that is true rather than assumed:
the only writers before this release were core's own enricher and the
camera-agent's descriptor endpoint, both of which pass an event_id they
were given. The backfill asserts it rather than hoping.

Revision ID: c2f7a9d4e8b1
Revises: b2d6f0a4c7e8
Create Date: 2026-09-23
"""
import sqlalchemy as sa
from alembic import op

revision = "c2f7a9d4e8b1"
down_revision = "b2d6f0a4c7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so the backfill is the same statement as the add —
    # a separate UPDATE would leave a window where existing rows read
    # NULL, and a NULL binding is the one value that must never mean
    # anything (it would read as "unknown provenance" on claims whose
    # provenance is the best in the table).
    op.add_column(
        "visit_descriptors",
        sa.Column("binding", sa.String(length=16), nullable=False,
                  server_default="direct"),
    )
    # Readers filter on it ("show me nothing bound by a timestamp"), and
    # on a busy site the descriptor table is the largest in the store.
    op.create_index("ix_descriptor_binding", "visit_descriptors", ["binding"])


def downgrade() -> None:
    op.drop_index("ix_descriptor_binding", table_name="visit_descriptors")
    op.drop_column("visit_descriptors", "binding")

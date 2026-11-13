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

"""add visit_descriptors — what each skill said about one visit

``event_text`` holds words. This holds FACTS, one row per claim: kind
("colour", "vehicle_type", "face_id", "plate"), value, the confidence the
skill gave it, and which task and adapter produced it.

Three reasons it is rows rather than columns on the visit:

* The set of claims depends on what is registered and healthy in KAI-C at
  the moment the visit was enriched. A deployment with LPR and a colour
  classifier writes different kinds from one with a captioner, and the
  schema must not need a migration per skill.
* Every claim carries its own confidence and provenance. "Red, 0.62, from
  yolo-attrib" and "KA01AB1234, 0.97, from fast-plate-ocr" cannot share a
  column and stay honest, and a report that cites evidence has to name the
  skill behind each line.
* A skill improves, or is found to be wrong. Rows can be re-run and
  replaced per (visit, kind, task) without touching the visit or the
  other skills' claims.

The (kind, value) index is not only for filtering: it is what makes the
discriminative weight of a value measurable. "Red" at a depot where half
the fleet is red is weak evidence and "red" at a site with one red van is
nearly an identifier, and the only way to know which is to count what
this deployment actually sees.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-09-18 10:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "visit_descriptors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # What kind of claim this is: colour, vehicle_type, face_id,
        # plate, clothing_top, carrying … An open vocabulary on purpose —
        # a new skill must not need a migration.
        sa.Column("kind", sa.String(length=40), nullable=False),
        # The claim itself, lowercased by the writer so "Red" and "red"
        # are one value and can be counted as one.
        sa.Column("value", sa.String(length=120), nullable=False),
        # What the skill thought of its own claim. Kept per row because
        # combining evidence without it is how a 0.51 guess ends up
        # weighing the same as a 0.99 read.
        sa.Column("confidence", sa.Float(), nullable=True),
        # Provenance: the canonical task, the adapter that served it, and
        # the model fingerprint — so a bad skill can be found, its rows
        # re-run, and a report can say who said what.
        sa.Column("source_task", sa.String(length=40), nullable=True),
        sa.Column("source_adapter", sa.String(length=60), nullable=True),
        sa.Column("model_fingerprint", sa.String(length=120), nullable=True),
        # The KAI-C correlation id of the inference that produced this
        # claim. KAI-C is the gate every model call goes through and the
        # thing that writes an audit line for each one; carrying its id
        # here is what lets an operator walk a claim on a report back to
        # the exact inference, adapter and model version that made it.
        # Without it a descriptor is an assertion; with it, it is
        # evidence.
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        # One claim per (visit, kind, task): re-running a skill REPLACES
        # what it said before instead of stacking a second opinion from
        # the same source. Two different tasks may still disagree, which
        # is information rather than a conflict.
        sa.UniqueConstraint("event_id", "kind", "source_task", name="uq_descriptor_claim"),
    )
    op.create_index("ix_descriptor_event", "visit_descriptors", ["event_id"])
    # Filtering ("every red van") and counting (how common "red" is here,
    # which is what turns a colour into weak or strong evidence).
    op.create_index("ix_descriptor_kind_value", "visit_descriptors", ["kind", "value"])


def downgrade() -> None:
    op.drop_index("ix_descriptor_kind_value", table_name="visit_descriptors")
    op.drop_index("ix_descriptor_event", table_name="visit_descriptors")
    op.drop_table("visit_descriptors")

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""attendance — people, groups, timetable, sessions, attempts, sightings, marks

Who was in the room, from the cameras: the attendance app reports what
each frame showed, core keeps the timetable and decides the marks.
Additive only: nine new tables, nothing existing is touched, so an
upgraded site keeps every row it had.

Revision ID: e7a1c3d5f9b2
Revises: b4d8f2a6c1e7
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = "e7a1c3d5f9b2"
down_revision = "b4d8f2a6c1e7"
branch_labels = None
depends_on = None

#: Creation order (foreign keys point backwards). Dropped in reverse.
_TABLES = (
    "attendance_people",
    "attendance_groups",
    "attendance_group_members",
    "attendance_timetable_slots",
    "attendance_sessions",
    "attendance_attempts",
    "attendance_attempt_sessions",
    "attendance_sightings",
    "attendance_marks",
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    # Each table is guarded on its own: create_all() may have built some
    # or all of them from the models on a stack that booted once, and a
    # bare create_table would then raise and roll back the whole batch.
    if not _has_table("attendance_people"):
        op.create_table(
            "attendance_people",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("uuid", sa.String(36), nullable=False),
            sa.Column("display_name", sa.String(200), nullable=False),
            sa.Column("external_ref", sa.String(64), nullable=True),
            sa.Column("email", sa.String(255), nullable=True),
            # sa.true(), not "1": Postgres refuses an integer default on a boolean.
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            _ts("consent_at", nullable=True),
            sa.Column("consent_by", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("face_samples", sa.Integer(), nullable=False, server_default="0"),
            _ts("created_at", server_default=sa.func.now()),
            _ts("updated_at", server_default=sa.func.now()),
            _ts("deleted_at", nullable=True),
        )
        op.create_index("ix_attendance_people_id", "attendance_people", ["id"])
        op.create_index("ix_attendance_people_uuid", "attendance_people", ["uuid"], unique=True)
        op.create_index("ix_attendance_people_deleted_at", "attendance_people", ["deleted_at"])
        op.create_index("uq_attendance_people_live_ref", "attendance_people", ["external_ref"],
                        unique=True,
                        postgresql_where=sa.text("deleted_at IS NULL"),
                        sqlite_where=sa.text("deleted_at IS NULL"))

    if not _has_table("attendance_groups"):
        op.create_table(
            "attendance_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("code", sa.String(40), nullable=True, unique=True),
            sa.Column("kind_label", sa.String(40), nullable=True),
            sa.Column("timezone", sa.String(64), nullable=False),
            sa.Column("policy", sa.JSON(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            _ts("created_at", server_default=sa.func.now()),
            _ts("updated_at", server_default=sa.func.now()),
        )
        op.create_index("ix_attendance_groups_id", "attendance_groups", ["id"])

    if not _has_table("attendance_group_members"):
        op.create_table(
            "attendance_group_members",
            sa.Column("group_id", sa.Integer(),
                      sa.ForeignKey("attendance_groups.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("person_id", sa.Integer(),
                      sa.ForeignKey("attendance_people.id", ondelete="CASCADE"),
                      primary_key=True),
            _ts("added_at", server_default=sa.func.now()),
        )
        op.create_index("ix_attendance_group_members_person_id",
                        "attendance_group_members", ["person_id"])

    if not _has_table("attendance_timetable_slots"):
        op.create_table(
            "attendance_timetable_slots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("group_id", sa.Integer(),
                      sa.ForeignKey("attendance_groups.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("weekday", sa.Integer(), nullable=False),
            sa.Column("start_time", sa.String(5), nullable=False),
            sa.Column("end_time", sa.String(5), nullable=False),
            sa.Column("camera_ids", sa.JSON(), nullable=False),
            sa.Column("valid_from", sa.Date(), nullable=True),
            sa.Column("valid_to", sa.Date(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            _ts("created_at", server_default=sa.func.now()),
            _ts("updated_at", server_default=sa.func.now()),
        )
        op.create_index("ix_attendance_timetable_slots_id", "attendance_timetable_slots", ["id"])
        op.create_index("ix_attendance_timetable_slots_group_id",
                        "attendance_timetable_slots", ["group_id"])

    if not _has_table("attendance_sessions"):
        op.create_table(
            "attendance_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("slot_id", sa.Integer(),
                      sa.ForeignKey("attendance_timetable_slots.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("group_id", sa.Integer(),
                      sa.ForeignKey("attendance_groups.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("local_date", sa.Date(), nullable=False),
            _ts("start_at", nullable=False),
            _ts("end_at", nullable=False),
            sa.Column("camera_ids", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="scheduled"),
            _ts("finalized_at", nullable=True),
            sa.Column("total_buckets", sa.Integer(), nullable=True),
            sa.Column("counted_buckets", sa.Integer(), nullable=True),
            sa.Column("no_coverage", sa.Boolean(), nullable=False, server_default=sa.false()),
            _ts("created_at", server_default=sa.func.now()),
            sa.UniqueConstraint("slot_id", "local_date", name="uq_attendance_session_slot_date"),
        )
        op.create_index("ix_attendance_sessions_id", "attendance_sessions", ["id"])
        op.create_index("ix_attendance_sessions_slot_id", "attendance_sessions", ["slot_id"])
        op.create_index("ix_attendance_sessions_group_id", "attendance_sessions", ["group_id"])
        op.create_index("ix_attendance_sessions_local_date", "attendance_sessions", ["local_date"])
        op.create_index("ix_attendance_sessions_status", "attendance_sessions", ["status"])
        op.create_index("ix_attendance_sessions_window", "attendance_sessions",
                        ["start_at", "end_at"])

    if not _has_table("attendance_attempts"):
        op.create_table(
            "attendance_attempts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.String(64), nullable=False),
            sa.Column("camera_id", sa.Integer(), nullable=False),
            _ts("at", nullable=False),
            sa.Column("ok", sa.Boolean(), nullable=False),
            sa.Column("faces_detected", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("matched", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("evidence_path", sa.String(500), nullable=True),
            _ts("created_at", server_default=sa.func.now()),
        )
        op.create_index("ix_attendance_attempts_id", "attendance_attempts", ["id"])
        op.create_index("ix_attendance_attempts_event_id", "attendance_attempts", ["event_id"],
                        unique=True)
        op.create_index("ix_attendance_attempts_camera_id", "attendance_attempts", ["camera_id"])
        op.create_index("ix_attendance_attempts_at", "attendance_attempts", ["at"])

    if not _has_table("attendance_attempt_sessions"):
        op.create_table(
            "attendance_attempt_sessions",
            sa.Column("attempt_id", sa.Integer(),
                      sa.ForeignKey("attendance_attempts.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("session_id", sa.Integer(),
                      sa.ForeignKey("attendance_sessions.id", ondelete="CASCADE"),
                      primary_key=True),
        )
        op.create_index("ix_attendance_attempt_sessions_session_id",
                        "attendance_attempt_sessions", ["session_id"])

    if not _has_table("attendance_sightings"):
        op.create_table(
            "attendance_sightings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("attempt_id", sa.Integer(),
                      sa.ForeignKey("attendance_attempts.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("session_id", sa.Integer(),
                      sa.ForeignKey("attendance_sessions.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("person_id", sa.Integer(),
                      sa.ForeignKey("attendance_people.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("similarity", sa.Float(), nullable=False),
            sa.Column("bbox", sa.JSON(), nullable=True),
            sa.UniqueConstraint("attempt_id", "session_id", "person_id",
                                name="uq_attendance_sighting"),
        )
        op.create_index("ix_attendance_sightings_id", "attendance_sightings", ["id"])
        op.create_index("ix_attendance_sightings_session_person", "attendance_sightings",
                        ["session_id", "person_id"])

    if not _has_table("attendance_marks"):
        op.create_table(
            "attendance_marks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("session_id", sa.Integer(),
                      sa.ForeignKey("attendance_sessions.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("person_id", sa.Integer(),
                      sa.ForeignKey("attendance_people.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("status", sa.String(10), nullable=False),
            sa.Column("buckets_present", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("buckets_total", sa.Integer(), nullable=False, server_default="0"),
            _ts("first_seen_at", nullable=True),
            sa.Column("source", sa.String(8), nullable=False, server_default="auto"),
            sa.Column("overridden_by", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            _ts("updated_at", server_default=sa.func.now()),
            sa.UniqueConstraint("session_id", "person_id", name="uq_attendance_mark"),
        )
        op.create_index("ix_attendance_marks_id", "attendance_marks", ["id"])
        op.create_index("ix_attendance_marks_session_id", "attendance_marks", ["session_id"])
        op.create_index("ix_attendance_marks_person_id", "attendance_marks", ["person_id"])


def downgrade() -> None:
    for name in reversed(_TABLES):
        if _has_table(name):
            op.drop_table(name)

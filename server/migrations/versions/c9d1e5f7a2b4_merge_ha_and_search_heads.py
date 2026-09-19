# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""merge heads: the Home Assistant work and main's search work

Both lines of work added migrations off the same base: the Home Assistant
integration (last: api_tokens.parent_id, b8e4c2d6f1a3) and footage search
(last: camera_transitions, e3f4a5b6c7d8). They touch different tables, so
this revision only joins them; it changes nothing itself.

Revision ID: c9d1e5f7a2b4
Revises: b8e4c2d6f1a3, e3f4a5b6c7d8
Create Date: 2026-09-19
"""

revision = "c9d1e5f7a2b4"
down_revision = ("b8e4c2d6f1a3", "e3f4a5b6c7d8")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

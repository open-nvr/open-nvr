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

"""add camera transport_security

Adds a per-camera ``transport_security`` field to ``camera_configs`` so
each camera can express its position on the operator-vs-camera RTSPS
policy spectrum (V-003).

Defaults all existing rows to ``rtsps_preferred`` — the safe middle
ground that tries RTSPS first but falls back to RTSP if the camera does
not support TLS. Operators can re-probe a camera (POST
``/cameras/{id}/probe-transport``) to refresh the value, or explicitly
set ``rtsps_required`` (no fallback) / ``plaintext_allowed`` (legacy).

Revision ID: b4e2a9c7f1d0
Revises: d75d15b88c1a
Create Date: 2026-05-18 04:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e2a9c7f1d0"
down_revision: str | None = "d75d15b88c1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "camera_configs",
        sa.Column(
            "transport_security",
            sa.String(length=20),
            nullable=False,
            server_default="rtsps_preferred",
            comment=(
                "V-003 per-camera transport policy: "
                "rtsps_required | rtsps_preferred | plaintext_allowed"
            ),
        ),
    )
    # M1c-selfrev H-2: track whether `transport_security` was last set
    # by an explicit operator action (True) or by the probe-driven
    # default (False). The /probe-transport endpoint refuses to
    # overwrite an operator-set value unless ?reset_policy=true.
    # Without this flag we couldn't distinguish a probe-driven
    # "rtsps_preferred" from an operator who deliberately chose
    # "rtsps_preferred" — they're the same value but mean different
    # things about the policy lifecycle.
    op.add_column(
        "camera_configs",
        sa.Column(
            "transport_security_operator_set",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "True when transport_security was last set explicitly by "
                "the operator (via PUT /cameras/{id}/transport-security or "
                "config update). False when set by the auto-probe."
            ),
        ),
    )
    op.add_column(
        "camera_configs",
        sa.Column(
            "transport_security_probe_result",
            sa.String(length=20),
            nullable=False,
            server_default="not_probed",
            comment=(
                "Latest RTSPS reachability probe outcome: "
                "supported | not_supported | inconclusive | not_probed"
            ),
        ),
    )
    op.add_column(
        "camera_configs",
        sa.Column(
            "transport_security_probed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Wall-clock timestamp of the latest RTSPS probe.",
        ),
    )


def downgrade() -> None:
    op.drop_column("camera_configs", "transport_security_probed_at")
    op.drop_column("camera_configs", "transport_security_probe_result")
    op.drop_column("camera_configs", "transport_security_operator_set")
    op.drop_column("camera_configs", "transport_security")

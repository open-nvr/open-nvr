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

"""backfill cameras.port from rtsp_url

``cameras.port`` is the camera's RTSP port, but nothing kept it in step with
``cameras.rtsp_url``. A camera added with an explicit URL on a non-standard
port (``rtsp://host:8554/...``) kept the 554 column default, so the cameras
list rendered ``host:554`` — an address nothing answers on, and the first
thing an operator checks when a stream drops.

The create/update paths now derive the column from the URL. This backfills the
rows that predate them. Only rows where the URL yields a different port are
touched; a camera with no URL, a non-rtsp URL, or an unparseable one is left
exactly as it is rather than overwritten with a guess.

Revision ID: bb88cc99dd00
Revises: aa77bb88cc99
Create Date: 2026-08-24 09:00:00.000000

"""

from collections.abc import Sequence
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb88cc99dd00"
down_revision: str | None = "aa77bb88cc99"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Scheme defaults for an rtsp(s) URL that omits the port (RFC 7826). Kept in
# step with ``services.camera_source_resolver._RTSP_DEFAULT_PORTS``; inlined
# because a migration must not import application code that may have moved on.
_DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322}


def _port_from(url: str | None) -> int | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    try:
        port = parsed.port
    except ValueError:
        # Malformed authority, e.g. "host:abc".
        return None
    if port is None:
        return _DEFAULT_PORTS[scheme]
    return port if 1 <= port <= 65535 else None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, port, rtsp_url FROM cameras WHERE rtsp_url IS NOT NULL")
    ).fetchall()

    # Grouped by target port so a fleet on one non-standard port costs one
    # statement, not one per camera.
    by_port: dict[int, list[int]] = {}
    for camera_id, port, rtsp_url in rows:
        derived = _port_from(rtsp_url)
        if derived is None or derived == port:
            continue
        by_port.setdefault(derived, []).append(camera_id)

    for derived, ids in by_port.items():
        conn.execute(
            sa.text("UPDATE cameras SET port = :port WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"port": derived, "ids": ids},
        )


def downgrade() -> None:
    # The pre-backfill values are unrecoverable (and were wrong), so there is
    # nothing to restore. Leaving the corrected ports in place is the safe
    # no-op: they describe where each camera actually streams.
    pass

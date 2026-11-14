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

"""SQLAlchemy-backed desired-state store for the app installer.

Reads/writes the ``app_install_intents`` table with stdlib + SQLAlchemy
Core only (no dependency on the server package), so the installer image
stays tiny. This is the production ``IntentStore`` the reconciler polls;
tests use an in-memory fake instead and never import this module.

Least privilege: the reconciler only needs SELECT on the whole row and
UPDATE of ``status``/``message``/``updated_at``. Point ``DATABASE_URL``
at a role scoped to exactly that (the installer never INSERTs intents —
only the web app does).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
    update,
)

from reconciler import Intent

_metadata = MetaData()

# Mirror of server/models.py::AppInstallIntent — declared standalone so
# the installer image doesn't pull in the whole server model graph.
app_install_intents = Table(
    "app_install_intents",
    _metadata,
    Column("id", String(100), primary_key=True),
    Column("image", String(500), nullable=False),
    Column("image_digest", String(100), nullable=True),
    Column("desired", String(20), nullable=False),
    Column("status", String(20), nullable=False),
    Column("message", Text, nullable=True),
    Column("requested_by", String(100), nullable=True),
    Column("requested_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=True),
)


def with_driver(database_url: str) -> str:
    """Name the driver the image ships (psycopg2) on a bare ``postgresql://``
    URL. Left to SQLAlchemy, the bare scheme means "its default driver",
    which 2.1 moved from psycopg2 to psycopg 3 — and the installer then
    died on import with ``No module named 'psycopg'``. A URL that already
    names a driver (``postgresql+psycopg2://``, ``sqlite://``) is untouched.
    """
    bare = "postgresql://"
    if database_url.startswith(bare):
        return "postgresql+psycopg2://" + database_url[len(bare):]
    return database_url


class SqlIntentStore:
    """Production IntentStore over the app_install_intents table."""

    def __init__(self, database_url: str):
        self._engine = create_engine(with_driver(database_url), future=True)

    def list_intents(self) -> list[Intent]:
        stmt = select(
            app_install_intents.c.id,
            app_install_intents.c.image,
            app_install_intents.c.image_digest,
            app_install_intents.c.desired,
            app_install_intents.c.status,
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [
            Intent(
                id=r.id,
                image=r.image,
                image_digest=r.image_digest,
                desired=r.desired,
                status=r.status,
            )
            for r in rows
        ]

    def set_status(
        self, intent_id: str, status: str, message: str, *, desired: str
    ) -> None:
        """Guarded status write: only applies while the row's ``desired``
        still equals the value this sweep acted on. If the operator
        flipped install→uninstall (or vice versa) mid-reconcile, the
        server has already rewritten ``desired`` + reset ``status`` to
        ``pending`` — this UPDATE then matches zero rows and the new
        request survives untouched for the next sweep, instead of being
        clobbered into a silently-lost ``applied``."""
        stmt = (
            update(app_install_intents)
            .where(
                app_install_intents.c.id == intent_id,
                app_install_intents.c.desired == desired,
            )
            .values(
                status=status,
                message=message,
                updated_at=datetime.now(timezone.utc),
            )
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

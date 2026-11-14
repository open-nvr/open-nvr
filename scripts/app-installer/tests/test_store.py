# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The installer's database URL always names the driver its image ships.

SQLAlchemy 2.1 made psycopg 3 the default behind a bare ``postgresql://``;
the image installs psycopg2, so the bare scheme must be made explicit or
the installer dies on import before its first poll.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store import with_driver  # noqa: E402


@pytest.mark.parametrize(
    "given, expected",
    [
        (
            "postgresql://u:p@db:5432/opennvr_db",
            "postgresql+psycopg2://u:p@db:5432/opennvr_db",
        ),
        # An explicit driver, any driver, is the operator's call.
        (
            "postgresql+psycopg2://u:p@db/opennvr_db",
            "postgresql+psycopg2://u:p@db/opennvr_db",
        ),
        ("postgresql+psycopg://u:p@db/opennvr_db", "postgresql+psycopg://u:p@db/opennvr_db"),
        ("sqlite:///intents.db", "sqlite:///intents.db"),
    ],
)
def test_with_driver(given: str, expected: str) -> None:
    assert with_driver(given) == expected

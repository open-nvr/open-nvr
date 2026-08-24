# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The ``bb88cc99dd00`` backfill puts ``cameras.port`` on the port its
``rtsp_url`` actually streams on.

The create/update paths derive the column now, but existing deployments carry
rows written before them — a camera added with an explicit URL on 8554 kept the
554 default, so the cameras list showed an address nothing answers on. This
covers the migration's row selection, because the failure mode is a silent one:
overwriting a port it had no business guessing at is worse than the bug.

Run with:

    cd server && pytest tests/test_camera_port_backfill_migration.py -v
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pytest
import sqlalchemy as sa

SERVER_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    SERVER_ROOT / "migrations" / "versions" / "bb88cc99dd00_camera_port_from_rtsp_url.py"
)


@pytest.fixture(scope="module")
def mig():
    spec = importlib.util.spec_from_file_location("_backfill_mig", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# (rtsp_url, stored port, port after the backfill)
ROWS = [
    # Derived: an explicit non-standard port, credentials and IPv6 included.
    ("rtsp://admin:pw@10.0.0.1:8554/", 554, 8554),
    ("rtsp://admin:pw@10.0.0.2:8554/", 554, 8554),  # same target, one statement
    ("rtsp://[fe80::1]:8554/s", 554, 8554),
    ("rtsps://10.0.0.8/ch1", 554, 322),  # scheme default when the URL omits it
    # Already correct — nothing to write.
    ("rtsp://10.0.0.3:554/ch1", 554, 554),
    ("rtsp://10.0.0.4/ch1", 554, 554),
    # Nothing to derive from: the stored value stands rather than being
    # replaced by a guess.
    (None, 8000, 8000),
    ("http://10.0.0.6:8554/", 8000, 8000),
    ("not a url", 8000, 8000),
    ("rtsp://10.0.0.7:abc/x", 554, 554),
    ("rtsp://10.0.0.9:0/x", 554, 554),
]


def test_backfill_only_rewrites_rows_the_url_can_speak_for(mig):
    eng = sa.create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE cameras "
                "(id INTEGER PRIMARY KEY, port INTEGER, rtsp_url TEXT)"
            )
        )
        conn.execute(
            sa.text("INSERT INTO cameras (id, port, rtsp_url) VALUES (:id, :p, :u)"),
            [
                {"id": i, "p": port, "u": url}
                for i, (url, port, _) in enumerate(ROWS, start=1)
            ],
        )

    conn = eng.connect()
    with mock.patch.object(mig.op, "get_bind", return_value=conn):
        with conn.begin():
            mig.upgrade()
        after = dict(conn.execute(sa.text("SELECT id, port FROM cameras")).fetchall())
    conn.close()

    expected = {i: port for i, (_, _, port) in enumerate(ROWS, start=1)}
    assert after == expected


def test_downgrade_is_a_no_op(mig):
    """The pre-backfill values are unrecoverable, and were wrong — leaving the
    corrected ports in place is the safe direction."""
    eng = sa.create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE cameras "
                "(id INTEGER PRIMARY KEY, port INTEGER, rtsp_url TEXT)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO cameras (id, port, rtsp_url) "
                "VALUES (1, 8554, 'rtsp://10.0.0.1:8554/')"
            )
        )

    conn = eng.connect()
    with mock.patch.object(mig.op, "get_bind", return_value=conn):
        mig.downgrade()
        assert conn.execute(sa.text("SELECT port FROM cameras")).scalar() == 8554
    conn.close()

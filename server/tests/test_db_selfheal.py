# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Self-healing schema init: additive columns are backfilled on existing tables.

Reproduces the real deployment gap — a DB first built by ``create_all`` (no
``alembic_version``) never gains new columns on an image upgrade because
``create_all`` only creates missing *tables*. ``_backfill_additive_columns``
closes that for nullable/defaulted columns.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import Column, Integer, String, create_engine, inspect, text
from sqlalchemy.orm import declarative_base

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from core import database  # noqa: E402


def test_backfill_adds_missing_additive_columns(monkeypatch, tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")

    reg_base = declarative_base()

    class Cam(reg_base):  # the CURRENT model (has the new columns)
        __tablename__ = "cams"
        id = Column(Integer, primary_key=True)
        control_scheme = Column(String(8), nullable=True)
        onvif_port = Column(Integer, nullable=True)

    # Simulate an OLDER on-disk table missing the new columns.
    with eng.begin() as c:
        c.execute(text("CREATE TABLE cams (id INTEGER PRIMARY KEY)"))

    monkeypatch.setattr(database, "engine", eng)
    monkeypatch.setattr(database, "Base", reg_base)

    before = {c["name"] for c in inspect(eng).get_columns("cams")}
    assert before == {"id"}

    database._backfill_additive_columns()

    after = {c["name"] for c in inspect(eng).get_columns("cams")}
    assert after == {"id", "control_scheme", "onvif_port"}


def test_backfill_skips_notnull_without_default(monkeypatch, tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    reg_base = declarative_base()

    class Cam(reg_base):
        __tablename__ = "cams"
        id = Column(Integer, primary_key=True)
        required = Column(String(8), nullable=False)  # unsafe to add to populated table

    with eng.begin() as c:
        c.execute(text("CREATE TABLE cams (id INTEGER PRIMARY KEY)"))

    monkeypatch.setattr(database, "engine", eng)
    monkeypatch.setattr(database, "Base", reg_base)

    database._backfill_additive_columns()  # must not raise

    cols = {c["name"] for c in inspect(eng).get_columns("cams")}
    assert cols == {"id"}  # NOT NULL-without-default left for a real migration

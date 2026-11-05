"""SQLite through SQLAlchemy. One file on the app's /data volume; the
engine workers run in threads, so every call opens its own short session."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from ..config import get_settings

Base = declarative_base()

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None
_lock = threading.Lock()


def _make_engine() -> Engine:
    url = get_settings().database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        path = url.replace("sqlite:///", "", 1)
        if path and path != ":memory:" and not path.startswith("/") and "/" in path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        elif path.startswith("/"):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _record):  # pragma: no cover - driver glue
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return eng


def get_engine() -> Engine:
    global _engine, _SessionLocal
    with _lock:
        if _engine is None:
            _engine = _make_engine()
            _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
        return _engine


def init_db() -> None:
    from . import models  # noqa: F401 - register tables

    eng = get_engine()
    Base.metadata.create_all(bind=eng)
    _add_missing_columns(eng)


# Columns added after a table first shipped: create_all never alters an
# existing table, so each is added here when absent (SQLite ALTER ADD).
_LATER_COLUMNS = {
    "students": [
        ("residence", "VARCHAR(16) NOT NULL DEFAULT 'day_scholar'"),
        ("hostel_block", "VARCHAR(64)"),
    ],
    "gates": [
        ("mode", "VARCHAR(16) NOT NULL DEFAULT 'barrier'"),
        ("direction", "VARCHAR(8) NOT NULL DEFAULT 'in'"),
        ("alert_on", "VARCHAR(8) NOT NULL DEFAULT 'all'"),
    ],
    "attendance_class_meta": [
        ("unwatched_slots", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "access_events": [
        ("movement", "VARCHAR(8)"),
        ("alerted", "BOOLEAN NOT NULL DEFAULT 0"),
    ],
}


def _add_missing_columns(eng: Engine) -> None:
    from sqlalchemy import inspect, text

    insp = inspect(eng)
    with eng.begin() as conn:
        for table, cols in _LATER_COLUMNS.items():
            if table not in insp.get_table_names():
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def reset_db() -> None:
    """Drop the cached engine (tests point at a fresh file per case)."""
    global _engine, _SessionLocal
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    db = _SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

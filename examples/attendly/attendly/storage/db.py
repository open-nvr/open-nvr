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

    Base.metadata.create_all(bind=get_engine())


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

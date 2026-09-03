# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Engine configuration — the connection-leak backstop, and its guard rails.

`core.database` sets `idle_in_transaction_session_timeout` so a connection
left idle inside a transaction is reclaimed instead of pinning a pool slot
forever. That is a net under the real fixes (`release()` and the
read/OCR/write split in plate_enrichment), not a substitute for them.

Two things can go wrong with a `connect_args`, and both are silent:

* it is applied to **sqlite**, whose `connect()` has no `options` kwarg — the
  module then raises at IMPORT, which takes out every suite that touches the
  database, not just this one;
* someone adds `statement_timeout` alongside it, which would let Postgres
  kill a migration mid-`ALTER TABLE` — and both migration paths swallow
  exceptions with a log line, so the schema would end up half-applied with
  the server cheerfully starting anyway.

Both are pinned below.
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_engine_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

import core.database as cdb  # noqa: E402
from core.database import connect_args_for  # noqa: E402

SOURCE = (REPO_ROOT / "server" / "core" / "database.py").read_text(
    encoding="utf-8",
)


def test_sqlite_gets_no_connect_args():
    """The suite runs on sqlite, whose connect() has no `options` kwarg. If
    the guard is dropped, importing core.database raises TypeError and every
    DB-touching test dies with it.

    Asserted on the pure helper, not on the live engine: another test module
    may already have pointed DATABASE_URL at postgres by the time this runs,
    and that would make an engine-level assertion test the environment
    instead of the code.
    """
    assert connect_args_for("sqlite:///./x.db") == {}
    assert connect_args_for("sqlite://") == {}


def test_a_postgres_url_arms_the_idle_timeout():
    args = connect_args_for("postgresql://u:p@db:5432/opennvr_db")
    assert args["options"] == "-c idle_in_transaction_session_timeout=60000"
    assert args["application_name"] == "opennvr-core"
    # The `postgres://` spelling is still in the wild.
    assert connect_args_for("postgres://u:p@db/opennvr_db") == args


def test_statement_timeout_is_not_set_on_the_shared_engine():
    """Migrations run on this engine and swallow their exceptions, so a
    killed ALTER TABLE would half-apply a schema and still boot. If you want
    one, scope it with SET LOCAL or give the DDL paths their own engine."""
    args = connect_args_for("postgresql://u:p@db/opennvr_db")
    assert "statement_timeout" not in args.get("options", "")
    assert "-c statement_timeout" not in SOURCE


def test_pre_ping_stays_on():
    """It is what turns a server-terminated connection into a silent
    reconnect at checkout instead of an error in whoever borrows it next —
    the only thing that makes the idle timeout safe to arm."""
    assert cdb.engine.pool._pre_ping is True


def test_release_is_idempotent_and_leaves_the_session_usable():
    """The contract every caller relies on: get_db's own finally: db.close()
    still runs after release(), and callers that own the session keep using
    it. Uses its own sqlite engine so it does not depend on wherever
    DATABASE_URL happens to point during a full-suite run."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=create_engine("sqlite://"))()
    try:
        session.execute(text("select 1"))
        assert session.in_transaction()
        cdb.release(session)
        assert not session.in_transaction()
        cdb.release(session)                  # no-op, must not raise
        assert session.execute(text("select 1")).scalar() == 1
    finally:
        session.close()

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

"""
Database configuration and session management.
Handles SQLAlchemy engine, session creation, and database initialization.
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.logging_config import main_logger

# Create SQLAlchemy engine
engine = create_engine(
    settings.database_url,
    echo=False,  # Disable SQL echo in terminal
    pool_pre_ping=True,  # Verify connections before use
    pool_recycle=300,  # Recycle connections every 5 minutes
    pool_size=20,  # Maximum number of connections to keep persistently
    max_overflow=10,  # Maximum number of connections to create beyond pool_size (total 30)
    pool_timeout=30,  # Seconds to wait for a connection before giving up
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create base class for models
Base = declarative_base()


def get_db():
    """Dependency to get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_alembic_migrations():
    """Run Alembic migrations automatically with fast check."""
    try:
        import time

        # Get the alembic.ini path (should be in server directory)
        from pathlib import Path

        from alembic import command
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        # Get the server directory (parent of this file's parent, since we're in core/)
        server_dir = Path(__file__).parent.parent
        alembic_ini_path = server_dir / "alembic.ini"

        if not alembic_ini_path.exists():
            main_logger.warning(
                f"Alembic config not found at {alembic_ini_path}, skipping automatic migrations"
            )
            return

        # Create Alembic config
        alembic_cfg = Config(str(alembic_ini_path))

        # Fast check: See if migrations are needed before running
        # This prevents hanging on command.upgrade() when DB is already up-to-date
        try:
            # Quick connection to check current version (with timeout protection)
            connection = engine.connect()
            try:
                context = MigrationContext.configure(connection)
                current_rev = context.get_current_revision()

                # Get target revision (head)
                script = ScriptDirectory.from_config(alembic_cfg)
                head_rev = script.get_current_head()

                # If already at head, skip upgrade entirely (fast path - no blocking)
                if current_rev == head_rev:
                    main_logger.debug(
                        f"Database already at head revision {head_rev}, skipping migrations"
                    )
                    return

                # Migration needed - log and proceed
                main_logger.info(
                    f"Database migration needed: {current_rev} -> {head_rev}"
                )
            finally:
                connection.close()
        except Exception as check_error:
            # If we can't check (e.g., fresh DB, connection timeout, or connection issue)
            # Log warning but don't proceed with upgrade to avoid hanging
            main_logger.warning(
                f"Could not check migration status: {check_error}. Skipping automatic migrations. Run 'alembic upgrade head' manually if needed."
            )
            return  # Don't proceed with upgrade if check fails - prevents hanging

        # Only run migrations if check succeeded and migration is needed
        main_logger.info("Running automatic database migrations...")
        start_time = time.time()
        command.upgrade(alembic_cfg, "head")
        elapsed = time.time() - start_time

        if elapsed > 1.0:
            main_logger.info(f"Database migrations completed in {elapsed:.2f}s")
        else:
            main_logger.debug("Database migrations check completed (no changes needed)")

    except Exception as e:
        main_logger.error(f"Failed to run automatic migrations: {e}", exc_info=True)
        # Don't raise - allow server to start even if migrations fail
        # This is important for development environments


def _make_alembic_config():
    """Return the Alembic Config, or None if alembic.ini isn't present."""
    from pathlib import Path

    from alembic.config import Config

    ini = Path(__file__).parent.parent / "alembic.ini"
    if not ini.exists():
        main_logger.warning(
            "alembic.ini not found at %s; schema managed by create_all only", ini
        )
        return None
    return Config(str(ini))


def _alembic_is_initialized() -> bool:
    """True if the DB has an ``alembic_version`` table (is Alembic-managed)."""
    from sqlalchemy import inspect

    try:
        return inspect(engine).has_table("alembic_version")
    except Exception as e:
        main_logger.warning("Could not inspect DB for alembic_version: %s", e)
        return False


def _backfill_additive_columns() -> None:
    """Add columns the models declare but existing tables are missing.

    ``create_all`` only creates missing *tables*, never adds *columns* to tables
    that already exist — so a DB first built by ``create_all`` at an older code
    version never gains new columns on an image upgrade (which is exactly how a
    stock deployment ends up missing e.g. ``cameras.control_scheme``). This
    reconciles that for additive columns that are nullable or have a server
    default — the only kind that can be added safely to a populated table, and
    the kind OpenNVR's migrations add. It only ever ADDs columns; it never drops
    or alters. Anything that needs more (NOT NULL without default, renames, data
    backfills, indexes) is left for a real migration once the DB is baselined.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            if not (col.nullable or col.server_default is not None):
                main_logger.warning(
                    "Column %s.%s is missing and NOT NULL without a default; "
                    "it needs a real migration to add safely.",
                    table.name,
                    col.name,
                )
                continue
            try:
                coltype = col.type.compile(dialect=engine.dialect)
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f'ALTER TABLE "{table.name}" '
                            f'ADD COLUMN "{col.name}" {coltype}'
                        )
                    )
                main_logger.info(
                    "Backfilled missing column %s.%s (%s)", table.name, col.name, coltype
                )
            except Exception as e:
                main_logger.error(
                    "Could not backfill column %s.%s: %s", table.name, col.name, e
                )


def init_db():
    """Ensure the schema is current AND Alembic-managed — self-healing across
    both create_all-bootstrapped and Alembic-managed databases.

    * Alembic-managed DB  -> apply pending migrations (then create_all as a net).
    * create_all/fresh DB -> create tables, backfill any additive columns
      create_all can't add, then *stamp head* so every FUTURE image upgrade
      auto-applies its migrations (the missing piece that made upgrades silently
      skip schema changes).
    """
    cfg = _make_alembic_config()

    # Path 1: already Alembic-managed — migrations first (they may create tables/
    # columns), then create_all only as a safety net for brand-new tables.
    if cfg is not None and _alembic_is_initialized():
        run_alembic_migrations()
        Base.metadata.create_all(bind=engine)
        return

    # Path 2: not Alembic-managed (stock create_all deployment, or a fresh DB).
    Base.metadata.create_all(bind=engine)  # fresh DB gets the full current schema
    if cfg is None:
        return
    try:
        _backfill_additive_columns()  # add columns older tables are missing
        from alembic import command

        command.stamp(cfg, "head")  # hand the DB to Alembic for the future
        main_logger.info(
            "Baselined Alembic at head — future image upgrades will auto-migrate."
        )
    except Exception as e:
        main_logger.error("Alembic baseline/backfill failed: %s", e, exc_info=True)
        # Never block startup on schema tooling.

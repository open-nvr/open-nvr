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


def init_db():
    """Initialize database tables."""
    # Run Alembic migrations first (handles schema changes)
    run_alembic_migrations()

    # Then create any missing tables (fallback for fresh databases)
    # This acts as a safety net if alembic fails, ensuring app can at least start
    Base.metadata.create_all(bind=engine)

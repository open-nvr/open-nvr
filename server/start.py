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

#!/usr/bin/env python3
"""
Startup script for the OpenNVR Surveillance System API.
Handles database initialization and application startup.
"""

import os
import platform
import sys
from pathlib import Path


def check_virtual_environment():
    """Check if virtual environment is activated."""
    if hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    ):
        print("✓ Virtual environment is activated")
        return True
    else:
        print("⚠️  Virtual environment is not activated")
        print("Please activate the virtual environment first:")
        if platform.system() == "Windows":
            print("   venv\\Scripts\\activate")
        else:
            print("   source venv/bin/activate")
        return False


def check_dependencies():
    """Check if required dependencies are installed."""
    try:
        import fastapi
        import psycopg2
        import sqlalchemy

        print("✓ All dependencies are installed")
        return True
    except ImportError as e:
        print(f"✗ Missing dependency: {e}")
        print("Please run: python setup_venv.py to set up the virtual environment")
        return False


def check_env_file():
    """Check if .env file exists and create from template if needed."""
    env_file = Path(".env")
    env_example = Path("env.example")

    if not env_file.exists():
        if env_example.exists():
            print("Creating .env file from template...")
            with open(env_example) as f:
                content = f.read()

            with open(env_file, "w") as f:
                f.write(content)
            print("✓ Created .env file from template")
            print("⚠️  Please edit .env file with your database credentials")
            return False
        else:
            print("✗ No .env file or env.example template found")
            return False
    else:
        print("✓ .env file exists")
        return True


def initialize_database():
    """Initialize the database with tables and initial data."""
    try:
        from scripts.init_db import main

        # Add scripts directory to path so it can find other modules if needed
        sys.path.append(os.path.join(os.path.dirname(__file__), "scripts"))
        print("Initializing database...")
        main()
        print("✓ Database initialized successfully")
        return True
    except Exception as e:
        print(f"✗ Database initialization failed: {e}")
    except Exception as e:
        print(f"✗ Database initialization failed: {e}")
        return False


def run_migrations():
    """Run Alembic migrations to upgrade database to latest head."""
    try:
        print("Checking/Running database migrations...")
        from alembic import command
        from alembic.config import Config

        # Point to alembic.ini in the server directory
        alembic_cfg_path = os.path.join(os.path.dirname(__file__), "alembic.ini")
        alembic_cfg = Config(alembic_cfg_path)

        # Execute upgrade head
        command.upgrade(alembic_cfg, "head")
        print("✓ Database migrations up to date")
        return True
    except Exception as e:
        print(f"✗ Migration failed: {e}")
        return False


def start_application():
    """Start the FastAPI application."""
    try:
        print("Starting FastAPI application...")
        import uvicorn

        from core.config import settings

        print(f"Server will be available at: http://{settings.host}:{settings.port}")
        print(f"API Documentation: http://{settings.host}:{settings.port}/docs")
        print("Press Ctrl+C to stop the server")

        uvicorn.run(
            "main:app",
            host=settings.host,
            port=settings.port,
            reload=settings.debug,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\n✓ Server stopped by user")
    except Exception as e:
        print(f"✗ Failed to start application: {e}")
        return False


def main():
    """Main startup function."""
    print("🚀 Starting OpenNVR Surveillance System API...")
    print("=" * 50)

    # Check virtual environment
    if not check_virtual_environment():
        sys.exit(1)

    # Check dependencies
    if not check_dependencies():
        sys.exit(1)

    # Check environment file
    if not check_env_file():
        print("\nPlease configure your .env file and run the script again.")
        sys.exit(1)

    # Initialize database
    if not check_env_file():
        sys.exit(1)

    # Run migrations before initializing/seeding DB
    if not run_migrations():
        print(
            "⚠️  Warning: Database migrations failed. Application may not retrieve data correctly."
        )
        # We don't exit here because sometimes it's just a lock issue or minor thing,
        # and init_db might still work if schemas are close enough.

    if not initialize_database():
        print(
            "\nDatabase initialization failed. Please check your database connection."
        )
        sys.exit(1)

    print("\n" + "=" * 50)
    print("🎯 All systems ready! Starting application...")
    print("=" * 50)

    # Start application
    start_application()


if __name__ == "__main__":
    main()

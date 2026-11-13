# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Validation details consumed by the user-management form."""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_user_validation_contract.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from schemas import UserCreate


def test_short_username_validation_keeps_field_location() -> None:
    """Keep the username location available for field-aware UI messages."""
    with pytest.raises(ValidationError) as exc_info:
        UserCreate(
            username="jv",
            email="jv@example.com",
            password="Str0ng!passw0rd",
            role_id=1,
        )

    error = exc_info.value.errors()[0]
    assert error["loc"] == ("username",)
    assert error["type"] == "string_too_short"

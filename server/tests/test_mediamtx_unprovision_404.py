# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A delete whose path is already gone counts as a successful teardown (#926).

MediaMTX answers 404 ``path configuration not found`` when the path to delete
no longer exists. For a delete that is the goal already met, so
``unprovision_path`` must report ``ok`` rather than ``error``. A real failure
(500) must still report ``error``.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_unprovision_404_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from services.mediamtx_admin_service import MediaMtxAdminService  # noqa: E402
from core.config import settings  # noqa: E402


@pytest.fixture
def admin_api(monkeypatch):
    monkeypatch.setattr(settings, "mediamtx_admin_api", "http://mediamtx.test/v3")
    monkeypatch.setattr(MediaMtxAdminService, "_headers", staticmethod(lambda: {}))
    return monkeypatch


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _client_with(monkeypatch, handler):
    def factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_unprovision_path_404_is_success_and_500_is_error(admin_api):
    import asyncio

    def gone(request):
        return httpx.Response(
            404, json={"error": "path configuration not found"}
        )

    _client_with(admin_api, gone)
    result = asyncio.run(MediaMtxAdminService.unprovision_path(9, "0.0.0.0"))
    assert result["http_status"] == 404
    assert result["status"] == "ok", f"expected ok, got {result['status']!r}"
    assert result["sub_http_status"] == 404

    def broken(request):
        return httpx.Response(500, json={"error": "boom"})

    _client_with(admin_api, broken)
    result = asyncio.run(MediaMtxAdminService.unprovision_path(9, "0.0.0.0"))
    assert result["status"] == "error"


def test_unprovision_path_other_404_stays_error(admin_api):
    import asyncio

    def wrong_route(request):
        return httpx.Response(404, text="404 page not found")

    _client_with(admin_api, wrong_route)
    result = asyncio.run(MediaMtxAdminService.unprovision_path(9, "0.0.0.0"))
    assert result["http_status"] == 404
    assert result["status"] == "error", f"expected error, got {result['status']!r}"

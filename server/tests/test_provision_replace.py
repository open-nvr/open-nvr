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

"""Regression tests for the atomic path-replace provisioning flow.

Background: push_rtsp_stream used to resolve a "path already exists"
conflict with unprovision + re-provision — two MediaMTX config reloads
with a window where the path didn't exist. A re-add that lost the race
with MediaMTX's API-listener reload left the camera with NO path at all
(no live view, no recording) until manual repair. The fix routes the
conflict through POST /config/paths/replace/{name}, a single config
transaction. These tests pin that behavior.
"""

import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
# Standard test-module env bootstrap (see test_device_firewall.py): lets this
# file run STANDALONE (pytest tests/test_provision_replace.py) instead of
# depending on an alphabetically-earlier module having built Settings first.
os.environ.setdefault("DATABASE_URL", "sqlite:///./_mtx_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest

from services.mediamtx_admin_service import MediaMtxAdminService


@pytest.mark.asyncio
async def test_push_rtsp_stream_replaces_in_place_on_conflict(monkeypatch):
    """On 'path already exists', push_rtsp_stream must go through
    replace_path and must NEVER delete the existing path first."""
    calls = {"replace": 0}

    async def _conflict(*a, **kw):
        return {
            "status": "error",
            "http_status": 400,
            "details": {"error": "path already exists"},
        }

    async def _replace(*a, **kw):
        calls["replace"] += 1
        return {"status": "ok", "http_status": 200, "details": {}}

    async def _forbidden_unprovision(*a, **kw):
        raise AssertionError(
            "unprovision_path called during conflict resolution — the "
            "delete + re-add window is exactly the bug this flow fixes"
        )

    monkeypatch.setattr(MediaMtxAdminService, "provision_path", _conflict)
    monkeypatch.setattr(MediaMtxAdminService, "replace_path", _replace)
    monkeypatch.setattr(
        MediaMtxAdminService, "unprovision_path", _forbidden_unprovision
    )

    result = await MediaMtxAdminService.push_rtsp_stream(
        camera_id=3,
        camera_ip="192.168.0.103",
        rtsp_url="rtsp://192.168.0.103:554/stream",
    )

    assert calls["replace"] == 1
    assert result.get("status") == "ok"
    assert result.get("action") == "rtsp_stream_replaced"


@pytest.mark.asyncio
async def test_push_rtsp_stream_surfaces_replace_failure(monkeypatch):
    """A failed replace must be reported as such — and the old path is
    still intact, unlike the old flow where failure meant no path."""

    async def _conflict(*a, **kw):
        return {
            "status": "error",
            "http_status": 400,
            "details": {"error": "path already exists"},
        }

    async def _replace_fails(*a, **kw):
        return {
            "status": "error",
            "http_status": 500,
            "details": {"error": "boom"},
        }

    monkeypatch.setattr(MediaMtxAdminService, "provision_path", _conflict)
    monkeypatch.setattr(MediaMtxAdminService, "replace_path", _replace_fails)

    result = await MediaMtxAdminService.push_rtsp_stream(
        camera_id=3,
        camera_ip="192.168.0.103",
        rtsp_url="rtsp://192.168.0.103:554/stream",
    )

    assert result.get("status") == "error"
    assert result.get("action") == "replace_failed"


@pytest.mark.asyncio
async def test_replace_path_falls_back_to_add_when_path_missing(monkeypatch):
    """If the path vanished between the add-conflict and the replace
    (concurrent unprovision), replace_path retries as a plain add."""
    reached = {"provision": 0}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **kw):
            class _R:
                status_code = 404
                is_success = False

                def json(self):
                    return {"error": "path not found: 'cam-3'"}

                @property
                def text(self):
                    return '{"error": "path not found"}'

            return _R()

    async def _record_provision(*a, **kw):
        reached["provision"] += 1
        return {"status": "ok", "http_status": 200, "details": {}}

    import services.mediamtx_admin_service as mam

    monkeypatch.setattr(mam.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(
        MediaMtxAdminService, "is_configured", staticmethod(lambda: True)
    )
    monkeypatch.setattr(
        MediaMtxAdminService,
        "_base",
        staticmethod(lambda: "http://stub.mediamtx.invalid"),
    )
    monkeypatch.setattr(MediaMtxAdminService, "provision_path", _record_provision)

    result = await MediaMtxAdminService.replace_path(
        camera_id=3,
        camera_ip="192.168.0.103",
        config={"source_url": "rtsp://192.168.0.103:554/stream"},
    )

    assert reached["provision"] == 1, "missing path must fall back to add"
    assert result.get("status") == "ok"


@pytest.mark.asyncio
async def test_replace_path_enforces_policy_before_http(monkeypatch):
    """replace_path is a new MediaMTX entry point, so it must run the
    same transport-policy gate as provision_path — before any HTTP."""
    from services.transport_probe_service import TransportPolicyViolation

    class _ForbiddenHttpx:
        def __init__(self, *a, **kw):
            raise AssertionError("HTTP reached despite policy violation")

    import services.mediamtx_admin_service as mam

    monkeypatch.setattr(mam.httpx, "AsyncClient", _ForbiddenHttpx)
    monkeypatch.setattr(
        MediaMtxAdminService, "is_configured", staticmethod(lambda: True)
    )

    with pytest.raises(TransportPolicyViolation):
        await MediaMtxAdminService.replace_path(
            camera_id=3,
            camera_ip="192.168.0.103",
            config={"source_url": "rtsp://192.168.0.103:554/stream"},
            transport_security="rtsps_required",
        )


@pytest.mark.asyncio
async def test_replace_skips_when_config_unchanged(monkeypatch):
    """Identical config must be a true no-op: no replace POST, no config
    reload, no stream blip — repeat provisions become free."""
    posts = {"n": 0}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, **kw):
            class _R:
                is_success = True

                def json(self):
                    # exactly what _map_conf produces for this config
                    return {"source": "rtsp://192.168.0.103:554/stream",
                            "extra_mediamtx_default": "untouched"}

            return _R()

        async def post(self, *a, **kw):
            posts["n"] += 1
            raise AssertionError("replace POSTed despite unchanged config")

    import services.mediamtx_admin_service as mam

    monkeypatch.setattr(mam.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(
        MediaMtxAdminService, "is_configured", staticmethod(lambda: True)
    )
    monkeypatch.setattr(
        MediaMtxAdminService, "_base",
        staticmethod(lambda: "http://stub.mediamtx.invalid"),
    )

    result = await MediaMtxAdminService.replace_path(
        camera_id=3,
        camera_ip="192.168.0.103",
        config={"source_url": "rtsp://192.168.0.103:554/stream"},
    )
    assert posts["n"] == 0
    assert result.get("status") == "ok"
    assert result.get("unchanged") is True


@pytest.mark.asyncio
async def test_replace_proceeds_when_config_differs(monkeypatch):
    """A changed source URL must still replace (the blip is then necessary)."""
    posts = {"n": 0}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, **kw):
            class _R:
                is_success = True

                def json(self):
                    return {"source": "rtsp://OLD-URL:554/stream"}

            return _R()

        async def post(self, url, **kw):
            posts["n"] += 1

            class _R:
                status_code = 200
                is_success = True

                def json(self):
                    return {}

                @property
                def text(self):
                    return "{}"

            return _R()

    import services.mediamtx_admin_service as mam

    monkeypatch.setattr(mam.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(
        MediaMtxAdminService, "is_configured", staticmethod(lambda: True)
    )
    monkeypatch.setattr(
        MediaMtxAdminService, "_base",
        staticmethod(lambda: "http://stub.mediamtx.invalid"),
    )

    result = await MediaMtxAdminService.replace_path(
        camera_id=3,
        camera_ip="192.168.0.103",
        config={"source_url": "rtsp://192.168.0.103:554/stream"},
    )
    assert posts["n"] == 1
    assert result.get("status") == "ok"
    assert not result.get("unchanged")

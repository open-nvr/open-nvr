# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""MediaMtxAdminService.kick_path_sessions: revoking a publisher must end
its running sessions, because MediaMTX checks a publish JWT only when a
session starts (issue #925, camera ingest over 443)."""
import os
import secrets
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
try:
    from cryptography.fernet import Fernet

    os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:  # pragma: no cover
    pass

from core.config import settings  # noqa: E402
from services import mediamtx_admin_service as mod  # noqa: E402
from services.mediamtx_admin_service import MediaMtxAdminService  # noqa: E402


def _fake_mediamtx(sessions: dict[str, list[dict]], kick_status: int = 200, list_status: dict | None = None):
    """A MockTransport that serves /v3/<kind>/list (two items per page) and
    records /v3/<kind>/kick/<id>."""
    kicked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")  # v3, kind, op, [id]
        kind, op = parts[1], parts[2]
        if op == "list":
            if list_status and kind in list_status:
                return httpx.Response(list_status[kind])
            items = sessions.get(kind, [])
            page = int(request.url.params.get("page", 0))
            per = 2
            chunk = items[page * per : (page + 1) * per]
            pages = (len(items) + per - 1) // per
            return httpx.Response(200, json={"itemCount": len(items), "pageCount": pages, "items": chunk})
        if op == "kick":
            kicked.append(f"{kind}:{parts[3]}")
            return httpx.Response(kick_status)
        return httpx.Response(404)

    return httpx.MockTransport(handler), kicked


@pytest.fixture
def mediamtx(monkeypatch):
    monkeypatch.setattr(settings, "mediamtx_admin_api", "http://mediamtx:9997/v3", raising=False)
    monkeypatch.setattr(MediaMtxAdminService, "_headers", classmethod(lambda cls: {}))

    def install(sessions, **kw):
        transport, kicked = _fake_mediamtx(sessions, **kw)
        real = httpx.AsyncClient

        def client(*a, **k):
            k["transport"] = transport
            return real(*a, **k)

        monkeypatch.setattr(mod.httpx, "AsyncClient", client)
        return kicked

    return install


@pytest.mark.asyncio
async def test_kicks_only_sessions_on_the_given_paths_across_pages_and_kinds(mediamtx):
    kicked = mediamtx({
        "rtspsessions": [
            {"id": "a", "path": "cam-7"},
            {"id": "b", "path": "cam-8"},
            {"id": "c", "path": "cam-7-sub"},  # page 2
            {"id": "d", "path": "cam-9"},
        ],
        "rtspssessions": [{"id": "e", "path": "cam-7"}],
    })
    out = await MediaMtxAdminService.kick_path_sessions(["cam-7", "cam-7-sub"])
    assert out["status"] == "ok"
    assert sorted(kicked) == ["rtspsessions:a", "rtspsessions:c", "rtspssessions:e"]
    assert len(out["kicked"]) == 3 and out["errors"] == []


@pytest.mark.asyncio
async def test_a_failing_list_is_reported_and_the_other_kind_still_swept(mediamtx):
    kicked = mediamtx(
        {"rtspssessions": [{"id": "e", "path": "cam-7"}]},
        list_status={"rtspsessions": 500},
    )
    out = await MediaMtxAdminService.kick_path_sessions(["cam-7"])
    assert out["status"] == "partial"
    assert kicked == ["rtspssessions:e"]
    assert any("rtspsessions/list: HTTP 500" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_nothing_to_kick_and_no_admin_api(mediamtx, monkeypatch):
    mediamtx({"rtspsessions": [{"id": "a", "path": "cam-1"}]})
    assert (await MediaMtxAdminService.kick_path_sessions([]))["kicked"] == []
    monkeypatch.setattr(settings, "mediamtx_admin_api", "", raising=False)
    assert (await MediaMtxAdminService.kick_path_sessions(["cam-1"]))["status"] == "no_admin_api"

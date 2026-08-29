# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tests for the WebRTC ICE host advertisement (``webrtcAdditionalHosts``).

The bug these pin: MediaMTX on the Docker bridge can only gather 127.0.0.1 and
172.28.0.x as ICE candidates. Neither is routable from a LAN browser, so with
nothing else advertised every WHEP session dies on a 10s ICE timeout and Live
View silently degrades to HLS. ``MEDIAMTX_WEBRTC_HOSTS`` used to be the only
source and was baked in at container-create time, so a bare
``docker compose up -d`` wiped it.

Coverage:

* ``is_advertisable`` rejects everything a browser could not use — crucially
  the Docker bridge addresses, which are exactly what MediaMTX already offers.
* ``is_trusted_proxy`` gates ``X-Server-Addr`` so a client cannot inject
  arbitrary hosts into MediaMTX's config.
* ``resolve`` seed order: learned values first, env as fallback seed, capped.
* ``learn`` persists, dedupes, evicts oldest, and pushes to MediaMTX.
* ``apply_to_mediamtx`` warns rather than silently doing nothing when there is
  no advertisable host — the silence is what hid the original bug.

    cd server && pytest tests/test_webrtc_ice_hosts.py -v
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
try:
    from cryptography.fernet import Fernet

    os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:  # pragma: no cover - cryptography is a hard dep in practice
    pass
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")

# A sibling test stubs core.logging_config via sys.modules.setdefault; drop it
# so we import the real module and get every logger our imports expect.
sys.modules.pop("core.logging_config", None)

from core.config import settings  # noqa: E402
from services.mediamtx_admin_service import MediaMtxAdminService  # noqa: E402
from services.webrtc_ice_host_service import (  # noqa: E402
    MAX_HOSTS,
    SETTING_KEY,
    WebRTCIceHostService,
)

# ---------------------------------------------------------------- fake DB


class _FakeRow:
    def __init__(self, key: str, json_value: str) -> None:
        self.key = key
        self.json_value = json_value


class _FakeQuery:
    def __init__(self, rows: dict[str, _FakeRow]) -> None:
        self._rows = rows
        self._key: str | None = None

    def filter(self, criterion):
        # The service always filters SecuritySetting.key == <literal>.
        self._key = criterion.right.value
        return self

    def first(self):
        return self._rows.get(self._key)


class FakeDB:
    """Just enough Session surface for load/store."""

    def __init__(self) -> None:
        self.rows: dict[str, _FakeRow] = {}
        self.commits = 0

    def query(self, _model):
        return _FakeQuery(self.rows)

    def add(self, row) -> None:
        self.rows[row.key] = row

    def commit(self) -> None:
        self.commits += 1

    # test helper
    def stored(self) -> list[str]:
        row = self.rows.get(SETTING_KEY)
        return json.loads(row.json_value) if row else []


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


# ------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",  # container loopback — MediaMTX already offers this
        "172.28.0.2",  # the Docker bridge — the address that broke live view
        "169.254.10.1",  # link-local
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "not-an-ip",
        "",
        "192.168.31.67:8189",  # port suffix is not an address
    ],
)
def test_rejects_unusable_ice_hosts(addr: str) -> None:
    assert WebRTCIceHostService.is_advertisable(addr) is False


@pytest.mark.parametrize("addr", ["192.168.31.67", "10.20.30.40", "49.43.161.182"])
def test_accepts_routable_ice_hosts(addr: str) -> None:
    assert WebRTCIceHostService.is_advertisable(addr) is True


def test_bridge_peer_may_set_the_header_but_a_lan_client_may_not() -> None:
    # nginx reaches the backend from the pinned Docker subnet.
    assert WebRTCIceHostService.is_trusted_proxy("172.28.0.8") is True
    assert WebRTCIceHostService.is_trusted_proxy("127.0.0.1") is True
    # A browser connecting directly must never be able to inject a host.
    assert WebRTCIceHostService.is_trusted_proxy("192.168.31.50") is False
    assert WebRTCIceHostService.is_trusted_proxy("") is False


# -------------------------------------------------------------- resolve


def test_resolve_prefers_learned_over_env_seed(db: FakeDB, monkeypatch) -> None:
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "10.0.0.5", raising=False)
    WebRTCIceHostService.store(db, ["192.168.31.67"])

    # Learned first, seed appended — a stale env value cannot mask the address
    # we have actually observed working.
    assert WebRTCIceHostService.resolve(db) == ["192.168.31.67", "10.0.0.5"]


def test_resolve_dedupes_and_drops_unusable_seed(db: FakeDB, monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "mediamtx_webrtc_hosts",
        "192.168.31.67, 172.28.0.2 , , 127.0.0.1",
        raising=False,
    )
    WebRTCIceHostService.store(db, ["192.168.31.67"])

    # The duplicate collapses; the bridge and loopback seeds are filtered out.
    assert WebRTCIceHostService.resolve(db) == ["192.168.31.67"]


def test_resolve_is_empty_when_nothing_is_known(db: FakeDB, monkeypatch) -> None:
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "", raising=False)
    assert WebRTCIceHostService.resolve(db) == []


# ---------------------------------------------------------------- learn


@pytest.mark.asyncio
async def test_learn_records_and_pushes_a_new_address(db: FakeDB, monkeypatch) -> None:
    pushed: list[list[str]] = []

    async def _fake_push(hosts):
        pushed.append(hosts)
        return {"status": "ok"}

    monkeypatch.setattr(
        MediaMtxAdminService, "set_webrtc_additional_hosts", _fake_push, raising=False
    )
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "", raising=False)

    assert await WebRTCIceHostService.learn(db, "192.168.31.67") is True
    assert db.stored() == ["192.168.31.67"]
    assert pushed == [["192.168.31.67"]]


@pytest.mark.asyncio
async def test_learn_is_a_noop_for_a_known_address(db: FakeDB, monkeypatch) -> None:
    called = False

    async def _fake_push(hosts):
        nonlocal called
        called = True
        return {"status": "ok"}

    monkeypatch.setattr(
        MediaMtxAdminService, "set_webrtc_additional_hosts", _fake_push, raising=False
    )
    WebRTCIceHostService.store(db, ["192.168.31.67"])
    before = db.commits

    assert await WebRTCIceHostService.learn(db, "192.168.31.67") is False
    # No write and no MediaMTX call on the steady-state path — this runs on
    # every stream-info request.
    assert db.commits == before
    assert called is False


@pytest.mark.asyncio
async def test_learn_refuses_an_unusable_address(db: FakeDB) -> None:
    assert await WebRTCIceHostService.learn(db, "172.28.0.2") is False
    assert await WebRTCIceHostService.learn(db, "evil.example") is False
    assert db.stored() == []


@pytest.mark.asyncio
async def test_learn_keeps_newest_first_and_evicts_oldest(
    db: FakeDB, monkeypatch
) -> None:
    async def _fake_push(hosts):
        return {"status": "ok"}

    monkeypatch.setattr(
        MediaMtxAdminService, "set_webrtc_additional_hosts", _fake_push, raising=False
    )
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "", raising=False)

    for i in range(MAX_HOSTS + 3):
        await WebRTCIceHostService.learn(db, f"10.0.0.{i + 1}")

    stored = db.stored()
    assert len(stored) == MAX_HOSTS
    # Most recently seen wins; a host that moves repeatedly keeps working.
    assert stored[0] == f"10.0.0.{MAX_HOSTS + 3}"
    assert "10.0.0.1" not in stored


# ------------------------------------------------------------ application


@pytest.mark.asyncio
async def test_apply_warns_instead_of_failing_silently(
    db: FakeDB, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "", raising=False)

    with caplog.at_level("WARNING"):
        assert await WebRTCIceHostService.apply_to_mediamtx(db) is False

    # The original bug was invisible; an empty list must say so out loud.
    assert any("No WebRTC ICE host" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_apply_pushes_the_resolved_list(db: FakeDB, monkeypatch) -> None:
    pushed: list[list[str]] = []

    async def _fake_push(hosts):
        pushed.append(hosts)
        return {"status": "ok"}

    monkeypatch.setattr(
        MediaMtxAdminService, "set_webrtc_additional_hosts", _fake_push, raising=False
    )
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "10.0.0.5", raising=False)
    WebRTCIceHostService.store(db, ["192.168.31.67"])

    assert await WebRTCIceHostService.apply_to_mediamtx(db) is True
    assert pushed == [["192.168.31.67", "10.0.0.5"]]

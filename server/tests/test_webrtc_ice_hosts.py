# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tests for the WebRTC ICE host advertisement (``webrtcAdditionalHosts``), #925.

MediaMTX on the Docker bridge can only gather 127.0.0.1 and 172.28.0.x as ICE
candidates, which no browser on another machine can reach. The service builds
the list MediaMTX advertises instead: the admin's public address, the public
IP found with STUN, and the LAN seed from start.sh, in that order.

Coverage:

* ``is_advertisable`` / ``is_hostname``: what may go into the list.
* STUN: URL parsing, Binding Success parsing (XOR-MAPPED first, MAPPED as
  the fallback, wrong transaction ignored), discovery over several servers,
  and the refresh cache.
* ``resolve_entries``: order, dedup, filtering, the cap, and the source tag
  the settings page shows.
* ``migrate_legacy``: a hand-made ``webrtc_ice_hosts`` row moves into the
  admin's address once and the row goes away.
* ``apply_to_mediamtx`` warns rather than silently doing nothing when there
  is no advertisable host; otherwise it pushes the resolved list.

    cd server && pytest tests/test_webrtc_ice_hosts.py -v
"""

from __future__ import annotations

import json
import os
import secrets
import struct
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
    WEBRTC_ROW_KEY,
    PublicIP,
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
    """Just enough Session surface for the service."""

    def __init__(self) -> None:
        self.rows: dict[str, _FakeRow] = {}
        self.commits = 0

    def query(self, _model):
        return _FakeQuery(self.rows)

    def add(self, row) -> None:
        self.rows[row.key] = row

    def delete(self, row) -> None:
        self.rows.pop(row.key, None)

    def commit(self) -> None:
        self.commits += 1

    # test helpers
    def set_webrtc(self, **fields) -> None:
        self.rows[WEBRTC_ROW_KEY] = _FakeRow(WEBRTC_ROW_KEY, json.dumps(fields))

    def set_legacy(self, hosts: list[str]) -> None:
        self.rows[SETTING_KEY] = _FakeRow(SETTING_KEY, json.dumps(hosts))

    def webrtc(self) -> dict:
        row = self.rows.get(WEBRTC_ROW_KEY)
        return json.loads(row.json_value) if row else {}


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Class-level caches must not leak between tests; no background task."""
    WebRTCIceHostService._public = PublicIP()
    WebRTCIceHostService._resolved = {}
    WebRTCIceHostService._refresher = None
    monkeypatch.setattr(WebRTCIceHostService, "ensure_refresher", classmethod(lambda cls: None))
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "", raising=False)
    yield


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


@pytest.mark.parametrize("name,ok", [
    ("nvr.example.org", True),
    ("my-site.dyndns.example", True),
    ("localhost", False),
    ("192.168.1.1", False),  # an IP is not a hostname
    ("bad_label.example", False),
    ("-dash.example", False),
    ("", False),
])
def test_hostnames(name: str, ok: bool) -> None:
    assert WebRTCIceHostService.is_hostname(name) is ok


# ------------------------------------------------------------------ STUN


@pytest.mark.parametrize("url,expected", [
    ("stun:stun.example.org:3478", ("stun.example.org", 3478)),
    ("stun:stun.example.org", ("stun.example.org", 3478)),
    ("stun:[2001:db8::1]:19302", ("2001:db8::1", 19302)),
    ("turn:relay.example.org:3478", None),
    ("stuns:stun.example.org:5349", None),
    ("stun:", None),
])
def test_stun_url_parsing(url: str, expected) -> None:
    assert WebRTCIceHostService.parse_stun_url(url) == expected


def _binding_success(txid: bytes, ip: str, port: int, xor: bool = True, kind: int | None = None) -> bytes:
    import ipaddress

    raw = ipaddress.ip_address(ip).packed
    family = 0x01 if len(raw) == 4 else 0x02
    if xor:
        key = struct.pack("!I", 0x2112A442) + (txid if family == 0x02 else b"")
        raw = bytes(a ^ b for a, b in zip(raw, key))
        port ^= 0x2112
    attr_type = kind if kind is not None else (0x0020 if xor else 0x0001)
    value = struct.pack("!BBH", 0, family, port) + raw
    attr = struct.pack("!HH", attr_type, len(value)) + value
    return struct.pack("!HHI", 0x0101, len(attr), 0x2112A442) + txid + attr


def test_stun_response_xor_mapped_v4_and_v6() -> None:
    txid = bytes(range(12))
    assert WebRTCIceHostService.parse_stun_response(_binding_success(txid, "171.78.153.53", 52858), txid) == "171.78.153.53"
    assert WebRTCIceHostService.parse_stun_response(_binding_success(txid, "2001:db8::10", 4000), txid) == "2001:db8::10"


def test_stun_response_falls_back_to_mapped_and_ignores_other_transactions() -> None:
    txid = b"\x01" * 12
    plain = _binding_success(txid, "203.0.113.9", 1234, xor=False)
    assert WebRTCIceHostService.parse_stun_response(plain, txid) == "203.0.113.9"
    assert WebRTCIceHostService.parse_stun_response(plain, b"\x02" * 12) is None
    assert WebRTCIceHostService.parse_stun_response(b"\x00" * 10, txid) is None


@pytest.mark.asyncio
async def test_discovery_takes_the_first_usable_answer(monkeypatch) -> None:
    calls: list[str] = []

    async def _query(cls, host, port, timeout):
        calls.append(host)
        if host == "down.example":
            raise OSError("unreachable")
        if host == "cgnat.example":
            return "172.28.0.9"  # a bridge-looking answer is not advertisable
        return "171.78.153.53"

    monkeypatch.setattr(WebRTCIceHostService, "_stun_query", classmethod(_query))
    got = await WebRTCIceHostService.discover_public_ip(
        ["turn:ignored.example:3478", "stun:down.example", "stun:cgnat.example", "stun:good.example:3478"]
    )
    assert got == ("171.78.153.53", "stun:good.example:3478")
    assert calls == ["down.example", "cgnat.example", "good.example"]


@pytest.mark.asyncio
async def test_refresh_caches_until_forced(db: FakeDB, monkeypatch) -> None:
    answers = iter([("1.2.3.4", "stun:a"), ("5.6.7.8", "stun:a")])
    asked = 0

    async def _discover(cls, servers, timeout=2.0):
        nonlocal asked
        asked += 1
        return next(answers)

    monkeypatch.setattr(WebRTCIceHostService, "discover_public_ip", classmethod(_discover))
    db.set_webrtc(stun_servers=["stun:a"])
    assert await WebRTCIceHostService.refresh_public_ip(db) == "1.2.3.4"
    assert await WebRTCIceHostService.refresh_public_ip(db) == "1.2.3.4"
    assert asked == 1
    assert await WebRTCIceHostService.refresh_public_ip(db, force=True) == "5.6.7.8"
    assert asked == 2


@pytest.mark.asyncio
async def test_no_stun_servers_means_no_lookup(db: FakeDB, monkeypatch) -> None:
    async def _boom(cls, servers, timeout=2.0):
        raise AssertionError("must not be called")

    monkeypatch.setattr(WebRTCIceHostService, "discover_public_ip", classmethod(_boom))
    db.set_webrtc(stun_servers=[])
    assert await WebRTCIceHostService.refresh_public_ip(db) is None


# -------------------------------------------------------------- resolve


def test_resolve_order_admin_then_stun_then_seed(db: FakeDB, monkeypatch) -> None:
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "192.168.2.51", raising=False)
    db.set_webrtc(public_address="nvr.example.org")
    WebRTCIceHostService._public = PublicIP("171.78.153.53", 1.0, "stun:a")

    assert WebRTCIceHostService.resolve_entries(db) == [
        {"host": "nvr.example.org", "source": "admin"},
        {"host": "171.78.153.53", "source": "stun"},
        {"host": "192.168.2.51", "source": "env"},
    ]
    assert WebRTCIceHostService.resolve(db) == ["nvr.example.org", "171.78.153.53", "192.168.2.51"]


def test_resolve_dedupes_filters_and_caps(db: FakeDB, monkeypatch) -> None:
    seed = ", ".join([f"10.0.0.{i}" for i in range(1, 12)] + ["172.28.0.2", "127.0.0.1"])
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", seed, raising=False)
    db.set_webrtc(public_address="10.0.0.1")  # same as the first seed entry
    WebRTCIceHostService._public = PublicIP("10.0.0.1", 1.0, "stun:a")

    hosts = WebRTCIceHostService.resolve(db)
    assert hosts[0] == "10.0.0.1" and hosts.count("10.0.0.1") == 1
    assert "172.28.0.2" not in hosts and "127.0.0.1" not in hosts
    assert len(hosts) == MAX_HOSTS


def test_resolve_is_empty_when_nothing_is_known(db: FakeDB) -> None:
    assert WebRTCIceHostService.resolve(db) == []


def test_admin_address_ignores_unusable_values(db: FakeDB) -> None:
    db.set_webrtc(public_address="127.0.0.1")
    assert WebRTCIceHostService.admin_address(db) is None
    db.set_webrtc(public_address="  ")
    assert WebRTCIceHostService.admin_address(db) is None
    db.set_webrtc(public_address="171.78.153.53")
    assert WebRTCIceHostService.admin_address(db) == "171.78.153.53"


def test_public_ice_port_falls_back_to_the_ice_port(db: FakeDB, monkeypatch) -> None:
    monkeypatch.setattr(settings, "webrtc_ice_port", 18189, raising=False)
    assert WebRTCIceHostService.public_ice_port(db) == 18189
    db.set_webrtc(public_ice_port=28189)
    assert WebRTCIceHostService.public_ice_port(db) == 28189


@pytest.mark.asyncio
async def test_resolved_ips_resolves_names_for_the_browser(db: FakeDB, monkeypatch) -> None:
    async def _lookup(cls, host):
        return ["203.0.113.5", "192.168.2.51"] if host == "nvr.example.org" else []

    monkeypatch.setattr(WebRTCIceHostService, "_lookup", classmethod(_lookup))
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "192.168.2.51", raising=False)
    db.set_webrtc(public_address="nvr.example.org")
    assert await WebRTCIceHostService.resolved_ips(db) == ["203.0.113.5", "192.168.2.51"]


# ------------------------------------------------------------- migration


def test_legacy_row_moves_into_the_public_address_once(db: FakeDB, monkeypatch) -> None:
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "192.168.2.51", raising=False)
    db.set_legacy(["171.78.153.53", "192.168.2.51"])

    assert WebRTCIceHostService.migrate_legacy(db) == "171.78.153.53"
    assert db.webrtc()["public_address"] == "171.78.153.53"
    assert SETTING_KEY not in db.rows
    # Nothing left to migrate the second time.
    assert WebRTCIceHostService.migrate_legacy(db) is None


def test_legacy_row_never_overwrites_an_admin_address(db: FakeDB) -> None:
    db.set_webrtc(public_address="nvr.example.org")
    db.set_legacy(["171.78.153.53"])
    assert WebRTCIceHostService.migrate_legacy(db) is None
    assert db.webrtc()["public_address"] == "nvr.example.org"
    assert SETTING_KEY not in db.rows


# ------------------------------------------------------------ application


@pytest.mark.asyncio
async def test_apply_warns_instead_of_failing_silently(db: FakeDB, monkeypatch, caplog) -> None:
    async def _none(cls, servers, timeout=2.0):
        return None

    monkeypatch.setattr(WebRTCIceHostService, "discover_public_ip", classmethod(_none))
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

    async def _discover(cls, servers, timeout=2.0):
        return ("171.78.153.53", "stun:a")

    monkeypatch.setattr(MediaMtxAdminService, "set_webrtc_additional_hosts", _fake_push, raising=False)
    monkeypatch.setattr(WebRTCIceHostService, "discover_public_ip", classmethod(_discover))
    monkeypatch.setattr(settings, "mediamtx_webrtc_hosts", "192.168.2.51", raising=False)
    db.set_webrtc(stun_servers=["stun:a"], public_address="nvr.example.org")

    assert await WebRTCIceHostService.apply_to_mediamtx(db) is True
    assert pushed == [["nvr.example.org", "171.78.153.53", "192.168.2.51"]]


@pytest.mark.asyncio
async def test_a_failed_stun_round_keeps_the_last_public_ip(db: FakeDB, monkeypatch) -> None:
    """One lost STUN exchange must not take the public candidate away from
    every remote viewer until the next refresh."""
    db.set_webrtc(stun_servers=["stun:a"])
    answers = iter([("1.2.3.4", "stun:a"), None, ("5.6.7.8", "stun:a")])

    async def fake_discover(cls, servers, timeout=2.0):
        return next(answers)

    monkeypatch.setattr(WebRTCIceHostService, "discover_public_ip", classmethod(fake_discover))
    WebRTCIceHostService._public = PublicIP()
    assert await WebRTCIceHostService.refresh_public_ip(db, force=True) == "1.2.3.4"
    assert await WebRTCIceHostService.refresh_public_ip(db, force=True) == "1.2.3.4"
    assert WebRTCIceHostService.public_ip().server == "stun:a"
    assert await WebRTCIceHostService.refresh_public_ip(db, force=True) == "5.6.7.8"


@pytest.mark.asyncio
async def test_removing_the_stun_servers_drops_the_public_ip(db: FakeDB, monkeypatch) -> None:
    db.set_webrtc(stun_servers=["stun:a"])

    async def fake_discover(cls, servers, timeout=2.0):
        return ("1.2.3.4", "stun:a")

    monkeypatch.setattr(WebRTCIceHostService, "discover_public_ip", classmethod(fake_discover))
    WebRTCIceHostService._public = PublicIP()
    assert await WebRTCIceHostService.refresh_public_ip(db, force=True) == "1.2.3.4"
    db.set_webrtc(stun_servers=[])
    assert await WebRTCIceHostService.refresh_public_ip(db, force=True) is None


@pytest.mark.asyncio
async def test_apply_without_stun_pushes_what_is_known(db: FakeDB, monkeypatch) -> None:
    """The settings save re-advertises at once and never waits on a STUN round."""
    from services.mediamtx_admin_service import MediaMtxAdminService

    db.set_webrtc(stun_servers=["stun:a"], public_address="nvr.example.org")
    pushed: list[list[str]] = []

    async def fake_set(hosts):
        pushed.append(list(hosts))
        return {"status": "ok"}

    async def never(cls, servers, timeout=2.0):
        raise AssertionError("STUN must not be asked with stun=False")

    monkeypatch.setattr(MediaMtxAdminService, "set_webrtc_additional_hosts", staticmethod(fake_set))
    monkeypatch.setattr(WebRTCIceHostService, "discover_public_ip", classmethod(never))
    monkeypatch.setattr(WebRTCIceHostService, "ensure_refresher", classmethod(lambda cls: None))
    WebRTCIceHostService._public = PublicIP()
    assert await WebRTCIceHostService.apply_to_mediamtx(db, stun=False) is True
    assert pushed == [["nvr.example.org"]]


def test_the_update_payload_refuses_a_bad_address_at_parse_time() -> None:
    """A typo in the Settings page is a 422 with the validator's words, not a
    500 from inside the route."""
    import pytest as _pytest
    from pydantic import ValidationError

    from schemas import WebRTCSettingsUpdate

    with _pytest.raises(ValidationError, match="IP address or a DNS name"):
        WebRTCSettingsUpdate(public_address="https://nvr.example.org")
    with _pytest.raises(ValidationError, match="other machines can reach"):
        WebRTCSettingsUpdate(public_address="127.0.0.1")
    assert WebRTCSettingsUpdate(public_address=" NVR.Example.org ").public_address == "nvr.example.org"
    assert WebRTCSettingsUpdate(public_address=None).public_address is None
    assert WebRTCSettingsUpdate(public_address="").public_address is None


# ------------------------------------------------------- the settings route


@pytest.mark.asyncio
async def test_saving_null_clears_the_public_address_and_port(db: FakeDB, monkeypatch) -> None:
    """The Settings page sends null for an emptied field; that must clear the
    stored value, not keep the old one (the merge's "None keeps" rule is for
    fields the payload left out)."""
    import types

    from routers import webrtc as webrtc_router
    from schemas import WebRTCSettingsUpdate

    pushed: list[list[str]] = []

    async def fake_set(hosts):
        pushed.append(list(hosts))
        return {"status": "ok"}

    monkeypatch.setattr(MediaMtxAdminService, "set_webrtc_additional_hosts", staticmethod(fake_set))
    monkeypatch.setattr(WebRTCIceHostService, "refresh_soon", classmethod(lambda cls: None))
    db.set_webrtc(public_address="nvr.example.org", public_ice_port=18189, stun_servers=["stun:a"])
    user = types.SimpleNamespace(id=1)

    # A payload that leaves the fields out keeps them.
    out = await webrtc_router.update_webrtc_settings(
        WebRTCSettingsUpdate(transport_policy="all"), db=db, current_user=user, request=None
    )
    assert out["public_address"] == "nvr.example.org"
    assert out["public_ice_port"] == 18189

    # Explicit nulls clear them, and the advertised list loses the address.
    out = await webrtc_router.update_webrtc_settings(
        WebRTCSettingsUpdate(public_address=None, public_ice_port=None), db=db, current_user=user, request=None
    )
    assert out["public_address"] is None
    assert out["public_ice_port"] is None
    assert db.webrtc()["public_address"] is None
    assert pushed and "nvr.example.org" not in pushed[-1]

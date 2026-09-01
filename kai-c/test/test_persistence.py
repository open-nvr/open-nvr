# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Issue #371 — adapter registrations must survive a KAI-C restart.

The failure this guards against: an app overlay's ONE-SHOT registrar
registers ``fast_plate_ocr`` at install time and exits; the registry was
in-memory only, so the next ``opennvr-core`` restart forgot the adapter,
every plate read 404'd, and nothing anywhere said so.

The fix has three legs, each tested here:

1. runtime registrations (and their permission grants) are persisted to
   a receipt file and restored on boot;
2. registrations that CANNOT complete yet — a seed or restored adapter
   whose container is still booting — are queued and retried by the poll
   loop instead of being dropped forever;
3. the unregistered-but-expected state is visible
   (``pending_registrations`` / the ``deferred`` field on the listing).

Plus the safety property that must hold through all of it: restore
re-applies ONLY the operator's recorded grants, intersected against the
freshly-declared key set — a restart must never widen approval.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from kai_c.audit import AuditStore
from kai_c.persistence import RegistryStateStore
from kai_c.registry import AdapterRegistry

from test_registry import _StubAdapter, _base_caps  # reuse the stub kit


@pytest.fixture
def audit(tmp_path: Path) -> AuditStore:
    return AuditStore(path=str(tmp_path / "audit.jsonl"))


def _store(tmp_path: Path) -> RegistryStateStore:
    return RegistryStateStore(tmp_path / "state")


def _registry_for(stub: _StubAdapter, audit: AuditStore,
                  store: RegistryStateStore | None) -> AdapterRegistry:
    transport = httpx.MockTransport(stub.respond)
    return AdapterRegistry(
        sovereignty_mode="local_only", audit=audit,
        http_client=httpx.AsyncClient(transport=transport),
        poll_interval_seconds=999,
        state_store=store,
    )


class _DownThenUpStub(_StubAdapter):
    """Refuses connections until ``bring_up()`` — the adapter container
    that boots slower than KAI-C."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.up = False

    def bring_up(self) -> None:
        self.up = True

    async def respond(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("connection refused", request=request)
        return await super().respond(request)


# ── The state store itself ─────────────────────────────────────────


def test_store_roundtrip(tmp_path: Path):
    store = _store(tmp_path)
    entries = [{"name": "fast_plate_ocr",
                "url": "http://ocr:9004",
                "granted_permissions": ["gpu"]}]
    store.save(entries)
    assert store.load() == entries


def test_store_missing_file_is_empty(tmp_path: Path):
    assert _store(tmp_path).load() == []


def test_store_disabled_is_inert(tmp_path: Path):
    store = RegistryStateStore(None)
    store.save([{"name": "x", "url": "http://x", "granted_permissions": []}])
    assert store.load() == []
    assert not store.enabled


def test_store_corrupt_file_degrades_to_empty(tmp_path: Path):
    store = _store(tmp_path)
    store.save([{"name": "a", "url": "http://a", "granted_permissions": []}])
    assert store.path is not None
    store.path.write_text("{not json", encoding="utf-8")
    # Corrupt state must mean "nothing persisted", never a crash.
    assert store.load() == []


def test_store_load_drops_malformed_entries(tmp_path: Path):
    store = _store(tmp_path)
    assert store.path is not None
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"version": 1, "adapters": [
        {"name": "good", "url": "http://good", "granted_permissions": ["gpu", 7]},
        {"name": "", "url": "http://nameless"},
        {"url": "http://no-name"},
        {"name": "no-url"},
        "not-a-dict",
    ]}), encoding="utf-8")
    loaded = store.load()
    assert loaded == [
        {"name": "good", "url": "http://good", "granted_permissions": ["gpu"]}
    ]


def test_store_save_survives_unwritable_dir(tmp_path: Path, caplog):
    target = tmp_path / "ro"
    target.mkdir()
    target.chmod(0o500)
    store = RegistryStateStore(target)
    try:
        # Must warn, must not raise — a full disk cannot break a grant.
        store.save([{"name": "a", "url": "http://a",
                     "granted_permissions": []}])
    finally:
        target.chmod(0o700)


# ── Persist on register / forget on deregister ─────────────────────


@pytest.mark.asyncio
async def test_runtime_registration_is_persisted(tmp_path: Path, audit):
    stub = _StubAdapter(url="http://127.0.0.1:9100",
                        capabilities=_base_caps())
    store = _store(tmp_path)
    reg = _registry_for(stub, audit, store)
    await reg.register("fast_plate_ocr", stub.url)
    assert store.load() == [{
        "name": "fast_plate_ocr", "url": stub.url,
        "granted_permissions": [],
    }]
    await reg.aclose()


@pytest.mark.asyncio
async def test_seed_registration_is_not_persisted(tmp_path: Path, audit):
    """Seeds re-register from config every boot; persisting them would
    resurrect a seed the operator removed from configuration."""
    stub = _StubAdapter(url="http://127.0.0.1:9100",
                        capabilities=_base_caps())
    store = _store(tmp_path)
    reg = _registry_for(stub, audit, store)
    await reg.register("default", stub.url, source="seed")
    assert store.load() == []
    await reg.aclose()


@pytest.mark.asyncio
async def test_grants_are_persisted(tmp_path: Path, audit):
    stub = _StubAdapter(url="http://127.0.0.1:9100",
                        capabilities=_base_caps(gpu=True))
    store = _store(tmp_path)
    reg = _registry_for(stub, audit, store)
    await reg.register("gpu_adapter", stub.url)
    reg.grant_permissions("gpu_adapter", ["gpu"], actor="operator")
    assert store.load()[0]["granted_permissions"] == ["gpu"]
    reg.revoke_permissions("gpu_adapter", ["gpu"], actor="operator")
    assert store.load()[0]["granted_permissions"] == []
    await reg.aclose()


@pytest.mark.asyncio
async def test_deregister_removes_receipt(tmp_path: Path, audit):
    """An operator-removed adapter must NOT resurrect on restart."""
    stub = _StubAdapter(url="http://127.0.0.1:9100",
                        capabilities=_base_caps())
    store = _store(tmp_path)
    reg = _registry_for(stub, audit, store)
    await reg.register("fast_plate_ocr", stub.url)
    await reg.deregister("fast_plate_ocr")
    assert store.load() == []
    # And a fresh registry restoring from the same store sees nothing.
    reg2 = _registry_for(stub, audit, store)
    assert reg2.restore_persisted() == []
    await reg.aclose()
    await reg2.aclose()


# ── Restore across a "restart" ─────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_restores_runtime_adapter_and_grants(tmp_path: Path, audit):
    """The issue-#371 scenario end to end: registrar registered +
    approved fast_plate_ocr; core restarts; the adapter container is
    still up; the new registry must bring it back — approved — without
    any re-registration from outside."""
    stub = _StubAdapter(url="http://127.0.0.1:9100",
                        capabilities=_base_caps(gpu=True))
    store = _store(tmp_path)
    reg1 = _registry_for(stub, audit, store)
    await reg1.register("fast_plate_ocr", stub.url)
    reg1.approve_all("fast_plate_ocr", actor="install")
    await reg1.aclose()

    # "Restart": brand-new registry over the same state file.
    reg2 = _registry_for(stub, audit, store)
    assert reg2.get("fast_plate_ocr") is None
    queued = reg2.restore_persisted()
    assert [p.name for p in queued] == ["fast_plate_ocr"]
    await reg2.retry_pending()

    restored = reg2.get("fast_plate_ocr")
    assert restored is not None
    assert restored.source == "runtime"
    assert restored.approval_status == "approved"
    assert restored.is_serving_allowed
    assert reg2.pending_registrations() == []
    await reg2.aclose()


@pytest.mark.asyncio
async def test_restore_never_widens_approval(tmp_path: Path, audit):
    """Sabotage guard for the security property: if the receipt has NO
    grants recorded, restore must land the adapter PENDING even though
    it declares permissions — a restart is not a consent act. (An
    approve-all-on-restore bug would pass the happy-path test above;
    this one catches it.)"""
    stub = _StubAdapter(url="http://127.0.0.1:9100",
                        capabilities=_base_caps(gpu=True))
    store = _store(tmp_path)
    reg1 = _registry_for(stub, audit, store)
    await reg1.register("gpu_adapter", stub.url)   # registered, NOT approved
    await reg1.aclose()

    reg2 = _registry_for(stub, audit, store)
    reg2.restore_persisted()
    await reg2.retry_pending()
    restored = reg2.get("gpu_adapter")
    assert restored is not None
    assert restored.approval_status == "pending"
    assert not restored.is_serving_allowed
    await reg2.aclose()


@pytest.mark.asyncio
async def test_restored_grants_intersect_freshly_declared_keys(tmp_path: Path, audit):
    """The adapter drifted while we were down: it now declares gpu AND
    host_metadata, but only gpu was granted before the restart. gpu must
    restore; host_metadata must stay pending → adapter pending overall."""
    stub = _StubAdapter(url="http://127.0.0.1:9100",
                        capabilities=_base_caps(gpu=True))
    store = _store(tmp_path)
    reg1 = _registry_for(stub, audit, store)
    await reg1.register("gpu_adapter", stub.url)
    reg1.grant_permissions("gpu_adapter", ["gpu"], actor="operator")
    await reg1.aclose()

    caps = _base_caps(gpu=True)
    caps["permissions"]["host_metadata"] = True
    stub.update_capabilities(caps)

    reg2 = _registry_for(stub, audit, store)
    reg2.restore_persisted()
    await reg2.retry_pending()
    restored = reg2.get("gpu_adapter")
    assert restored is not None
    assert restored.granted_permissions == {"gpu"}
    assert restored.approval_status == "pending"
    await reg2.aclose()


@pytest.mark.asyncio
async def test_seed_name_wins_over_persisted_entry(tmp_path: Path, audit):
    """Configuration beats history: a persisted runtime entry whose name
    collides with an already-registered seed is skipped, not fought over."""
    stub = _StubAdapter(url="http://127.0.0.1:9100",
                        capabilities=_base_caps())
    store = _store(tmp_path)
    store.save([{"name": "default", "url": "http://old-ghost:1",
                 "granted_permissions": []}])
    reg = _registry_for(stub, audit, store)
    await reg.register("default", stub.url, source="seed")
    assert reg.restore_persisted() == []
    adapter = reg.get("default")
    assert adapter is not None and adapter.url == stub.url
    await reg.aclose()


# ── Deferred registration + retry (the cold-boot race) ─────────────


@pytest.mark.asyncio
async def test_deferred_seed_retries_until_up_with_config_consent(tmp_path: Path, audit):
    """A seed whose container boots after KAI-C used to be 'deferred'
    forever. Now: queued, visible, retried — and once it registers, the
    §8.5 config-as-consent approve-all applies, exactly as if it had
    been up at boot."""
    stub = _DownThenUpStub(url="http://127.0.0.1:9100",
                           capabilities=_base_caps(gpu=True))
    reg = _registry_for(stub, audit, _store(tmp_path))
    reg.defer("default", stub.url, source="seed",
              grant_all_on_register=True, error="boot race")

    await reg.retry_pending()          # still down — stays queued
    pending = reg.pending_registrations()
    assert len(pending) == 1
    assert pending[0]["name"] == "default"
    assert pending[0]["attempts"] == 1
    assert reg.get("default") is None

    stub.bring_up()
    await reg.retry_pending()          # up now — registers + auto-grants
    adapter = reg.get("default")
    assert adapter is not None
    assert adapter.approval_status == "approved"
    assert reg.pending_registrations() == []
    await reg.aclose()


@pytest.mark.asyncio
async def test_pending_restore_survives_a_second_restart(tmp_path: Path, audit):
    """Restart while the adapter is STILL down: the queued restore must
    keep its receipt (grants included), or a double restart would
    re-open the amnesia hole."""
    stub = _DownThenUpStub(url="http://127.0.0.1:9100",
                           capabilities=_base_caps(gpu=True))
    store = _store(tmp_path)
    reg1 = _registry_for(stub, audit, store)
    stub.bring_up()
    await reg1.register("fast_plate_ocr", stub.url)
    reg1.approve_all("fast_plate_ocr", actor="install")
    await reg1.aclose()

    stub.up = False                     # adapter down across restarts
    reg2 = _registry_for(stub, audit, store)
    reg2.restore_persisted()
    await reg2.retry_pending()          # fails, stays pending
    assert reg2.get("fast_plate_ocr") is None
    # Force a rewrite of the receipt file WHILE the restore is still
    # pending (any other runtime registration does one): the pending
    # entry must survive the rewrite, grants intact. (First sabotage run
    # proved the weaker "file untouched" assertion passes even when
    # pending entries are dropped from _persist_locked.)
    stub.bring_up()
    await reg2.register("other_adapter", stub.url)
    stub.up = False
    receipts = {e["name"]: e for e in store.load()}
    assert set(receipts) == {"fast_plate_ocr", "other_adapter"}
    assert receipts["fast_plate_ocr"]["granted_permissions"] == ["gpu"]
    await reg2.aclose()

    reg3 = _registry_for(stub, audit, store)  # second restart
    reg3.restore_persisted()
    stub.bring_up()
    await reg3.retry_pending()
    restored = reg3.get("fast_plate_ocr")
    assert restored is not None
    assert restored.approval_status == "approved"
    await reg3.aclose()


@pytest.mark.asyncio
async def test_sovereignty_violation_drops_pending_and_receipt(tmp_path: Path, audit):
    """Retrying can never become a way around policy: a pending adapter
    that turns out to declare egress under local_only is audited,
    dropped from the queue, and its receipt removed — not retried
    forever, not registered."""
    stub = _StubAdapter(url="http://127.0.0.1:9100",
                        capabilities=_base_caps(egress=["evil.example.com"]))
    store = _store(tmp_path)
    store.save([{"name": "egressy", "url": stub.url,
                 "granted_permissions": []}])
    reg = _registry_for(stub, audit, store)
    reg.restore_persisted()
    await reg.retry_pending()
    assert reg.get("egressy") is None
    assert reg.pending_registrations() == []
    assert store.load() == []
    await reg.aclose()


@pytest.mark.asyncio
async def test_fresh_runtime_register_supersedes_queued_restore(tmp_path: Path, audit):
    """The overlay registrar re-runs while a restore for the same name
    is still queued (both containers came back together): the live
    registration wins and the queue entry is dropped."""
    stub = _StubAdapter(url="http://127.0.0.1:9100",
                        capabilities=_base_caps())
    store = _store(tmp_path)
    store.save([{"name": "fast_plate_ocr", "url": "http://stale-url:9",
                 "granted_permissions": []}])
    reg = _registry_for(stub, audit, store)
    reg.restore_persisted()
    await reg.register("fast_plate_ocr", stub.url)  # registrar beat the retry
    assert reg.pending_registrations() == []
    adapter = reg.get("fast_plate_ocr")
    assert adapter is not None and adapter.url == stub.url
    assert store.load()[0]["url"] == stub.url
    await reg.aclose()


@pytest.mark.asyncio
async def test_poll_loop_drives_the_retry(tmp_path: Path, audit):
    """The retry must be wired into the BACKGROUND poll loop — calling
    retry_pending() from tests proves the method, this proves the loop.
    A pending seed with a down adapter registers within a few fast poll
    cycles once the adapter comes up, with no explicit retry call."""
    stub = _DownThenUpStub(url="http://127.0.0.1:9100",
                           capabilities=_base_caps())
    transport = httpx.MockTransport(stub.respond)
    reg = AdapterRegistry(
        sovereignty_mode="local_only", audit=audit,
        http_client=httpx.AsyncClient(transport=transport),
        poll_interval_seconds=1,
        state_store=_store(tmp_path),
    )
    reg.defer("default", stub.url, source="seed",
              grant_all_on_register=True, error="boot race")
    await reg.start_polling()
    try:
        stub.bring_up()
        import asyncio
        for _ in range(40):            # up to ~4s of 0.1s waits
            await asyncio.sleep(0.1)
            if reg.get("default") is not None:
                break
        assert reg.get("default") is not None, (
            "poll loop never retried the deferred registration")
    finally:
        await reg.aclose()

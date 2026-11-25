# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An operator's approval survives the adapter being registered again.

The failure this guards against (seen on a client site after every host
reboot): KAI-C boots, queues the face app's adapter for restore with its
recorded grant, and the first restore attempt fails because the adapter
container is still starting. The app then comes up, sees KAI-C does not
know its adapter, and registers it itself. That registration replaced the
queued restore and its grant, so the adapter went back to ``pending`` and
every inference was refused until somebody approved it again by hand.

The safety properties still hold: only consent given to the same adapter
at the same URL is kept, only for keys it still declares, and the re-grant
is audited like any other.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kai_c.audit import AuditStore

from test_persistence import _DownThenUpStub, _registry_for, _store
from test_registry import _StubAdapter, _base_caps

URL = "http://127.0.0.1:9005"
NAME = "insightface_attendly"


def _faces_caps() -> dict:
    """What the face adapter declares: its gallery file on the host."""
    caps = _base_caps()
    caps["permissions"]["host_filesystem"] = ["/data/faces.json"]
    return caps


@pytest.fixture
def audit(tmp_path: Path) -> AuditStore:
    return AuditStore(path=str(tmp_path / "audit.jsonl"))


async def _approved_before_restart(stub, audit, store):
    reg = _registry_for(stub, audit, store)
    await reg.register(NAME, URL)
    reg.approve_all(NAME, actor="admin")
    assert reg.get(NAME).approval_status == "approved"
    await reg.aclose()


@pytest.mark.asyncio
async def test_the_app_registering_first_keeps_the_restored_approval(tmp_path: Path, audit):
    """The reboot, step by step."""
    stub = _DownThenUpStub(url=URL, capabilities=_faces_caps())
    store = _store(tmp_path)
    stub.bring_up()
    await _approved_before_restart(stub, audit, store)

    stub.up = False                       # the adapter container boots slower
    reg = _registry_for(stub, audit, store)
    reg.restore_persisted()
    await reg.retry_pending()             # first restore attempt: refused
    assert reg.get(NAME) is None and reg.pending_registrations()

    stub.bring_up()
    adapter = await reg.register(NAME, URL)   # the app gets there first
    assert adapter.approval_status == "approved"
    assert adapter.is_serving_allowed
    assert reg.pending_registrations() == []
    receipt = {e["name"]: e for e in store.load()}[NAME]
    assert receipt["granted_permissions"] == ["host_filesystem:/data/faces.json"]
    # Re-applied through the normal, audited grant path.
    grants = [e for e in audit.read_all() if e.get("type") == "adapter.permission_granted"]
    assert grants[-1].get("actor") == "system:state-restore"
    # The queued restore, retried later, changes nothing and grants nothing twice.
    await reg.retry_pending()
    assert reg.get(NAME).approval_status == "approved"
    assert len([e for e in audit.read_all() if e.get("type") == "adapter.permission_granted"]) == len(grants)
    await reg.aclose()


@pytest.mark.asyncio
async def test_registering_before_the_restore_has_run_keeps_it_too(tmp_path: Path, audit):
    """The receipt is read at construction; a registration that lands
    before restore_persisted runs must find the grant there."""
    stub = _StubAdapter(url=URL, capabilities=_faces_caps())
    store = _store(tmp_path)
    await _approved_before_restart(stub, audit, store)
    reg = _registry_for(stub, audit, store)
    adapter = await reg.register(NAME, URL)
    assert adapter.approval_status == "approved"
    reg.restore_persisted()               # now nothing left to restore
    await reg.retry_pending()
    assert reg.get(NAME).approval_status == "approved"
    await reg.aclose()


@pytest.mark.asyncio
async def test_re_registering_a_registered_adapter_keeps_its_approval(tmp_path: Path, audit):
    stub = _StubAdapter(url=URL, capabilities=_base_caps(gpu=True))
    reg = _registry_for(stub, audit, _store(tmp_path))
    await reg.register(NAME, URL)
    reg.approve_all(NAME, actor="admin")
    adapter = await reg.register(NAME, URL)
    assert adapter.approval_status == "approved"
    await reg.aclose()


@pytest.mark.asyncio
async def test_another_url_under_the_same_name_needs_fresh_consent(tmp_path: Path, audit):
    stub = _StubAdapter(url=URL, capabilities=_base_caps(gpu=True))
    store = _store(tmp_path)
    await _approved_before_restart(stub, audit, store)
    reg = _registry_for(stub, audit, store)
    reg.restore_persisted()
    adapter = await reg.register(NAME, "http://127.0.0.1:9999")   # re-pointed
    assert adapter.approval_status == "pending"
    assert not adapter.is_serving_allowed
    await reg.aclose()


@pytest.mark.asyncio
async def test_new_scope_still_waits_for_the_operator(tmp_path: Path, audit):
    """The adapter came back asking for more: what was granted is kept,
    the new key is not, so it stays pending."""
    stub = _StubAdapter(url=URL, capabilities=_base_caps(gpu=True))
    store = _store(tmp_path)
    await _approved_before_restart(stub, audit, store)
    caps = _base_caps(gpu=True)
    caps["permissions"]["host_metadata"] = True
    stub.update_capabilities(caps)
    reg = _registry_for(stub, audit, store)
    reg.restore_persisted()
    adapter = await reg.register(NAME, URL)
    assert adapter.granted_permissions == {"gpu"}
    assert adapter.approval_status == "pending"
    await reg.aclose()


@pytest.mark.asyncio
async def test_never_approved_stays_pending(tmp_path: Path, audit):
    """A restart or re-registration is not a consent act."""
    stub = _StubAdapter(url=URL, capabilities=_base_caps(gpu=True))
    store = _store(tmp_path)
    reg1 = _registry_for(stub, audit, store)
    await reg1.register(NAME, URL)        # registered, never approved
    await reg1.aclose()
    reg = _registry_for(stub, audit, store)
    reg.restore_persisted()
    adapter = await reg.register(NAME, URL)
    assert adapter.approval_status == "pending"
    await reg.aclose()

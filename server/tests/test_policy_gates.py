# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Regression tests for the sovereignty/governance policy gates in
``core.policy``.

These exercise the deployment-mode and AI-sovereignty gates directly with a
fake ``Request`` and a patched audit logger — no live DB, no FastAPI app.
Env is self-bootstrapped (there is no conftest.py) using the same pattern as
``test_agent_substream.py``: secrets are set before importing server modules
and ``server/`` is inserted on sys.path.
"""
from __future__ import annotations

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
except Exception:
    pass
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")

from fastapi import HTTPException, status  # noqa: E402

import core.policy as policy  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────


class _FakeClient:
    host = "203.0.113.7"


class _FakeURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeRequest:
    """Minimal stand-in for starlette.Request touching only the attrs the
    gate/audit path reads: ``.url.path``, ``.method``, ``.client.host`` and
    ``.headers.get``."""

    def __init__(self, path: str = "/api/cloud/push", method: str = "POST") -> None:
        self.url = _FakeURL(path)
        self.method = method
        self.client = _FakeClient()
        self.headers = {"user-agent": "pytest-agent/1.0"}


class _AuditSpy:
    """Records ``log_action`` calls; optionally raises to prove fail-closed."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self._raises = raises

    def log_action(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises:
            raise RuntimeError("audit sink is down")


@pytest.fixture
def audit_spy(monkeypatch):
    spy = _AuditSpy()
    monkeypatch.setattr(policy, "auth_logger", spy)
    return spy


# ── 1. require_outbound_allowed: deployment_mode gate ──────────────────


def test_require_outbound_allowed_blocks_offline(monkeypatch, audit_spy):
    monkeypatch.setattr(policy.settings, "deployment_mode", "offline")
    with pytest.raises(HTTPException) as ei:
        policy.require_outbound_allowed(_FakeRequest())
    assert ei.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("mode", ["hybrid", "cloud"])
def test_require_outbound_allowed_passes_when_online(monkeypatch, audit_spy, mode):
    monkeypatch.setattr(policy.settings, "deployment_mode", mode)
    # No raise, no audit block emitted.
    assert policy.require_outbound_allowed(_FakeRequest()) is None
    assert audit_spy.calls == []


# ── 2. require_ai_sovereignty_allowed: independent AI gate ─────────────


def test_require_ai_sovereignty_blocks_local_only(monkeypatch, audit_spy):
    monkeypatch.setattr(policy.settings, "ai_sovereignty", "local_only")
    with pytest.raises(HTTPException) as ei:
        policy.require_ai_sovereignty_allowed(_FakeRequest())
    assert ei.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("mode", ["federated", "cloud_allowed"])
def test_require_ai_sovereignty_passes_when_permitted(monkeypatch, audit_spy, mode):
    monkeypatch.setattr(policy.settings, "ai_sovereignty", mode)
    assert policy.require_ai_sovereignty_allowed(_FakeRequest()) is None
    assert audit_spy.calls == []


def test_gates_are_independent(monkeypatch, audit_spy):
    """Each gate refuses on its own axis: an offline deployment with a
    permissive AI policy still blocks outbound, and a cloud-allowed
    deployment with local_only AI still blocks AI — neither gate reads the
    other's setting."""
    # offline + cloud_allowed AI: outbound refused, AI gate open.
    monkeypatch.setattr(policy.settings, "deployment_mode", "offline")
    monkeypatch.setattr(policy.settings, "ai_sovereignty", "cloud_allowed")
    with pytest.raises(HTTPException):
        policy.require_outbound_allowed(_FakeRequest())
    assert policy.require_ai_sovereignty_allowed(_FakeRequest()) is None

    # cloud deployment + local_only AI: outbound open, AI refused.
    monkeypatch.setattr(policy.settings, "deployment_mode", "cloud")
    monkeypatch.setattr(policy.settings, "ai_sovereignty", "local_only")
    assert policy.require_outbound_allowed(_FakeRequest()) is None
    with pytest.raises(HTTPException):
        policy.require_ai_sovereignty_allowed(_FakeRequest())


# ── 3. plain-Python assert_* helpers (defence-in-depth at call site) ───


def test_assert_cloud_outbound_allowed_raises_offline(monkeypatch):
    monkeypatch.setattr(policy.settings, "deployment_mode", "offline")
    with pytest.raises(PermissionError):
        policy.assert_cloud_outbound_allowed(reason="unit-test push")


@pytest.mark.parametrize("mode", ["hybrid", "cloud"])
def test_assert_cloud_outbound_allowed_noop_online(monkeypatch, mode):
    monkeypatch.setattr(policy.settings, "deployment_mode", mode)
    assert policy.assert_cloud_outbound_allowed(reason="unit-test push") is None


def test_assert_ai_sovereignty_allows_remote_raises_local_only(monkeypatch):
    monkeypatch.setattr(policy.settings, "ai_sovereignty", "local_only")
    with pytest.raises(PermissionError):
        policy.assert_ai_sovereignty_allows_remote(reason="unit-test infer")


@pytest.mark.parametrize("mode", ["federated", "cloud_allowed"])
def test_assert_ai_sovereignty_allows_remote_noop_when_permitted(monkeypatch, mode):
    monkeypatch.setattr(policy.settings, "ai_sovereignty", mode)
    assert policy.assert_ai_sovereignty_allows_remote(reason="unit-test infer") is None


# ── 4. current_posture + audit_boot_posture ────────────────────────────


def test_current_posture_reflects_settings(monkeypatch):
    monkeypatch.setattr(policy.settings, "deployment_mode", "hybrid")
    monkeypatch.setattr(policy.settings, "ai_sovereignty", "federated")
    monkeypatch.setattr(policy.settings, "mediamtx_allow_plaintext_outputs", True)
    posture = policy.current_posture()
    assert posture == {
        "deployment_mode": "hybrid",
        "ai_sovereignty": "federated",
        "mediamtx_allow_plaintext_outputs": True,
    }


def test_audit_boot_posture_emits_boot_action(monkeypatch, audit_spy):
    monkeypatch.setattr(policy.settings, "deployment_mode", "offline")
    monkeypatch.setattr(policy.settings, "ai_sovereignty", "local_only")
    monkeypatch.setattr(policy.settings, "mediamtx_allow_plaintext_outputs", False)
    policy.audit_boot_posture()
    assert len(audit_spy.calls) == 1
    args, kwargs = audit_spy.calls[0]
    assert args[0] == "policy.boot_posture"
    # The full posture snapshot rides along as extra_data.
    assert kwargs["extra_data"] == {
        "deployment_mode": "offline",
        "ai_sovereignty": "local_only",
        "mediamtx_allow_plaintext_outputs": False,
    }


def test_audit_boot_posture_survives_broken_audit(monkeypatch):
    """Boot must not crash if the audit sink raises."""
    monkeypatch.setattr(policy, "auth_logger", _AuditSpy(raises=True))
    # Must not propagate.
    assert policy.audit_boot_posture() is None


# ── 5. refusal is auditable AND fail-closed ────────────────────────────


def test_outbound_refusal_is_audited(monkeypatch, audit_spy):
    monkeypatch.setattr(policy.settings, "deployment_mode", "offline")
    req = _FakeRequest(path="/api/cloud/push", method="POST")
    with pytest.raises(HTTPException):
        policy.require_outbound_allowed(req)
    assert len(audit_spy.calls) == 1
    args, kwargs = audit_spy.calls[0]
    assert args[0] == "policy.outbound_blocked"
    detail = kwargs["extra_data"]
    assert detail["path"] == "/api/cloud/push"
    assert detail["method"] == "POST"
    assert detail["policy"] == "deployment_mode=offline"
    assert detail["reason"] == "cloud_route_disabled_by_policy"


def test_ai_sovereignty_refusal_is_audited(monkeypatch, audit_spy):
    monkeypatch.setattr(policy.settings, "ai_sovereignty", "local_only")
    req = _FakeRequest(path="/api/ai/infer", method="POST")
    with pytest.raises(HTTPException):
        policy.require_ai_sovereignty_allowed(req)
    assert len(audit_spy.calls) == 1
    args, kwargs = audit_spy.calls[0]
    assert args[0] == "policy.ai_sovereignty_blocked"
    detail = kwargs["extra_data"]
    assert detail["path"] == "/api/ai/infer"
    assert detail["method"] == "POST"
    assert detail["policy"] == "ai_sovereignty=local_only"
    assert detail["reason"] == "non_local_ai_route_disabled_by_policy"


def test_refusal_fail_closed_when_audit_raises(monkeypatch):
    """Critical: if the audit logger itself raises, ``_log_block``'s
    ``except: pass`` must not swallow the refusal — the gate STILL 403s."""
    monkeypatch.setattr(policy, "auth_logger", _AuditSpy(raises=True))

    monkeypatch.setattr(policy.settings, "deployment_mode", "offline")
    with pytest.raises(HTTPException) as ei:
        policy.require_outbound_allowed(_FakeRequest())
    assert ei.value.status_code == status.HTTP_403_FORBIDDEN

    monkeypatch.setattr(policy.settings, "ai_sovereignty", "local_only")
    with pytest.raises(HTTPException) as ei2:
        policy.require_ai_sovereignty_allowed(_FakeRequest())
    assert ei2.value.status_code == status.HTTP_403_FORBIDDEN

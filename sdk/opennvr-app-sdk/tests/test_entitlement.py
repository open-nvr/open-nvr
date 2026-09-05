# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Commerce fields on the manifest and the licence-verification hook."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from opennvr_app_sdk import (
    AlertDispatcher, AppManifest, Detector, Entitlement, StdoutChannel,
)
from opennvr_app_sdk import contract as contract_mod


def _manifest(**over):
    base = dict(id="paid-app", name="Paid", version="1.0.0", category="test",
                summary="t", requires_tasks=[], subscribes="opennvr.inference.>")
    base.update(over)
    return AppManifest(**base)


def test_manifest_commerce_fields_default_and_validate():
    free = _manifest()
    assert free.pricing == "free" and free.entitlement == "none"
    d = free.to_dict()
    assert (d["pricing"], d["price_note"], d["entitlement"]) == ("free", "", "none")
    paid = _manifest(pricing="subscription", price_note="$29 / camera / year",
                     entitlement="license_key")
    assert paid.to_dict()["entitlement"] == "license_key"
    with pytest.raises(ValueError, match="pricing"):
        _manifest(pricing="donationware")
    with pytest.raises(ValueError, match="entitlement"):
        _manifest(entitlement="honor-system")


class _Free(Detector):
    manifest = _manifest(id="free-app")

    def on_detections(self, *a):
        pass


class _Licensed(Detector):
    manifest = _manifest(pricing="paid", entitlement="license_key")
    updates: list = []

    def on_detections(self, *a):
        pass

    def verify_license(self, license_key):
        if license_key == "GOOD-KEY":
            return Entitlement(valid=True, plan="pro", expires_at="2027-01-01",
                               limits={"cameras": 8})
        return Entitlement(valid=False, message="unknown key")

    def on_entitlement_update(self, entitlement):
        self.updates.append(entitlement)


class _Forgot(Detector):
    manifest = _manifest(id="forgot", entitlement="license_key")

    def on_detections(self, *a):
        pass


def _serve(cls, monkeypatch):
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "site")
    cfg = SimpleNamespace(contract_port=0, contract_bind_host="127.0.0.1",
                          nats_url="nats://x", nats_token=None, opennvr_token=None)
    app = cls(cfg, AlertDispatcher([StdoutChannel()]))
    server = app.start_contract_server()
    return app, f"http://127.0.0.1:{server.port}"


def test_default_verdicts(monkeypatch):
    free, _ = _serve(_Free, monkeypatch)
    assert free.verify_license("anything").valid is True
    forgot, _ = _serve(_Forgot, monkeypatch)
    v = forgot.verify_license("anything")
    assert v.valid is False and "verify_license" in v.message


def test_verify_route_is_key_gated_and_returns_the_verdict(monkeypatch):
    app, base = _serve(_Licensed, monkeypatch)
    try:
        h = {"X-Internal-Api-Key": "site"}
        assert httpx.post(f"{base}/entitlement/verify", json={"license_key": "x"}).status_code == 401
        r = httpx.post(f"{base}/entitlement/verify", json={"license_key": "GOOD-KEY"}, headers=h)
        assert r.json() == {"valid": True, "plan": "pro", "expires_at": "2027-01-01",
                            "message": "", "limits": {"cameras": 8}}
        r = httpx.post(f"{base}/entitlement/verify", json={"license_key": "BAD"}, headers=h)
        assert r.json()["valid"] is False and r.json()["message"] == "unknown key"
    finally:
        app.stop_contract_server()


def test_config_poll_delivers_the_entitlement(monkeypatch):
    app, _ = _serve(_Licensed, monkeypatch)
    app.stop_contract_server()
    _Licensed.updates = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"config": {"a": 1},
                    "entitlement": {"status": "valid", "plan": "pro",
                                    "expires_at": "2027-01-01", "message": ""}}

    monkeypatch.setattr(contract_mod.httpx, "get", lambda *a, **k: _Resp())
    app._config_poll_once("http://core/api/v1/apps/paid-app/config", {})
    assert app.entitlement["status"] == "valid"
    assert _Licensed.updates == [app.entitlement]
    # Unchanged verdict → no second hook call.
    app._config_poll_once("http://core/api/v1/apps/paid-app/config", {})
    assert len(_Licensed.updates) == 1

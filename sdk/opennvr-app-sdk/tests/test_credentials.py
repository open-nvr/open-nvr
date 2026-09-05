# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""credentials.py + the registration handshake: an app bootstraps with
the site key, is issued its own key once, persists it, sends it from
then on, and falls back when core rejects it."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from opennvr_app_sdk import (
    AlertDispatcher, AppCredentials, AppManifest, Detector, StdoutChannel,
    contract as contract_mod,
)
from opennvr_app_sdk import credentials as creds_mod
from opennvr_app_sdk.cameras import internal_api_key


@pytest.fixture(autouse=True)
def _isolated_key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENNVR_APP_KEY_FILE", str(tmp_path / "app.key"))
    monkeypatch.delenv("OPENNVR_APP_KEY", raising=False)
    monkeypatch.delenv("OPENNVR_INTERNAL_API_KEY", raising=False)
    yield


def test_site_key_until_an_app_key_is_issued(monkeypatch):
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "site-secret")
    c = AppCredentials()
    assert not c.has_app_key and c.token() == "site-secret"
    assert c.headers() == {"Authorization": "Bearer site-secret",
                           "X-Internal-Api-Key": "site-secret"}
    c.adopt("oak_my-app_" + "a" * 32)
    assert c.has_app_key and c.token().startswith("oak_my-app_")
    # Persisted: a fresh resolver (next boot) finds it, and every client
    # that builds headers from the credential now sends the app key.
    assert AppCredentials().token() == c.token()
    assert internal_api_key() == c.token()
    assert creds_mod.key_file().stat().st_mode & 0o777 == 0o600


def test_env_app_key_and_explicit_app_key_win(monkeypatch):
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "site-secret")
    monkeypatch.setenv("OPENNVR_APP_KEY", "oak_x_" + "b" * 32)
    assert AppCredentials().token() == "oak_x_" + "b" * 32
    assert AppCredentials("oak_y_" + "c" * 32).token() == "oak_y_" + "c" * 32
    # An explicit non-app token is treated as the site key.
    monkeypatch.delenv("OPENNVR_APP_KEY")
    assert AppCredentials("cfg-site").token() == "cfg-site"


def test_invalidate_drops_the_key_and_falls_back(monkeypatch):
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "site-secret")
    c = AppCredentials()
    c.adopt("oak_my-app_" + "d" * 32)
    c.invalidate()
    assert c.token() == "site-secret" and not creds_mod.key_file().exists()


# ── the handshake through register_with_opennvr ────────────────────────

MANIFEST = AppManifest(id="my-app", name="My App", version="1.0.0",
                       category="test", summary="t", requires_tasks=[],
                       subscribes="opennvr.inference.>")


class _App(Detector):
    manifest = MANIFEST

    def on_detections(self, camera_id, detections, event):
        pass


class _FakePost:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, *, json=None, headers=None, timeout=None, trust_env=None):
        self.calls.append({"json": json, "headers": headers or {}})
        status, body = self.responses.pop(0)
        return SimpleNamespace(status_code=status, text="", json=lambda: body)


def _app():
    cfg = SimpleNamespace(opennvr_url="http://core:8080", contract_port=9200,
                          contract_host="my-app", opennvr_token="site-secret",
                          nats_url="nats://x", nats_token=None)
    return _App(cfg, AlertDispatcher([StdoutChannel()]))


def test_registration_adopts_the_issued_key_then_uses_it(monkeypatch):
    fake = _FakePost([
        (200, {"id": "my-app", "api_key": "oak_my-app_" + "e" * 32,
               "registry": {"server_version": "0.1.4", "api_version": "1.1",
                            "min_sdk_version": "0.2.0"}}),
        (200, {"id": "my-app"}),
    ])
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    app = _app()
    assert app.register_with_opennvr() is True
    first = fake.calls[0]
    assert first["headers"]["X-Internal-Api-Key"] == "site-secret"
    assert first["json"]["wants_key"] is True
    # Second boot-time registration: our own key, no request for a new one.
    assert app.register_with_opennvr() is True
    second = fake.calls[1]
    assert second["headers"]["X-Internal-Api-Key"] == "oak_my-app_" + "e" * 32
    assert second["json"]["wants_key"] is False
    # …and the config poll sends the same key.
    url, headers = app._config_poll_target()
    assert url.endswith("/api/v1/apps/my-app/config")
    assert headers["X-Internal-Api-Key"] == "oak_my-app_" + "e" * 32


def test_rejected_app_key_is_discarded_for_the_next_attempt(monkeypatch):
    creds_mod.store_app_key("oak_my-app_" + "f" * 32)
    fake = _FakePost([(401, {"detail": "Invalid or revoked app key"}),
                      (200, {"id": "my-app", "api_key": "oak_my-app_" + "1" * 32})])
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    app = _app()
    assert app.register_with_opennvr() is False
    assert fake.calls[0]["headers"]["X-Internal-Api-Key"].startswith("oak_")
    # Next attempt bootstraps with the site key and asks anew.
    assert app.register_with_opennvr() is True
    assert fake.calls[1]["headers"]["X-Internal-Api-Key"] == "site-secret"
    assert fake.calls[1]["json"]["wants_key"] is True
    assert app.credentials.token() == "oak_my-app_" + "1" * 32


def test_old_sdk_is_warned_not_broken(monkeypatch, caplog):
    fake = _FakePost([(200, {"id": "my-app",
                             "registry": {"server_version": "9.0.0",
                                          "min_sdk_version": "99.0.0"}})])
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    with caplog.at_level("WARNING"):
        assert _app().register_with_opennvr() is True
    assert any("requires opennvr-app-sdk >= 99.0.0" in r.message for r in caplog.records)

# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-OpenNVR-User: the operator's identity and camera scope reach the
app's /ui and actions, verified against the app's own key."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from opennvr_app_sdk import (
    AlertDispatcher, AppManifest, Action, ContractServer, Detector,
    StdoutChannel, UserContext, current_user,
)
from opennvr_app_sdk import usercontext as uc

APP_KEY = "oak_my-app_" + "a" * 32
SECRET = hashlib.sha256(APP_KEY.encode()).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def mint(claims: dict, secret: str = SECRET) -> str:
    """What core does (jose HS256), spelled out with the stdlib."""
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def _claims(**over):
    now = int(time.time())
    base = {"iss": "opennvr", "aud": "my-app", "sub": "7", "username": "guard",
            "is_superuser": False, "cameras": [1, 3], "manage": [3],
            "purpose": "action", "iat": now, "exp": now + 60}
    base.update(over)
    return base


# ── verification ───────────────────────────────────────────────────────


def test_valid_token_yields_the_user_and_scope():
    u = uc.verify_user_context(mint(_claims()), SECRET, audience="my-app")
    assert u == UserContext(user_id=7, username="guard", cameras=frozenset({1, 3}),
                            manage=frozenset({3}), purpose="action")
    assert u.can_see("cam1") and u.can_see(3) and not u.can_see("cam-2")
    assert u.can_manage("cam3") and not u.can_manage("cam1")
    assert u.visible(["cam1", "cam2", "cam3"]) == ["cam1", "cam3"]


def test_superuser_sees_everything():
    u = uc.verify_user_context(mint(_claims(is_superuser=True, cameras=None, manage=None)),
                               SECRET)
    assert u.is_superuser and u.cameras is None and u.can_see(999) and u.can_manage(999)


@pytest.mark.parametrize("bad", [
    lambda: mint(_claims(), secret="wrong"),
    lambda: mint(_claims(exp=int(time.time()) - 120)),
    lambda: mint(_claims(aud="other-app")),
    lambda: mint(_claims(iss="someone")),
    lambda: mint(_claims(sub="not-an-int")),
    lambda: "garbage",
    lambda: "",
])
def test_anything_wrong_means_no_user(bad):
    assert uc.verify_user_context(bad(), SECRET, audience="my-app") is None


def test_no_secret_means_no_user():
    assert uc.verify_user_context(mint(_claims()), None) is None
    assert uc.signing_secret(None) is None and uc.signing_secret(APP_KEY) == SECRET


# ── through the contract server ───────────────────────────────────────


MANIFEST = AppManifest(
    id="my-app", name="My App", version="1.0.0", category="test", summary="t",
    requires_tasks=[], subscribes="opennvr.inference.>", has_ui=True,
    actions=[Action("reset", "Reset", params=[])],
)


class _App(Detector):
    manifest = MANIFEST

    def on_detections(self, camera_id, detections, event):
        pass

    def ui_html(self) -> str:
        u = self.current_user
        return f"<p>{u.username if u else 'anonymous'}</p>"

    def on_action(self, name, params):
        u = current_user()
        return {"by": u.username if u else None,
                "sees_cam2": bool(u and u.can_see("cam2"))}


@pytest.fixture
def served(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENNVR_APP_KEY", APP_KEY)
    monkeypatch.delenv("OPENNVR_INTERNAL_API_KEY", raising=False)
    from types import SimpleNamespace
    cfg = SimpleNamespace(contract_port=0, contract_bind_host="127.0.0.1",
                          nats_url="nats://x", nats_token=None, opennvr_token=None)
    app = _App(cfg, AlertDispatcher([StdoutChannel()]))
    server = app.start_contract_server()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        app.stop_contract_server()


def test_ui_and_actions_see_the_forwarded_user(served):
    tok = mint(_claims(purpose="ui"))
    assert httpx.get(f"{served}/ui", headers={"X-OpenNVR-User": tok}).text == "<p>guard</p>"
    assert httpx.get(f"{served}/ui").text == "<p>anonymous</p>"
    r = httpx.post(f"{served}/actions/reset", json={},
                   headers={"X-OpenNVR-User": mint(_claims())})
    assert r.json() == {"by": "guard", "sees_cam2": False}
    # A forged token (wrong secret) is simply "no user".
    r = httpx.post(f"{served}/actions/reset", json={},
                   headers={"X-OpenNVR-User": mint(_claims(), secret="x")})
    assert r.json() == {"by": None, "sees_cam2": False}
    # Nothing leaks between requests.
    assert current_user() is None

# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Contract surface tests (spec §03) — the /health /manifest /state
server, the loop-fed counters, and best-effort self-registration
against the OpenNVR app registry. Real HTTP against an ephemeral
127.0.0.1 port; the registry side is a monkeypatched ``httpx.post``."""
from __future__ import annotations

from types import SimpleNamespace

import os

import httpx
import pytest

from opennvr_app_sdk import (
    Action,
    Alert,
    AlertDispatcher,
    AppManifest,
    Detector,
    FrameApp,
    Param,
)
from opennvr_app_sdk import contract as contract_mod


class _RecorderChannel:
    name = "recorder"

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        return True


MANIFEST = AppManifest(
    id="contract-echo",
    name="Contract Echo",
    version="1.2.3",
    category="test",
    summary="Fires one alert per detection batch.",
)


class _EchoDetector(Detector):
    manifest = MANIFEST

    def on_detections(self, camera_id, detections, event):
        if detections:
            yield Alert(title="hit", description="d", camera_id=camera_id)

    def state_snapshot(self):
        return {"note": "live state"}


def _cfg(**overrides) -> SimpleNamespace:
    base = {"contract_port": 0, "contract_bind_host": "127.0.0.1"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _detector(cfg=None) -> _EchoDetector:
    return _EchoDetector(cfg or _cfg(), AlertDispatcher([_RecorderChannel()]))


def _event(n: int = 1) -> dict:
    return {
        "camera_id": "cam-1",
        "result": {"detections": [{"label": "person"}] * n},
    }


def _get(port: int, path: str) -> httpx.Response:
    return httpx.get(f"http://127.0.0.1:{port}{path}", timeout=2.0, trust_env=False)


# ── The three endpoints ────────────────────────────────────────────


def test_serves_health_manifest_and_state():
    det = _detector()
    server = det.start_contract_server()
    assert server is not None
    try:
        health = _get(server.port, "/health").json()
        assert health["ready"] is True
        assert health["uptime_s"] >= 0
        assert health["events_seen"] == 0
        assert health["alerts_fired"] == 0
        assert health["last_event_age_s"] is None

        manifest = _get(server.port, "/manifest").json()
        assert manifest == MANIFEST.to_dict()

        state = _get(server.port, "/state").json()
        assert state == {"note": "live state"}
    finally:
        det.stop_contract_server()


def test_health_counters_follow_the_event_loop():
    det = _detector()
    server = det.start_contract_server()
    try:
        det.handle_event(_event(2))           # 1 event, 1 alert
        det.handle_event({"camera_id": "cam-1", "result": {"detections": []}})
        det.handle_event("not-a-dict")        # malformed still counts as seen

        health = _get(server.port, "/health").json()
        assert health["events_seen"] == 3
        assert health["alerts_fired"] == 1
        assert health["last_event_age_s"] is not None
        assert health["last_event_age_s"] >= 0
    finally:
        det.stop_contract_server()


def test_default_state_snapshot_is_empty_dict():
    class _Bare(Detector):
        manifest = MANIFEST

        def on_detections(self, camera_id, detections, event):
            return None

    det = _Bare(_cfg(), AlertDispatcher([_RecorderChannel()]))
    server = det.start_contract_server()
    try:
        assert _get(server.port, "/state").json() == {}
    finally:
        det.stop_contract_server()


def test_unknown_path_is_json_404():
    det = _detector()
    server = det.start_contract_server()
    try:
        response = _get(server.port, "/nope")
        assert response.status_code == 404
        assert "unknown path" in response.json()["error"]
    finally:
        det.stop_contract_server()


def test_server_off_without_contract_port():
    det = _detector(SimpleNamespace())
    assert det.start_contract_server() is None
    det.stop_contract_server()  # must be a safe no-op


def test_frame_app_counters_follow_handle_tick():
    class _App(FrameApp):
        manifest = MANIFEST

        def on_frame(self, camera_id, frame_bytes):
            yield Alert(title="t", description="d", camera_id=camera_id)

    class _Source:
        def get_frame(self, camera_id):
            return b"jpeg" if camera_id == "cam-1" else None

    app_obj = _App(
        SimpleNamespace(poll_interval_seconds=0.01),
        AlertDispatcher([_RecorderChannel()]),
        frame_source=_Source(),
        cameras=["cam-1", "cam-2"],
    )
    app_obj.handle_tick()
    # cam-2 produced no frame → 1 event; cam-1's rule fired 1 alert.
    assert app_obj._events_seen == 1
    assert app_obj._alerts_fired == 1


# ── Self-registration ──────────────────────────────────────────────


class _FakePost:
    def __init__(self, status_code: int = 200, raise_exc: Exception | None = None):
        self.calls: list[dict] = []
        self._status = status_code
        self._raise = raise_exc

    def __call__(self, url, *, json=None, headers=None, timeout=None, trust_env=None):
        self.calls.append(
            {"url": url, "json": json, "headers": headers or {}, "timeout": timeout}
        )
        if self._raise is not None:
            raise self._raise
        return SimpleNamespace(status_code=self._status, text="registered")


def test_registration_posts_url_manifest_and_auth_headers(monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    det = _detector(_cfg(
        contract_port=9200,
        contract_host="loitering",
        opennvr_url="http://opennvr:8080/",
        opennvr_token="sekrit",
    ))
    assert det.register_with_opennvr() is True

    call = fake.calls[0]
    assert call["url"] == "http://opennvr:8080/api/v1/apps/register"
    assert call["json"] == {
        "url": "http://loitering:9200",
        "manifest": MANIFEST.to_dict(),
        # Negotiation + credential bootstrap (credentials.py): the SDK
        # states its version and, holding no app key yet, asks for one.
        "sdk_version": contract_mod._sdk_version,
        "wants_key": True,
    }
    # One token, both header shapes: the registry's register route
    # accepts a user JWT (bearer) or the deployment's INTERNAL_API_KEY
    # (X-Internal-Api-Key) — the SDK can't know which kind it holds.
    assert call["headers"]["Authorization"] == "Bearer sekrit"
    assert call["headers"]["X-Internal-Api-Key"] == "sekrit"


def test_registration_token_falls_back_to_env(monkeypatch):
    """No ``opennvr_token`` in the config ⇒ the token comes from the
    ``OPENNVR_INTERNAL_API_KEY`` environment variable (the compose
    overlay's wiring)."""
    fake = _FakePost()
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "env-sekrit")
    det = _detector(_cfg(
        contract_port=9200,
        contract_host="loitering",
        opennvr_url="http://opennvr:8080",
    ))
    assert det.register_with_opennvr() is True
    headers = fake.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer env-sekrit"
    assert headers["X-Internal-Api-Key"] == "env-sekrit"


def test_registration_cfg_token_beats_env(monkeypatch):
    """An explicit config token wins over the env fallback."""
    fake = _FakePost()
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "env-sekrit")
    det = _detector(_cfg(
        contract_port=9200,
        contract_host="loitering",
        opennvr_url="http://opennvr:8080",
        opennvr_token="cfg-sekrit",
    ))
    assert det.register_with_opennvr() is True
    assert fake.calls[0]["headers"]["X-Internal-Api-Key"] == "cfg-sekrit"


def test_registration_no_token_sends_no_auth_headers(monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    monkeypatch.delenv("OPENNVR_INTERNAL_API_KEY", raising=False)
    det = _detector(_cfg(
        contract_port=9200,
        contract_host="loitering",
        opennvr_url="http://opennvr:8080",
    ))
    assert det.register_with_opennvr() is True
    headers = fake.calls[0]["headers"]
    assert "Authorization" not in headers
    assert "X-Internal-Api-Key" not in headers


def test_registration_advertises_actual_ephemeral_port(monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    det = _detector(_cfg(
        contract_host="myapp", opennvr_url="http://opennvr:8080",
    ))
    server = det.start_contract_server()
    try:
        assert det.register_with_opennvr() is True
        assert server.port != 0
        assert fake.calls[0]["json"]["url"] == f"http://myapp:{server.port}"
    finally:
        det.stop_contract_server()


def test_registration_failure_is_nonfatal(monkeypatch, caplog):
    fake = _FakePost(raise_exc=RuntimeError("registry down"))
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    det = _detector(_cfg(contract_port=9200, opennvr_url="http://opennvr:8080"))
    with caplog.at_level("WARNING", logger="opennvr_app_sdk.contract"):
        assert det.register_with_opennvr() is False
    assert any("self-registration failed" in r.getMessage() for r in caplog.records)


def test_registration_rejection_is_nonfatal(monkeypatch, caplog):
    fake = _FakePost(status_code=400)
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    det = _detector(_cfg(contract_port=9200, opennvr_url="http://opennvr:8080"))
    with caplog.at_level("WARNING", logger="opennvr_app_sdk.contract"):
        assert det.register_with_opennvr() is False
    assert any("rejected" in r.getMessage() for r in caplog.records)


def test_registration_skipped_without_opennvr_url(monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    assert _detector(_cfg(contract_port=9200)).register_with_opennvr() is False
    assert fake.calls == []


def test_registration_needs_a_contract_port(monkeypatch, caplog):
    fake = _FakePost()
    monkeypatch.setattr(contract_mod.httpx, "post", fake)
    det = _detector(SimpleNamespace(opennvr_url="http://opennvr:8080"))
    with caplog.at_level("WARNING", logger="opennvr_app_sdk.contract"):
        assert det.register_with_opennvr() is False
    assert fake.calls == []
    assert any("contract_port" in r.getMessage() for r in caplog.records)


# ── run() lifecycle ────────────────────────────────────────────────


async def test_frame_app_run_starts_registers_and_stops_contract(monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(contract_mod.httpx, "post", fake)

    seen_ports: list[int] = []

    class _App(FrameApp):
        manifest = MANIFEST

        def on_frame(self, camera_id, frame_bytes):
            # Prove the contract server is live DURING the loop.
            seen_ports.append(self._contract_server.port)
            health = _get(self._contract_server.port, "/health").json()
            assert health["ready"] is True
            return None

    class _Source:
        def get_frame(self, camera_id):
            return b"jpeg"

    app_obj = _App(
        SimpleNamespace(
            poll_interval_seconds=0.01,
            contract_port=0,
            contract_bind_host="127.0.0.1",
            contract_host="pkg",
            opennvr_url="http://opennvr:8080",
        ),
        AlertDispatcher([_RecorderChannel()]),
        frame_source=_Source(),
        cameras=["cam-1"],
    )
    await app_obj.run(once=True)

    # Registered once, advertising the ephemeral port that was bound.
    assert len(fake.calls) == 1
    assert fake.calls[0]["json"]["url"] == f"http://pkg:{seen_ports[0]}"
    # And the server is torn down after run() returns.
    assert app_obj._contract_server is None
    with pytest.raises(httpx.TransportError):
        _get(seen_ports[0], "/health")


# ── Live config delivery (registry poll) ───────────────────────────


class _FakeGet:
    """Sequenced httpx.get stub: pops canned (status, body) responses,
    repeating the last one; records every (url, headers)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers=None, timeout=None, trust_env=None):
        self.calls.append((url, dict(headers or {})))
        status, body = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        request = httpx.Request("GET", url)
        return httpx.Response(status, json=body, request=request)


class _ReloadDetector(_EchoDetector):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.applied: list[dict] = []

    def on_config_update(self, config):
        self.applied.append(config)


def test_config_poll_off_without_url_or_interval():
    # No opennvr_url → unwired.
    det = _ReloadDetector(_cfg(), AlertDispatcher([_RecorderChannel()]))
    assert det.start_config_poll() is False
    # Wired but explicitly disabled.
    det2 = _ReloadDetector(
        _cfg(opennvr_url="http://reg:8000", config_poll_seconds=0),
        AlertDispatcher([_RecorderChannel()]),
    )
    assert det2.start_config_poll() is False


def test_config_poll_target_url_and_auth_headers(monkeypatch):
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "env-sekrit")
    det = _ReloadDetector(
        _cfg(opennvr_url="http://reg:8000/"),
        AlertDispatcher([_RecorderChannel()]),
    )
    url, headers = det._config_poll_target()
    assert url == "http://reg:8000/api/v1/apps/contract-echo/config"
    assert headers["X-Internal-Api-Key"] == "env-sekrit"
    assert headers["Authorization"] == "Bearer env-sekrit"


def test_config_poll_applies_first_fetch_then_only_changes(monkeypatch):
    """The hook fires on the FIRST successful fetch (registry is the
    source of truth — spec §05) and again only when the config
    actually changes."""
    fake = _FakeGet([
        (200, {"id": "contract-echo", "config": {"threshold": 1}}),
        (200, {"id": "contract-echo", "config": {"threshold": 1}}),  # no change
        (200, {"id": "contract-echo", "config": {"threshold": 2}}),
    ])
    monkeypatch.setattr(contract_mod.httpx, "get", fake)
    det = _ReloadDetector(
        _cfg(opennvr_url="http://reg:8000"),
        AlertDispatcher([_RecorderChannel()]),
    )
    url, headers = det._config_poll_target()
    det._config_poll_once(url, headers)
    det._config_poll_once(url, headers)
    det._config_poll_once(url, headers)
    assert det.applied == [{"threshold": 1}, {"threshold": 2}]


def test_config_poll_tolerates_registry_failures(monkeypatch):
    """404 (not registered yet), 500, and connection errors are all
    debug-logged no-ops — delivery resumes when the registry recovers."""
    fake = _FakeGet([
        (404, {"detail": "not registered"}),
        (500, {"detail": "boom"}),
        (200, {"id": "contract-echo", "config": {"a": 1}}),
    ])
    monkeypatch.setattr(contract_mod.httpx, "get", fake)
    det = _ReloadDetector(
        _cfg(opennvr_url="http://reg:8000"),
        AlertDispatcher([_RecorderChannel()]),
    )
    url, headers = det._config_poll_target()
    det._config_poll_once(url, headers)
    det._config_poll_once(url, headers)
    assert det.applied == []
    det._config_poll_once(url, headers)
    assert det.applied == [{"a": 1}]


def test_config_poll_raising_hook_does_not_stop_delivery(monkeypatch):
    """A buggy on_config_update must not kill the poll — the NEXT edit
    still gets delivered."""

    class _Angry(_EchoDetector):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls = 0

        def on_config_update(self, config):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("bad hook")

    fake = _FakeGet([
        (200, {"config": {"v": 1}}),
        (200, {"config": {"v": 2}}),
    ])
    monkeypatch.setattr(contract_mod.httpx, "get", fake)
    det = _Angry(
        _cfg(opennvr_url="http://reg:8000"),
        AlertDispatcher([_RecorderChannel()]),
    )
    url, headers = det._config_poll_target()
    det._config_poll_once(url, headers)   # raises inside, swallowed
    det._config_poll_once(url, headers)   # next change still delivered
    assert det.calls == 2


def test_config_poll_thread_lifecycle(monkeypatch):
    """start_config_poll spins the daemon thread, delivers, and
    stop_config_poll joins it."""
    import time as _time

    fake = _FakeGet([(200, {"config": {"live": True}})])
    monkeypatch.setattr(contract_mod.httpx, "get", fake)
    det = _ReloadDetector(
        _cfg(opennvr_url="http://reg:8000", config_poll_seconds=0.01),
        AlertDispatcher([_RecorderChannel()]),
    )
    assert det.start_config_poll() is True
    deadline = _time.time() + 3.0
    while not det.applied and _time.time() < deadline:
        _time.sleep(0.01)
    det.stop_config_poll()
    assert det.applied and det.applied[0] == {"live": True}
    assert det._config_poll_thread is None


def test_default_hook_logs_restart_needed_once(monkeypatch, caplog):
    fake = _FakeGet([
        (200, {"config": {"v": 1}}),
        (200, {"config": {"v": 2}}),
    ])
    monkeypatch.setattr(contract_mod.httpx, "get", fake)
    det = _detector(_cfg(opennvr_url="http://reg:8000"))  # no override
    url, headers = det._config_poll_target()
    import logging as _logging

    with caplog.at_level(_logging.INFO, logger="opennvr_app_sdk.contract"):
        det._config_poll_once(url, headers)
        det._config_poll_once(url, headers)
    restarts = [r for r in caplog.records if "restart" in r.message]
    assert len(restarts) == 1  # warned once, not per change


# ── POST /actions/{name} (manifest-declared operator verbs) ────────


ACTION_MANIFEST = AppManifest(
    id="action-echo",
    name="Action Echo",
    version="1.0.0",
    category="test",
    actions=[
        Action("greet", "Greet", params=[Param("who", str, required=True)]),
        Action("boom", "Boom"),
    ],
)


class _ActionDetector(_EchoDetector):
    manifest = ACTION_MANIFEST

    def on_action(self, name, params):
        if name == "greet":
            who = str(params.get("who") or "").strip()
            if not who:
                raise ValueError("'who' must be non-empty")
            return {"results": [{"greeting": f"hello {who}"}]}
        if name == "boom":
            raise RuntimeError("kaboom")
        raise KeyError(name)


def _action_detector():
    # Key-less on purpose: these tests exercise dispatch semantics; the
    # key gate has its own test. Shield against a leaked env var.
    os.environ.pop("OPENNVR_INTERNAL_API_KEY", None)
    det = _ActionDetector(_cfg(), AlertDispatcher([_RecorderChannel()]))
    server = det.start_contract_server()
    assert server is not None
    return det, server.port


def _post(port: int, path: str, body) -> httpx.Response:
    return httpx.post(
        f"http://127.0.0.1:{port}{path}", json=body, timeout=2.0, trust_env=False
    )


def test_action_dispatches_and_returns_result():
    det, port = _action_detector()
    try:
        resp = _post(port, "/actions/greet", {"who": "ops"})
        assert resp.status_code == 200
        assert resp.json() == {"results": [{"greeting": "hello ops"}]}
    finally:
        det.stop_contract_server()


def test_undeclared_action_is_404_even_with_a_handler():
    """The manifest is the single source of truth: _dispatch_action
    refuses names the manifest doesn't declare, even though the
    subclass's on_action would happily raise KeyError anyway — and more
    importantly, even if it WOULD have handled the name."""
    det, port = _action_detector()
    try:
        resp = _post(port, "/actions/not-declared", {})
        assert resp.status_code == 404
    finally:
        det.stop_contract_server()


def test_action_value_error_is_400_and_crash_is_500():
    det, port = _action_detector()
    try:
        bad = _post(port, "/actions/greet", {"who": "  "})
        assert bad.status_code == 400
        assert "non-empty" in bad.json()["error"]
        crash = _post(port, "/actions/boom", {})
        assert crash.status_code == 500
        assert crash.json() == {"error": "internal error"}
    finally:
        det.stop_contract_server()


def test_action_body_must_be_json_object():
    det, port = _action_detector()
    try:
        resp = _post(port, "/actions/greet", ["not", "an", "object"])
        assert resp.status_code == 400
    finally:
        det.stop_contract_server()


def test_actionless_manifest_has_no_post_surface():
    """The original echo detector declares no actions — every POST 404s
    (the dispatcher's declared-set is empty)."""
    det = _detector()
    server = det.start_contract_server()
    try:
        resp = _post(server.port, "/actions/greet", {"who": "x"})
        assert resp.status_code == 404
    finally:
        det.stop_contract_server()


def test_action_post_requires_deployment_key_when_configured(monkeypatch):
    """The app's own action surface is key-gated (review M1): with the
    deployment token known, an internal-network caller WITHOUT the key
    gets 401 and the dispatcher is never reached; presenting the key
    (as the server proxy does) works."""
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "deploy-sekrit")
    det = _ActionDetector(_cfg(), AlertDispatcher([_RecorderChannel()]))
    server = det.start_contract_server()
    try:
        bare = _post(server.port, "/actions/greet", {"who": "x"})
        assert bare.status_code == 401
        ok = httpx.post(
            f"http://127.0.0.1:{server.port}/actions/greet",
            json={"who": "x"},
            headers={"X-Internal-Api-Key": "deploy-sekrit"},
            timeout=2.0, trust_env=False,
        )
        assert ok.status_code == 200
        wrong = httpx.post(
            f"http://127.0.0.1:{server.port}/actions/greet",
            json={"who": "x"},
            headers={"X-Internal-Api-Key": "guess"},
            timeout=2.0, trust_env=False,
        )
        assert wrong.status_code == 401
        # Reads stay open — health polls don't need the key.
        assert _get(server.port, "/health").status_code == 200
    finally:
        det.stop_contract_server()


def test_action_body_size_cap(monkeypatch):
    """A forged/huge Content-Length is refused up front (review M2) —
    no arbitrary-size read into memory on this surface. Raw socket:
    the server answers 413 WITHOUT reading the body, which well-behaved
    HTTP clients treat as a protocol violation mid-send — exactly the
    point."""
    import socket

    monkeypatch.delenv("OPENNVR_INTERNAL_API_KEY", raising=False)
    det, port = _action_detector()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as s:
            s.sendall(
                b"POST /actions/greet HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 524288000\r\n"
                b"\r\n"
            )
            status_line = s.recv(4096).split(b"\r\n", 1)[0]
        assert b"413" in status_line, status_line
    finally:
        det.stop_contract_server()


def test_action_body_accepts_image_sized_payload(monkeypatch):
    """A ~1MB body (a base64 face photo) is UNDER the 8MB cap and
    dispatches normally — the cap must not block face enrollment."""
    monkeypatch.delenv("OPENNVR_INTERNAL_API_KEY", raising=False)
    det, port = _action_detector()
    try:
        big = {"who": "x" * (1024 * 1024)}  # ~1 MB
        resp = _post(port, "/actions/greet", big)
        assert resp.status_code == 200
    finally:
        det.stop_contract_server()


def test_action_deeply_nested_body_is_400_not_thread_death(monkeypatch):
    """RecursionError from json.loads is the bad-body class (review L3)
    — 400, and the server keeps serving."""
    monkeypatch.delenv("OPENNVR_INTERNAL_API_KEY", raising=False)
    det, port = _action_detector()
    try:
        nested = b"[" * 10000 + b"]" * 10000
        resp = httpx.post(
            f"http://127.0.0.1:{port}/actions/greet",
            content=nested,
            headers={"Content-Type": "application/json"},
            timeout=5.0, trust_env=False,
        )
        assert resp.status_code == 400
        # Still alive after the recursion bomb.
        ok = _post(port, "/actions/greet", {"who": "ops"})
        assert ok.status_code == 200
    finally:
        det.stop_contract_server()


# ── Tier-0 bridge in handle_event (opt-in) ─────────────────────────

def _tier0_ev() -> dict:
    return {
        "schema": "opennvr.tier0.v1",
        "adapter": "tier0",
        "camera_id": "cam-1",
        "frame": {"w": 100, "h": 100},
        "tracks": [{"id": 1, "label": "person", "score": 0.9,
                    "box": [10, 10, 50, 90], "stationary": False, "best": False}],
    }


def test_tier0_ignored_and_not_counted_by_default():
    det = _detector()
    assert det.handle_event(_tier0_ev()) == []
    # Not counted as seen: per-frame tier0 traffic must not refresh
    # last_event_age_s for apps that don't consume it (adapter-stall masking).
    assert det._events_seen == 0


def test_tier0_bridged_into_on_detections_when_opted_in():
    det = _detector(_cfg(consume_tier0=True))
    fired = det.handle_event(_tier0_ev())
    assert len(fired) == 1
    assert det._events_seen == 1


# ── GET /ui (RFC-0002 Phase 4 app-surface convention) ──────────────


class _UiDetector(_EchoDetector):
    def ui_html(self) -> str:
        return "<title>Echo</title><h1>hello dashboard</h1>"


class _BrokenUiDetector(_EchoDetector):
    def ui_html(self) -> str:
        raise RuntimeError("renderer bug")


def test_serves_ui_as_html_when_the_app_defines_it():
    det = _UiDetector(_cfg(), AlertDispatcher([_RecorderChannel()]))
    server = det.start_contract_server()
    try:
        resp = _get(server.port, "/ui")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "hello dashboard" in resp.text
    finally:
        det.stop_contract_server()


def test_no_ui_hook_means_404_not_a_blank_page():
    det = _detector()
    server = det.start_contract_server()
    try:
        resp = _get(server.port, "/ui")
        assert resp.status_code == 404
    finally:
        det.stop_contract_server()


def test_a_raising_ui_renderer_is_a_500_never_a_crash():
    det = _BrokenUiDetector(_cfg(), AlertDispatcher([_RecorderChannel()]))
    server = det.start_contract_server()
    try:
        assert _get(server.port, "/ui").status_code == 500
        # The contract surface survives the renderer bug.
        assert _get(server.port, "/health").status_code == 200
    finally:
        det.stop_contract_server()

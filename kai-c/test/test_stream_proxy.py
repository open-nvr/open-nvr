# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
End-to-end tests for the §6 WebSocket streaming proxy
(/api/v1/infer/{adapter_name}/stream).

Pattern: spin up a tiny ``websockets.serve`` adapter on a real
localhost port (MockTransport doesn't speak WS), register it with
KAI-C as if it were a real adapter, then drive the proxy via
``TestClient.websocket_connect``.

What's covered:

* Happy path — handshake → frame_meta + binary → result roundtrip
* Audit emission — stream.opened on session start, stream.closed on
  end, stream.failed on per-frame §7 error envelopes
* Auth (X-Internal-Api-Key) — close 4001 when missing/wrong
* Unknown adapter — close 4001
* Adapter doesn't support streaming — close 4002
* Adapter unreachable at upstream connect time — close 4002 (model_error)
* Correlation_id threading — header in → adapter sees it in upstream connect
* http://...:port → ws://...:port URL translation
"""
from __future__ import annotations

import asyncio
import importlib
import json
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
from starlette.websockets import WebSocketDisconnect


# ── Fake adapter HTTP responses (capabilities/health for registration) ──


def _caps_with_stream(*, stream: bool = True) -> dict:
    """A contract-compliant /capabilities response. ``infer_stream.supported``
    is the toggle the proxy reads to decide whether to accept the WS upgrade."""
    return {
        "adapter": {
            "name": "stub-stream", "version": "1.0.0", "vendor": "open-nvr",
            "license": "AGPL-3.0", "supported_contract_versions": ["1"],
        },
        "model": {
            "name": "stub-model", "version": "v1",
            "framework": "f", "fingerprint": "sha256:zzz",
        },
        "endpoints": {
            "infer": {"supported": True, "input_content_types": ["application/json"]},
            "infer_stream": {
                "supported": stream,
                "max_concurrent_streams": 1 if stream else 0,
                "supports_shared_memory": False,
            },
        },
        "tasks_advertised": ["object_detection"],
        "permissions": {
            "gpu": False, "network_egress": [], "host_filesystem": [],
            "shared_memory_paths": [], "host_metadata": False,
        },
        "scheduling": {
            "max_inflight": 1, "preferred_batch_size": 1,
            "fair_queuing": "per_camera",
        },
        "cost": {
            "currency": "USD", "estimated_per_call": 0.0, "estimated_per_hour": 0.0,
            "rate_limit_per_minute": None, "is_metered": False,
        },
    }


def _stub_adapter_http(*, stream: bool = True):
    """Build an httpx.MockTransport that satisfies registration's
    /capabilities + /health probes. Streaming is exercised over a real
    socket — we just need the HTTP side to make registration succeed."""
    caps = _caps_with_stream(stream=stream)

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capabilities":
            return httpx.Response(200, json=caps)
        if request.url.path == "/health":
            return httpx.Response(200, json={
                "status": "ok",
                "adapter_name": "stub-stream", "adapter_version": "1.0.0",
                "model_name": "stub-model", "model_version": "v1",
                "started_at": "2026-05-19T00:00:00Z", "uptime_seconds": 1,
            })
        return httpx.Response(404)
    return httpx.MockTransport(respond)


# ── Real WS adapter on localhost (for the streaming bridge) ──


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeAdapterWSServer:
    """A tiny §6 adapter — handshake_ack on connect, echoes back a
    result for each frame_meta + binary pair, supports configurable
    error injection.

    Implements just enough of the contract to validate the proxy is
    relaying messages correctly. Runs in its own asyncio loop on a
    background thread so the FastAPI TestClient (which is synchronous)
    can drive both sides.
    """

    def __init__(self, *, port: int) -> None:
        self.port = port
        self.last_headers: dict[str, str] = {}
        self.error_injection: dict | None = None  # set to a FailureEnvelope dict to error on next frame
        self._server: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=5.0), "fake adapter WS server didn't start"

    def stop(self) -> None:
        if self._loop is not None and self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        async def _main():
            self._server = await websockets.serve(
                self._handle, "127.0.0.1", self.port,
            )
            self._ready.set()
            try:
                await self._server.wait_closed()
            except asyncio.CancelledError:
                pass

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(_main())
        finally:
            self._loop.close()

    async def _handle(self, ws) -> None:
        # websockets v10+: ws.request_headers; v11+: ws.request.headers.
        try:
            headers = dict(ws.request.headers)
        except AttributeError:
            headers = dict(ws.request_headers)
        self.last_headers = {k.lower(): v for k, v in headers.items()}

        try:
            # 1. Read the client's handshake message.
            raw = await ws.recv()
            handshake = json.loads(raw)
            assert handshake["type"] == "handshake"
            # 2. Reply with handshake_ack.
            await ws.send(json.dumps({
                "type": "handshake_ack",
                "frame_transport": "websocket",
                "result_sink": "websocket",
                "max_inflight": 1,
                "session_id": "fake-session-1",
            }))
            # 3. Loop: handle frame_meta + binary pairs and control messages.
            while True:
                msg = await ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    # Stray binary — protocol violation; close.
                    await ws.close(code=4001, reason="binary without metadata")
                    return
                parsed = json.loads(msg)
                msg_type = parsed.get("type")
                if msg_type == "close":
                    return
                if msg_type == "frame":
                    bin_msg = await ws.recv()
                    if not isinstance(bin_msg, (bytes, bytearray)):
                        await ws.close(code=4001, reason="frame not followed by binary")
                        return
                    if self.error_injection is not None:
                        result_body = self.error_injection
                    else:
                        result_body = {
                            "detections": [{"label": "person", "confidence": 0.9,
                                            "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3}}],
                            "frame_dimensions": {"w": 64, "h": 64},
                        }
                    await ws.send(json.dumps({
                        "type": "result",
                        "seq": parsed.get("seq", 0),
                        "ts_ms": parsed.get("ts_ms", 0),
                        "inference_ms": 1,
                        "result": result_body,
                    }))
                elif msg_type in ("pause", "resume", "stats"):
                    if msg_type == "stats":
                        await ws.send(json.dumps({
                            "type": "stats", "inflight": 0,
                            "queue_depth": 0, "fps": 0.0,
                        }))
                    # pause/resume are no-ops in the fake
                else:
                    await ws.close(code=4001, reason=f"unknown: {msg_type}")
                    return
        except websockets.ConnectionClosed:
            return


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def kaic_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_SOVEREIGNTY", "local_only")
    monkeypatch.setenv("ADAPTER_URL", "http://127.0.0.1:65535")
    monkeypatch.setenv("KAI_C_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("INTERNAL_API_KEY", "")  # dev-mode for most tests
    # Internal surface fails closed on an unset key (c6d7b1f); the dev-mode
    # tests run as an authorised local-dev box via the explicit opt-in. Tests
    # that assert auth rejection set a real INTERNAL_API_KEY, which overrides.
    monkeypatch.setenv("KAI_C_ALLOW_ANONYMOUS", "true")
    return {"audit_path": tmp_path / "audit.jsonl"}


@pytest.fixture
def kaic_app(kaic_test_env, monkeypatch: pytest.MonkeyPatch):
    """KAI-C TestClient with an HTTP stub for registration.

    Returns (client, audit_path, register_with_url) — the helper
    `register_with_url` re-registers the stub adapter pointing at a
    real WS port (each streaming test mints its own port)."""
    if "main" in sys.modules:
        importlib.reload(sys.modules["main"])
    import main as kaic_main

    transport = _stub_adapter_http(stream=True)
    original_init = kaic_main.AdapterRegistry.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(kaic_main.AdapterRegistry, "__init__", patched_init)

    from fastapi.testclient import TestClient
    with TestClient(kaic_main.app) as client:
        def register(name: str, url: str) -> None:
            r = client.post("/api/v1/adapters/register",
                            json={"name": name, "url": url})
            assert r.status_code == 200, r.text

        yield client, kaic_test_env["audit_path"], register


@pytest.fixture
def fake_adapter():
    """Start + tear down a §6 fake adapter on a real localhost port."""
    port = _free_port()
    server = _FakeAdapterWSServer(port=port)
    server.start()
    try:
        yield server, f"http://127.0.0.1:{port}"
    finally:
        server.stop()


# ── Tests ─────────────────────────────────────────────────────────


def _read_audit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_url_translation():
    """http://host:port → ws://host:port/infer/stream;
    https → wss; other schemes rejected."""
    from kai_c.stream_proxy import adapter_ws_url
    assert adapter_ws_url("http://localhost:9002") == "ws://localhost:9002/infer/stream"
    assert adapter_ws_url("https://adapter.example/") == "wss://adapter.example/infer/stream"
    with pytest.raises(ValueError, match="not supported"):
        adapter_ws_url("ftp://nope")


def test_unknown_adapter_closes_4001(kaic_app):
    client, _, _ = kaic_app
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/infer/no-such-adapter/stream") as ws:
            ws.receive_text()
    assert exc_info.value.code == 4001


def test_adapter_without_streaming_closes_4002(
    kaic_test_env, monkeypatch: pytest.MonkeyPatch
):
    """A registered adapter whose capabilities.infer_stream.supported is
    False must refuse the WS upgrade with model_error (4002)."""
    if "main" in sys.modules:
        importlib.reload(sys.modules["main"])
    import main as kaic_main

    transport = _stub_adapter_http(stream=False)  # streaming disabled
    original_init = kaic_main.AdapterRegistry.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(kaic_main.AdapterRegistry, "__init__", patched_init)

    from fastapi.testclient import TestClient
    with TestClient(kaic_main.app) as client:
        client.post("/api/v1/adapters/register",
                    json={"name": "no-stream", "url": "http://127.0.0.1:65500"})
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/api/v1/infer/no-stream/stream") as ws:
                ws.receive_text()
        assert exc_info.value.code == 4002


def test_auth_required_when_internal_api_key_set(
    kaic_test_env, monkeypatch: pytest.MonkeyPatch
):
    """When INTERNAL_API_KEY is set, WS upgrades without the header
    are refused with policy_refused (4001). The HTTP path's
    Depends(require_internal_api_key) doesn't run on WS — this verifies
    we check it inline."""
    monkeypatch.setenv("INTERNAL_API_KEY", "secret-test-key")
    if "main" in sys.modules:
        importlib.reload(sys.modules["main"])
    import main as kaic_main

    transport = _stub_adapter_http(stream=True)
    original_init = kaic_main.AdapterRegistry.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(kaic_main.AdapterRegistry, "__init__", patched_init)

    from fastapi.testclient import TestClient
    with TestClient(kaic_main.app) as client:
        # Registration itself needs the header — supply it for setup.
        r = client.post(
            "/api/v1/adapters/register",
            json={"name": "auth-test", "url": "http://127.0.0.1:65500"},
            headers={"X-Internal-Api-Key": "secret-test-key"},
        )
        assert r.status_code == 200, r.text

        # WS without header → refused.
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/api/v1/infer/auth-test/stream") as ws:
                ws.receive_text()
        assert exc_info.value.code == 4001

        # WS with wrong header → refused.
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/api/v1/infer/auth-test/stream",
                headers={"X-Internal-Api-Key": "wrong"},
            ) as ws:
                ws.receive_text()
        assert exc_info.value.code == 4001


def test_adapter_unreachable_closes_4002(kaic_app):
    """Registered adapter URL points at a port nothing is listening on.
    Upstream connect fails → client gets model_error (4002) and we
    emit a stream.failed audit event."""
    client, audit_path, register = kaic_app
    # Register pointing at a port that's almost certainly free.
    register("unreachable", "http://127.0.0.1:9999")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/infer/unreachable/stream") as ws:
            ws.receive_text()
    assert exc_info.value.code == 4002

    events = _read_audit(audit_path)
    failed = [e for e in events if e["type"] == "stream.failed"]
    assert len(failed) == 1
    assert failed[0]["error_code"] == "adapter_unreachable"


def test_happy_path_roundtrip(kaic_app, fake_adapter):
    """End-to-end: client → KAI-C proxy → fake adapter → handshake_ack
    → frame_meta + binary → result. Audit log shows opened + closed."""
    server, http_url = fake_adapter
    client, audit_path, register = kaic_app
    register("stub-stream", http_url)

    with client.websocket_connect(
        "/api/v1/infer/stub-stream/stream",
        headers={"X-Correlation-Id": "corr-happy-1"},
    ) as ws:
        # 1. Handshake.
        ws.send_text(json.dumps({
            "type": "handshake", "client_id": "test", "camera_id": "cam-1",
            "frame_transport": "websocket",
        }))
        ack = json.loads(ws.receive_text())
        assert ack["type"] == "handshake_ack"
        assert ack["frame_transport"] == "websocket"
        # 2. Frame metadata + binary.
        ws.send_text(json.dumps({
            "type": "frame", "seq": 42, "ts_ms": 1000,
            "content_type": "image/jpeg",
        }))
        ws.send_bytes(b"fake-jpeg-bytes")
        result = json.loads(ws.receive_text())
        assert result["type"] == "result"
        assert result["seq"] == 42
        assert "detections" in result["result"]
        # 3. Close.
        ws.send_text(json.dumps({"type": "close", "reason": "done"}))

    # Audit chain: opened + closed for the session.
    events = _read_audit(audit_path)
    opened = [e for e in events if e["type"] == "stream.opened"]
    closed = [e for e in events if e["type"] == "stream.closed"]
    assert len(opened) == 1 and opened[0]["correlation_id"] == "corr-happy-1"
    assert len(closed) == 1 and closed[0]["correlation_id"] == "corr-happy-1"
    assert closed[0]["adapter"] == "stub-stream"

    # The fake adapter saw the correlation_id on its upstream connect.
    assert server.last_headers.get("x-correlation-id") == "corr-happy-1"


def test_correlation_id_minted_when_absent(kaic_app, fake_adapter):
    """No X-Correlation-Id on the upgrade → KAI-C mints one and threads
    it through. Fake adapter sees a non-empty value."""
    server, http_url = fake_adapter
    client, audit_path, register = kaic_app
    register("stub-stream", http_url)

    with client.websocket_connect("/api/v1/infer/stub-stream/stream") as ws:
        ws.send_text(json.dumps({
            "type": "handshake", "client_id": "t", "camera_id": "c",
            "frame_transport": "websocket",
        }))
        json.loads(ws.receive_text())  # consume ack
        ws.send_text(json.dumps({"type": "close"}))

    minted = server.last_headers.get("x-correlation-id")
    assert minted is not None and len(minted) >= 8

    events = _read_audit(audit_path)
    opened = [e for e in events if e["type"] == "stream.opened"]
    assert opened[0]["correlation_id"] == minted


# Regression test for A2.4b peer-review H1 (upstream close code →
# stream.closed audit close_reason) deferred to a follow-up slice —
# the asyncio interaction between the websockets-server fake adapter,
# the websockets-client proxy upstream, and Starlette's anyio-wrapped
# test client makes "adapter unilaterally closes with code 4003" hang
# in `ws.receive_text()`. The fix is in `_pump_adapter_to_client`
# (captures `ConnectionClosed.code` into ``self._upstream_close_code``)
# and the finally block prefers it over "normal" — that's mechanical
# and reviewable from the diff. A real-streams smoke test against a
# live SDK adapter will exercise it.


def test_frame_error_envelope_audits_stream_failed(kaic_app, fake_adapter):
    """When the adapter's result message embeds a §7 FailureEnvelope
    (status=error), the proxy emits a stream.failed audit event but
    still relays the message to the client (no session abort)."""
    server, http_url = fake_adapter
    client, audit_path, register = kaic_app
    register("stub-stream", http_url)

    # Tell the fake adapter to emit an error envelope on the next frame.
    server.error_injection = {
        "status": "error",
        "error": {
            "category": "transport_error", "code": "malformed_input",
            "message": "bad bytes", "transient": False, "details": {},
        },
    }

    with client.websocket_connect("/api/v1/infer/stub-stream/stream") as ws:
        ws.send_text(json.dumps({
            "type": "handshake", "client_id": "t", "camera_id": "c",
            "frame_transport": "websocket",
        }))
        json.loads(ws.receive_text())  # ack
        ws.send_text(json.dumps({
            "type": "frame", "seq": 7, "ts_ms": 0,
            "content_type": "image/jpeg",
        }))
        ws.send_bytes(b"some-bytes")
        result = json.loads(ws.receive_text())
        # Client receives the error envelope verbatim — the proxy
        # doesn't swallow it.
        assert result["result"]["status"] == "error"
        assert result["result"]["error"]["code"] == "malformed_input"
        # Session stays open after a frame error.
        ws.send_text(json.dumps({"type": "close"}))

    events = _read_audit(audit_path)
    failed = [e for e in events if e["type"] == "stream.failed"]
    assert len(failed) == 1
    assert failed[0]["error_code"] == "malformed_input"
    assert failed[0]["seq"] == 7

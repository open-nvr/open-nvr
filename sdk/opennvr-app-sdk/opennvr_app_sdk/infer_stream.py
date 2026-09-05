# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A persistent KAI-C inference session over WebSocket (contract §6).

The HTTP path (``KaiCClient.infer``) pays a connection and a base64
round-trip per frame; a detector polling several frames a second per
camera wants one open session per camera and raw JPEG bytes on the
wire. Three example apps each carried their own copy of this client —
this is the one they share::

    with nvr.ai.stream("yolov8", camera_id="cam1") as session:
        while running:
            result = session.infer(jpeg)     # §5.1-shaped dict

Reconnect-on-failure is the caller's loop: a failed ``infer`` tears the
session down and raises ``KaiCError``; the next call reconnects and
re-handshakes with a fresh sequence. Needs the ``websockets`` package
(an SDK dependency).
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from .frame_app import KaiCError

MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class InferStream:
    def __init__(self, kaic_url: str, api_key: str | None, *, adapter: str,
                 camera_id: str, client_id: str = "opennvr-app",
                 timeout: float = 10.0,
                 websocket_factory: Callable[[str, list[tuple[str, str]]], Any] | None = None,
                 ) -> None:
        parsed = urlparse(kaic_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        prefix = (parsed.path or "").rstrip("/")
        self.url = urlunparse((scheme, parsed.netloc,
                               f"{prefix}/api/v1/infer/{adapter}/stream", "", "", ""))
        self._api_key = api_key
        self._camera_id = str(camera_id)
        self._client_id = client_id
        self._timeout = timeout
        self._factory = websocket_factory
        self._conn: Any = None
        self._seq = 0
        self.correlation_id: str | None = None

    # ── session ────────────────────────────────────────────────────

    def _connect(self, headers: list[tuple[str, str]]) -> Any:
        if self._factory is not None:
            return self._factory(self.url, headers)
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover
            raise KaiCError("streaming inference needs the 'websockets' package") from exc
        try:
            return connect(self.url, additional_headers=headers,
                           open_timeout=self._timeout, close_timeout=2.0,
                           max_size=MAX_MESSAGE_BYTES)
        except Exception as exc:  # noqa: BLE001
            raise KaiCError(f"WS connect to {self.url} failed: {exc}") from exc

    def open(self, correlation_id: str | None = None) -> "InferStream":
        """Connect + §6.1 handshake (idempotent)."""
        if self._conn is not None:
            return self
        cid = correlation_id or f"app-{uuid.uuid4().hex[:12]}"
        headers = [("X-Correlation-Id", cid)]
        if self._api_key:
            headers.append(("X-Internal-Api-Key", self._api_key))
        conn = self._connect(headers)
        try:
            conn.send(json.dumps({"type": "handshake", "client_id": self._client_id,
                                  "camera_id": self._camera_id,
                                  "frame_transport": "websocket"}))
            ack = _loads(conn.recv(timeout=self._timeout))
            if not isinstance(ack, dict) or ack.get("type") != "handshake_ack":
                raise KaiCError(f"unexpected handshake response: {ack!r}")
        except KaiCError:
            _close(conn)
            raise
        except Exception as exc:  # noqa: BLE001
            _close(conn)
            raise KaiCError(f"WS handshake failed: {exc}") from exc
        self._conn = conn
        self.correlation_id = cid
        self._seq = 0                     # §6.3: monotonic PER SESSION
        return self

    def infer(self, jpeg: bytes) -> dict[str, Any]:
        """Send one frame, return a §5.1-shaped result. Raises
        ``KaiCError`` and closes the session on any failure."""
        self.open()
        self._seq += 1
        try:
            self._conn.send(json.dumps({"type": "frame", "seq": self._seq,
                                        "ts_ms": int(time.monotonic() * 1000),
                                        "content_type": "image/jpeg"}))
            self._conn.send(jpeg)
            raw = self._conn.recv(timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise KaiCError(f"WS infer failed: {exc}") from exc
        try:
            payload = _loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise KaiCError(f"WS recv: non-JSON payload: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("type") != "result":
            raise KaiCError(f"WS recv: unexpected message {payload!r}")
        return {
            "status": "ok",
            "model_name": "", "model_version": "",
            "inference_ms": int(payload.get("inference_ms", 0) or 0),
            "result": payload.get("result") or {},
            # All frames of a session share KAI-C's audit correlation id.
            "correlation_id": self.correlation_id,
        }

    def close(self) -> None:
        if self._conn is not None:
            _close(self._conn)
            self._conn = None

    def __enter__(self) -> "InferStream":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()


def _loads(raw: Any) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass

# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
KAI-C WebSocket streaming proxy (§6).

Bridges a monitoring app's WebSocket connection to a registered
adapter's ``/infer/stream`` endpoint. The proxy is deliberately thin:

* Inbound  (monitoring app → KAI-C)  : already a FastAPI ``WebSocket``
* Outbound (KAI-C → adapter)         : a ``websockets`` client
* Bidi message pump                  : two ``asyncio.Task``s wired via
                                       ``asyncio.gather`` with cancel-
                                       on-first-completion semantics.

Audit emission is per-session, not per-frame:

* ``stream.opened``  — after the adapter accepts the WS upgrade and
                       sends its handshake_ack
* ``stream.closed``  — on normal close (either side initiated)
* ``stream.failed``  — on transport errors (adapter unreachable,
                       protocol violation) AND on per-frame error
                       envelopes the adapter embeds in §6.3 result
                       messages (one event per error frame; chatty
                       error rates surface as audit-log volume)

Why session-level for OK outcomes: 30 fps × 10 cameras × 86 400 s/day
= 26M events/day. Even at the §11.5 alert grain (which we'll add when
we have real customers needing it), per-frame OK auditing would dwarf
the audit log. Session-level is the right granularity; per-frame
metrics live in the adapter's Prometheus output, not the audit trail.

Auth (current state):

* Inbound  : ``X-Internal-Api-Key`` header on WS upgrade — same as the
             HTTP path's middleware would enforce, but FastAPI doesn't
             run BaseHTTPMiddleware on WS upgrades so we check
             explicitly.
* Outbound : Bearer-token auth to adapters is NOT yet wired — adapters
             run in "dev mode" today. When that gap is closed in a
             follow-up slice, pass the token via the upstream
             ``Authorization`` header here.

Streaming-related items intentionally NOT in this slice:

* Shared-memory transport (§6.2) — proxy forces ``frame_transport=
  websocket`` in the handshake_ack pass-through. The adapter already
  downgrades; we don't introduce a new path.
* Per-camera fair queuing at the proxy layer — adapters do this
  themselves via ``scheduling.fair_queuing="per_camera"`` (§9).
  KAI-C is a transparent pipe.
* Adapter-side bearer-token auth — see above.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect

# ``websockets`` provides the async client we use to reach the adapter.
# We import the small subset we actually call so we don't depend on
# the library's evolving module layout.
import websockets
from websockets.exceptions import ConnectionClosed

from kai_c.audit import AuditEventType, AuditStore

logger = logging.getLogger(__name__)


# §6.5 close codes used by the proxy when rejecting WS upgrades.
# We import these by name from main.py — only the codes the proxy
# itself emits are defined here; adapter-emitted codes pass through
# verbatim and are recorded in stream.closed audit events.
CLOSE_POLICY_REFUSED: int = 4001  # §6.5 — auth, unknown adapter, etc.
CLOSE_MODEL_ERROR: int = 4002      # §6.5 — upstream unreachable / not stream-capable

# Mapping from adapter URL scheme → WS scheme. Adapters register with
# their HTTP URL (http://host:9002); KAI-C connects to the WS path on
# the same host/port.
_SCHEME_MAP: dict[str, str] = {"http": "ws", "https": "wss"}


def _extract_handshake_camera_id(text: str) -> str | None:
    """Pull ``camera_id`` out of a §6.1 handshake text frame. Returns
    None for any malformed input — the adapter is responsible for the
    actual protocol enforcement; we just need camera_id for the B1
    NATS subject and treat parse failure as ``camera_id=None`` →
    falls back to ``unknown`` in the subject.

    Only the FIRST text frame is inspected (the §6.1 handshake). If a
    broken client sends a control message before the handshake, the
    adapter closes the WS with policy_refused (4001) — at which point
    nothing else gets published anyway. We do NOT keep trying to
    extract camera_id from subsequent frames; that would risk picking
    up a frame_meta's incidental ``camera_id`` field (if any) and
    bind the stream to the wrong identifier mid-session.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "handshake":
        return None
    cam = payload.get("camera_id")
    if cam is None or not isinstance(cam, str) or not cam.strip():
        return None
    return cam.strip()


def adapter_ws_url(adapter_http_url: str) -> str:
    """Translate ``http(s)://host:port`` → ``ws(s)://host:port/infer/stream``.

    Adapter registration takes the HTTP URL (used for /capabilities,
    /health, /infer); we derive the WS URL from it so operators only
    register one URL per adapter.
    """
    parsed = urlparse(adapter_http_url)
    ws_scheme = _SCHEME_MAP.get(parsed.scheme.lower())
    if ws_scheme is None:
        raise ValueError(
            f"adapter URL scheme {parsed.scheme!r} is not supported "
            "(expected http or https)"
        )
    # Replace the path (whatever it was) with /infer/stream.
    return urlunparse(parsed._replace(scheme=ws_scheme, path="/infer/stream"))


class StreamProxy:
    """One instance per session. Coordinates the bidi pump."""

    def __init__(
        self,
        *,
        client_ws: WebSocket,
        adapter_name: str,
        adapter_url: str,
        correlation_id: str,
        audit: AuditStore,
        connect_timeout_seconds: float = 5.0,
        nats_publisher: Any = None,
        adapter_info: Any = None,
    ) -> None:
        self._client = client_ws
        self._adapter_name = adapter_name
        self._adapter_url = adapter_url
        self._correlation_id = correlation_id
        self._audit = audit
        self._connect_timeout = connect_timeout_seconds
        # B1 — NATS broadcast surface. ``nats_publisher`` is optional
        # (back-compat with existing tests; production builds it from
        # config in main.py). ``adapter_info`` is the registry's
        # cached ``RegisteredAdapter`` so we can pull model_name /
        # version / fingerprint without re-querying /capabilities
        # per frame.
        self._nats_publisher = nats_publisher
        self._adapter_info = adapter_info
        # Camera_id is captured from the §6.1 handshake — needed for
        # the published event's subject + body. Defaulted to None
        # until handshake completes.
        self._camera_id: str | None = None
        # In-flight NATS publish tasks. ``asyncio.create_task`` doesn't
        # hold a strong ref to the task; under burst load
        # (30 fps × N streams) GC could theoretically collect pending
        # publishes. Keep a set + ``add_done_callback(discard)`` so
        # tasks live until they complete. (Peer review M1.)
        self._inflight_publishes: set = set()
        # Captured by either pump if the upstream closes with a non-
        # 1000 code; surfaced in the ``stream.closed`` audit event so
        # operators see e.g. "model_error" vs "normal" in the audit
        # trail. (Peer review H1 — without this the audit log records
        # a clean close even when the upstream blew up.)
        self._upstream_close_code: int | None = None
        self._upstream_close_reason: str | None = None

    async def run(self) -> None:
        """Drive the full session lifecycle. ``client_ws`` has NOT been
        accepted yet — we accept only after we know the upstream is
        reachable, so failures surface as a clean WS close with a §6.5
        code rather than a confusing post-accept disconnect."""
        upstream_url = adapter_ws_url(self._adapter_url)

        # Connect to the adapter first. If the adapter is unreachable
        # we reject the client upgrade with a typed close code rather
        # than letting them dangle.
        upstream_headers = [("X-Correlation-Id", self._correlation_id)]
        try:
            upstream = await asyncio.wait_for(
                websockets.connect(
                    upstream_url,
                    additional_headers=upstream_headers,
                    max_size=None,  # adapter enforces its own limit
                ),
                timeout=self._connect_timeout,
            )
        except (OSError, asyncio.TimeoutError, ConnectionClosed) as exc:
            self._audit.emit(
                AuditEventType.STREAM_FAILED,
                correlation_id=self._correlation_id,
                adapter=self._adapter_name,
                error_category="transport_error",
                error_code="adapter_unreachable",
                error_message=str(exc),
            )
            # Reject the client WS upgrade with the §6.5 model_error
            # code — the symptom from the client's POV is "I can't
            # talk to my model" regardless of whether the adapter is
            # down or just slow.
            await self._client.close(
                code=CLOSE_MODEL_ERROR,
                reason="adapter unreachable",
            )
            return

        # Upstream is up — accept the client's WS upgrade.
        await self._client.accept()
        self._audit.emit(
            AuditEventType.STREAM_OPENED,
            correlation_id=self._correlation_id,
            adapter=self._adapter_name,
        )

        # Bidi pump. Whichever side closes first cancels the other.
        client_to_adapter = asyncio.create_task(
            self._pump_client_to_adapter(upstream)
        )
        adapter_to_client = asyncio.create_task(
            self._pump_adapter_to_client(upstream)
        )

        close_reason = "normal"
        try:
            done, pending = await asyncio.wait(
                {client_to_adapter, adapter_to_client},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            # Surface the first exception (if any) so it lands in the
            # audit log as the close_reason. Other task results are
            # discarded — once one side closes the session is over.
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    close_reason = f"{type(exc).__name__}: {exc}"
                    break
            # Drain pending tasks. We swallow CancelledError (expected
            # — that's why we cancelled them), but anything else is
            # logged at WARNING so an ASGI/transport bug doesn't
            # vanish into the void. (Peer review H2.)
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "drained pump task raised: %s",
                        exc,
                        extra={"correlation_id": self._correlation_id},
                    )
        finally:
            # If the upstream closed with a non-1000 code, prefer that
            # over the loop's "normal" — the close code is the
            # adapter's reason for hanging up and is what an operator
            # needs to see in the audit trail. (Peer review H1.)
            if (
                close_reason == "normal"
                and self._upstream_close_code is not None
                and self._upstream_close_code != 1000
            ):
                detail = self._upstream_close_reason or ""
                close_reason = f"upstream_close:{self._upstream_close_code}"
                if detail:
                    close_reason = f"{close_reason} {detail}"
            # Emit the audit event BEFORE closing the sockets — the
            # close calls below can stall on a slow WS close-handshake
            # (we've observed this with anyio-wrapped Starlette WS in
            # the test client), and the audit-log integrity guarantee
            # is "every opened session has a closed event you can read
            # immediately after the WS returns." A close that itself
            # errors gets logged at WARNING in ``_safe_close_*``; if
            # operators need close-handshake-error visibility in the
            # audit trail itself, a follow-up slice can split this
            # into a separate ``stream.close_error`` event. (Peer
            # review M4 — tradeoff documented.)
            self._audit.emit(
                AuditEventType.STREAM_CLOSED,
                correlation_id=self._correlation_id,
                adapter=self._adapter_name,
                close_reason=close_reason,
            )
            await self._safe_close_upstream(upstream)
            await self._safe_close_client()

    async def _pump_client_to_adapter(self, upstream: Any) -> None:
        """Forward control + frame_meta + binary messages from the
        monitoring app to the adapter. Distinguishes text vs binary
        and forwards each verbatim — no contract enforcement here;
        the adapter does that.
        """
        try:
            while True:
                msg = await self._client.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                if (text := msg.get("text")) is not None:
                    # B1 — capture camera_id from the first text frame
                    # (the §6.1 handshake). Subsequent frames are
                    # ``frame_meta`` / ``pause`` / etc. and don't
                    # carry a camera_id, but the handshake does and
                    # we need it for the NATS subject. Best-effort:
                    # a malformed handshake just leaves camera_id as
                    # None; the broader §6 protocol-violation handling
                    # in the adapter will close the session.
                    if self._camera_id is None:
                        self._camera_id = _extract_handshake_camera_id(text)
                    await upstream.send(text)
                    continue
                if (data := msg.get("bytes")) is not None:
                    await upstream.send(data)
                    continue
                # Unknown message shape — Starlette guarantees text
                # XOR bytes, so this branch is defensive. If it ever
                # fires it's an ASGI-layer regression worth seeing in
                # the logs rather than silently ending the session.
                # (Peer review L5.)
                logger.warning(
                    "WS pump received message with neither text nor bytes: %r",
                    msg,
                    extra={"correlation_id": self._correlation_id},
                )
                return
        except WebSocketDisconnect:
            return
        except ConnectionClosed:
            # Upstream went away while we were writing. The other pump
            # will catch it and close the client side.
            return

    async def _pump_adapter_to_client(self, upstream: Any) -> None:
        """Forward handshake_ack + result + control messages from the
        adapter back to the monitoring app. Inspect text frames for
        §6.3 error-shaped results and emit per-error audit events
        without consuming the message — the client still receives it.

        Captures the adapter's close code on ``ConnectionClosed`` so
        the finally block can prefer it over the generic "normal"
        close_reason in the audit event. (Peer review H1.)
        """
        try:
            async for msg in upstream:
                if isinstance(msg, (bytes, bytearray)):
                    await self._client.send_bytes(bytes(msg))
                    continue
                # Text frame. Inspect once — audit on error envelopes
                # AND broadcast successful results to NATS. Both
                # inspections share the json.loads cost (one parse
                # per result frame).
                self._inspect_result_text(msg)
                await self._client.send_text(msg)
        except ConnectionClosed as exc:
            # Capture for the audit event. ``code`` is always present
            # on ConnectionClosed; ``reason`` may be empty.
            self._upstream_close_code = getattr(exc, "code", None)
            self._upstream_close_reason = getattr(exc, "reason", None) or None
            return
        except WebSocketDisconnect:
            return

    def _inspect_result_text(self, text: str) -> None:
        """Parse the §6.3 ``result`` message once, then:

        * if it's an error-shaped envelope (status=error), emit a
          STREAM_FAILED audit event;
        * otherwise, publish an ``InferenceCompletedEvent`` on NATS
          for the broadcast surface (B1).

        Both branches are best-effort — the parse error path returns
        silently and the broadcast publish path swallows its own
        errors. The client still receives the relayed message
        regardless. One ``json.loads`` per result frame; at sustained
        30 fps that's negligible.
        """
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "result":
            return
        result = payload.get("result")
        if not isinstance(result, dict):
            return
        # §7 envelope shape: top-level status="error" + error object.
        if result.get("status") == "error":
            error = result.get("error") or {}
            self._audit.emit(
                AuditEventType.STREAM_FAILED,
                correlation_id=self._correlation_id,
                adapter=self._adapter_name,
                seq=payload.get("seq"),
                error_category=error.get("category", "unknown"),
                error_code=error.get("code", "unknown"),
                transient=error.get("transient", False),
            )
            return
        # Success path — broadcast to NATS. Synchronously kicked
        # off as an asyncio task so the pump isn't blocked by a slow
        # publish; ``NatsPublisher.publish_inference_completed``
        # already swallows errors internally.
        if self._nats_publisher is not None and getattr(
            self._nats_publisher, "enabled", False
        ):
            self._schedule_inference_broadcast(payload, result)

    def _schedule_inference_broadcast(
        self,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Build the ``InferenceCompletedEvent`` from a successful §6.3
        result frame and schedule its publish on the running event
        loop. Per-frame; never raises.
        """
        # Lazy import to keep stream_proxy.py importable without
        # nats-py installed (the publisher is optional).
        from kai_c.events import InferenceCompletedEvent

        # Adapter info is registry-cached so we don't re-query
        # /capabilities per frame. May be None in test fixtures that
        # don't pass adapter_info — broadcast still works, just with
        # empty model_name/version/fingerprint.
        model_name = ""
        model_version = ""
        fingerprint = None
        adapter_version = None
        if self._adapter_info is not None:
            try:
                model_name = self._adapter_info.capabilities.model.name or ""
                model_version = self._adapter_info.capabilities.model.version or ""
                fingerprint = self._adapter_info.capabilities.model.fingerprint
                adapter_version = self._adapter_info.capabilities.adapter.version
            except Exception:  # noqa: BLE001
                pass

        try:
            event = InferenceCompletedEvent(
                correlation_id=self._correlation_id,
                adapter=self._adapter_name,
                adapter_version=adapter_version,
                camera_id=self._camera_id,
                model_name=model_name,
                model_version=model_version,
                model_fingerprint=fingerprint,
                inference_ms=int(payload.get("inference_ms", 0) or 0),
                # §6.3 seq — WS path only; HTTP path leaves None.
                # Subscribers can dedupe / detect drops with this.
                # (Peer review M2.)
                seq=payload.get("seq") if isinstance(payload.get("seq"), int) else None,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "NATS broadcast skipped — WS event-build failure: %s "
                "[correlation_id=%s]", exc, self._correlation_id,
            )
            return
        # Keep a ref so the task isn't GC'd before it completes; the
        # done-callback discards it so the set doesn't grow unbounded.
        task = asyncio.create_task(
            self._nats_publisher.publish_inference_completed(event)
        )
        self._inflight_publishes.add(task)
        task.add_done_callback(self._inflight_publishes.discard)

    # Close helpers bound at 1s — a stalled WS close-handshake
    # shouldn't pin the proxy task. Errors are logged at WARNING (peer
    # review L2 — was previously DEBUG, which silently lost transport
    # issues).

    _CLOSE_TIMEOUT_SECONDS: float = 1.0

    async def _safe_close_upstream(self, upstream: Any) -> None:
        try:
            await asyncio.wait_for(upstream.close(), timeout=self._CLOSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "upstream close timed out after %ss",
                self._CLOSE_TIMEOUT_SECONDS,
                extra={"correlation_id": self._correlation_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "upstream close raised: %s",
                exc,
                extra={"correlation_id": self._correlation_id},
            )

    async def _safe_close_client(self) -> None:
        try:
            await asyncio.wait_for(self._client.close(), timeout=self._CLOSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "client close timed out after %ss",
                self._CLOSE_TIMEOUT_SECONDS,
                extra={"correlation_id": self._correlation_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "client close raised: %s",
                exc,
                extra={"correlation_id": self._correlation_id},
            )

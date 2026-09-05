# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
The FrameApp base — the frame-driving archetype (App SDK spec §02).

Where a :class:`~.detector.Detector` rides an inference stream someone
else is paying for, a FrameApp DRIVES inference itself: poll a frame
per camera, POST it to KAI-C's contract-v1 proxy, act on the response.
This is the intrusion-detection / LPR / package-delivery /
smart-doorbell shape.

Deliberately minimal for now — the poll skeleton + the KAI-C HTTP
client, modeled on ``examples/intrusion-detection``'s ``KaicClient``.
It exists to prove the archetype fits the SDK surface; the full
intrusion-detection migration (WS streaming transport, cooldowns,
multi-camera fan-out) lands in a later rollout step and may grow this
module.

The frame-fetching edge is a Protocol (:class:`FrameSource`) so apps
can plug OpenNVR's snapshot API, an RTSP grabber, or a test fake
without the base loop caring.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from typing import Any, Iterable, Protocol

import httpx

from .alerts import Alert, AlertDispatcher
from .contract import ContractMixin
from .manifest import AppManifest

logger = logging.getLogger(__name__)


class FrameSource(Protocol):
    """Anything that can produce the latest frame for a camera.

    Returns encoded image bytes (JPEG/PNG — whatever the adapter's
    contract accepts) or ``None`` when no frame is available right now
    (camera offline, snapshot endpoint empty). Raising is also fine —
    the poll loop isolates per-camera fetch failures."""

    def get_frame(self, camera_id: str) -> bytes | None:
        ...  # pragma: no cover — Protocol


class KaiCError(Exception):
    """Raised when KAI-C is unreachable or returns a non-200. Frame
    loops treat this as a transient skip — alerts don't fire on a
    comms failure (the failure itself is visible in KAI-C's audit log
    via the correlation_id we sent)."""


def build_infer_request(
    base_url: str, adapter_name: str, frame_bytes: bytes, *, task: str,
    api_key: str | None = None, camera_id: str | None = None,
    params: dict[str, Any] | None = None, correlation_id: str | None = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """``(url, headers, json body)`` for one KAI-C inference call — the
    one wire shape both the sync :class:`KaiCClient` and the async
    client (``aio.py``) send, so they cannot drift."""
    url = f"{base_url.rstrip('/')}/api/v1/infer/{adapter_name}"
    headers = {
        "X-Correlation-Id": correlation_id or f"app-{uuid.uuid4().hex[:12]}",
    }
    if api_key:
        headers["X-Internal-Api-Key"] = api_key
    # Body shape matches the server's build_infer_payload:
    # {"frame_b64", "task", "camera_id", **params} — adapters read
    # task + params, KAI-C reads camera_id for NATS subject + audit.
    body: dict[str, Any] = {
        "task": task,
        "frame_b64": base64.b64encode(frame_bytes).decode("ascii"),
        **(params or {}),
    }
    if camera_id is not None:
        body["camera_id"] = str(camera_id)
    return url, headers, body


class KaiCClient:
    """Tiny client for KAI-C's ``POST /api/v1/infer/{adapter}``.

    Sends the frame as a base64 JSON body (the contract-v1 convenience
    path — multipart adds boilerplate without benefit at ~1 fps
    polling) and threads ``X-Correlation-Id`` so every alert traces
    back through KAI-C's audit log and the adapter's logs alike. The
    optional API key rides the ``X-Internal-Api-Key`` header, matching
    the intrusion-detection example and KAI-C's internal-auth scheme.
    """

    def __init__(
        self,
        base_url: str,
        adapter_name: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._adapter_name = adapter_name
        self._api_key = api_key
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=timeout_seconds, trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client and hasattr(self._client, "close"):
            self._client.close()

    def infer(
        self,
        frame_bytes: bytes,
        *,
        task: str,
        camera_id: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one frame; return the raw §5.1 ``InferResponse`` body.

        ``camera_id`` is optional, as it is for KAI-C itself: with it
        the call is attributed to the camera (audit, skill budgets, the
        NATS subject); without it KAI-C treats the call as a one-off
        probe. Raises :class:`KaiCError` on transport failure or
        non-200; the frame loop catches and decides whether to alert /
        skip / abort."""
        url, headers, body = build_infer_request(
            self._base_url, self._adapter_name, frame_bytes, task=task,
            api_key=self._api_key, camera_id=camera_id, params=params,
            correlation_id=correlation_id)
        try:
            response = self._client.post(url, json=body, headers=headers)
        except Exception as exc:
            raise KaiCError(f"KAI-C unreachable at {url}: {exc}") from exc
        if response.status_code != 200:
            raise KaiCError(
                f"KAI-C returned HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()


class FrameApp(ContractMixin):
    """Base class for frame-polling apps.

    Subclasses set ``manifest``, optionally override :meth:`setup`, and
    implement :meth:`on_frame`. The base owns the interval loop and
    alert dispatch; per-camera fetch and rule failures are isolated so
    one bad camera never stalls the rest.

    ``cameras`` / ``poll_interval_seconds`` default from the config
    object (``cfg.cameras`` may be a list of ids or a dict keyed by
    id; ``cfg.poll_interval_seconds`` defaults to 5.0).
    """

    manifest: AppManifest | None = None

    def __init__(
        self,
        config: Any,
        dispatcher: AlertDispatcher,
        *,
        frame_source: FrameSource,
        cameras: Iterable[str] | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self.cfg = config
        self._dispatcher = dispatcher
        self._source = frame_source
        if cameras is None:
            cameras = list(getattr(config, "cameras", None) or [])
        self._cameras: list[str] = [str(c) for c in cameras]
        if poll_interval_seconds is None:
            poll_interval_seconds = float(
                getattr(config, "poll_interval_seconds", 5.0)
            )
        if poll_interval_seconds <= 0:
            raise ValueError(
                f"poll_interval_seconds must be > 0, got {poll_interval_seconds!r}"
            )
        self._interval = poll_interval_seconds
        self._stop_event = asyncio.Event()
        self._contract_init()
        self.setup()

    # ── App surface ────────────────────────────────────────────────

    def setup(self) -> None:
        """Optional hook — allocate per-app state."""

    def on_frame(
        self, camera_id: str, frame_bytes: bytes
    ) -> Iterable[Alert] | None:
        """The rule. Called once per fetched frame. Return or yield
        Alerts to fire (or ``None`` / empty)."""
        raise NotImplementedError

    def stop(self) -> None:
        self._stop_event.set()

    # ── Poll loop (tick is testable without asyncio) ───────────────

    def handle_tick(self) -> list[Alert]:
        """One poll cycle: fetch a frame per camera, run the rule,
        dispatch whatever it produced. Fetch / rule failures are
        logged per camera and never propagate."""
        fired: list[Alert] = []
        for camera_id in self._cameras:
            try:
                frame = self._source.get_frame(camera_id)
            except Exception:
                logger.exception("frame fetch failed for camera=%s", camera_id)
                continue
            if not frame:
                continue
            # Contract counters (spec §03): for a FrameApp, one fetched
            # frame is one "event" — /health's last_event_age_s then
            # doubles as camera-stall detection.
            self._contract_note_event()
            try:
                produced = self.on_frame(camera_id, frame)
            except Exception:
                logger.exception("on_frame failed for camera=%s", camera_id)
                continue
            for alert in produced or []:
                self._dispatcher.fire(alert)
                fired.append(alert)
        self._contract_note_alerts(len(fired))
        return fired

    async def run(self, *, once: bool = False) -> None:
        """Poll every ``poll_interval_seconds`` until ``stop()`` (or
        one tick with ``once=True``). The inter-tick sleep is
        interruptible so shutdown is immediate.

        Also owns the app-contract lifecycle (spec §03): starts the
        ``/health`` / ``/manifest`` / ``/state`` server when
        ``cfg.contract_port`` is set and self-registers with the
        OpenNVR app registry when ``cfg.opennvr_url`` is set — both
        best-effort no-ops otherwise."""
        self.start_contract_server()
        self.register_with_opennvr()
        self.start_config_poll()
        try:
            await self._run_poll_loop(once=once)
        finally:
            self.stop_config_poll()
            self.stop_contract_server()

    async def _run_poll_loop(self, *, once: bool) -> None:
        logger.info(
            "%s started: %d cameras, interval=%.1fs",
            self.manifest.id if self.manifest else type(self).__name__,
            len(self._cameras),
            self._interval,
        )
        while not self._stop_event.is_set():
            self.handle_tick()
            if once:
                break
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

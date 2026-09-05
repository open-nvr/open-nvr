# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Intrusion-detection example app — now on the ``opennvr-app-sdk``.

Watches one or more cameras for persons/vehicles entering operator-
defined restricted zones during operator-defined restricted hours. On
detection, fires an alert via stdout (always) and an optional
webhook. Uses KAI-C's contract proxy (``POST /api/v1/infer/{adapter}``)
for inference — so every alert is correlation-id-traceable through
the audit log.

What lives where after the migration
------------------------------------

This is a FrameApp (App SDK spec §02): it DRIVES inference by polling
frames into KAI-C rather than riding an existing inference stream. The
SDK's :class:`~opennvr_app_sdk.FrameApp` base owns the interval loop,
per-camera fetch/rule failure isolation, and the §03 contract
endpoints. The frame sources, the zone geometry, and the §11.5 alert
stack moved into the SDK (thin shims remain at ``frame_sources.py`` /
``zone.py`` / ``alerts.py`` for import compatibility).

Both KAI-C transports come from the SDK too: ``KaicClient`` is the
SDK's ``KaiCClient`` (HTTP, contract-v1 body) and ``KaicStreamClient``
is the SDK's ``InferStream`` (the §6 WebSocket session, opt-in via
``kaic_transport: ws``), each behind this app's historical
``infer_frame`` spelling. What stays here is the app's business logic:
the restricted-hours gate and the zone rule.

Run:
    python intrusion_detection.py --config config.yml          # daemon
    python intrusion_detection.py --config config.yml --once    # one cycle (testing)
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import logging
import signal
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from alerts import Alert, AlertDispatcher, build_dispatcher
from frame_sources import FrameSource, FrameSourceError, build_frame_source
from opennvr_app_sdk import (
    AlertType, AppManifest, FrameApp, InferStream, Param, StateView,
)
from opennvr_app_sdk.frame_app import KaiCClient as SdkKaiCClient, KaiCError
from opennvr_app_sdk.frame_sources import DictFrameSource
from zone import Point, Zone, bbox_center, scale_vertices

logger = logging.getLogger("intrusion-detection")


MANIFEST = AppManifest(
    id="intrusion-detection",
    name="Intrusion Detection",
    version="1.0.0",
    category="perimeter",
    summary=(
        "Alerts when a watched object enters a restricted zone during "
        "restricted hours."
    ),
    requires_tasks=["object_detection"],  # checked vs GET /api/v1/adapters
    subscribes=None,  # FrameApp: drives inference itself via KAI-C
    params=[
        Param("watch_labels", list, default=["person"]),
        Param("poll_interval_seconds", float, default=5.0),
        Param("restricted_hours", "time_range",
              description="Daily {start, end} window; cross-midnight supported."),
        Param("kaic_transport", str, default="http",
              description="'http' polls per frame; 'ws' streams per camera (§6)."),
        Param("zones", "geometry.polygon", per_camera=True),  # drawn in the catalog UI
    ],
    emits=[AlertType("intrusion", severity="high")],
    state_schema=[
        StateView(
            "restricted_now",
            "Restricted now",
            path="restricted_now",
            description=(
                "Whether the app is currently within restricted hours "
                "(i.e. armed and firing on intrusions)."
            ),
        ),
        StateView(
            "intrusions",
            "Intrusions today",
            path="intrusions",
            description="Count of intrusion alerts fired since the app started.",
        ),
        StateView(
            "recent",
            "Recent intrusions",
            path="recent",
            kind="log",
            limit=10,
            description="Most recent intrusion alerts, newest last.",
        ),
    ],
)


# ── Config ─────────────────────────────────────────────────────────


@dataclass
class CameraWatch:
    """One camera + its zone + its frame source. Multiple cameras
    can share the same KAI-C/adapter target — each gets its own
    detector loop iteration."""

    camera_id: str
    frame_url: str  # file://, http://, https://
    zone: Zone
    # Camera frame dimensions in pixels. The contract emits
    # normalized [0, 1] bboxes; we translate back to pixels to
    # compare against the zone polygon, which is operator-defined
    # in pixels.
    frame_width: int
    frame_height: int


@dataclass
class RestrictedHours:
    """A daily time window during which alerts fire. Supports
    cross-midnight ranges (e.g. ``start=22:00, end=06:00``).

    All comparisons use the LOCAL timezone of the host (or the
    operator-supplied ``timezone`` if pytz/zoneinfo is configured).
    For v1 we use ``datetime.now()`` which picks up the host TZ.
    """

    start: _dt.time
    end: _dt.time

    def contains(self, when: _dt.datetime) -> bool:
        """True if ``when.time()`` is within [start, end). Handles
        cross-midnight ranges by inverting the comparison."""
        t = when.time()
        if self.start <= self.end:
            # Normal range, e.g. 09:00 - 17:00.
            return self.start <= t < self.end
        # Cross-midnight range, e.g. 22:00 - 06:00.
        return t >= self.start or t < self.end


@dataclass
class AppConfig:
    """Top-level config loaded from YAML."""

    kaic_url: str
    kaic_adapter_name: str
    kaic_api_key: str | None
    poll_interval_seconds: float
    watch_labels: list[str]
    restricted_hours: RestrictedHours
    cameras: list[CameraWatch]
    webhook_url: str | None
    # Optional NATS alert fan-out. When ``nats_alerts_url`` is set,
    # every fired alert is also published as JSON onto
    # ``{nats_alerts_subject_prefix}.{source.kind}.{source.name}.{camera_id}``.
    # Wire this up to feed the OpenNVR alerts inbox, a SIEM, or any
    # other bus subscriber without standing up additional webhooks.
    # Default-disabled so single-host deployments without NATS just
    # work.
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = "opennvr.alerts"
    request_timeout_seconds: float = 30.0
    # ``kaic_transport`` selects how this example talks to KAI-C:
    #
    # * ``http`` (default, back-compat) — one POST to
    #   /api/v1/infer/{adapter} per polled frame. Simpler; one
    #   connection per cycle (httpx keeps it alive). Latency floor is
    #   the poll interval (~1s default).
    #
    # * ``ws`` — one persistent WebSocket per camera to KAI-C's
    #   /api/v1/infer/{adapter}/stream proxy (§6). Drops per-frame
    #   latency from ~poll_interval to ~adapter inference time
    #   (~30-50ms for YOLOv8) at the cost of one open connection per
    #   camera. Use when you actually need sub-second response on
    #   alerts; HTTP is fine for typical surveillance.
    kaic_transport: str = "http"
    # App contract (spec §03) — all optional; see the SDK's contract
    # module. ``contract_port`` serves /health /manifest /state;
    # ``opennvr_url`` triggers registry self-registration on boot.
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def load_config(path: str) -> AppConfig:
    """Parse a YAML config file into a typed AppConfig.

    Raises ``ValueError`` on malformed config — caller's job to
    surface a useful operator message and exit non-zero."""
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"config {path!r}: root must be a mapping")

    try:
        kaic_url = str(raw["kaic_url"]).rstrip("/")
    except KeyError as exc:
        raise ValueError("config: 'kaic_url' is required") from exc

    poll_interval = float(raw.get("poll_interval_seconds", 5.0))
    if poll_interval <= 0:
        raise ValueError("config: 'poll_interval_seconds' must be > 0")

    rh_raw = raw.get("restricted_hours", {})
    try:
        rh = RestrictedHours(
            start=_dt.time.fromisoformat(str(rh_raw.get("start", "00:00"))),
            end=_dt.time.fromisoformat(str(rh_raw.get("end", "23:59"))),
        )
    except ValueError as exc:
        raise ValueError(f"config: bad restricted_hours value: {exc}") from exc

    cameras_raw = raw.get("cameras") or []
    if not cameras_raw:
        raise ValueError("config: at least one camera entry is required")
    # The App Catalog's zone editor stores geometry as a top-level
    # ``zones`` dict keyed by camera_id, in NORMALIZED 0-1 coords. When
    # present it OVERRIDES the per-camera ``zone`` (the operator's drawn
    # zone wins), scaled to that camera's pixels. The nested ``zone:``
    # form still works for hand-written / legacy config.
    zones_override = raw.get("zones")
    zones_map = zones_override if isinstance(zones_override, dict) else {}
    cameras: list[CameraWatch] = []
    for idx, c in enumerate(cameras_raw):
        try:
            camera_id = str(c["camera_id"])
            frame_width = int(c.get("frame_width", 1920))
            frame_height = int(c.get("frame_height", 1080))
            drawn = zones_map.get(camera_id)
            zone_vertices = (
                scale_vertices(drawn, frame_width, frame_height)
                if isinstance(drawn, list) and drawn
                else c["zone"]
            )
            zone = Zone.from_config(
                name=str(c.get("zone_name", f"zone-{idx}")),
                vertices=zone_vertices,
            )
            cameras.append(
                CameraWatch(
                    camera_id=camera_id,
                    frame_url=str(c["frame_url"]),
                    zone=zone,
                    frame_width=frame_width,
                    frame_height=frame_height,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"config: camera entry {idx} malformed: {exc}"
            ) from exc

    kaic_transport = str(raw.get("kaic_transport", "http")).lower()
    if kaic_transport not in ("http", "ws"):
        raise ValueError(
            f"config: kaic_transport must be 'http' or 'ws', got {kaic_transport!r}"
        )

    nats_alerts_url = str(raw["nats_alerts_url"]).strip() if raw.get("nats_alerts_url") else None
    nats_alerts_token = str(raw["nats_alerts_token"]) if raw.get("nats_alerts_token") else None
    # Refuse an explicitly-empty prefix; absent → use the default.
    if "nats_alerts_subject_prefix" in raw:
        nats_prefix = str(raw["nats_alerts_subject_prefix"]).strip()
        if not nats_prefix:
            raise ValueError(
                "config: 'nats_alerts_subject_prefix' must not be empty "
                "(omit the key to use the default 'opennvr.alerts')"
            )
    else:
        nats_prefix = "opennvr.alerts"

    return AppConfig(
        kaic_url=kaic_url,
        kaic_adapter_name=str(raw.get("kaic_adapter_name", "yolov8")),
        kaic_api_key=str(raw["kaic_api_key"]) if raw.get("kaic_api_key") else None,
        poll_interval_seconds=poll_interval,
        watch_labels=[str(s).lower() for s in raw.get("watch_labels", ["person"])],
        restricted_hours=rh,
        cameras=cameras,
        webhook_url=str(raw["webhook_url"]) if raw.get("webhook_url") else None,
        nats_alerts_url=nats_alerts_url,
        nats_alerts_token=nats_alerts_token,
        nats_alerts_subject_prefix=nats_prefix,
        request_timeout_seconds=float(raw.get("request_timeout_seconds", 30.0)),
        kaic_transport=kaic_transport,
        contract_port=(
            int(raw["contract_port"]) if raw.get("contract_port") is not None else None
        ),
        contract_bind_host=raw.get("contract_bind_host"),
        contract_host=raw.get("contract_host"),
        opennvr_url=raw.get("opennvr_url"),
        opennvr_token=raw.get("opennvr_token"),
    )


# ── KAI-C clients (SDK-backed) ─────────────────────────────────────
#
# Both transports come from the SDK now: ``KaiCClient`` for the one-shot
# HTTP path and ``InferStream`` for the contract §6 WebSocket session.
# The two classes below keep this app's historical call shape
# (``infer_frame(camera_id=, frame_bytes=, correlation_id=)``) so the
# detector loop and its tests read unchanged; the wire body is the
# contract-v1 one the SDK speaks (``task`` + ``camera_id`` + ``frame_b64``).


class KaicClient(SdkKaiCClient):
    """HTTP path — the SDK client with this app's ``infer_frame`` spelling."""

    def infer_frame(
        self,
        *,
        camera_id: str,
        frame_bytes: bytes,
        correlation_id: str,
    ) -> dict[str, Any]:
        return self.infer(
            frame_bytes, task=INFER_TASK, camera_id=camera_id,
            correlation_id=correlation_id,
        )


#: The detector's task on KAI-C (contract v1 ``task`` field).
INFER_TASK = "object_detection"

#: Raised on transport failure / non-200 / protocol violation. The
#: detector loop treats it as a transient skip — alerts don't fire on a
#: comms failure (the failure itself is in KAI-C's audit log via the
#: correlation_id we sent). Alias of the SDK's exception so either
#: spelling catches both transports.
KaicError = KaiCError


class KaicStreamClient:
    """Per-camera persistent WebSocket session — ``opennvr_app_sdk``'s
    :class:`InferStream` behind this app's ``infer_frame`` spelling.

    Adds the ``__session_correlation_id`` response key the detector
    uses: KAI-C audits at SESSION grain (``stream.opened`` / ``closed``),
    so every alert from a session must reference the session's
    correlation id, not the per-step one ``step()`` generated.
    """

    def __init__(
        self,
        base_url: str,
        adapter_name: str,
        camera_id: str,
        *,
        api_key: str | None,
        timeout_seconds: float,
        websocket_factory: Callable[[str, list[tuple[str, str]]], Any] | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"kaic_url must be http:// or https:// (got {base_url!r})"
            )
        self._stream = InferStream(
            base_url, api_key, adapter=adapter_name, camera_id=camera_id,
            client_id="intrusion-detection", timeout=timeout_seconds,
            websocket_factory=websocket_factory,
        )
        self._url = self._stream.url

    def infer_frame(
        self,
        *,
        frame_bytes: bytes,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Send one frame; return the §5.1-shaped result. Raises
        ``KaicError`` (the SDK's ``KaiCError``) on any failure, after
        tearing the session down so the next call reconnects."""
        self._stream.open(correlation_id)
        result = self._stream.infer(frame_bytes)
        result["__session_correlation_id"] = self._stream.correlation_id
        return result

    def close(self) -> None:
        self._stream.close()


# ── Detector loop ──────────────────────────────────────────────────


class IntrusionDetector(FrameApp):
    """The main detector. Holds config + KAI-C client + dispatcher.

    ``step(camera)`` runs one cycle for one camera (the historical
    surface tests and ``--once`` drive); the SDK FrameApp base owns the
    daemon loop, which reaches the same rule through :meth:`on_frame`.
    """

    manifest = MANIFEST

    def __init__(
        self,
        config: AppConfig,
        kaic_client: KaicClient,
        dispatcher: AlertDispatcher,
        *,
        now: Callable[[], _dt.datetime] = _dt.datetime.now,
        stream_client_factory: Callable[[str], KaicStreamClient] | None = None,
    ) -> None:
        # Compat alias — pre-SDK code (and the tests) used ``_config``;
        # the SDK base spells it ``cfg``.
        self._config = config
        self._kaic = kaic_client
        self._now = now
        # Cache frame sources at init time so config errors surface
        # immediately, not on the first cycle.
        self._frame_sources: dict[str, FrameSource] = {}
        for camera in config.cameras:
            self._frame_sources[camera.camera_id] = build_frame_source(
                camera_id=camera.camera_id,
                url=camera.frame_url,
            )
        # WS mode: one persistent stream client per camera, built
        # lazily on first ``step``. ``stream_client_factory`` is an
        # injection point for tests; production builds the default
        # ``KaicStreamClient`` from config.
        self._stream_client_factory = stream_client_factory or self._default_stream_client_factory

        super().__init__(
            config,
            dispatcher,
            # By-reference bridge: swapping an entry in
            # ``self._frame_sources`` is picked up on the next tick.
            frame_source=DictFrameSource(self._frame_sources),
            cameras=[camera.camera_id for camera in config.cameras],
            poll_interval_seconds=config.poll_interval_seconds,
        )

    def setup(self) -> None:
        self._cameras_by_id: dict[str, CameraWatch] = {
            camera.camera_id: camera for camera in self.cfg.cameras
        }
        self._stream_clients: dict[str, KaicStreamClient] = {}
        # Live dashboard state (spec §03 /state). Bounded feed of the
        # most recent intrusions + a running count since start.
        self._recent: deque[dict[str, Any]] = deque(maxlen=25)
        self._intrusions = 0

    def _default_stream_client_factory(self, camera_id: str) -> KaicStreamClient:
        return KaicStreamClient(
            self._config.kaic_url,
            self._config.kaic_adapter_name,
            camera_id,
            api_key=self._config.kaic_api_key,
            timeout_seconds=self._config.request_timeout_seconds,
        )

    def close(self) -> None:
        """Tear down WS clients (no-op if HTTP mode). Called from the
        CLI's finally block so a clean shutdown returns sockets."""
        for client in self._stream_clients.values():
            client.close()
        self._stream_clients.clear()

    def _call_kaic(
        self,
        camera: CameraWatch,
        frame_bytes: bytes,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Send one frame to KAI-C via whichever transport this
        deployment configured. HTTP is one-shot per call; WS reuses
        a persistent connection per camera. Both raise ``KaicError``
        on transport failure so ``step()``'s catch handles them
        identically — same alert semantics across modes (no alert
        on comms failure; the failure is in KAI-C's audit log via
        the correlation_id we sent)."""
        if self._config.kaic_transport == "ws":
            client = self._stream_clients.get(camera.camera_id)
            if client is None:
                client = self._stream_client_factory(camera.camera_id)
                self._stream_clients[camera.camera_id] = client
            return client.infer_frame(
                frame_bytes=frame_bytes,
                correlation_id=correlation_id,
            )
        # Default: HTTP path (back-compat).
        return self._kaic.infer_frame(
            camera_id=camera.camera_id,
            frame_bytes=frame_bytes,
            correlation_id=correlation_id,
        )

    # ── The rule (one camera × one fetched frame) ──────────────────

    def on_frame(self, camera_id: str, frame_bytes: bytes) -> list[Alert]:
        """SDK FrameApp hook — the daemon loop's path to the rule. The
        base loop fetched the frame; gate on restricted hours, then run
        inference + zone matching. The base dispatches what we return."""
        if not self._config.restricted_hours.contains(self._now()):
            return []
        camera = self._cameras_by_id[camera_id]
        return self._detect_intrusions(camera, frame_bytes)

    def step(self, camera: CameraWatch) -> list[Alert]:
        """Run one detection cycle for one camera. Returns the list
        of alerts that were fired (mostly for testing — the dispatcher
        already sent them through every channel). Historical surface,
        kept for ``--once`` and the tests; dispatches its own alerts
        because it runs outside the base loop."""
        # Outside restricted hours → no inference, no alert.
        now = self._now()
        if not self._config.restricted_hours.contains(now):
            return []

        try:
            frame_bytes = self._frame_sources[camera.camera_id].fetch()
        except FrameSourceError as exc:
            logger.warning("frame fetch failed for %s: %s", camera.camera_id, exc)
            return []

        # Contract counters (spec §03): one fetched frame is one
        # "event", mirroring the base loop's bookkeeping.
        self._contract_note_event()
        fired = self._detect_intrusions(camera, frame_bytes)
        for alert in fired:
            self._dispatcher.fire(alert)
        self._contract_note_alerts(len(fired))
        return fired

    def _detect_intrusions(
        self, camera: CameraWatch, frame_bytes: bytes
    ) -> list[Alert]:
        """Inference + zone matching for one frame. Pure w.r.t. the
        dispatcher — callers (``step`` / the base loop) dispatch."""
        correlation_id = uuid.uuid4().hex
        try:
            infer_response = self._call_kaic(camera, frame_bytes, correlation_id)
        except KaicError as exc:
            logger.warning("kaic inference failed for %s: %s", camera.camera_id, exc)
            return []

        # WS mode: KAI-C audits at session grain (one correlation_id
        # per WS session, not per frame). Use whatever the stream
        # client reports as the session's effective correlation_id so
        # alerts join back to the right KAI-C audit row. HTTP mode is
        # per-call so the per-step ID and effective ID always match.
        # (Peer review H1.)
        if isinstance(infer_response, dict):
            effective_correlation_id = (
                infer_response.get("__session_correlation_id") or correlation_id
            )
            correlation_id = effective_correlation_id

        # Detection list lives at ``response.result.detections`` per
        # §5.1. Defensive parsing — adapters might return error
        # envelopes too, or (in pathological cases) non-dict bodies.
        if not isinstance(infer_response, dict):
            logger.warning(
                "kaic returned non-dict body for %s: %r", camera.camera_id, type(infer_response).__name__,
            )
            return []
        result = infer_response.get("result") or {}
        if not isinstance(result, dict) or result.get("status") == "error":
            logger.warning(
                "kaic returned error envelope for %s: %s",
                camera.camera_id,
                result.get("error", {}) if isinstance(result, dict) else result,
            )
            return []
        detections = result.get("detections") or []

        fired: list[Alert] = []
        for det in detections:
            label = str(det.get("label", "")).lower()
            if label not in self._config.watch_labels:
                continue
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                continue
            center = bbox_center(bbox, camera.frame_width, camera.frame_height)
            if not camera.zone.contains(center):
                continue
            fired.append(self._build_alert(camera, det, center, correlation_id))
            # Record for the live dashboard (§03 /state).
            camera_id = camera.camera_id
            zone_name = camera.zone.name
            self._intrusions += 1
            self._recent.append({
                "message": f"{label} entered {zone_name!r} on {camera_id}",
                "time": time.time(),
                "level": "high",
            })
        return fired

    def _build_alert(
        self,
        camera: CameraWatch,
        detection: dict[str, Any],
        center: Point,
        correlation_id: str,
    ) -> Alert:
        label = str(detection.get("label", "object"))
        confidence = float(detection.get("confidence", 0.0))
        return Alert(
            title=f"{label.capitalize()} in restricted zone {camera.zone.name!r}",
            description=(
                f"Detected {label} (confidence={confidence:.2f}) inside zone "
                f"{camera.zone.name!r} on camera {camera.camera_id} at "
                f"({center.x:.0f}, {center.y:.0f})."
            ),
            camera_id=camera.camera_id,
            severity="high",
            correlation_id=correlation_id,
            evidence={
                "detection": detection,
                "bbox_center_px": {"x": center.x, "y": center.y},
                "zone_name": camera.zone.name,
                "kaic_adapter": self._config.kaic_adapter_name,
            },
            tags=["intrusion", "restricted-zone", label],
        )

    # ── Live dashboard (spec §03 /state) ───────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        """Point-in-time view for the App Catalog's live dashboard.
        ``restricted_now`` reuses the same restricted-hours gate the
        rule uses, so the dashboard shows exactly whether the app is
        armed right now."""
        return {
            "restricted_now": self._config.restricted_hours.contains(self._now()),
            "intrusions": self._intrusions,
            "recent": list(self._recent),
        }


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intrusion-detection",
        description="Watch cameras for intrusions; alert via KAI-C audit + webhook.",
    )
    parser.add_argument("--config", required=True, help="Path to config.yml")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle per configured camera and exit (testing).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except (ValueError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    dispatcher = build_dispatcher(
        webhook_url=config.webhook_url,
        nats_alerts_url=config.nats_alerts_url,
        nats_alerts_token=config.nats_alerts_token,
        nats_alerts_subject_prefix=config.nats_alerts_subject_prefix,
    )
    kaic_client = KaicClient(
        config.kaic_url,
        config.kaic_adapter_name,
        api_key=config.kaic_api_key,
        timeout_seconds=config.request_timeout_seconds,
    )
    detector = IntrusionDetector(config, kaic_client, dispatcher)

    try:
        if args.once:
            for camera in config.cameras:
                detector.step(camera)
        else:
            # The SDK FrameApp loop is async; drive it the same way the
            # SDK AppRunner drives a Detector. SIGINT / SIGTERM trigger
            # a clean exit.
            loop = asyncio.new_event_loop()

            def _handle_signal(_signum, _frame):
                logger.info("signal received, stopping…")
                loop.call_soon_threadsafe(detector.stop)

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
            try:
                loop.run_until_complete(detector.run())
            finally:
                loop.close()
    finally:
        detector.close()   # WS clients (no-op in HTTP mode)
        kaic_client.close()
        # Drain in-flight NATS alert publishes (no-op for stdout +
        # webhook channels). Stays at the end of the finally clause
        # so it runs even if detector/kaic_client close raises.
        dispatcher.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Intrusion-detection example app.

Watches one or more cameras for persons/vehicles entering operator-
defined restricted zones during operator-defined restricted hours. On
detection, fires an alert via stdout (always) and an optional
webhook. Uses KAI-C's contract proxy (``POST /api/v1/infer/{adapter}``)
for inference — so every alert is correlation-id-traceable through
the audit log.

This is the first first-party example app per §12 of the AI Adapter
Contract design. Operators run it as a sidecar to OpenNVR; community
contributors copy it as a template for their own monitoring apps.

Run:
    python intrusion_detection.py --config config.yml          # daemon
    python intrusion_detection.py --config config.yml --once    # one cycle (testing)
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import logging
import signal
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from alerts import Alert, AlertDispatcher, build_dispatcher
from frame_sources import FrameSource, FrameSourceError, build_frame_source
from zone import Point, Zone, bbox_center

logger = logging.getLogger("intrusion-detection")


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
    request_timeout_seconds: float = 30.0


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
    cameras: list[CameraWatch] = []
    for idx, c in enumerate(cameras_raw):
        try:
            zone = Zone.from_config(
                name=str(c.get("zone_name", f"zone-{idx}")),
                vertices=c["zone"],
            )
            cameras.append(
                CameraWatch(
                    camera_id=str(c["camera_id"]),
                    frame_url=str(c["frame_url"]),
                    zone=zone,
                    frame_width=int(c.get("frame_width", 1920)),
                    frame_height=int(c.get("frame_height", 1080)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"config: camera entry {idx} malformed: {exc}"
            ) from exc

    return AppConfig(
        kaic_url=kaic_url,
        kaic_adapter_name=str(raw.get("kaic_adapter_name", "yolov8")),
        kaic_api_key=str(raw["kaic_api_key"]) if raw.get("kaic_api_key") else None,
        poll_interval_seconds=poll_interval,
        watch_labels=[str(s).lower() for s in raw.get("watch_labels", ["person"])],
        restricted_hours=rh,
        cameras=cameras,
        webhook_url=str(raw["webhook_url"]) if raw.get("webhook_url") else None,
        request_timeout_seconds=float(raw.get("request_timeout_seconds", 30.0)),
    )


# ── KAI-C client ───────────────────────────────────────────────────


class KaicClient:
    """Tiny client for KAI-C's ``POST /api/v1/infer/{adapter}``.

    We send the frame as base64 JSON (the convenience path) because
    multipart adds boilerplate without benefit at 1-fps polling.
    Threads ``X-Correlation-Id`` so every alert traces back through
    KAI-C's audit log and the adapter's logs alike.
    """

    def __init__(
        self,
        base_url: str,
        adapter_name: str,
        *,
        api_key: str | None,
        timeout_seconds: float,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url
        self._adapter_name = adapter_name
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_seconds, trust_env=False)

    def close(self) -> None:
        if self._owns_client and hasattr(self._client, "close"):
            self._client.close()

    def infer_frame(
        self,
        *,
        camera_id: str,
        frame_bytes: bytes,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Send a frame to KAI-C; return the raw InferResponse body.

        Raises ``KaicError`` on transport failure or non-200; the
        detector loop catches and decides whether to alert / skip /
        abort."""
        url = f"{self._base_url}/api/v1/infer/{self._adapter_name}"
        headers = {"X-Correlation-Id": correlation_id}
        if self._api_key:
            headers["X-Internal-Api-Key"] = self._api_key
        body = {
            "camera_id": camera_id,
            "frame_b64": base64.b64encode(frame_bytes).decode("ascii"),
        }
        try:
            response = self._client.post(url, json=body, headers=headers)
        except Exception as exc:
            raise KaicError(f"KAI-C unreachable at {url}: {exc}") from exc
        if response.status_code != 200:
            raise KaicError(
                f"KAI-C returned HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()


class KaicError(Exception):
    """Raised when KAI-C is unreachable or returns a non-200. The
    detector loop treats this as a transient skip — alerts don't fire
    on a comms failure (the failure itself is visible in KAI-C's
    audit log via the correlation_id we sent)."""


# ── Detector loop ──────────────────────────────────────────────────


class IntrusionDetector:
    """The main detector. Holds config + KAI-C client + dispatcher.

    ``step(camera)`` runs one cycle for one camera; ``run()`` schedules
    every camera every ``poll_interval_seconds`` until SIGINT/SIGTERM
    or a stop_flag is set.
    """

    def __init__(
        self,
        config: AppConfig,
        kaic_client: KaicClient,
        dispatcher: AlertDispatcher,
        *,
        now: Callable[[], _dt.datetime] = _dt.datetime.now,
    ) -> None:
        self._config = config
        self._kaic = kaic_client
        self._dispatcher = dispatcher
        self._now = now
        self._stop_flag = False
        # Cache frame sources at init time so config errors surface
        # immediately, not on the first cycle.
        self._frame_sources: dict[str, FrameSource] = {}
        for camera in config.cameras:
            self._frame_sources[camera.camera_id] = build_frame_source(
                camera_id=camera.camera_id,
                url=camera.frame_url,
            )

    def stop(self) -> None:
        self._stop_flag = True

    def step(self, camera: CameraWatch) -> list[Alert]:
        """Run one detection cycle for one camera. Returns the list
        of alerts that were fired (mostly for testing — the dispatcher
        already sent them through every channel)."""
        # Outside restricted hours → no inference, no alert.
        now = self._now()
        if not self._config.restricted_hours.contains(now):
            return []

        try:
            frame_bytes = self._frame_sources[camera.camera_id].fetch()
        except FrameSourceError as exc:
            logger.warning("frame fetch failed for %s: %s", camera.camera_id, exc)
            return []

        correlation_id = uuid.uuid4().hex
        try:
            infer_response = self._kaic.infer_frame(
                camera_id=camera.camera_id,
                frame_bytes=frame_bytes,
                correlation_id=correlation_id,
            )
        except KaicError as exc:
            logger.warning("kaic inference failed for %s: %s", camera.camera_id, exc)
            return []

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
            alert = self._build_alert(camera, det, center, correlation_id)
            self._dispatcher.fire(alert)
            fired.append(alert)
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

    def run(self) -> None:
        """Daemon loop. Polls every camera every
        ``poll_interval_seconds``. Returns when ``stop()`` is called
        (e.g. via SIGINT handler) or when the process is killed."""
        logger.info(
            "intrusion-detection started: %d cameras, poll=%.1fs, watch=%s, hours=%s-%s",
            len(self._config.cameras),
            self._config.poll_interval_seconds,
            self._config.watch_labels,
            self._config.restricted_hours.start.isoformat(),
            self._config.restricted_hours.end.isoformat(),
        )
        while not self._stop_flag:
            cycle_started = time.monotonic()
            for camera in self._config.cameras:
                if self._stop_flag:
                    break
                try:
                    self.step(camera)
                except Exception:
                    # No single camera failure should kill the loop.
                    logger.exception("step() raised for camera=%s", camera.camera_id)
            elapsed = time.monotonic() - cycle_started
            sleep_for = max(0.0, self._config.poll_interval_seconds - elapsed)
            # Sleep in short slices so SIGINT is responsive.
            slept = 0.0
            while slept < sleep_for and not self._stop_flag:
                chunk = min(0.25, sleep_for - slept)
                time.sleep(chunk)
                slept += chunk
        logger.info("intrusion-detection stopped")


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

    dispatcher = build_dispatcher(webhook_url=config.webhook_url)
    kaic_client = KaicClient(
        config.kaic_url,
        config.kaic_adapter_name,
        api_key=config.kaic_api_key,
        timeout_seconds=config.request_timeout_seconds,
    )
    detector = IntrusionDetector(config, kaic_client, dispatcher)

    # Wire SIGINT / SIGTERM to graceful shutdown.
    def _handle_signal(_signum, _frame):
        logger.info("signal received, stopping…")
        detector.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        if args.once:
            for camera in config.cameras:
                detector.step(camera)
        else:
            detector.run()
    finally:
        kaic_client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

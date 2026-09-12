# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Bridge Tier-0 tracks from the NATS bus to the in-process event bus.

Tier-0 (``detect-pipeline``) runs YOLO + ByteTrack on every camera and
publishes each frame's tracks to ``opennvr.inference.tier0.<cam>.completed``
(``opennvr.tier0.v1``). Until this consumer existed nothing carried those
tracks to the browser: the only NATS→bus bridge was the BYOM inference
path, so the live view had detections happening behind it and no way to
draw them. This is the missing hop — it is what puts bounding boxes on
the video.

It does three things and nothing else:

* maps the bus camera handle (``"cam3"``) to core's integer id, because
  the WebSocket entitlement check is a set of ints and an unmapped
  event would be dropped for every scoped user;
* normalizes the pixel-space ``[x1, y1, x2, y2]`` boxes into
  ``[x, y, w, h]`` in 0..1, using the frame size the producer ships —
  the overlay must not care what resolution the detector ran at;
* republishes on the in-process bus as ``event_type="tracks"``, which
  the WebSocket streams to entitled subscribers.

Best-effort like its siblings: no NATS URL, no nats-py, or a down
broker degrades to "no overlay", never to a crashed process. Payloads
that fail to parse are counted and dropped, not raised.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SUBJECT = "opennvr.inference.tier0.*.completed"

#: Reconnect cadence after a connect/subscribe failure — one warning a
#: minute for a down bus, not a hot loop.
_RETRY_SECONDS = 60.0

#: Below this the tracker is guessing; the overlay would just flicker.
DEFAULT_MIN_SCORE = 0.25

_dropped = 0


def normalize_box(box: Any, frame_w: Any, frame_h: Any) -> list[float] | None:
    """``[x1, y1, x2, y2]`` (pixels, or already 0..1) → ``[x, y, w, h]`` in
    0..1, clamped. ``None`` when it cannot be made sense of.

    Tier-0 boxes are pixels of the frame it ran on (bus.py ships
    ``frame.w/h`` precisely so consumers can do this). A box whose every
    coordinate is already ≤ 1 is taken as normalized — a producer that
    normalized upstream must not be squashed into the corner by dividing
    again.
    """
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if not all(v == v for v in (x1, y1, x2, y2)):   # NaN
        return None
    if max(x1, y1, x2, y2) > 1.0:
        try:
            fw, fh = float(frame_w), float(frame_h)
        except (TypeError, ValueError):
            return None
        if fw <= 0 or fh <= 0:
            return None
        x1, x2 = x1 / fw, x2 / fw
        y1, y2 = y1 / fh, y2 / fh
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    clamp = lambda v: min(1.0, max(0.0, v))  # noqa: E731
    x1, y1, x2, y2 = clamp(x1), clamp(y1), clamp(x2), clamp(y2)
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return None
    return [round(x1, 4), round(y1, 4), round(w, 4), round(h, 4)]


def to_overlay_payload(
    raw: dict[str, Any], *, min_score: float = DEFAULT_MIN_SCORE
) -> dict[str, Any] | None:
    """The browser-facing payload for one Tier-0 frame, or ``None`` when
    nothing drawable survives. Pure, so it is testable without a bus."""
    frame = raw.get("frame") or {}
    fw, fh = frame.get("w"), frame.get("h")
    tracks_out: list[dict[str, Any]] = []
    for t in raw.get("tracks") or []:
        if not isinstance(t, dict):
            continue
        try:
            score = float(t.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        if score < min_score:
            continue
        box = normalize_box(t.get("box"), fw, fh)
        if box is None:
            continue
        tracks_out.append({
            "id": t.get("id"),
            "label": str(t.get("label") or "object"),
            "score": round(score, 3),
            "box": box,
            "stationary": bool(t.get("stationary", False)),
        })
    if not tracks_out:
        return None
    return {
        "schema": "opennvr.overlay.tracks.v1",
        "seq": raw.get("seq"),
        "wall_ts": raw.get("wall_ts"),
        "calibrating": bool(raw.get("calibrating", False)),
        "frame": {"w": fw, "h": fh},
        "tracks": tracks_out,
    }


async def _handle_message(msg) -> None:
    global _dropped
    try:
        raw = json.loads(msg.data)
        if not isinstance(raw, dict):
            raise ValueError("payload is not an object")
        from services.camera_scope import camera_id_from_handle

        camera_id = camera_id_from_handle(raw.get("camera_id"))
        if camera_id is None:
            # Also try the subject — opennvr.inference.tier0.<cam>.completed
            parts = str(getattr(msg, "subject", "")).split(".")
            camera_id = camera_id_from_handle(parts[3]) if len(parts) >= 5 else None
        if camera_id is None:
            raise ValueError(f"unmappable camera_id {raw.get('camera_id')!r}")
        payload = to_overlay_payload(raw)
        if payload is None:
            return   # nothing drawable this frame; not an error
        from services.event_bus_service import publish_tracks

        await publish_tracks(camera_id=camera_id, payload=payload)
    except Exception as exc:  # noqa: BLE001
        _dropped += 1
        if _dropped in (1, 10, 100) or _dropped % 1000 == 0:
            logger.warning(
                "tier0 track consumer: dropped payload #%d (%s)", _dropped, exc)


async def run_consumer_loop() -> None:
    """Subscribe to Tier-0 completions for the process lifetime. Returns
    immediately when no NATS URL is configured; retries slowly otherwise."""
    from core.config import settings

    url = (getattr(settings, "nats_url", "") or "").strip()
    if not url:
        logger.info("tier0 track consumer disabled (no NATS_URL) — "
                    "no live detection overlay")
        return
    try:
        import nats
    except ImportError:
        logger.warning("tier0 track consumer disabled: nats-py not installed")
        return

    token = (getattr(settings, "internal_api_key", "") or "").strip() or None

    while True:
        client = sub = None
        try:
            client = await nats.connect(url, connect_timeout=5, token=token)
            sub = await client.subscribe(SUBJECT, cb=_handle_message)
            logger.info("tier0 track consumer subscribed to %s", SUBJECT)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await _teardown(sub, client)
            raise
        except Exception as exc:  # noqa: BLE001
            await _teardown(sub, client)
            logger.warning(
                "tier0 track consumer: connect/subscribe failed (%s); "
                "retrying in %.0fs", exc, _RETRY_SECONDS)
            await asyncio.sleep(_RETRY_SECONDS)


async def _teardown(sub, client) -> None:
    try:
        if sub is not None:
            await sub.unsubscribe()
    except Exception:  # noqa: BLE001
        pass
    try:
        if client is not None:
            await client.drain()
    except Exception:  # noqa: BLE001
        pass

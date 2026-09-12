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
#: Apps publish their own boxes here (SDK OverlayBoxes / overlay.boxes.v1):
#: ANPR's plate localisations, occupancy's zones. Same wire shape out —
#: the browser draws one kind of thing — but forwarded ONLY for apps the
#: operator switched on in the catalog (installed_apps.overlay_enabled).
APP_SUBJECT = "opennvr.events.overlay.boxes.v1.>"
#: How long a "may this app draw?" answer is trusted before the DB is
#: asked again. Overlay frames arrive many times a second; the toggle
#: changes a few times a year.
_APP_ALLOW_TTL_S = 15.0
_app_allow_cache: dict[str, tuple[float, bool]] = {}

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
        # COASTING tracks are not drawn. The tracker keeps an unmatched
        # track alive for up to coast_ttl_seconds (five minutes by
        # default) at its last box — right for visit continuity and
        # best-frame retention, wrong for a live overlay, where it reads
        # as a phantom sitting on the sky while the real vehicle goes
        # unboxed. Tier-0 marks each track `matched` for the frame it was
        # actually detected in; absent (an older producer) is taken as
        # matched so a bus without the field keeps drawing rather than
        # going dark.
        if t.get("matched") is False:
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


def _app_may_draw(app_id: str, now: float | None = None) -> bool:
    """installed_apps.overlay_enabled for ``app_id``, cached briefly. A
    missing row, a DB error, or a blank id all answer False — an app
    draws only when the operator demonstrably said so."""
    import time as _time

    key = str(app_id or "").strip().lower()
    if not key:
        return False
    now = _time.monotonic() if now is None else now
    hit = _app_allow_cache.get(key)
    if hit and now - hit[0] < _APP_ALLOW_TTL_S:
        return hit[1]
    allowed = False
    try:
        from core.database import SessionLocal
        from models import InstalledApp

        db = SessionLocal()
        try:
            row = db.query(InstalledApp.overlay_enabled, InstalledApp.enabled).filter(
                InstalledApp.id == key).first()
            allowed = bool(row and row[0] and row[1])
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.debug("overlay: could not read overlay_enabled for %s", key,
                     exc_info=True)
        allowed = False
    _app_allow_cache[key] = (now, allowed)
    return allowed


def _invalidate_app_allow_cache() -> None:
    _app_allow_cache.clear()


def _xywh_to_xyxy(box: Any) -> list[float] | None:
    """``[x, y, w, h]`` → ``[x1, y1, x2, y2]`` in the same units; None if
    it is not four numbers. Width/height ≤ 0 stays and is rejected by
    normalize_box as zero-area."""
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    return [x, y, x + w, y + h]


async def _handle_app_message(msg) -> None:
    """An app's overlay.boxes.v1 envelope → the same `tracks` event the
    Tier-0 path emits, tagged with its source, if the operator allowed
    that app to draw. Boxes arrive already normalized per the contract;
    normalize_box still runs so a pixel-space slip is caught, not drawn."""
    global _dropped
    try:
        env = json.loads(msg.data)
        if not isinstance(env, dict):
            raise ValueError("envelope is not an object")
        payload = env.get("payload") if isinstance(env.get("payload"), dict) else env
        app_id = str(env.get("producer") or payload.get("app_id") or "").strip()
        # Producer id may arrive as "app:<id>" from the SDK envelope.
        if app_id.startswith("app:"):
            app_id = app_id[4:]
        if not _app_may_draw(app_id):
            return
        from services.camera_scope import camera_id_from_handle

        camera_id = camera_id_from_handle(env.get("camera_id") or payload.get("camera_id"))
        if camera_id is None:
            parts = str(getattr(msg, "subject", "")).split(".")
            camera_id = camera_id_from_handle(parts[-1]) if parts else None
        if camera_id is None:
            raise ValueError("unmappable camera_id")
        raw = {"frame": payload.get("frame") or {},
               "calibrating": False,
               "seq": payload.get("seq"), "wall_ts": env.get("ts") or payload.get("wall_ts"),
               # The app contract is [x, y, w, h]; Tier-0's is [x1, y1, x2, y2]
               # and normalize_box speaks the latter. Convert here — feeding
               # xywh straight in silently produced boxes of the wrong size.
               "tracks": [{"id": b.get("id"), "label": b.get("label"),
                           "score": b.get("score", 1.0),
                           "box": _xywh_to_xyxy(b.get("box"))}
                          for b in (payload.get("boxes") or []) if isinstance(b, dict)]}
        out = to_overlay_payload(raw, min_score=0.0)
        if out is None:
            return
        out["source"] = f"app:{app_id}"
        from services.event_bus_service import publish_tracks

        await publish_tracks(camera_id=camera_id, payload=out, task="overlay")
    except Exception as exc:  # noqa: BLE001
        _dropped += 1
        if _dropped in (1, 10, 100) or _dropped % 1000 == 0:
            logger.warning("overlay consumer: dropped app payload #%d (%s)", _dropped, exc)


async def run_consumer_loop() -> None:
    """Subscribe to Tier-0 completions for the process lifetime. Returns
    immediately when no NATS URL is configured; retries slowly otherwise."""
    from core.config import settings

    if not bool(getattr(settings, "detection_overlay_enabled", True)):
        logger.info("detection overlay disabled by DETECTION_OVERLAY_ENABLED "
                    "— no track data will reach any consumer")
        return
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
        client = sub = app_sub = None
        try:
            client = await nats.connect(url, connect_timeout=5, token=token)
            sub = await client.subscribe(SUBJECT, cb=_handle_message)
            app_sub = await client.subscribe(APP_SUBJECT, cb=_handle_app_message)
            logger.info("tier0 track consumer subscribed to %s and %s",
                        SUBJECT, APP_SUBJECT)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await _teardown(app_sub, None)
            await _teardown(sub, client)
            raise
        except Exception as exc:  # noqa: BLE001
            await _teardown(app_sub, None)
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

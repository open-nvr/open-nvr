# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
Tier-0 consumption helpers — answer from the always-on detector, and reuse its
best frame, instead of re-running inference (App SDK).

OpenNVR's Tier-0 detect-pipeline runs cheap detection full-time and publishes,
per frame, the current tracks to ``opennvr.inference.tier0.<camera>.completed``.
For a large class of questions an app does **not** need to fire its own expensive
model:

* **Counts / presence** — "how many cars?", "is anyone at the door?" — are already
  in the latest Tier-0 event's ``tracks``. Read them; run nothing.
* **Appearance** — "what colour is the car?" — still needs a vision model, but it
  should run on Tier-0's **best frame** (the sharpest / largest / most-confident
  frame it already selected for that track), not an arbitrary live grab. Tier-0
  retains that crop and serves it at ``<pipeline>/best_frame?camera=&track=``; each
  track in the event carries a ``best`` flag when one is available.

This module gives every app those two primitives so they aren't re-implemented per
app:

* :func:`snapshot_from_event` / :class:`Tier0Snapshot` — parse a Tier-0 event into
  counts / presence / best-availability.
* :class:`BestFrameClient` / :func:`make_best_frame_fetch` — fetch the best frame.

Both are **opt-in**: an app uses them where they fit its semantics (a liveness app
still wants a current frame; a scene app still wants the full frame; an audio app
never touches frames at all). Nothing here is mandatory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

TIER0_ADAPTER = "tier0"
TIER0_SUBJECT_PREFIX = "opennvr.inference.tier0."

# Irregular plurals worth getting right for the COCO labels detectors emit most;
# everything else takes a trailing 's'.
_IRREGULAR_PLURALS = {"person": "people", "man": "men", "woman": "women"}


def is_tier0_subject(subject: str) -> bool:
    """True if a NATS subject is a Tier-0 inference event."""
    return subject.startswith(TIER0_SUBJECT_PREFIX)


@dataclass
class Tier0Snapshot:
    """The latest Tier-0 result for one camera, reduced to what apps ask of it.

    Built from a Tier-0 event payload (``snapshot_from_event``). Defensive by
    design — the payload is whatever JSON arrived on the bus, so missing fields
    degrade to empty rather than raising.
    """

    camera_id: str = ""
    tracks: list[dict[str, Any]] = field(default_factory=list)
    ts: float | None = None
    seq: int | None = None

    @property
    def counts(self) -> dict[str, int]:
        """Object count per label, e.g. ``{"person": 1, "car": 2}``."""
        out: dict[str, int] = {}
        for t in self.tracks:
            label = str(t.get("label") or "").strip()
            if label:
                out[label] = out.get(label, 0) + 1
        return out

    def count(self, label: str) -> int:
        return self.counts.get(label, 0)

    def present(self, label: str) -> bool:
        return self.count(label) > 0

    @property
    def total(self) -> int:
        return len(self.tracks)

    def has_best(self, track_id: Any) -> bool:
        """Whether a fetchable best frame is advertised for a given track id."""
        for t in self.tracks:
            if t.get("id") == track_id:
                return bool(t.get("best"))
        return False

    def tracks_with_best(self) -> list[dict[str, Any]]:
        return [t for t in self.tracks if t.get("best")]

    def describe(self, *, limit: int = 8) -> str:
        """A short, human/speakable phrase, e.g. ``"a person, 2 cars"``."""
        return describe_counts(self.counts, limit=limit)


def snapshot_from_event(payload: dict[str, Any]) -> Tier0Snapshot:
    """Parse a Tier-0 event payload (``opennvr.tier0.v1``) into a snapshot."""
    payload = payload or {}
    # Keep only dict tracks — the payload is whatever JSON arrived on the bus, so a
    # junk `tracks` (a string, or a list of ints) must degrade, not raise.
    tracks = [t for t in (payload.get("tracks") or []) if isinstance(t, dict)]
    return Tier0Snapshot(
        camera_id=str(payload.get("camera_id") or ""),
        tracks=tracks,
        ts=payload.get("ts"),
        seq=payload.get("seq"),
    )


def describe_counts(counts: dict[str, int], *,
                    irregular_plurals: dict[str, str] | None = None,
                    limit: int = 8) -> str:
    """Turn a label→count map into a short phrase: ``"a person, 2 cars"``."""
    plurals = irregular_plurals or _IRREGULAR_PLURALS
    parts: list[str] = []
    for label, count in sorted(counts.items()):
        if count <= 0 or not label:
            continue
        if count == 1:
            article = "an" if label[:1].lower() in "aeiou" else "a"
            parts.append(f"{article} {label}")
        else:
            parts.append(f"{count} {plurals.get(label, f'{label}s')}")
    return ", ".join(parts[:limit])


# ── best-frame client ──────────────────────────────────────────────

# async callable(url) -> (status_code, body_bytes)
HttpGet = Callable[[str], Awaitable[tuple[int, bytes]]]
# agent-camera-id -> pipeline-camera-id (the id on the Tier-0 bus subject)
ResolveCamera = Callable[[str], str]


async def _default_http_get(url: str) -> tuple[int, bytes]:  # pragma: no cover - network
    import httpx
    async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
        resp = await client.get(url)
        return resp.status_code, resp.content


class BestFrameClient:
    """Fetch Tier-0's best frame from the detect-pipeline ``/best_frame`` endpoint.

    ``base_url`` is the pipeline metrics origin (e.g. ``http://tier0:9109``).
    ``resolve_camera`` optionally maps an app's camera id → the pipeline's camera
    id (identity by default). ``http_get`` is injectable for tests.
    """

    def __init__(self, base_url: str, *, resolve_camera: ResolveCamera | None = None,
                 http_get: HttpGet | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._resolve = resolve_camera
        self._get = http_get or _default_http_get

    async def fetch(self, camera_id: str, track_id: Any = None) -> bytes | None:
        """Best frame as JPEG bytes, or None. A specific ``track_id`` fetches that
        track's best; omitting it fetches the camera's most-recent best."""
        cam = self._resolve(camera_id) if self._resolve else camera_id
        if not cam:
            return None
        url = f"{self._base}/best_frame?camera={cam}"
        if track_id is not None:
            url += f"&track={track_id}"
        status, body = await self._get(url)
        return body if status == 200 and body else None


def make_best_frame_fetch(base_url: str, *, resolve_camera: ResolveCamera | None = None,
                          http_get: HttpGet | None = None):
    """Convenience: a bound ``async fetch(camera_id) -> bytes | None`` — the shape
    a consumer (e.g. the camera-agent's describe path) plugs in directly."""
    client = BestFrameClient(base_url, resolve_camera=resolve_camera, http_get=http_get)

    async def fetch(camera_id: str) -> bytes | None:
        return await client.fetch(camera_id)

    return fetch


def tier0_to_detections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Bridge a Tier-0 event's ``tracks`` into contract-shaped detections.

    Lets any :class:`DetectorApp` consume the always-on Tier-0 stream with the
    same ``on_detections`` code it already runs on adapter events: each track
    becomes ``{label, score, bbox, track_id, stationary, best}`` where ``bbox``
    is the contract's NormalizedBBox (x/y/w/h in 0-1), computed from the
    track's pixel box and the event's ``frame`` size.

    Defensive: a malformed track is skipped; if the event carries no frame
    size (older detect-pipeline), ``bbox`` is omitted so bbox-free consumers
    (counting, presence) still work while zone tests skip the track.
    """
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        return []
    frame = payload.get("frame") or {}
    fw, fh = frame.get("w"), frame.get("h")
    have_dims = (
        isinstance(fw, (int, float)) and fw > 0
        and isinstance(fh, (int, float)) and fh > 0
    )
    out: list[dict[str, Any]] = []
    for t in tracks:
        if not isinstance(t, dict) or not t.get("label"):
            continue
        det: dict[str, Any] = {
            "label": str(t["label"]),
            "score": t.get("score"),
            "track_id": t.get("id"),
            "stationary": t.get("stationary"),
            "best": t.get("best"),
        }
        box = t.get("box")
        if have_dims and isinstance(box, (list, tuple)) and len(box) == 4:
            try:
                x1, y1, x2, y2 = (float(v) for v in box)
                det["bbox"] = {
                    "x": max(0.0, x1 / fw),
                    "y": max(0.0, y1 / fh),
                    "w": max(0.0, (x2 - x1) / fw),
                    "h": max(0.0, (y2 - y1) / fh),
                }
            except (TypeError, ValueError):
                pass
        out.append(det)
    return out

# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Best-frame store — expose Tier-0's per-track best frame for on-demand use.

Tier-0 already selects, per track, the best frame it has seen (largest / sharpest /
highest-confidence) and retains the crop pixels (`Track.best_crop`, added for the
gate's Tier-1 dispatch). That best frame is *exactly* what a consumer (the
camera-agent's VLM path) should describe — a clean, representative frame — instead
of grabbing an arbitrary live frame that may be blurred, mid-turn, or occluded.

Until now that crop lived only in the worker's memory, reachable only by the
automatic gate→dispatch path. This store makes it fetchable **on demand**, keyed by
``(camera_id, track_id)``, so an app can pull "the best frame Tier-0 has for the
person on cam3" and run one expensive inference on the right frame — more accurate
and cheaper than re-grabbing and re-inferring on a random frame.

Design:
- The hot path (per frame, per track) only stores a **reference** to the crop
  array — no encode. JPEG encoding happens **lazily on fetch** and is cached, so a
  best frame that's never requested costs nothing beyond a dict entry.
- Bounded by entry count (LRU by last-touch) and max age, so a 24/7 feed can't grow
  it without limit.
- Thread-safe: the store is shared across camera worker threads.
"""
from __future__ import annotations

import threading
import time


def _encode_jpeg(crop_bgr, quality: int = 85) -> bytes:
    import cv2
    ok, buf = cv2.imencode(".jpg", crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise ValueError("jpeg encode failed")
    return buf.tobytes()


class BestFrameStore:
    """Thread-safe, bounded cache of each track's best-frame crop (encoded lazily)."""

    def __init__(self, *, max_entries: int = 256, max_age_s: float = 120.0,
                 jpeg_quality: int = 85, _clock=time.monotonic, _encode=_encode_jpeg) -> None:
        self._max_entries = max_entries
        self._max_age_s = max_age_s
        self._quality = jpeg_quality
        self._clock = _clock
        self._encode = _encode
        self._lock = threading.Lock()
        # key -> {crop, ts, jpeg(None until encoded)}
        self._d: dict[tuple[str, str], dict] = {}

    def put(self, camera_id: str, track_id, crop_bgr, ts: float | None = None) -> None:
        """Record a track's current best crop. Cheap: stores a reference, no encode.

        A crop that is the *same array* as the stored one only refreshes recency (so
        we don't re-encode an unchanged best frame); a new crop resets the cache.
        """
        if crop_bgr is None:
            return
        key = (camera_id, str(track_id))
        now = self._clock() if ts is None else ts
        with self._lock:
            e = self._d.get(key)
            if e is not None and e["crop"] is crop_bgr:
                e["ts"] = now                     # unchanged best → just touch recency
            else:
                self._d[key] = {"crop": crop_bgr, "ts": now, "jpeg": None}
            self._evict_locked()

    def get_jpeg(self, camera_id: str, track_id) -> bytes | None:
        """Best frame for one track as JPEG bytes, or None. Encodes once, caches."""
        key = (camera_id, str(track_id))
        with self._lock:
            e = self._d.get(key)
            if e is None or self._expired_locked(e):
                self._d.pop(key, None)
                return None
            if e["jpeg"] is None:
                e["jpeg"] = self._encode(e["crop"], self._quality)
            e["ts"] = self._clock()               # fetch counts as a touch
            return e["jpeg"]

    def latest_jpeg(self, camera_id: str) -> bytes | None:
        """Best frame for the camera's most-recently-updated track (no track id)."""
        with self._lock:
            best_key = None
            best_ts = -1.0
            for key, e in self._d.items():
                if key[0] == camera_id and not self._expired_locked(e) and e["ts"] > best_ts:
                    best_key, best_ts = key, e["ts"]
            if best_key is None:
                return None
            e = self._d[best_key]
            if e["jpeg"] is None:
                e["jpeg"] = self._encode(e["crop"], self._quality)
            return e["jpeg"]

    def _expired_locked(self, e: dict) -> bool:
        return (self._clock() - e["ts"]) > self._max_age_s

    def _evict_locked(self) -> None:
        # drop expired first, then oldest-touch while over the count cap
        for key in [k for k, e in self._d.items() if self._expired_locked(e)]:
            self._d.pop(key, None)
        if len(self._d) > self._max_entries:
            for key, _ in sorted(self._d.items(), key=lambda kv: kv[1]["ts"])[
                : len(self._d) - self._max_entries
            ]:
                self._d.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)

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
from collections import OrderedDict


def _encode_jpeg(crop_bgr, quality: int = 85) -> bytes:
    import cv2
    ok, buf = cv2.imencode(".jpg", crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise ValueError("jpeg encode failed")
    return buf.tobytes()


class BestFrameStore:
    """Thread-safe, bounded cache of each track's best-frame crop (encoded lazily)."""

    def __init__(self, *, max_entries: int = 0, max_age_s: float = 120.0,
                 max_per_camera: int = 16, jpeg_quality: int = 85,
                 _clock=time.monotonic, _encode=_encode_jpeg) -> None:
        # 0 = derive from the per-camera quota rather than a fixed number.
        # A hard-coded 256 silently overruled DETECT_BESTFRAME_PER_CAMERA
        # past ~16 cameras: 20 x 16 = 320 > 256, so from camera 17 onward
        # every put evicted from someone's bucket and the quota an
        # operator configured was simply unreachable.
        self._max_entries = max_entries if max_entries > 0 else 0
        # Per-camera quota. Without one, the count cap was global and a busy
        # camera simply evicted the quiet ones: at DETECT_MAX_TRACKS=50 the
        # 256-entry cap saturates at ~6 busy cameras, and because recency is
        # driven by PUT RATE the high-fps camera always won the sort — so
        # "best frame for the quiet camera" returned nothing on a fleet.
        self._max_per_camera = max(1, max_per_camera)
        self._max_age_s = max_age_s
        self._quality = jpeg_quality
        self._clock = _clock
        self._encode = _encode
        self._lock = threading.Lock()
        # camera_id -> OrderedDict[track_id, {crop, ts, jpeg}], ordered
        # oldest-touch first. An OrderedDict makes both the LRU eviction and
        # "newest track for this camera" O(1); the previous flat dict re-sorted
        # EVERY entry on EVERY put, under this lock, on the frame hot path.
        self._cams: dict[str, "OrderedDict[str, dict]"] = {}

    def put(self, camera_id: str, track_id, crop_bgr, ts: float | None = None) -> None:
        """Record a track's current best crop. Cheap: stores a reference, no encode.

        A crop that is the *same array* as the stored one only refreshes recency (so
        we don't re-encode an unchanged best frame); a new crop resets the cache.
        Recency/expiry always use the store's own clock — never the caller's frame
        ``ts`` (whose epoch may differ), so expiry can't misfire. ``ts`` is accepted
        for API symmetry but not used for expiry.
        """
        if crop_bgr is None:
            return
        tid = str(track_id)
        now = self._clock()
        with self._lock:
            bucket = self._cams.get(camera_id)
            if bucket is None:
                bucket = self._cams[camera_id] = OrderedDict()
            e = bucket.get(tid)
            if e is not None and e["crop"] is crop_bgr:
                e["ts"] = now                     # unchanged best → just touch recency
            else:
                bucket[tid] = {"crop": crop_bgr, "ts": now, "jpeg": None}
            bucket.move_to_end(tid)               # newest last
            self._evict_locked(camera_id, bucket)

    def _encode_cached(self, camera_id: str, tid: str) -> bytes | None:
        """Return the cached JPEG for a track, encoding it if needed. Encoding
        runs OUTSIDE the lock (cv2 can be slow) so it never blocks ``put``s."""
        with self._lock:
            bucket = self._cams.get(camera_id)
            e = bucket.get(tid) if bucket is not None else None
            if e is None or self._expired_locked(e):
                if bucket is not None:
                    bucket.pop(tid, None)
                    if not bucket:
                        self._cams.pop(camera_id, None)
                return None
            if e["jpeg"] is not None:
                e["ts"] = self._clock()
                bucket.move_to_end(tid)
                return e["jpeg"]
            crop = e["crop"]                      # snapshot the ref; encode unlocked
        jpeg = self._encode(crop, self._quality)
        with self._lock:
            bucket = self._cams.get(camera_id)
            e = bucket.get(tid) if bucket is not None else None
            if e is not None and e["crop"] is crop:
                e["jpeg"] = jpeg
                e["ts"] = self._clock()
                bucket.move_to_end(tid)
        return jpeg

    def get_jpeg(self, camera_id: str, track_id) -> bytes | None:
        """Best frame for one track as JPEG bytes, or None. Encodes once, caches."""
        return self._encode_cached(camera_id, str(track_id))

    def latest_jpeg(self, camera_id: str) -> bytes | None:
        """Best frame for the camera's most-recently-updated track (no track id).

        O(1) in the bucket: the newest live track is simply its last key. This
        used to scan EVERY entry of EVERY camera under the lock.
        """
        with self._lock:
            bucket = self._cams.get(camera_id)
            if not bucket:
                return None
            newest = None
            for tid in reversed(bucket):          # newest first; stop at the first live one
                if not self._expired_locked(bucket[tid]):
                    newest = tid
                    break
        return self._encode_cached(camera_id, newest) if newest is not None else None

    def _expired_locked(self, e: dict) -> bool:
        return (self._clock() - e["ts"]) > self._max_age_s

    def _evict_locked(self, camera_id: str, bucket) -> None:
        """Bound this camera's bucket, then the store as a whole.

        Amortised O(1): the bucket is ordered oldest-touch first, so expired
        entries are a prefix — pop until the first live one and stop. The old
        implementation re-scanned every entry in the store (calling the clock
        per entry) and re-sorted them on every single put.
        """
        while bucket:
            tid = next(iter(bucket))
            if not self._expired_locked(bucket[tid]):
                break
            bucket.pop(tid, None)
        while len(bucket) > self._max_per_camera:
            bucket.popitem(last=False)            # drop this camera's oldest
        if not bucket:
            self._cams.pop(camera_id, None)
            return
        # Global backstop for memory. Take from the LARGEST bucket rather than
        # the globally-oldest entry, so a busy camera cannot evict a quiet
        # camera's only best frame.
        if self._max_entries <= 0:      # per-camera quota is the only bound
            return
        while self._count_locked() > self._max_entries:
            biggest = max(self._cams.values(), key=len, default=None)
            if not biggest:
                break
            biggest.popitem(last=False)
            for cam in [c for c, b in self._cams.items() if not b]:
                self._cams.pop(cam, None)

    def _count_locked(self) -> int:
        return sum(len(b) for b in self._cams.values())

    def __len__(self) -> int:
        with self._lock:
            return self._count_locked()

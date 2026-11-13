# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keeping the same person the same person, frame to frame.

The pose adapter DETECTS: it answers "where are the people in this
picture", and nothing more. It has no idea that the person on the left
of this frame is the person who was on the left of the last one.

Everything the screening logic does is about a person over time — this
customer has had their left arm scanned, that guard has been reaching
for the last thirty seconds — so without stable identity it all falls
apart, and it falls apart QUIETLY. Detections come back ordered by
confidence, which flips between frames, so numbering them 1, 2, 3 as
they arrive makes the guard and the customer swap identities several
times a second. The guard election oscillates, sessions split, and the
briefest step in the procedure — the pass down the back — is the first
thing to vanish. The verdict is then "incomplete" for a scan that was
performed perfectly.

This is the piece the prototype got from the pose model itself
(``model.track(persist=True)``, ByteTrack underneath). The adapter
contract has no tracking in it, so the app keeps its own — deliberately
small: overlap first, then nearest-centre, both measured against the
person's own size so it behaves the same at any distance or resolution.
"""
from __future__ import annotations

import math

#: How much two boxes must overlap to be the same person. Low, because
#: at 10 fps somebody walking briskly moves a long way between frames.
DEFAULT_IOU_MIN = 0.25
#: Fallback match distance, in multiples of the box's own width — for
#: the frames where overlap fails but "it is obviously them, half a
#: step on" is still true.
DEFAULT_MOVE_SCALE = 1.2
#: How long a person can be missing before their id is retired. A pose
#: model loses people behind each other constantly; retiring instantly
#: would mint a new id every time somebody passes in front of somebody.
DEFAULT_MAX_GAP_S = 1.5


def iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0.0:
        return 0.0
    union = ((a[2] - a[0]) * (a[3] - a[1])
             + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter / union if union > 0 else 0.0


def _centre(box):
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


class Track:
    __slots__ = ("id", "box", "last_seen", "hits")

    def __init__(self, track_id: int, box, now: float) -> None:
        self.id = track_id
        self.box = box
        self.last_seen = now
        self.hits = 1


class Tracker:
    """Detections in, stable ids out.

    Greedy and deterministic: the strongest overlap wins first, so one
    good match cannot be stolen by a worse one later in the list. Ids
    are never reused, because a recycled id would silently graft one
    person's screening onto another's.
    """

    def __init__(self, *, iou_min: float = DEFAULT_IOU_MIN,
                 move_scale: float = DEFAULT_MOVE_SCALE,
                 max_gap_s: float = DEFAULT_MAX_GAP_S) -> None:
        self.iou_min = iou_min
        self.move_scale = move_scale
        self.max_gap_s = max_gap_s
        self._tracks: list[Track] = []
        self._next_id = 1

    @property
    def live(self) -> int:
        return len(self._tracks)

    def update(self, boxes, now: float) -> list[int]:
        """Ids for ``boxes``, in the same order they were given."""
        self._retire(now)
        assigned: dict[int, int] = {}          # detection index -> track id
        taken: set[int] = set()

        # Pass one: overlap, best first.
        pairs = []
        for di, box in enumerate(boxes):
            for track in self._tracks:
                score = iou(box, track.box)
                if score >= self.iou_min:
                    pairs.append((score, di, track.id))
        for _, di, tid in sorted(pairs, key=lambda p: -p[0]):
            if di in assigned or tid in taken:
                continue
            assigned[di] = tid
            taken.add(tid)

        # Pass two: nearest centre, for the person who moved too far to
        # overlap. Measured against their own width so it means the same
        # thing close to the camera and far from it.
        for di, box in enumerate(boxes):
            if di in assigned:
                continue
            cx, cy = _centre(box)
            width = max(box[2] - box[0], 1.0)
            best, best_dist = None, None
            for track in self._tracks:
                if track.id in taken:
                    continue
                tx, ty = _centre(track.box)
                dist = math.hypot(cx - tx, cy - ty)
                if dist <= width * self.move_scale and (best_dist is None
                                                        or dist < best_dist):
                    best, best_dist = track, dist
            if best is not None:
                assigned[di] = best.id
                taken.add(best.id)

        out: list[int] = []
        by_id = {t.id: t for t in self._tracks}
        for di, box in enumerate(boxes):
            tid = assigned.get(di)
            if tid is None:
                tid = self._next_id
                self._next_id += 1
                self._tracks.append(Track(tid, box, now))
            else:
                track = by_id[tid]
                track.box = box
                track.last_seen = now
                track.hits += 1
            out.append(tid)
        return out

    def _retire(self, now: float) -> None:
        self._tracks = [t for t in self._tracks
                        if now - t.last_seen <= self.max_gap_s]

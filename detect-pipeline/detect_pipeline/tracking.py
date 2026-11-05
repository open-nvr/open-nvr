# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Object tracking for the Tier-0 pipeline.

Design intent (see docs/design/compute-gated-inference.md) and the Frigate source
review (frigate/track/norfair_tracker.py, MIT 6f80bcd19) call out one load-bearing
property: matching uses a **size-aware bottom-center distance**, NOT centroid-IoU,
because centroid-IoU swaps IDs between nearby same-class objects. This module
implements that metric with greedy nearest-match assignment plus the track
lifecycle (initialization delay, max-disappeared) and a ``motionless_count`` so a
later stage (PR B) can treat settled objects as stationary and skip re-detecting
them.

Matching (2026-11-05, replayed on the Ventuno Q): each track carries a
constant-velocity Kalman filter; every frame it PREDICTS where its box will
be, and detections are assigned to tracks by overlap with that prediction,
globally (Hungarian), in two stages the way ByteTrack does it — confident
boxes first, against every track; then the low-score boxes the detector is
unsure of, against the tracks still unmatched that were seen last frame,
so a person half hidden behind a colleague keeps their track instead of
losing it and getting a second one. The size-aware distance stays as the
coasting fallback. Greedy nearest-first on the distance alone let three
people crossing swap identities (one track jumped 140 px to another
person) and every swap left a fresh track to confirm beside the live ones.
Kept dependency-light on purpose: the filter and the assignment are a few
lines of numpy, no scipy.
"""
from __future__ import annotations

import math
import time

import numpy as np
from dataclasses import dataclass, field
from itertools import count

from .regions import Box, intersection_over_union
from .thumbnail import BLUR_VETO, ThumbCandidate, blur_vetoed, crop_sharpness, is_better_thumbnail

#: A main-stream crop must look at least this much like the detect-frame
#: crop of the same box (Pearson correlation of the two boxes' luma, both
#: reduced to a MATCH_THUMB x MATCH_THUMB thumbnail) or it is refused for
#: the detect crop. Measured on a 960x1080 H.265 main against its 352x288
#: sub (keyframes a second apart): regions of the same place scored
#: 0.83-1.00, the neighbouring region at most 0.48, a half-shifted one at
#: most 0.36. 0.5 sits in the gap; 0 disables the check.
DEFAULT_EVIDENCE_MATCH_MIN = 0.5
MATCH_THUMB = 16
#: Luma standard deviation (0-255) of a 16x16 thumbnail below which a crop
#: is "flat": a blank wall, a night-IR patch, nothing to correlate.
FLAT_TEXTURE = 2.0
#: How many evidence attempts a camera's debug dump keeps before it stops
#: writing (a walk-through is a few hundred; this is a disk guard).
EVIDENCE_DUMP_MAX = 2000

#: Labels the detector confuses on ONE object. A vehicle read as "car" on
#: one frame and "truck" on the next is the same vehicle: matching strictly
#: per label gave it two tracks, each fed on alternate frames, each ageing
#: toward deletion and re-spawning — the churn behind flickering boxes and
#: duplicate visits. Labels in one group match each other; the track keeps
#: a vote and carries the label the detector says most often.
MATCH_LABEL_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"car", "truck", "bus"}),
)
_LABEL_GROUP: dict[str, frozenset[str]] = {
    label: group for group in MATCH_LABEL_GROUPS for label in group
}


def labels_match(a: str, b: str) -> bool:
    """Whether a detection labelled ``a`` may match a track labelled ``b``."""
    if a == b:
        return True
    group = _LABEL_GROUP.get(a)
    return group is not None and b in group


@dataclass(frozen=True)
class Detection:
    """A detector result for one object in one frame."""

    label: str
    box: Box
    score: float


@dataclass
class TrackConfig:
    """Lifecycle + matching tunables. Frigate ties these to detect fps."""

    fps: int = 5
    # consecutive hits before a track is "confirmed" (Frigate: min_initialized = fps//2)
    min_initialized: int | None = None
    # A coasting track (missed the last frames) is matched with a wider
    # gate: the distance threshold grows by this fraction per miss, up to
    # ``coast_gate_max`` times. Someone who steps behind a colleague and
    # reappears a stride away is the same track, not a new one; without
    # this every occlusion in a group of people made a second visit
    # (Ventuno Q, 2026-11-05). 0 keeps the gate fixed.
    coast_gate_growth: float = 0.25
    coast_gate_max: float = 3.0
    # A detection much WIDER than the track's box (over width_grow_max
    # times) or much narrower (under 1/width_shrink_max) is never a match,
    # whatever the distance. The size-aware distance lets a box three
    # times as wide through when the bottom-centres agree, so a track
    # following one person took the box the detector drew around a group,
    # and a group track stole a member (replayed on the Ventuno Q,
    # 2026-11-05). Shrinking is allowed more: a person half hidden behind
    # a chair is a narrower box of the same person. Width only: heights
    # swing with pose and partial boxes. 0 turns a limit off.
    width_grow_max: float = 1.6
    width_shrink_max: float = 2.5
    # A detection under 1/height_shrink_max of the PREDICTED height is a
    # part (a torso box on the frame the whole person was missed) and
    # never a match: matched, it would shrink the track to the torso and
    # the next whole-person box would not overlap it. A torso is about
    # half a person, so the limit sits well above that. 0 turns it off.
    height_shrink_max: float = 2.0
    # ── Motion-predicted, two-stage assignment ───────────────────────
    # Stage 1: detections scoring at least ``high_score`` are assigned to
    # every track by overlap (IoU) with the track's Kalman-predicted box,
    # globally; ``match_iou`` is the least overlap that counts for a track
    # seen last frame, ``coast_match_iou`` for one that is coasting (its
    # prediction is less sure). Stage 2: the lower-scoring detections go
    # to the tracks stage 1 left unmatched that WERE seen last frame, at
    # ``low_match_iou``; they keep a track alive and never spawn one.
    # ``motion=False`` returns to the greedy size-aware distance alone.
    motion: bool = True
    high_score: float = 0.5
    match_iou: float = 0.2
    coast_match_iou: float = 0.1
    low_match_iou: float = 0.4
    # frames a confirmed track survives unmatched (Frigate: ~fps*5)
    max_disappeared: int | None = None
    # size-aware distance below which a detection matches a track (Frigate ~2.5)
    distance_threshold: float = 2.5
    # per-class distance override (Frigate tunes car/license_plate/person)
    per_class_threshold: dict[str, float] = field(default_factory=dict)
    # IoU above which a matched box counts as "not moved" this frame
    motionless_iou: float = 0.9
    # motionless frames before a track is considered stationary (Frigate: 50)
    stationary_threshold: int = 50
    # ── Bounded-load guards (#track-explosion) ─────────────────────────
    # Hard ceiling on live tracks per camera. A cluttered scene full of
    # persistent false positives (wires → "kite"/"banana") must not grow the
    # track list — and with it the per-frame re-verify region count — without
    # bound. At the cap, new spawns are refused (counted on
    # ``Tracker.spawns_dropped``); expiry below drains the population.
    max_tracks: int = 50
    # Wall-clock TTL: a track not positively MATCHED for this many seconds
    # expires no matter what, coasting or not. Frame-based miss counting
    # drains far too slowly when the pipeline is overloaded (the very moment
    # an explosion needs draining) — wall time doesn't care how slow frames
    # are. A real object that expires is simply re-detected on the next scan
    # of its region; cheap churn, bounded state. 0 disables.
    coast_ttl_seconds: float = 300.0
    # Minimum detection score to SPAWN a new track (matching an existing
    # track has no floor beyond the detector's own threshold). Frigate's
    # min_score/threshold hysteresis: it takes solid evidence to create an
    # object, weaker evidence to keep following it. 0 preserves the
    # pre-guard behavior for existing callers/tests.
    min_spawn_score: float = 0.0

    def initialized(self) -> int:
        # Three hits at 5 fps (0.6 s), six at 10. Two hits let every
        # fragment the detector boxes for a moment in a crowd (a head, a
        # sleeve) confirm a track and post a visit: replayed on the Ventuno
        # Q, 2026-11-05, three hits took a three-person pass from six
        # visits to between three and five.
        return self.min_initialized if self.min_initialized is not None else max(1, self.fps * 3 // 5)

    def disappeared(self) -> int:
        return self.max_disappeared if self.max_disappeared is not None else max(1, self.fps * 5)

    def threshold_for(self, label: str) -> float:
        return self.per_class_threshold.get(label, self.distance_threshold)


@dataclass
class Track:
    id: int
    label: str
    box: Box
    score: float
    hits: int = 1
    misses: int = 0
    age: int = 1
    motionless_count: int = 0
    confirmed: bool = False
    best: ThumbCandidate | None = None
    stationary_threshold: int = 50
    # Matches per label (labels_match lets a group of confusable labels
    # feed one track); ``label`` follows the majority so a vehicle first
    # seen as "car" and then steadily read as "truck" is reported as one.
    label_votes: dict[str, int] = field(default_factory=dict, compare=False)
    # Monotonic timestamp of the last positive match (set at spawn and on
    # every match) — the coast-TTL expiry anchor.
    last_matched: float = 0.0
    # True for exactly the update() in which this track was matched to a
    # detection (or spawned from one); False while it COASTS. Not the same
    # as ``misses == 0``: a track coasting unscanned never counts a miss,
    # so misses cannot tell "seen this frame" from "not looked for". The
    # bus ships this so a live overlay draws only what was actually
    # detected — a coasting track at its last box read as a phantom.
    matched_now: bool = field(default=False, compare=False)
    # Seconds since the last positive match, stamped at the end of every
    # update() from the tracker's own clock. 0.0 on the frame it was
    # matched; grows while it coasts. This — not matched_now alone — is
    # what a live renderer needs: under a detector budget a present object
    # is re-verified every few frames, not every frame, so "matched this
    # frame" flickers, while "matched within the last couple of seconds"
    # is steady for anything real and false for a phantom whose spot is
    # never scanned again.
    since_match_s: float = field(default=0.0, compare=False)
    # BGR crop of the best frame, retained only when the tracker is fed pixels
    # (the Tier-1 gate dispatches THIS on escalation — see gate/dispatch, PR B #10).
    best_crop: object | None = field(default=None, repr=False, compare=False)
    # Monotonic capture stamp of the frame best_crop came from, set with it.
    # A visit whose camera has no LPR skill ships no plate candidates, so
    # core's sweep OCRs THIS crop — and without a time of its own that read
    # got no observed_at at all, which is every read on a default install
    # (#451). Same clock as the candidate ring, so both convert alike.
    best_crop_ts: float | None = field(default=None, compare=False)
    # Multi-frame OCR: top-K plate-readability-scored crops spread across
    # the pass (vehicle labels on LPR cameras only — None everywhere else,
    # so non-LPR deployments pay zero memory or scoring cost).
    plate_ring: object | None = field(default=None, repr=False, compare=False)
    # JPEG of the WHOLE frame the best crop was taken from — the visit's
    # second evidence image (best_crop answers "what was it", this answers
    # "where, and what else was in shot"). Encoded EAGERLY, at best-frame
    # update time, rather than kept as pixels: a 1080p BGR array is 6.2 MB,
    # so one per track is 310 MB per camera at DETECT_MAX_TRACKS=50.
    # repr=False is load-bearing, not cosmetic — 150 KB of bytes in a log
    # line or a pytest assertion diff is its own outage.
    best_scene_jpeg: object | None = field(default=None, repr=False, compare=False)
    # Which stream best_crop (and the scene) were cut from: "main" when the
    # worker runs an evidence source on the camera's main stream and a main
    # frame close enough in time was there, else "detect" (the frame the
    # detector ran on). The skew is the capture-time distance between the
    # sub frame and the main frame the crop came from. Both exist so a
    # quality study can tell the two apart per visit; None = no evidence
    # source configured, the pre-feature behaviour.
    best_crop_source: str | None = field(default=None, compare=False)
    best_crop_skew_s: float | None = field(default=None, compare=False)
    # The detect-frame stamp the latest evidence REQUEST (playback.py) was
    # filed for; a result for an older moment is ignored.
    best_crop_req_mono: float | None = field(default=None, compare=False)
    # The request a tentative track would have filed: kept until the track
    # is confirmed, since a tentative track dies on a single miss and its
    # result would come back for a visit that never existed.
    evidence_req_held: object | None = field(default=None, repr=False, compare=False)
    # Frames on which this track's box held two or more other live tracks
    # of the same label: the detector boxed a GROUP of people as one, and a
    # track that follows the group box is not a visit. The lifecycle drops
    # a visit that was a group for most of its frames.
    group_frames: int = field(default=0, compare=False)
    group_now: bool = field(default=False, compare=False)
    # The motion filter and the box it predicts for the current frame
    # (``box`` stays the last DETECTED box: evidence, overlays and the
    # stationary test read that; only matching reads the prediction).
    kf: object | None = field(default=None, repr=False, compare=False)
    predicted: Box | None = field(default=None, compare=False)
    # The widest the box has been over the last second of matches: the
    # width-growth gate measures against THIS, not the current box. A
    # person half behind a colleague is a narrow box for a few frames;
    # measured against that, their own full box on the next frame was
    # "2.6 times wider" and refused, and a new track took it (replayed on
    # the Ventuno Q, 2026-11-05: track 13 lost, track 15 spawned on the
    # same person 0.4 s later).
    width_ref: float = field(default=0.0, compare=False)
    _widths: list = field(default_factory=list, repr=False, compare=False)

    @property
    def stationary(self) -> bool:
        """True once the object has been motionless long enough (PR B skips
        re-detecting stationary tracks; here we only track the count)."""
        return self.motionless_count >= self.stationary_threshold


def bottom_center_distance(a: Box, b: Box) -> float:
    """Size-aware distance between two boxes.

    Compares bottom-center position (normalized by the target box's size) plus
    width/height ratios — the same shape as Frigate's custom Norfair distance, so
    a car passing a parked car does not get merged.
    """
    a_cx = (a[0] + a[2]) / 2.0
    b_cx = (b[0] + b[2]) / 2.0
    a_bottom, b_bottom = float(a[3]), float(b[3])
    a_w, a_h = max(a[2] - a[0], 1), max(a[3] - a[1], 1)
    b_w, b_h = max(b[2] - b[0], 1), max(b[3] - b[1], 1)

    dx = (a_cx - b_cx) / b_w
    dy = (a_bottom - b_bottom) / b_h
    dw = abs(a_w - b_w) / b_w
    dh = abs(a_h - b_h) / b_h
    return math.sqrt(dx * dx + dy * dy + dw * dw + dh * dh)


def _width_ratio(det: Box, track: Box) -> float:
    """The detection's width over the track box's width."""
    return max(det[2] - det[0], 1) / max(track[2] - track[0], 1)


def _width_share(inner: Box, outer: Box) -> float:
    ow = max(0.0, outer[2] - outer[0])
    return (max(0.0, inner[2] - inner[0]) / ow) if ow > 0 else 0.0


def _coverage(inner: Box, outer: Box) -> float:
    """Fraction of ``inner``'s area that lies inside ``outer`` (0..1)."""
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area = max((inner[2] - inner[0]) * (inner[3] - inner[1]), 1)
    return inter / area


def _gray_thumb(bgr, n: int):
    """``bgr`` as an n x n float32 luma thumbnail, area-averaged when the
    crop is bigger than n on both sides, nearest-sampled when smaller."""
    import numpy as np

    gray = bgr.mean(axis=2, dtype=np.float32) if bgr.ndim == 3 else bgr.astype(np.float32)
    h, w = gray.shape
    if h >= n and w >= n:
        ys = (np.arange(n + 1) * h // n)
        xs = (np.arange(n + 1) * w // n)
        sums = np.add.reduceat(np.add.reduceat(gray, ys[:-1], axis=0), xs[:-1], axis=1)
        counts = np.outer(np.diff(ys), np.diff(xs)).astype(np.float32)
        return sums / counts
    yi = np.linspace(0, h - 1, n).astype(int)
    xi = np.linspace(0, w - 1, n).astype(int)
    return gray[yi][:, xi]


def crop_texture(a, n: int = MATCH_THUMB) -> float | None:
    """How much there is to compare in a crop: the luma standard deviation
    of its n x n thumbnail (0-255). None for an empty crop."""
    if a is None or a.size == 0:
        return None
    return float(_gray_thumb(a, n).std())


def crop_similarity(a, b, n: int = MATCH_THUMB) -> float | None:
    """How alike two crops of (supposedly) the same place look: Pearson
    correlation of their n x n luma thumbnails, in [-1, 1]. Resolution,
    aspect and encoder differences wash out in the thumbnail; brightness
    and contrast differences in the normalisation. None when either crop
    is empty or flat (no texture to compare) — undecidable, not unalike."""
    import numpy as np

    if a is None or b is None or a.size == 0 or b.size == 0:
        return None
    ta = _gray_thumb(a, n).ravel()
    tb = _gray_thumb(b, n).ravel()
    ta = ta - ta.mean()
    tb = tb - tb.mean()
    denom = float(np.sqrt(float((ta * ta).sum()) * float((tb * tb).sum())))
    if denom < 1e-6:
        return None
    return float((ta * tb).sum() / denom)


def _crop_bgr(bgr, box: Box, margin: float = 0.25):
    """Crop ``box`` plus a context margin, clamped to the frame; None if empty.

    The margin is the point. This crop becomes the visit's stored EVIDENCE —
    the photo the operator is shown when they ask "who came today?" — and the
    bare detection box is the least legible framing of it. Field case: a
    person leaned over a desk camera, the highest-scoring box of the visit
    was their hands, and the evidence photo was a 163x187 crop of knuckles
    with zero surroundings. A quarter of the box's size on every side keeps
    the subject dominant while showing enough scene to tell WHERE they were
    and what they were near — which is what evidence is for.
    """
    h, w = bgr.shape[:2]
    mx = int((int(box[2]) - int(box[0])) * margin)
    my = int((int(box[3]) - int(box[1])) * margin)
    x1 = max(0, min(int(box[0]) - mx, w - 1))
    y1 = max(0, min(int(box[1]) - my, h - 1))
    x2 = max(x1 + 1, min(int(box[2]) + mx, w))
    y2 = max(y1 + 1, min(int(box[3]) + my, h))
    crop = bgr[y1:y2, x1:x2]
    return crop.copy() if crop.size else None


class BoxMotion:
    """A constant-velocity Kalman filter over a box: centre (cx, cy) with
    its velocity, width and height without one. SORT / ByteTrack also
    carry a size velocity; here the detector's part boxes make the size
    jump by half from one frame to the next, and a size velocity learnt
    from that predicted a box far wider than the person, whose real box
    then failed the shrink gate (replayed on the Ventuno Q, 2026-11-05).
    Noise scales with the box height so a far-away person and a near one
    are treated alike. Pure numpy."""

    __slots__ = ("mean", "cov", "_dt")
    _POS = 1.0 / 20   # measurement / process noise, as a share of height
    _VEL = 1.0 / 160

    def __init__(self, box: Box, dt: float = 1.0) -> None:
        z = self._measure(box)
        self.mean = np.r_[z, np.zeros(2)]
        h = max(z[3], 1.0)
        std = np.r_[[2 * self._POS * h] * 4, [10 * self._VEL * h] * 2]
        self.cov = np.diag(std ** 2)
        self._dt = dt

    @staticmethod
    def _measure(box: Box) -> np.ndarray:
        w, h = max(box[2] - box[0], 1.0), max(box[3] - box[1], 1.0)
        return np.array([box[0] + w / 2, box[1] + h / 2, w, h], dtype=float)

    def predict(self) -> Box:
        """Advance one frame; returns the predicted box."""
        F = np.eye(6)
        F[0, 4] = F[1, 5] = self._dt
        h = max(self.mean[3], 1.0)
        std = np.r_[[self._POS * h] * 4, [self._VEL * h] * 2]
        self.mean = F @ self.mean
        self.cov = F @ self.cov @ F.T + np.diag(std ** 2)
        return self.box

    def update(self, box: Box) -> None:
        H = np.eye(4, 6)
        h = max(self.mean[3], 1.0)
        R = np.diag([(self._POS * h) ** 2] * 4)
        S = H @ self.cov @ H.T + R
        K = self.cov @ H.T @ np.linalg.inv(S)
        self.mean = self.mean + K @ (self._measure(box) - H @ self.mean)
        self.cov = (np.eye(6) - K @ H) @ self.cov

    @property
    def box(self) -> Box:
        cx, cy, w, h = self.mean[:4]
        w, h = max(w, 1.0), max(h, 1.0)
        return (float(cx - w / 2), float(cy - h / 2), float(cx + w / 2), float(cy + h / 2))


def assign(cost: np.ndarray, max_cost: float) -> list[tuple[int, int]]:
    """Minimum-cost one-to-one assignment (Hungarian, O(n^3)) of rows to
    columns, returning the (row, col) pairs whose cost is below
    ``max_cost``. Small matrices only (tracks x detections in one frame)."""
    if cost.size == 0:
        return []
    rows, cols = cost.shape
    transposed = rows > cols
    a = cost.T if transposed else cost
    n, m = a.shape                                    # n <= m
    INF = float("inf")
    u = [0.0] * (n + 1); v = [0.0] * (m + 1); p = [0] * (m + 1); way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i; j0 = 0
        minv = [INF] * (m + 1); used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]; delta = INF; j1 = 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = a[i0 - 1, j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur; way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]; j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta; v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]; p[j0] = p[j1]; j0 = j1
    out = []
    for j in range(1, m + 1):
        if p[j] and a[p[j] - 1, j - 1] < max_cost:
            out.append((j - 1, p[j] - 1) if transposed else (p[j] - 1, j - 1))
    return out


class Tracker:
    """Multi-object tracker: motion-predicted two-stage assignment (see the
    module docstring), with the size-aware distance as the coasting fallback."""

    def __init__(
        self, frame_shape: tuple[int, int], config: TrackConfig | None = None,
        clock=None,
    ) -> None:
        self.frame_shape = frame_shape  # (h, w)
        self.config = config or TrackConfig()
        self._tracks: list[Track] = []
        self._ids = count(1)
        # Injectable monotonic clock (tests); wall time drives coast-TTL expiry.
        self._clock = clock or time.monotonic
        # Spawns refused because the track cap or spawn-score floor was hit —
        # observability for the bounded-load guards (service exports it).
        self.spawns_dropped = 0
        # Unmatched detections that were a PART of a tracked object (mostly
        # inside a live track's box, same label) and so spawned nothing; the
        # frame-level rule in pipeline.nms catches the pair when both boxes
        # are in one frame, this catches the part box on frames where the
        # whole was missed. 0 turns it off.
        self.spawn_containment: float = 0.7
        self.spawns_absorbed = 0
        # Stage-2 matches: a low-score box that kept a track alive.
        self.low_matches = 0
        # Multi-frame OCR (opt-in per camera): retain top-K plate candidates
        # on vehicle tracks. The worker enables this only when the camera's
        # assignments include the LPR skill.
        self.retain_plate_candidates: bool = False
        self.plate_candidates_max: int = 4
        self.plate_candidates_gap_s: float = 0.75
        # Context kept around the detection box in every stored crop (the
        # visit's evidence AND the plate candidates), as a fraction of
        # the box per side. 0.25 keeps the subject dominant; a wider
        # margin survives a detector box that trails a moving car (the
        # read frame then shows the whole car, not a car with its tail
        # cut off). Purely cosmetic for reading: the OCR localises the
        # plate inside whatever it is given.
        self.crop_margin: float = 0.25
        # Scene evidence: the whole frame behind best_crop, as JPEG. Off
        # here so every existing caller and test pays one boolean per
        # best-frame update; the worker opts in from env.
        self.retain_scene: bool = False
        self.scene_max_px: int = 1280
        self.scene_quality: int = 78
        # Per-frame encode memo. 50 tracks peaking on the SAME frame must
        # cost one imencode, not 50 — and the frame reference is dropped at
        # the end of update(), so nothing holds 6 MB between frames.
        self._scene_bgr = None
        self._scene_jpeg: bytes | None = None
        self._scene_done = False
        # Evidence from the MAIN stream (see evidence.py): a callable that,
        # given the detect frame's capture stamp, returns the main frame
        # captured nearest to it (or None). When it answers, every crop
        # this update stores — best crop, scene, plate candidates — is cut
        # from that frame instead of the detect frame, at the main stream's
        # resolution. None (the default) = crops from the detect frame, as
        # before. Resolved ONCE per update, not per track.
        self.evidence = None
        self._ev = None
        self._ev_skew: float | None = None
        self._ev_ts: float | None = None
        # How many best-crop updates were cut from which stream, for the
        # worker's metrics. "main" / "detect" keys, monotonic.
        self.evidence_stats: dict[str, int] = {"main": 0, "detect": 0}
        # Best-frame blur veto (thumbnail.py): a candidate much blurrier
        # than the current best does not replace it on score or area; 0
        # turns it off. Vetoes are counted for the worker's gauge.
        self.blur_veto: float = BLUR_VETO
        self.blur_vetoes: int = 0
        # Plate candidates whose pixels the recording replaced at full
        # resolution (CandidateRing.upgrade), for the worker's gauge.
        self.plate_upgrades: int = 0
        # Evidence from the recording (playback.py): a callable that takes
        # an EvidenceRequest when a track's best crop improves. The result
        # comes back later through apply_evidence_result(). None = off.
        self.evidence_request = None
        # The main crop is only used when it LOOKS like the detect crop
        # (crop_similarity). The two streams are the same sensor, so a
        # correct pairing scores near 1; a keyframe old enough for the
        # subject to have moved on, or a sub-stream the camera crops to
        # a different field of view, scores near 0 — and either would
        # otherwise hand the operator (and the VLM, and the plate reader)
        # a sharp picture of the wrong thing. Refusals are counted.
        self.evidence_match_min: float = DEFAULT_EVIDENCE_MATCH_MIN
        self.evidence_mismatches = 0
        # Refusals by reason ("low_corr": the crops do not look alike;
        # "flat_main": the sub saw a textured subject where the main shows a
        # blank), and one sample per attempt — (score, skew_s, accepted,
        # reason, sim_main_ref, sim_sub_ref) — for the worker's histograms;
        # drained by drain_evidence_samples().
        self.evidence_refusals: dict[str, int] = {}
        self.evidence_samples: list[tuple] = []
        # A second lookup: a main frame from a few seconds BEFORE the detect
        # frame (the scene without the subject, if it moved in since). Only
        # measured for now — sim(main, ref) high while sim(sub, main) is
        # high too means the box is mostly background and the compare is
        # uninformative — and written to the dump so the rule can be set
        # from data rather than guessed. None = not measured.
        self.evidence_ref = None
        self._ev_ref = None
        # DETECT_EVIDENCE_DEBUG_DIR: every attempt's crops and numbers go to
        # <dir>/<camera>/ for the offline guard report (tools/
        # evidence_guard_report.py). None = off.
        self.evidence_debug_dir: str | None = None
        self.evidence_debug_camera: str = "cam"
        self._dumped = 0

    def drain_evidence_samples(self) -> list[tuple]:
        out, self.evidence_samples = self.evidence_samples, []
        return out

    @property
    def population(self) -> int:
        """ALL internal tracks, tentative included — what ``max_tracks`` caps.

        ``tracks`` returns only confirmed ones, so a camera could sit at the
        cap refusing new spawns while the gauge meant to explain that looked
        perfectly healthy.
        """
        return len(self._tracks)

    @property
    def tracks(self) -> list[Track]:
        """Confirmed tracks only (past the initialization delay)."""
        return [t for t in self._tracks if t.confirmed]

    def update(
        self, detections: list[Detection], bgr=None,
        scanned_regions: list[Box] | None = None,
        frame_ts: float | None = None,
    ) -> list[Track]:
        # ``bgr`` (the full-frame BGR image) is optional: when given, the best
        # frame's crop pixels are retained on the track for Tier-1 dispatch.
        #
        # ``scanned_regions`` is the PR B coasting contract: the regions the
        # detector actually LOOKED AT this frame. An unmatched track only
        # counts a miss if its box intersects a scanned region — being
        # missed where we looked is evidence of absence; being unmatched
        # because its region was skipped (stationary gating) is not. None
        # (the default, and every pre-PR-B caller) preserves the original
        # behavior: every unmatched track counts a miss.
        cfg = self.config
        now = self._clock()
        # Every track starts this update unmatched; _match/_spawn set it.
        for tr in self._tracks:
            tr.matched_now = False
        # This frame's scene memo. Stamped here, cleared before the return:
        # the encode is shared by every track that peaks on this frame, and
        # the frame reference never outlives the call.
        self._scene_bgr, self._scene_jpeg, self._scene_done = bgr, None, False
        # The main frame for this update's crops, looked up once. ``frame_ts``
        # is the detect frame's capture stamp (Frame.ts); the evidence
        # source refuses a main frame too far from it, and then the detect
        # frame's own pixels are used — never a crop of the wrong moment.
        self._ev, self._ev_skew, self._ev_ts, self._ev_ref = None, None, frame_ts, None
        if self.evidence is not None and bgr is not None and detections:
            try:
                self._ev = self.evidence(frame_ts)
            except Exception:
                self._ev = None
            if self._ev is not None and self.evidence_ref is not None:
                try:
                    self._ev_ref = self.evidence_ref(frame_ts)
                except Exception:
                    self._ev_ref = None
        unmatched_tracks = set(range(len(self._tracks)))
        unmatched_dets = set(range(len(detections)))
        # Low-score boxes only ever KEEP a track (stage 2); they never
        # spawn one and are not offered to coasting tracks.
        low_dets: set[int] = set()
        if cfg.motion:
            low_dets = {di for di, d in enumerate(detections) if d.score < cfg.high_score}
            seen_last = {ti for ti, tr in enumerate(self._tracks) if tr.misses == 0}
            for tr in self._tracks:
                if tr.kf is None:
                    tr.kf = BoxMotion(tr.box)
                tr.predicted = tr.kf.predict()
            # Stage 1: confident boxes against every track, by overlap
            # with the prediction.
            for ti, di in self._assign_stage(
                    detections, unmatched_tracks, unmatched_dets - low_dets,
                    lambda tr: cfg.match_iou if tr.misses == 0 else cfg.coast_match_iou, bgr, now):
                unmatched_tracks.discard(ti)
                unmatched_dets.discard(di)
            # Stage 2: the unsure boxes against the tracks seen last frame
            # that stage 1 left unmatched.
            for ti, di in self._assign_stage(
                    detections, unmatched_tracks & seen_last, unmatched_dets & low_dets,
                    lambda tr: cfg.low_match_iou, bgr, now):
                unmatched_tracks.discard(ti)
                unmatched_dets.discard(di)
                self.low_matches += 1
        # The size-aware distance: the whole matcher with motion off, the
        # coasting fallback with it on (a track whose prediction drifted
        # while it coasted can still take a box its last position vouches
        # for, with the gate widening per miss). Unsure boxes are offered
        # here too: on a far, small person the detector is unsure most
        # frames, and a coasting track refused them starved and died
        # while the person stood there (CAVIAR lobby replay, 2026-11-05).
        pairs: list[tuple[float, int, int]] = []
        for ti in unmatched_tracks:
            tr = self._tracks[ti]
            if cfg.motion and tr.misses == 0:
                continue
            for di in unmatched_dets:
                det = detections[di]
                if not labels_match(det.label, tr.label) or not self._size_ok(det.box, tr.box, tr):
                    continue
                dist = bottom_center_distance(tr.box, det.box)
                gate = cfg.threshold_for(det.label)
                if tr.misses > 0 and cfg.coast_gate_growth > 0:
                    gate *= min(cfg.coast_gate_max, 1.0 + cfg.coast_gate_growth * tr.misses)
                if dist <= gate:
                    pairs.append((dist, ti, di))
        pairs.sort()
        for _dist, ti, di in pairs:
            if ti in unmatched_tracks and di in unmatched_dets:
                self._match(self._tracks[ti], detections[di], bgr, now)
                unmatched_tracks.discard(ti)
                unmatched_dets.discard(di)
        if low_dets:
            # Never spawn from an unsure box: refused like a weak spawn.
            self.spawns_dropped += len(unmatched_dets & low_dets)
            unmatched_dets -= low_dets

        # unmatched detections -> new tentative tracks (bounded: the track
        # cap and the spawn-score floor refuse, never grow without limit)
        for di in unmatched_dets:
            det = detections[di]
            if det.score < cfg.min_spawn_score or len(self._tracks) >= cfg.max_tracks:
                self.spawns_dropped += 1
                continue
            if self.spawn_containment > 0 and sum(
                    1 for tr in self._tracks
                    if labels_match(det.label, tr.label)
                    and _coverage(tr.box, det.box) >= self.spawn_containment) >= 2:
                # The detector boxed a group of people already tracked one
                # by one as a single person: a group box, never a track.
                self.spawns_absorbed += 1
                continue
            if self.spawn_containment > 0 and any(
                    labels_match(det.label, tr.label)
                    and _coverage(det.box, tr.box) >= self.spawn_containment
                    and (tr.matched_now or _width_share(det.box, tr.box) >= 0.6)
                    for tr in self._tracks):
                # The detector boxed a part of someone already tracked; its
                # bottom-centre sits far from the track's, so it did not
                # match, and it would have become a second visit. Inside a
                # COASTING track only a box about as wide counts as a part:
                # a narrow one is as likely a person the group box covered.
                self.spawns_absorbed += 1
                continue
            self._spawn(det, bgr, now)

        # unmatched tracks -> age out (or coast, if never scanned this frame)
        survivors: list[Track] = []
        for ti, tr in enumerate(self._tracks):
            # Coast-TTL: however it got here — coasting unscanned, surviving
            # on misses — a track that hasn't been positively matched in
            # ``coast_ttl_seconds`` of WALL time is gone. This is the drain
            # that frame-based miss counting can't provide under load.
            if (
                cfg.coast_ttl_seconds > 0
                and (now - tr.last_matched) > cfg.coast_ttl_seconds
            ):
                continue
            if ti in unmatched_tracks:
                tr.age += 1
                if scanned_regions is not None and not any(
                    _coverage(tr.box, r) >= 0.7 for r in scanned_regions
                ):
                    # Coast: no scanned region substantially covered this
                    # object, so its absence from ``detections`` is not
                    # evidence it left. Mere partial overlap coasts too —
                    # motion NEXT TO a gated stationary object scans a
                    # region that clips it; a half-visible car at a crop
                    # edge going undetected must not accumulate misses, or
                    # someone loitering beside a parked car would get its
                    # track deleted. Re-verification frames still expire a
                    # departed object: the track's own region fully
                    # contains its box (coverage 1.0).
                    survivors.append(tr)
                    continue
                tr.misses += 1
                # tentative tracks die on a single miss; confirmed survive to max_disappeared
                if not tr.confirmed or tr.misses > cfg.disappeared():
                    continue
            survivors.append(tr)
        self._tracks = survivors
        for tr in survivors:
            tr.since_match_s = max(0.0, now - tr.last_matched)
        # A live track whose box holds two or more other live tracks of the
        # same label is following a group box this frame (the members may
        # have been missed on the frame it spawned on, so the spawn guard
        # could not know). Marked per frame; the lifecycle decides.
        if self.spawn_containment > 0 and len(survivors) >= 3:
            for tr in survivors:
                inside = sum(
                    1 for o in survivors
                    if o is not tr and labels_match(o.label, tr.label)
                    and _coverage(o.box, tr.box) >= self.spawn_containment)
                tr.group_now = inside >= 2
                if tr.group_now:
                    tr.group_frames += 1
        else:
            for tr in survivors:
                tr.group_now = False
        # Drop the frame reference: holding it would keep 6 MB alive between
        # frames for nothing. (An exception escaping above leaves one frame
        # referenced, and that path already unwinds into the worker's
        # crashed-camera handler, which drops the whole Tracker.)
        self._scene_bgr, self._scene_jpeg, self._scene_done = None, None, False
        self._ev, self._ev_skew = None, None
        return self.tracks

    def _size_ok(self, det: Box, ref: Box, tr: Track | None = None) -> bool:
        """The width and height gates against a reference box (the
        prediction when matching by motion, the last box otherwise).
        Growth is measured against the track's recent widest box."""
        cfg = self.config
        grow = _width_ratio(det, ref)
        wide = max(ref[2] - ref[0], tr.width_ref if tr is not None else 0.0, 1.0)
        if (cfg.width_grow_max > 0 and (det[2] - det[0]) / wide > cfg.width_grow_max) or \
                (cfg.width_shrink_max > 0 and grow < 1.0 / cfg.width_shrink_max):
            return False
        if cfg.height_shrink_max > 0:
            dh = max(det[3] - det[1], 1.0) / max(ref[3] - ref[1], 1.0)
            if dh < 1.0 / cfg.height_shrink_max:
                return False
        return True

    def _assign_stage(self, detections, track_ids: set[int], det_ids: set[int],
                      iou_floor, bgr, now) -> list[tuple[int, int]]:
        """One assignment stage: overlap of each candidate detection with
        each track's predicted box, solved globally; matches are applied
        and returned as (track index, detection index)."""
        if not track_ids or not det_ids:
            return []
        tis, dis = sorted(track_ids), sorted(det_ids)
        cost = np.ones((len(tis), len(dis)))
        for r, ti in enumerate(tis):
            tr = self._tracks[ti]
            floor = iou_floor(tr)
            for c, di in enumerate(dis):
                det = detections[di]
                if not labels_match(det.label, tr.label) or not self._size_ok(det.box, tr.predicted, tr):
                    continue
                # Overlap with the prediction OR with the last box: a
                # person who stops short (at a wall, to turn) is behind
                # the prediction but still on their last box.
                iou = max(intersection_over_union(tr.predicted, det.box),
                          intersection_over_union(tr.box, det.box))
                if iou >= floor:
                    cost[r, c] = 1.0 - iou
        matched = []
        for r, c in assign(cost, 1.0):
            self._match(self._tracks[tis[r]], detections[dis[c]], bgr, now)
            matched.append((tis[r], dis[c]))
        return matched

    def _match(self, tr: Track, det: Detection, bgr=None, now: float | None = None) -> None:
        iou = intersection_over_union(tr.box, det.box)
        if iou >= self.config.motionless_iou:
            tr.motionless_count += 1
        else:
            tr.motionless_count = 0
        tr.box = det.box
        if tr.kf is not None:
            tr.kf.update(det.box)
        tr._widths.append(det.box[2] - det.box[0])
        del tr._widths[:-max(1, self.config.fps)]
        tr.width_ref = max(tr._widths)
        tr.score = det.score
        tr.hits += 1
        tr.age += 1
        tr.misses = 0
        tr.matched_now = True
        if det.label != tr.label:
            votes = tr.label_votes
            votes[det.label] = votes.get(det.label, 0) + 1
            if votes[det.label] > votes.get(tr.label, 0):
                tr.label = det.label
        else:
            tr.label_votes[det.label] = tr.label_votes.get(det.label, 0) + 1
        tr.last_matched = now if now is not None else self._clock()
        if not tr.confirmed and tr.hits >= self.config.initialized():
            tr.confirmed = True
            held = tr.evidence_req_held
            if held is not None and self.evidence_request is not None:
                tr.evidence_req_held = None
                tr.best_crop_req_mono = held.mono_ts
                self.evidence_request(held)           # its best moment so far
        self._update_best(tr, det, bgr)

    def _spawn(self, det: Detection, bgr=None, now: float | None = None) -> None:
        tr = Track(
            id=next(self._ids),
            label=det.label,
            box=det.box,
            score=det.score,
            stationary_threshold=self.config.stationary_threshold,
            last_matched=now if now is not None else self._clock(),
            matched_now=True,
            label_votes={det.label: 1},
        )
        tr.confirmed = self.config.initialized() <= 1
        tr._widths = [det.box[2] - det.box[0]]
        tr.width_ref = tr._widths[0]
        if self.config.motion:
            tr.kf = BoxMotion(det.box)
            tr.predicted = det.box
        self._update_best(tr, det, bgr)
        self._tracks.append(tr)

    def _update_best(self, tr: Track, det: Detection, bgr=None) -> None:
        sharp = crop_sharpness(bgr, det.box) if (bgr is not None and self.blur_veto > 0) else 0.0
        cand = ThumbCandidate(label=det.label, box=det.box, score=det.score, sharpness=sharp)
        if tr.best is not None and blur_vetoed(tr.best, cand, self.blur_veto) \
                and is_better_thumbnail(tr.best, cand, self.frame_shape, blur_veto=0.0):
            self.blur_vetoes += 1                      # would have won on score/area alone
        if tr.best is None or is_better_thumbnail(tr.best, cand, self.frame_shape, self.blur_veto):
            tr.best = cand
            if bgr is not None:                       # retain the best frame's pixels
                crop, source, frame_bgr = self._crop_for(bgr, det.box)
                if crop is not None:
                    tr.best_crop = crop
                    tr.best_crop_ts = self._clock()
                    # Labelled whenever any evidence path is on: a fresh detect
                    # crop must not keep a "playback" label from the result it
                    # just replaced.
                    if self.evidence is not None or self.evidence_request is not None:
                        tr.best_crop_source = source
                        tr.best_crop_skew_s = (
                            self._ev_skew if source == "main" else None)
                        self.evidence_stats[source] = (
                            self.evidence_stats.get(source, 0) + 1)
                    # Gated on the crop, so the two evidence images always
                    # describe the SAME frame — a scene whose crop was
                    # rejected would be an inconsistent pair. Same frame
                    # as the crop: the main one when the crop came from it.
                    scene = self._scene_jpeg_for(frame_bgr)
                    if scene is not None:
                        tr.best_scene_jpeg = scene
                    self._request_evidence(tr, det, bgr)
        self._update_plate_candidates(tr, det, bgr)

    def _request_evidence(self, tr: Track, det: Detection, bgr) -> None:
        """File a request for the recording's frame of this moment
        (playback.py). A tentative track keeps its request until it is
        confirmed: it dies on a single miss, and a duplicate box on one
        person spawns such tracks every few frames; their results came
        back for visits that never existed (counted late) and cost a
        fetch and a decode each."""
        if self.evidence_request is None or self._ev_ts is None:
            return
        try:
            from .playback import EvidenceRequest
            sub_crop = _crop_bgr(bgr, det.box, margin=0.0)
            if sub_crop is None:
                return
            req = EvidenceRequest(
                track_id=tr.id, mono_ts=float(self._ev_ts), box=det.box,
                frame_shape=tuple(bgr.shape[:2]), sub_crop=sub_crop, label=det.label)
            if not tr.confirmed:
                tr.evidence_req_held = req
                return
            tr.evidence_req_held = None
            tr.best_crop_req_mono = req.mono_ts
            self.evidence_request(req)
        except Exception:
            pass                                          # evidence is an enhancement

    def note_playback_result(self, res) -> None:
        """A recording result, whatever became of it: a sample for the
        histograms (source "playback") and, when the dump is on, the pair on
        disk beside the keyframe attempts, so the guard report compares the
        two paths on the same walk-through."""
        if res.outcome in ("accepted", "refused"):
            self.evidence_samples.append((res.score, res.skew_s, res.outcome == "accepted",
                                          "" if res.outcome == "accepted" else res.reason,
                                          None, None, "playback"))
        if self.evidence_debug_dir and res.outcome in ("accepted", "refused") and res.crop is not None:
            self._dump_playback(res)

    def _dump_playback(self, res) -> None:
        if self._dumped >= EVIDENCE_DUMP_MAX:
            return
        try:
            import json
            import os
            import cv2
            req = res.request
            d = os.path.join(self.evidence_debug_dir, self.evidence_debug_camera)
            os.makedirs(d, exist_ok=True)
            stem = os.path.join(d, f"{int(req.mono_ts * 1000):013d}_{self._dumped:04d}p")
            cv2.imwrite(stem + "_sub.jpg", req.sub_crop)
            cv2.imwrite(stem + "_main.jpg", res.crop)
            with open(stem + ".json", "w") as f:
                json.dump({
                    "source": "playback", "detect_ts": req.mono_ts, "skew_s": res.skew_s,
                    "box": [float(v) for v in req.box], "sub_shape": list(req.frame_shape),
                    "score": res.score, "accepted": res.outcome == "accepted", "reason": res.reason,
                    "candidates": res.candidates, "fetch_s": res.fetch_s, "decode_s": res.decode_s,
                    "floor": float(self.evidence_match_min or 0.0),
                }, f)
            self._dumped += 1
        except Exception:
            self._dumped = EVIDENCE_DUMP_MAX

    def apply_evidence_result(self, res) -> bool:
        """A playback result for a LIVE track: its full-resolution crop (and
        scene) replace the best crop when the result is for the latest
        requested moment and was accepted. Returns whether a track took it."""
        tr = next((t for t in self._tracks if t.id == res.track_id), None)
        if tr is None:
            return False
        if res.outcome != "accepted" or res.crop is None:
            return True                                   # the track is live; nothing to change
        # A plate candidate cut at the same moment takes the full-resolution
        # pixels whatever became of the best crop since: it is the same frame.
        if tr.plate_ring is not None and tr.plate_ring.upgrade(res.request.mono_ts, res.crop):
            self.plate_upgrades += 1
        if tr.best_crop_req_mono != res.request.mono_ts:
            return True                                   # an older moment than the latest request
        tr.best_crop = res.crop
        tr.best_crop_source = "playback"
        tr.best_crop_skew_s = res.skew_s
        tr.best_crop_ts = self._clock()
        self.evidence_stats["playback"] = self.evidence_stats.get("playback", 0) + 1
        if self.retain_scene and res.frame is not None:
            try:
                from .bestframe import encode_scene_jpeg
                tr.best_scene_jpeg = encode_scene_jpeg(res.frame)
            except Exception:
                pass
        return True

    def _crop_for(self, bgr, box: Box):
        """``(crop, source, frame)``: the crop of ``box`` with the context
        margin, from the main-stream evidence frame when this update has
        one (the box scaled by the resolution ratio — same sensor, same
        pixels), else from the detect frame. ``frame`` is the array the
        crop was cut from, for the scene that must describe the same
        moment."""
        ev = self._ev
        if ev is not None:
            main = ev.bgr()
            if main is not None and main.shape[0] > 0 and main.shape[1] > 0:
                h, w = bgr.shape[:2]
                sx = main.shape[1] / float(w)
                sy = main.shape[0] / float(h)
                scaled = (box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy)
                skew = (abs(ev.ts - float(self._ev_ts))
                        if self._ev_ts is not None else None)
                ok, reason, sim = self._judge_main_crop(main, scaled, bgr, box)
                refs = self._reference_sims(main, scaled, bgr, box, sx, sy)
                self.evidence_samples.append((sim, skew, ok, reason, *refs, "keyframe"))
                if self.evidence_debug_dir:
                    self._dump_attempt(main, scaled, bgr, box, ev, skew, sim, ok, reason, refs)
                if ok:
                    crop = _crop_bgr(main, scaled, margin=self.crop_margin)
                    if crop is not None:
                        if self._ev_skew is None and skew is not None:
                            self._ev_skew = skew
                        return crop, "main", main
                else:
                    self.evidence_mismatches += 1
                    self.evidence_refusals[reason] = self.evidence_refusals.get(reason, 0) + 1
        return _crop_bgr(bgr, box, margin=self.crop_margin), "detect", bgr

    def _judge_main_crop(self, main, scaled: Box, bgr, box: Box):
        """``(accepted, reason, score)``: does the main frame show the same
        thing inside the box as the detect frame did? Compared on the bare
        boxes (no margin — the subject fills them, the margin is mostly
        shared background). Refused on positive evidence only: the crops
        do not correlate (``low_corr``), or the sub frame saw a textured
        subject where the main frame shows a blank (``flat_main`` — a wall
        or an IR-dark patch after the subject left, which the old rule
        waved through as undecidable). Both flat, or a failed compare, is
        undecidable and accepted, as before."""
        floor = float(self.evidence_match_min or 0.0)
        if floor <= 0.0:
            return True, "", None
        try:
            mc = _crop_bgr(main, scaled, margin=0.0)
            sc = _crop_bgr(bgr, box, margin=0.0)
            sim = crop_similarity(mc, sc)
            if sim is None:
                tex_sub, tex_main = crop_texture(sc), crop_texture(mc)
                if (tex_sub is not None and tex_main is not None
                        and tex_sub >= FLAT_TEXTURE and tex_main < FLAT_TEXTURE):
                    return False, "flat_main", None
                return True, "", None
        except Exception:
            return True, "", None
        if sim >= floor:
            return True, "", sim
        return False, "low_corr", sim

    def _reference_sims(self, main, scaled: Box, bgr, box: Box, sx: float, sy: float):
        """``(sim_main_ref, sim_sub_ref)`` against the same box on the
        reference frame (a main frame from before the subject arrived),
        or ``(None, None)`` when there is no reference. Measurement only."""
        ref = self._ev_ref
        if ref is None:
            return None, None
        try:
            rb = ref.bgr()
            if rb is None or rb.shape[:2] != main.shape[:2]:
                return None, None
            rc = _crop_bgr(rb, scaled, margin=0.0)
            return (crop_similarity(_crop_bgr(main, scaled, margin=0.0), rc),
                    crop_similarity(_crop_bgr(bgr, box, margin=0.0), rc))
        except Exception:
            return None, None

    def _dump_attempt(self, main, scaled: Box, bgr, box: Box, ev, skew, sim, ok, reason, refs) -> None:
        """One evidence attempt to disk: the two bare-box crops as JPEG and
        the numbers as JSON, for tools/evidence_guard_report.py. Best effort,
        capped, never raises into the detect loop."""
        if self._dumped >= EVIDENCE_DUMP_MAX:
            return
        try:
            import json
            import os
            import cv2
            d = os.path.join(self.evidence_debug_dir, self.evidence_debug_camera)
            os.makedirs(d, exist_ok=True)
            stem = os.path.join(d, f"{int((self._ev_ts or 0.0) * 1000):013d}_{self._dumped:04d}")
            cv2.imwrite(stem + "_sub.jpg", _crop_bgr(bgr, box, margin=0.0))
            cv2.imwrite(stem + "_main.jpg", _crop_bgr(main, scaled, margin=0.0))
            with open(stem + ".json", "w") as f:
                json.dump({
                    "detect_ts": self._ev_ts, "main_ts": float(ev.ts), "skew_s": skew,
                    "box": [float(v) for v in box], "scaled_box": [float(v) for v in scaled],
                    "sub_shape": list(bgr.shape[:2]), "main_shape": list(main.shape[:2]),
                    "score": sim, "accepted": bool(ok), "reason": reason,
                    "sim_main_ref": refs[0], "sim_sub_ref": refs[1],
                    "texture_sub": crop_texture(_crop_bgr(bgr, box, margin=0.0)),
                    "texture_main": crop_texture(_crop_bgr(main, scaled, margin=0.0)),
                    "floor": float(self.evidence_match_min or 0.0),
                    "source": "keyframe",
                }, f)
            self._dumped += 1
        except Exception:
            self._dumped = EVIDENCE_DUMP_MAX     # one failure switches the dump off

    def _scene_jpeg_for(self, bgr) -> bytes | None:
        """This frame as JPEG — encoded at most ONCE per ``update`` call.

        Eager encode rather than retained pixels. The best frame changes
        O(log(area growth)) times per visit (is_better_thumbnail wants +0.05
        score or +10% area — roughly 34 times for a car growing 100px to
        500px wide, not once per frame), so the CPU is a rounding error,
        while holding the BGR array would be 6.2 MB x DETECT_MAX_TRACKS per
        camera. Tracks that peak on the same frame share the same immutable
        bytes object.

        A failed encode is cached as a failure (``_scene_done``), so one bad
        frame costs one attempt, not one per track — and never costs the
        visit its crop, which is the image that actually matters.
        """
        if not self.retain_scene or bgr is None:
            return None
        if self._scene_bgr is bgr and self._scene_done:
            return self._scene_jpeg
        try:
            from .bestframe import encode_scene_jpeg

            self._scene_jpeg = encode_scene_jpeg(
                bgr, max_px=self.scene_max_px, quality=self.scene_quality,
            )
        except Exception:
            self._scene_jpeg = None
        self._scene_bgr, self._scene_done = bgr, True
        return self._scene_jpeg

    def _update_plate_candidates(self, tr: Track, det: Detection, bgr=None) -> None:
        """Multi-frame OCR: offer this frame's crop to the track's
        plate-candidate ring. Guarded to vehicles + opt-in + pixels
        present, so every other code path is a two-comparison no-op."""
        if not self.retain_plate_candidates or bgr is None:
            return
        from .platecands import (
            VEHICLE_LABELS, CandidateRing, candidate_score,
        )
        if det.label not in VEHICLE_LABELS:
            return
        crop, _source, _frame = self._crop_for(bgr, det.box)
        if crop is None:
            return
        if tr.plate_ring is None:
            tr.plate_ring = CandidateRing(
                max_candidates=self.plate_candidates_max,
                min_gap_s=self.plate_candidates_gap_s,
            )
        from .regions import area as _area
        tr.plate_ring.offer(
            self._clock(), candidate_score(_area(det.box), crop), crop,
            req_mono=float(self._ev_ts) if self._ev_ts is not None else None,
        )

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

Deliberately lean: this is greedy matching over the size-aware distance, not a
full Norfair/Kalman port. Kalman motion prediction (Frigate's per-class R/Q
tuning) is a worthwhile follow-up but is not required for the metric that
prevents ID churn. Kept dependency-light on purpose.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from itertools import count

from .regions import Box, intersection_over_union
from .thumbnail import ThumbCandidate, is_better_thumbnail

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
        return self.min_initialized if self.min_initialized is not None else max(1, self.fps // 2)

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


class Tracker:
    """Greedy, size-aware multi-object tracker."""

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

        # candidate pairs (same label — or a confusable one, see
        # labels_match — within per-class distance threshold)
        pairs: list[tuple[float, int, int]] = []
        for ti, tr in enumerate(self._tracks):
            for di, det in enumerate(detections):
                if not labels_match(det.label, tr.label):
                    continue
                dist = bottom_center_distance(tr.box, det.box)
                if dist <= cfg.threshold_for(det.label):
                    pairs.append((dist, ti, di))
        pairs.sort()

        for _dist, ti, di in pairs:
            if ti in unmatched_tracks and di in unmatched_dets:
                self._match(self._tracks[ti], detections[di], bgr, now)
                unmatched_tracks.discard(ti)
                unmatched_dets.discard(di)

        # unmatched detections -> new tentative tracks (bounded: the track
        # cap and the spawn-score floor refuse, never grow without limit)
        for di in unmatched_dets:
            det = detections[di]
            if det.score < cfg.min_spawn_score or len(self._tracks) >= cfg.max_tracks:
                self.spawns_dropped += 1
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
        # Drop the frame reference: holding it would keep 6 MB alive between
        # frames for nothing. (An exception escaping above leaves one frame
        # referenced, and that path already unwinds into the worker's
        # crashed-camera handler, which drops the whole Tracker.)
        self._scene_bgr, self._scene_jpeg, self._scene_done = None, None, False
        self._ev, self._ev_skew = None, None
        return self.tracks

    def _match(self, tr: Track, det: Detection, bgr=None, now: float | None = None) -> None:
        iou = intersection_over_union(tr.box, det.box)
        if iou >= self.config.motionless_iou:
            tr.motionless_count += 1
        else:
            tr.motionless_count = 0
        tr.box = det.box
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
        self._update_best(tr, det, bgr)
        self._tracks.append(tr)

    def _update_best(self, tr: Track, det: Detection, bgr=None) -> None:
        cand = ThumbCandidate(label=det.label, box=det.box, score=det.score)
        if tr.best is None or is_better_thumbnail(tr.best, cand, self.frame_shape):
            tr.best = cand
            if bgr is not None:                       # retain the best frame's pixels
                crop, source, frame_bgr = self._crop_for(bgr, det.box)
                if crop is not None:
                    tr.best_crop = crop
                    tr.best_crop_ts = self._clock()
                    if self.evidence is not None:
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
        self._update_plate_candidates(tr, det, bgr)

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
                self.evidence_samples.append((sim, skew, ok, reason, *refs))
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
        )

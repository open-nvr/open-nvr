# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Multi-frame plate candidates — the recall half of multi-frame OCR.

Today a vehicle's visit carries ONE crop: the frame where the *vehicle*
looked best (biggest/sharpest box). That is the right frame for a
thumbnail and usually the wrong one for the plate — a car is biggest
when closest, which is when its plate is angled, motion-blurred, or
sliding out of frame. The frames where the plate was small-but-straight
were thrown away, and OCR got exactly one lottery ticket per car.

This module keeps a small ring of the top-K *plate-readability* scored
crops per vehicle track, deliberately spread across the pass:

* **Score** = vehicle box area × crop sharpness (variance of the
  Laplacian). Area because plate pixels scale with vehicle pixels;
  sharpness because motion blur is what actually kills OCR. Both are
  proxies computed in microseconds — the real plate detector lives in
  the OCR adapter and never runs in Tier-0.
* **Diversity**: a new candidate only enters the ring if at least
  ``min_gap_s`` has passed since the last accepted one (or it beats an
  existing candidate outright). Four crops from the same half-second
  are one lottery ticket photocopied; four crops spread across the
  pass are four draws.
* **Bounded**: K crops per track (default 4), vehicle labels only, and
  only on cameras whose assignments include the LPR skill — everyone
  else pays nothing.

Cost: candidates piggyback on the SAME decoded frames Tier-0 already
paid for; scoring is a Laplacian on a small crop (sub-millisecond);
memory is K small BGR crops per live vehicle track.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Vehicle classes whose tracks retain plate candidates.
VEHICLE_LABELS = frozenset({"car", "truck", "bus", "motorcycle"})

DEFAULT_MAX_CANDIDATES = 4
DEFAULT_MIN_GAP_SECONDS = 0.75


def sharpness(crop) -> float:
    """Variance of the Laplacian — the standard cheap blur measure.
    Grayscale conversion + 3x3 Laplacian on a small crop is microseconds.
    Returns 0.0 for degenerate input rather than raising: candidate
    scoring must never take down the frame loop."""
    try:
        import cv2

        if crop is None or crop.size == 0:
            return 0.0
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())
    except Exception:
        return 0.0


def candidate_score(box_area: int, crop) -> float:
    """Plate-readability proxy: bigger vehicle × sharper pixels."""
    return float(max(box_area, 1)) * (1.0 + sharpness(crop))


@dataclass
class PlateCandidate:
    """One retained crop: when it was seen, how promising it looks."""

    ts: float
    score: float
    crop: Any = field(repr=False, compare=False)


class CandidateRing:
    """Top-K plate candidates for ONE track, diversity-gapped.

    Pure and clock-agnostic (timestamps are passed in) so the whole
    policy is unit-testable without frames or wall time.
    """

    def __init__(
        self,
        *,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        min_gap_s: float = DEFAULT_MIN_GAP_SECONDS,
    ) -> None:
        self.max_candidates = max(1, int(max_candidates))
        self.min_gap_s = float(min_gap_s)
        self._items: list[PlateCandidate] = []
        self._last_accept_ts: float | None = None

    def __len__(self) -> int:
        return len(self._items)

    def offer(self, ts: float, score: float, crop) -> bool:
        """Consider one crop. Returns True if it was retained.

        Ring not full → accept if the diversity gap allows (or it beats
        an existing candidate — a much better frame is never refused
        just for arriving quickly). Ring full → accept only if it beats
        the current WORST, which it replaces."""
        if crop is None or score <= 0.0:
            return False
        gap_ok = (
            self._last_accept_ts is None
            or (ts - self._last_accept_ts) >= self.min_gap_s
        )
        if len(self._items) < self.max_candidates:
            if not gap_ok and not self._beats_worst(score):
                return False
            self._items.append(PlateCandidate(ts=ts, score=score, crop=crop))
        else:
            if not self._beats_worst(score):
                return False
            worst = min(range(len(self._items)), key=lambda i: self._items[i].score)
            self._items[worst] = PlateCandidate(ts=ts, score=score, crop=crop)
        self._last_accept_ts = ts
        return True

    def _beats_worst(self, score: float) -> bool:
        return bool(self._items) and score > min(c.score for c in self._items)

    def best(self) -> PlateCandidate | None:
        """The single most promising candidate (for the early attempt)."""
        if not self._items:
            return None
        return max(self._items, key=lambda c: c.score)

    def ranked(self) -> list[PlateCandidate]:
        """All candidates, most promising first (for the ingest sweep)."""
        return sorted(self._items, key=lambda c: c.score, reverse=True)


class EarlyAttemptPolicy:
    """When does a live track deserve an early OCR attempt?

    Pure decision logic, one instance per track:

    * attempt #1 the moment the track is confirmed AND has a candidate —
      that is what moves reads from "when the track dies" (minutes, when
      a lookalike car adopts the track) to "seconds after the car
      appears";
    * attempt #2..N only when a MUCH better candidate has shown up
      (score ≥ ``improve_factor`` × the score attempt #1 used) and at
      least ``min_retry_gap_s`` has passed — a car that was far away at
      confirmation gets a second shot once it fills the frame;
    * hard budget ``max_attempts`` (the compute cap), and never more
      than one attempt per call site invocation.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 2,
        improve_factor: float = 1.5,
        min_retry_gap_s: float = 2.0,
    ) -> None:
        self.max_attempts = max(0, int(max_attempts))
        self.improve_factor = float(improve_factor)
        self.min_retry_gap_s = float(min_retry_gap_s)
        self.attempts_made = 0
        self._last_attempt_ts: float | None = None
        self._last_attempt_score: float = 0.0

    def should_attempt(self, *, confirmed: bool, now: float,
                       best_score: float) -> bool:
        if not confirmed or self.attempts_made >= self.max_attempts:
            return False
        if best_score <= 0.0:
            return False
        if self.attempts_made == 0:
            return True
        assert self._last_attempt_ts is not None
        if (now - self._last_attempt_ts) < self.min_retry_gap_s:
            return False
        return best_score >= self._last_attempt_score * self.improve_factor

    def note_attempt(self, *, now: float, score: float) -> None:
        self.attempts_made += 1
        self._last_attempt_ts = now
        self._last_attempt_score = max(self._last_attempt_score, score)

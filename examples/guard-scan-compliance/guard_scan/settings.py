# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every number the screening logic can be tuned by, in one place.

The prototype took these as command-line flags. An operator does not
have a command line, so they arrive from the app's config form instead
and land here — same names, so the logic and its tests did not have to
change to gain a UI.

Anything that measures time is in SECONDS. The prototype counted frames,
which quietly made the same rule stricter on a slow machine than a fast
one: "three frames of dwell" was 0.4s on the box it was written on and
0.1s on a faster one, so a guard who passed at the client's site failed
on the demo laptop.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass
class ScanSettings:
    """Thresholds for one camera's screening logic."""

    # ── what counts as a step ──
    #: How much time the wand must spend on a surface for it to count.
    #: Cumulative across the screening, not one unbroken hold: a wand
    #: being swept is never still, and on real footage each pass over
    #: the torso is a third of a second at a time.
    dwell_s: float = 0.6
    #: How fast that progress drains while the wand is elsewhere, as a
    #: fraction of real time. 1.0 is "forget it as fast as it was
    #: earned", which is what made a genuine pass score zero; 0 never
    #: forgets, which would let a wand travelling past credit a surface
    #: it only crossed.
    dwell_decay: float = 0.25
    #: How long a done step survives without being seen again. A step
    #: used to latch for good, so a wrist that clipped the torso once
    #: credited "front" for the whole screening.
    step_hold_s: float = 0.0

    # ── who is being scanned ──
    #: How close the wand must be, in shoulder-widths.
    reach_dist: float = 2.2
    #: How squarely the arm must point at them (cosine).
    reach_cos: float = 0.45
    near_guard: float = 5.0
    #: Seconds of quiet before a screening is ruled on.
    session_gap: float = 8.0
    exit_grace: float = 3.0
    settle: float = 2.0
    handover: float = 5.0
    max_session: float = 240.0
    rescan_lock: float = 20.0
    scanned_lock: float = 1.5
    #: Floors that keep passers-by out of the record: a real screening
    #: on entrance footage ran tens of seconds of wand-on-person, while
    #: someone walking past collected a fraction of one.
    min_screen: float = 3.0
    min_engaged: int = 6
    #: How much wand-on-person time is still consistent with "nobody
    #: scanned them". Above this the wand WAS on them and we merely saw
    #: too little to credit a surface — a fragment, not an unscanned
    #: entry, and reporting it as one accuses a guard who did the job.
    no_scan_engaged: float = 1.0

    # ── identity ──
    dup_iou: float = 0.6
    subject_reid: float = 4.0
    subject_reid_dist: float = 1.6
    guard_min_reach: float = 0.20
    guard_min_frames: int = 25
    guard_window: float = 20.0
    guard_margin: float = 1.25
    guard_hold: int = 12
    guard_reid: int = 5
    guard_reid_after: float = 0.5

    # ── the scanner's light ──
    led_ratio: float = 0.08
    led_hits: int = 3
    led_window_s: float = 0.8

    # ── rules/site come from config, not files ──
    site: str | None = None
    rules: str | None = None
    order_weight: float | None = None
    require: str | None = None

    @classmethod
    def from_config(cls, config: dict) -> "ScanSettings":
        """Build from the app's config dict, ignoring anything else in
        it — the config form carries zones and procedure too."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (config or {}).items()
                      if k in known and v is not None})

    def as_dict(self) -> dict:
        return asdict(self)

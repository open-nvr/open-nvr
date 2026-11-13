# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frame capture time — monotonic frame stamps as wall clock.

Frames carry ``Frame.ts``, a ``time.monotonic()`` reading taken when the
frame was fully decoded (``frame_source``). Monotonic is the right clock
for the pipeline's own gaps and gates — it cannot jump when NTP steps
the wall clock — but it is meaningless outside this process, so nothing
downstream could ever say WHEN a plate was read.

Everything downstream stamped its own arrival instead: the visit poster,
the attempt poster, KAI-C's publish, the app's alert. Each of those is a
processing time, so the operator-visible "when was this car here" drifted
with OCR backlog, and the same read showed a different time on every
page.

This converts a monotonic reading to wall clock by measuring its AGE:

    wall = time.time() - (time.monotonic() - mono_ts)

Deliberately not a stored anchor captured at stream start. An anchor's
error is the wall-clock drift accumulated since it was taken (unbounded
over a long uptime, and stepped by every NTP correction); this form's
error is only the drift over the frame's age, which is milliseconds. It
also needs no re-anchoring when ffmpeg restarts.

What this is NOT: the moment light hit the sensor. It excludes the
camera's own exposure/encode delay and the network hop, and includes our
decode buffer — so it lands within the decode pipeline of true capture,
not on it. That is deliberate: the camera's own clock is the only source
for true capture time, and IP camera clocks drift freely and are rarely
NTP-synced, so trusting them would put each camera's private idea of
time into the evidence store.
"""
from __future__ import annotations

import time


def capture_wall(mono_ts: float, *, _mono=time.monotonic, _wall=time.time) -> float:
    """Wall-clock seconds for a ``time.monotonic()`` frame stamp.

    The clocks are injectable for tests only; production always reads the
    two real clocks back to back.
    """
    return _wall() - (_mono() - float(mono_ts))

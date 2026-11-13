# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Watching for the wand's red indicator.

The hand scanner says "something metal" with a red light and a beep.
The beep needs a microphone nobody has wired yet, so this reads the
light.

Two decisions carry the whole thing:

**Look only where the wand is.** A jewellery showroom is full of red —
velvet trays, festival decoration, a red sari. Searching the frame for
red would alarm on the furniture. The only red that means anything is
red in the guard's scanning hand, so that is the only place looked at.

**Require it to persist, in TIME.** A single bright frame is a
reflection off a glass counter. The old rule was "3 of the last 6
frames", which quietly meant a third of a second on one machine and a
tenth on another; this one is "N sightings inside a window of seconds",
which means the same thing everywhere.
"""
from __future__ import annotations

import logging
from collections import deque

log = logging.getLogger("guard_scan.led")

#: Default HSV bands for "lit red". Red wraps the hue circle, so it
#: takes two. The saturation and value floors are what keep dull red
#: cloth out: an LED is bright and saturated, a sari is neither.
DEFAULT_LOW = (0, 130, 130)
DEFAULT_LOW_HI = (10, 255, 255)
DEFAULT_HIGH = (170, 130, 130)
DEFAULT_HIGH_HI = (180, 255, 255)


class RedLightWatch:
    """Is the wand's indicator lit, right now, in this hand?

    ``ratio`` is how much of the patch around the wrist must be lit red;
    ``hits`` inside ``window_s`` is how stubborn that has to be before
    it counts as the scanner speaking rather than a glint.
    """

    def __init__(self, *, ratio: float = 0.08, hits: int = 3,
                 window_s: float = 0.8, bands=None) -> None:
        self.ratio = float(ratio)
        self.hits = int(hits)
        self.window_s = float(window_s)
        self.bands = bands
        self._seen: deque[float] = deque(maxlen=64)
        self.last_ratio = 0.0

    def reset(self) -> None:
        """Forget what was seen — used when a new person steps up, so
        one person's flag cannot spill onto the next."""
        self._seen.clear()
        self.last_ratio = 0.0

    def update(self, frame, wrist, scale: float, now: float) -> bool:
        """Look at this frame; True when the light has been on long
        enough to believe it."""
        import cv2
        import numpy as np

        if wrist is None or frame is None:
            self.last_ratio = 0.0
            return self._decide(now)

        half = max(int(scale * 0.45), 14)
        h, w = frame.shape[:2]
        x1 = max(0, int(wrist[0] - half))
        y1 = max(0, int(wrist[1] - half))
        x2 = min(w, int(wrist[0] + half))
        y2 = min(h, int(wrist[1] + half))
        if x2 - x1 < 6 or y2 - y1 < 6:
            self.last_ratio = 0.0
            return self._decide(now)

        patch = frame[y1:y2, x1:x2]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        low_a, low_b, high_a, high_b = self.bands or (
            DEFAULT_LOW, DEFAULT_LOW_HI, DEFAULT_HIGH, DEFAULT_HIGH_HI)
        mask = cv2.bitwise_or(cv2.inRange(hsv, low_a, low_b),
                              cv2.inRange(hsv, high_a, high_b))
        self.last_ratio = float(np.count_nonzero(mask)) / mask.size
        if self.last_ratio >= self.ratio:
            self._seen.append(now)
        return self._decide(now)

    def _decide(self, now: float) -> bool:
        while self._seen and now - self._seen[0] > self.window_s:
            self._seen.popleft()
        return len(self._seen) >= self.hits

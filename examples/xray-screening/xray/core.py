# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What is on a baggage X-ray screen, and when a new bag arrives.

Knows nothing about the platform: it takes BGR frames and hands back
scans. ``ScanWatcher`` decides when the screen shows a new, settled
image; ``analyse`` says what that image holds.

Dual-energy scanners colour what the beam passes through: orange is
organic, green is mixed, blue is metal, and near-black is anything it
could not get through at all. Gold is the densest thing likely to be in
a tray, so on these screens it is near-black and solid. The screening
here flags such shapes for a person to look at. It never says "gold":
a lead-lined pouch or a steel weight looks the same, and an operator who
learns to trust "gold" will be told "no gold" by a tray it cannot read.

Every size is a fraction of the scan's height squared, not of pixels or
of the image: the height of a scan is the scanner's belt, the same on
every bag, while its width depends on how many trays went through
together. So a ring is the same size on a one-tray scan and a two-tray
one, and on a 576 px export or a 1080 p capture of the screen.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class XraySettings:
    """Thresholds, every one a fraction rather than pixels (see above)."""
    # Background: the scanner's empty field is white.
    bg_value: int = 225
    bg_saturation: int = 30
    # "The beam did not get through": dark whatever the hue.
    dense_value: int = 70
    # A dense shape this big (of height^2) is flagged outright.
    large_dense: float = 0.0012
    # Smaller dense shapes are flagged only when compact and very dark:
    # the overlapping walls of a food tin or a tool's edge are long thin
    # strips, a ring, coin or small bar is not.
    small_dense: float = 0.00036
    small_solidity: float = 0.75
    small_elongation: float = 2.2
    small_darkness: int = 50
    # Share of the scan that is metal and electronics before the tray is
    # too cluttered to clear on screen.
    clutter_metal: float = 0.45
    # A scan wider than this (width / height) is two trays side by side.
    two_tray_aspect: float = 0.8

    @classmethod
    def from_config(cls, cfg: dict) -> "XraySettings":
        out = cls()
        for name in out.__dataclass_fields__:
            if name in (cfg or {}) and cfg[name] is not None:
                setattr(out, name, type(getattr(out, name))(cfg[name]))
        return out


@dataclass
class DenseObject:
    box: tuple[int, int, int, int]        # x, y, w, h in frame pixels
    size: float                           # area / height^2
    solidity: float
    elongation: float
    darkness: int
    large: bool


@dataclass
class Scan:
    """One bag, as the screen showed it."""
    trays: int
    materials: dict[str, float]           # organic / mixed / metal / dense
    objects: list[DenseObject]
    flags: list[dict] = field(default_factory=list)
    box: tuple[int, int, int, int] | None = None   # the scan on screen

    @property
    def verdict(self) -> str:
        return "review" if self.flags else "clear"

    @property
    def severity(self) -> str | None:
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        if not self.flags:
            return None
        return max((f["severity"] for f in self.flags), key=order.__getitem__)


def foreground(bgr, s: XraySettings, area=None):
    """Everything that is not the scanner's white field, inside ``area``."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    fg = ~((hsv[..., 2] > s.bg_value) & (hsv[..., 1] < s.bg_saturation))
    if area is not None:
        fg &= area
    return hsv, fg


def analyse(bgr, s: XraySettings | None = None, area=None) -> Scan | None:
    """What one settled scan holds. None for an empty screen."""
    s = s or XraySettings()
    hsv, fg = foreground(bgr, s, area)
    ys, xs = np.where(fg)
    if len(xs) < 0.01 * fg.size:
        return None                         # the belt is empty
    x0, x1 = int(np.percentile(xs, 0.5)), int(np.percentile(xs, 99.5))
    y0, y1 = int(np.percentile(ys, 0.5)), int(np.percentile(ys, 99.5))
    height = max(y1 - y0, 1)
    unit = float(height * height)

    h, sat, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    n = max(int(fg.sum()), 1)
    colour = sat >= 40
    materials = {
        "organic": float(((h <= 28) & colour & fg).sum()) / n,
        "mixed": float(((h > 28) & (h <= 85) & colour & fg).sum()) / n,
        "metal": float(((h > 85) & (h <= 135) & colour & fg).sum()) / n,
    }
    dark = ((v < s.dense_value) & fg).astype(np.uint8)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    materials["dense"] = float(dark.sum()) / n

    objects = []
    contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        a = cv2.contourArea(c)
        size = a / unit
        if size < s.small_dense:
            continue
        hull = cv2.contourArea(cv2.convexHull(c)) or 1.0
        bx, by, bw, bh = cv2.boundingRect(c)
        inside = np.zeros(v.shape, np.uint8)
        cv2.drawContours(inside, [c], -1, 1, -1)
        objects.append(DenseObject(
            box=(int(bx), int(by), int(bw), int(bh)), size=size,
            solidity=round(a / hull, 2),
            elongation=round(max(bw, bh) / max(min(bw, bh), 1), 1),
            darkness=int(np.mean(v[inside > 0])),
            large=size >= s.large_dense))

    scan = Scan(trays=2 if (x1 - x0) / height > s.two_tray_aspect else 1,
                materials=materials, objects=objects,
                box=(x0, y0, x1 - x0, height))
    scan.flags = judge(scan, s)
    return scan


def is_small_item(o: DenseObject, s: XraySettings) -> bool:
    return (not o.large and o.solidity >= s.small_solidity
            and o.elongation <= s.small_elongation and o.darkness < s.small_darkness)


def judge(scan: Scan, s: XraySettings) -> list[dict]:
    flags = []
    large = [o for o in scan.objects if o.large]
    small = [o for o in scan.objects if is_small_item(o, s)]
    if large:
        flags.append(dict(
            kind="dense_object", severity="high",
            title="Dense object the X-ray cannot see through",
            detail="%d solid near-black shape%s. Precious metal looks like this. "
                   "Open the tray and check." % (len(large), _s(large))))
    if small:
        flags.append(dict(
            kind="small_dense_item", severity="medium",
            title="Small dense item",
            detail="%d small, solid, very dense shape%s, about jewellery size. "
                   "Look before clearing." % (len(small), _s(small))))
    if scan.materials["metal"] >= s.clutter_metal:
        flags.append(dict(
            kind="cluttered_tray", severity="medium",
            title="Too cluttered to clear on screen",
            detail="%.0f%% of the scan is metal and electronics; something small "
                   "could hide in it. Hand search." % (100 * scan.materials["metal"])))
    return flags


def _s(items) -> str:
    return "" if len(items) == 1 else "s"


def annotate(bgr, scan: Scan, s: XraySettings | None = None):
    """The scan with its flagged shapes boxed: red for dense objects,
    orange for small dense items."""
    s = s or XraySettings()
    out = bgr.copy()
    for o in scan.objects:
        x, y, w, h = o.box
        if o.large:
            cv2.rectangle(out, (x - 6, y - 6), (x + w + 6, y + h + 6), (0, 0, 230), 3)
        elif is_small_item(o, s):
            cv2.rectangle(out, (x - 5, y - 5), (x + w + 5, y + h + 5), (0, 140, 255), 2)
    return out


# ── when is there a new bag on screen ──────────────────────────────────

def thumbnail(bgr):
    """The scan small and grey, which is what change and repeat checks
    compare. Mean absolute difference between two of these, on 0-255:
    measured on real scans, two DIFFERENT bags (even two near-empty
    trays) differ by 0.8 or more, while a scan held on screen differs from
    itself by 0.0 frame to frame and by about 0.5 from its own source
    image after H.264."""
    return cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (96, 54),
                      interpolation=cv2.INTER_AREA).astype(np.float32)


def difference(a, b) -> float:
    return float(np.mean(np.abs(a - b)))


class ScanWatcher:
    """Turns a screen recording into one event per bag.

    The screen holds a scan, changes, and holds the next one. A scan is
    taken once the picture has settled for ``settle_s`` AFTER a change,
    so a bag scrolling onto the screen is not screened half-drawn, and
    one that sits there is screened once. Two bags that look nearly the
    same (empty trays) are still two bags: any change at all counts,
    because the screen does not change by itself.

    ``move_threshold`` is the change that counts. 0.3 suits a clean
    digital feed; a noisy capture card may need more.
    """

    def __init__(self, settle_s: float = 0.6, move_threshold: float = 0.3) -> None:
        self.settle_s = settle_s
        self.move_threshold = move_threshold
        self._prev = None
        self._still_since: float | None = None
        self._changed = True            # the first settled picture counts

    def feed(self, bgr, t: float) -> bool:
        """True when ``bgr`` is a new, settled scan to analyse."""
        thumb = thumbnail(bgr)
        moving = (self._prev is not None and
                  difference(thumb, self._prev) > self.move_threshold)
        self._prev = thumb
        if moving:
            self._changed = True
            self._still_since = None
            return False
        if self._still_since is None:
            self._still_since = t
        if not self._changed or t - self._still_since < self.settle_s:
            return False
        self._changed = False
        return True


class RepeatFilter:
    """Remembers recent scans, so a looping recording (or an operator
    pulling the same image up again) is counted once per window. A real
    belt never shows the same bag twice, so on a live scanner this never
    fires."""

    def __init__(self, window_s: float = 3600.0, threshold: float = 0.3) -> None:
        self.window_s = window_s
        self.threshold = threshold
        self._seen: list[tuple[float, np.ndarray]] = []

    def seen_recently(self, bgr, t: float) -> bool:
        thumb = thumbnail(bgr)
        self._seen = [(when, old) for when, old in self._seen
                      if t - when <= self.window_s]
        for i, (when, old) in enumerate(self._seen):
            if difference(old, thumb) <= self.threshold:
                self._seen[i] = (t, old)
                return True
        self._seen.append((t, thumb))
        return False


def scan_id(bgr) -> str:
    """A short stable id for one scan image, for alert de-duplication."""
    return hashlib.sha1(cv2.resize(bgr, (32, 18)).tobytes()).hexdigest()[:12]

# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The X-ray analysis, on synthetic scans.

No scanner images ship with this repo, so every scan here is drawn: a
white field, a pale orange tray, and whatever the test needs in it. The
colours follow the dual-energy convention the analysis reads: orange
organic, green mixed, blue metal, near-black for what the beam could not
get through.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xray.core import (  # noqa: E402
    RepeatFilter, ScanWatcher, XraySettings, analyse,
)

ORANGE = (90, 170, 245)      # BGR, organic
BLUE = (200, 110, 40)        # metal
BLACK = (20, 20, 20)         # dense


def tray(width=300, height=576, canvas=(720, 1280)):
    """One empty tray on the scanner's white field, centred."""
    img = np.full((*canvas, 3), 255, np.uint8)
    y0 = (canvas[0] - height) // 2
    x0 = (canvas[1] - width) // 2
    cv2.rectangle(img, (x0, y0), (x0 + width, y0 + height), (205, 225, 250), -1)
    cv2.rectangle(img, (x0, y0), (x0 + width, y0 + height), ORANGE, 6)
    return img, (x0, y0)


def test_an_empty_tray_is_clear():
    img, _ = tray()
    scan = analyse(img)
    assert scan is not None
    assert scan.verdict == "clear"
    assert scan.trays == 1


def test_an_empty_screen_is_not_a_scan():
    assert analyse(np.full((720, 1280, 3), 255, np.uint8)) is None


def test_a_solid_dense_block_is_flagged_high():
    img, (x, y) = tray()
    cv2.rectangle(img, (x + 100, y + 300), (x + 125, y + 330), BLACK, -1)  # 750 px
    scan = analyse(img)
    kinds = [f["kind"] for f in scan.flags]
    assert "dense_object" in kinds
    assert scan.severity == "high"


def test_a_ring_sized_dense_item_is_flagged_medium():
    img, (x, y) = tray()
    cv2.circle(img, (x + 150, y + 200), 8, BLACK, -1)              # ~200 px, compact
    scan = analyse(img)
    assert [f["kind"] for f in scan.flags] == ["small_dense_item"]
    assert scan.severity == "medium"


def test_a_thin_dark_strip_is_not_flagged():
    """The overlapping walls of a food tin, or a tool's edge: dark, but a
    long thin line rather than a compact shape. These were the false
    flags on real scans before elongation was checked."""
    img, (x, y) = tray()
    cv2.rectangle(img, (x + 100, y + 200), (x + 106, y + 260), BLACK, -1)
    assert analyse(img).flags == []


def test_metal_clutter_is_flagged():
    img, (x, y) = tray()
    cv2.rectangle(img, (x + 10, y + 10), (x + 290, y + 566), BLUE, -1)
    scan = analyse(img)
    assert "cluttered_tray" in [f["kind"] for f in scan.flags]


def test_a_phone_is_metal_but_not_flagged():
    img, (x, y) = tray()
    cv2.rectangle(img, (x + 60, y + 100), (x + 140, y + 250), BLUE, -1)
    scan = analyse(img)
    assert scan.materials["metal"] > 0.05
    assert scan.flags == []


def test_two_trays_side_by_side_count_as_two():
    img = np.full((720, 1280, 3), 255, np.uint8)
    for x0 in (340, 650):
        cv2.rectangle(img, (x0, 72), (x0 + 290, 648), ORANGE, 6)
    assert analyse(img).trays == 2


def test_sizes_do_not_depend_on_resolution():
    """The same scan exported small and captured large must be judged
    the same: sizes are fractions of the scan height squared."""
    img, (x, y) = tray()
    cv2.circle(img, (x + 150, y + 200), 8, BLACK, -1)
    small = cv2.resize(img, (640, 360), interpolation=cv2.INTER_AREA)
    assert [f["kind"] for f in analyse(img).flags] == \
           [f["kind"] for f in analyse(small).flags]


def test_the_scan_area_limits_what_is_read():
    img, (x, y) = tray()
    cv2.rectangle(img, (20, 20), (80, 80), BLACK, -1)   # a logo in the corner
    area = np.zeros(img.shape[:2], bool)
    area[100:700, 200:1100] = True
    assert analyse(img, area=area).flags == []
    assert analyse(img).flags != []


def test_settings_come_from_config():
    s = XraySettings.from_config({"large_dense": "0.002", "dense_value": 60,
                                  "unknown": 1})
    assert s.large_dense == 0.002 and s.dense_value == 60


# ── one event per bag ─────────────────────────────────────────────────

def _bag(n):
    img, (x, y) = tray(width=260 + 10 * n)
    cv2.rectangle(img, (x + 20 + n, y + 40), (x + 80 + n, y + 120), BLUE, -1)
    return img


def test_a_held_scan_is_taken_once_after_it_settles():
    w = ScanWatcher(settle_s=0.6)
    bag = _bag(1)
    taken = [w.feed(bag, t / 10) for t in range(30)]
    assert taken.count(True) == 1
    assert taken.index(True) >= 6          # not before it has settled


def test_each_new_bag_is_taken_once_even_when_they_look_alike():
    """Near-empty trays differ by under one grey level on average, which
    is still a different bag: the screen does not change by itself."""
    w = ScanWatcher(settle_s=0.5)
    bags = [_bag(0), _bag(1), _bag(2)]
    t, taken = 0.0, 0
    for bag in bags:
        for _ in range(15):
            taken += w.feed(bag, t)
            t += 0.1
    assert taken == 3


def test_a_replayed_scan_is_counted_once():
    rep = RepeatFilter(window_s=60)
    a, b = _bag(1), _bag(5)
    assert rep.seen_recently(a, 0) is False
    assert rep.seen_recently(b, 1) is False
    assert rep.seen_recently(a, 30) is True        # the loop came round
    assert rep.seen_recently(a, 200) is False      # outside the window

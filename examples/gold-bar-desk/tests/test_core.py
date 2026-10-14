# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The desk rules, on synthetic frames.

No desk footage ships with this repo. Every frame here is drawn: a black
tray, gold rectangles for bars, a skin-coloured block for an arm. The
segmenter is a stub that returns the bars the test drew and can see — so
these tests pin down the RULES (what counts, what alarms, what clears),
not the model.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bar_desk.core import DeskMonitor, DeskSettings, Zones  # noqa: E402

W, H = 960, 540
GOLD = (40, 190, 230)        # BGR: hue ~24, well saturated, bright
SKIN = (60, 80, 120)         # an arm: darker and less gold than a bar
TRAY = (200, 160, 600, 460)  # x0, y0, x1, y1
BAR_W, BAR_H = 30, 64


def rect(x0, y0, x1, y1):
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float32)


def zones(**extra):
    return Zones(tray=rect(*TRAY), shape=(H, W),
                 balance=rect(40, 160, 160, 260),
                 display=rect(40, 280, 120, 320),
                 handling=rect(0, 330, 200, 540),
                 body=rect(0, 460, 200, 540), **extra)


def slots(n):
    """Positions for n bars in rows inside the tray."""
    out = []
    for i in range(n):
        col, row = i % 10, i // 10
        out.append((TRAY[0] + 10 + col * (BAR_W + 8), TRAY[1] + 10 + row * (BAR_H + 12)))
    return out


class Scene:
    """Draws a desk and tells the stub segmenter what is visible."""

    def __init__(self, n: int):
        self.bars = slots(n)
        self.hidden: set[int] = set()       # bar indexes under an arm
        self.arm = None                     # (x0, y0, x1, y1)
        self.on_pan = False
        self.in_hand = None                 # (x, y) of a held bar
        self.dark = False

    def frame(self):
        img = np.full((H, W, 3), 235, np.uint8)            # bright desk/wall
        x0, y0, x1, y1 = TRAY
        cv2.rectangle(img, (x0, y0), (x1, y1), (20, 20, 20), -1)
        for x, y in self.bars:
            cv2.rectangle(img, (x, y), (x + BAR_W, y + BAR_H), GOLD, -1)
        if self.on_pan:
            cv2.rectangle(img, (70, 180), (70 + BAR_W, 180 + BAR_H), GOLD, -1)
        if self.in_hand:
            x, y = self.in_hand
            cv2.rectangle(img, (x, y), (x + BAR_W, y + BAR_H), GOLD, -1)
        if self.arm:
            a = self.arm
            cv2.rectangle(img, (a[0], a[1]), (a[2], a[3]), SKIN, -1)
        if self.dark:
            img[:] = 5
        return img

    def segment(self, crop):
        x0, y0 = TRAY[0], TRAY[1]
        return [(x - x0, y - y0, BAR_W, BAR_H) for i, (x, y) in enumerate(self.bars)
                if i not in self.hidden and not self.dark]


def monitor(scene, bars_at_start=0, **settings):
    events = []
    s = DeskSettings(**{"learn_s": 3.0, "missing_after_s": 10.0, "extra_after_s": 10.0,
                        "missing_min_frames": 10,
                        "default_bar_area": BAR_W * BAR_H / (W * H), **settings})
    mon = DeskMonitor(scene.segment, zones(), s, events.append, bars_at_start=bars_at_start)
    return mon, events


def run(mon, scene, start, seconds, step=0.5):
    t = start
    while t < start + seconds:
        mon.analyse(scene.frame(), t)
        t += step
    return t


def alerts(events, kind=None):
    return [e for e in events if e["type"] == "alert" and (kind is None or e["kind"] == kind)]


def test_counts_every_bar_and_stays_quiet():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    run(mon, scene, 0, 30)
    assert mon.last["bars"] == 12
    assert alerts(events) == []


def test_a_bar_under_an_arm_stays_counted():
    """An arm over a bar hides it from the segmenter, but its spot is not
    bare tray, so it stays on the count."""
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 5)
    x, y = scene.bars[3]
    scene.hidden = {3}
    scene.arm = (x - 5, y - 5, x + BAR_W + 5, y + BAR_H + 5)
    run(mon, scene, t, 20)
    assert mon.last["bars"] == 12
    assert alerts(events, "bar_missing") == []


def test_one_bar_in_hand_for_a_while_is_not_an_alarm_if_it_comes_back():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 5)
    held = scene.bars.pop()                         # picked up...
    t = run(mon, scene, t, 8)                       # ...out of sight for 8 s
    scene.bars.append(held)                         # ...and back
    run(mon, scene, t, 10)
    assert alerts(events, "bar_missing") == []


def test_a_bar_that_never_comes_back_raises_one_alarm():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 5)
    scene.bars.pop()
    run(mon, scene, t, 30)
    missing = alerts(events, "bar_missing")
    assert len(missing) == 1
    assert missing[0]["severity"] == "high"
    assert "up to 1 bar unaccounted" in missing[0]["detail"]
    assert mon.count_alarm == "missing"


def test_a_slow_camera_waits_for_enough_looks_before_alarming():
    """Above the minimum rate but slower than intended: the time window
    alone is then too few looks, so the alarm waits for enough of them."""
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12, missing_min_frames=15)
    t = run(mon, scene, 0, 5, step=0.9)
    scene.bars.pop()
    t = run(mon, scene, t, 10.5, step=0.9)          # past 10 s, but ~12 looks of 15
    early = len(alerts(events, "bar_missing"))
    run(mon, scene, t, 20, step=0.9)
    assert early == 0
    assert len(alerts(events, "bar_missing")) == 1


def test_a_starved_camera_pauses_the_missing_alarm_and_says_so():
    """At one look every 2.5 s the app cannot tell a hidden bar from a
    missing one: no alarm, however long, and it says it is too slow.
    When speed recovers, a full window of good looks raises it."""
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 10, step=2.5)
    scene.bars.pop()
    t = run(mon, scene, t, 120, step=2.5)
    assert alerts(events, "bar_missing") == []
    assert mon.too_slow is True
    run(mon, scene, t, 40, step=0.5)
    assert mon.too_slow is False
    assert len(alerts(events, "bar_missing")) == 1


def test_the_alarm_clears_only_on_the_full_count_held():
    """A moved bar briefly reads as one too many; that must not clear
    an alarm for a bar that is really gone. Three full frames running do."""
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 5)
    held = scene.bars.pop()
    t = run(mon, scene, t, 15)
    assert mon.count_alarm == "missing"
    scene.bars.append(held)
    mon.analyse(scene.frame(), t)
    mon.analyse(scene.frame(), t + 0.5)
    assert mon.count_alarm == "missing"             # two is not enough
    mon.analyse(scene.frame(), t + 1.0)
    assert mon.count_alarm is None


def test_a_moved_bar_does_not_clear_a_missing_alarm():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 5)
    scene.bars.pop()                                 # really gone
    t = run(mon, scene, t, 15)
    assert mon.count_alarm == "missing"
    x, y = scene.bars[0]
    scene.bars[0] = (x, y + 200)                     # another bar moved along
    t = run(mon, scene, t, 5)
    assert mon.count_alarm == "missing"
    assert len(alerts(events, "bar_missing")) == 1


def test_a_bar_on_the_balance_is_on_the_desk_and_is_a_weighing():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 3)
    held = scene.bars.pop()
    scene.on_pan = True
    t = run(mon, scene, t, 4)
    assert mon.last["bars"] == 12
    scene.on_pan = False
    scene.bars.append(held)
    run(mon, scene, t, 4)
    weighings = [e for e in events if e["type"] == "weighing"]
    assert len(weighings) == 1
    assert weighings[0]["displays"]                 # the display was photographed
    assert alerts(events) == []


def test_a_bar_held_in_the_handling_zone_is_counted_in_hand():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 3)
    scene.bars.pop()
    scene.in_hand = (60, 360)                        # handling zone, above the body zone
    run(mon, scene, t, 15)
    assert mon.last["in_hand"] == 1
    assert mon.last["bars"] == 12
    assert alerts(events, "bar_missing") == []
    assert alerts(events, "bar_near_body") == []


def test_a_bar_held_against_the_body_is_logged_once_per_cooldown():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12, near_body_cooldown_s=60.0)
    t = run(mon, scene, 0, 3)
    scene.bars.pop()
    scene.in_hand = (60, 470)                        # inside the body zone
    run(mon, scene, t, 20)
    near = alerts(events, "bar_near_body")
    assert len(near) == 1
    assert near[0]["box"] is not None


def test_a_bar_near_the_body_is_logged_even_when_looks_are_slow():
    """At one look every 2.5 s a two-second event is seen once; that one
    sighting must still be logged."""
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 5, step=2.5)
    scene.bars.pop()
    scene.in_hand = (60, 470)
    mon.analyse(scene.frame(), t)                    # seen once
    assert len(alerts(events, "bar_near_body")) == 1


def test_one_fast_glimpse_near_the_body_is_not_logged():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 5)                        # 2 fps
    held = scene.bars.pop()
    scene.in_hand = (60, 470)
    mon.analyse(scene.frame(), t)                    # one frame, half a second
    scene.in_hand = None
    scene.bars.append(held)
    run(mon, scene, t + 0.5, 5)
    assert alerts(events, "bar_near_body") == []


def test_an_extra_bar_is_flagged():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=11)
    run(mon, scene, 0, 20)
    assert [e["kind"] for e in alerts(events)] == ["bar_extra"]


def test_the_start_count_is_learned_when_not_given():
    scene = Scene(12)
    mon, events = monitor(scene)
    run(mon, scene, 0, 5)
    assert mon.baseline == 12


def test_learning_takes_the_highest_count_not_the_typical_one():
    """Occlusion only hides bars: a desk where a bar is hidden half the
    time still holds all of them."""
    scene = Scene(12)
    mon, events = monitor(scene)
    for i in range(10):
        scene.hidden = {0} if i % 2 else set()
        x, y = scene.bars[0]
        scene.arm = (x - 5, y - 5, x + BAR_W + 5, y + BAR_H + 5) if i % 2 else None
        mon.analyse(scene.frame(), i * 0.5)
    assert mon.baseline == 12


def test_a_dark_camera_is_tamper():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 3)
    scene.dark = True
    run(mon, scene, t, 5)
    assert "camera_tamper" in [e["kind"] for e in alerts(events)]


def test_recount_starts_the_shift_again():
    scene = Scene(12)
    mon, events = monitor(scene, bars_at_start=12)
    t = run(mon, scene, 0, 5)
    scene.bars.pop()
    t = run(mon, scene, t, 15)
    assert mon.count_alarm == "missing"
    mon.recount()
    run(mon, scene, t, 5)
    assert mon.count_alarm is None
    assert mon.baseline == 11


@pytest.mark.parametrize("value", [0.0012, "0.0012"])
def test_settings_come_from_config(value):
    s = DeskSettings.from_config({"default_bar_area": value, "unknown": 3})
    assert s.default_bar_area == pytest.approx(0.0012)

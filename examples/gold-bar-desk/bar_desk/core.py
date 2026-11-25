# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Counting bars on a weighing desk, and noticing when one goes missing.

Knows nothing about the platform: frames in, events out. Bars are found
by a segmenter handed in from outside (FastSAM in the app, a stub in the
tests); everything here is rules over the zones the operator drew.

The rule that matters, and why it is shaped the way it is
---------------------------------------------------------
A worker trimming bars always has one in hand, on the balance, or under
an arm. So the count on screen is nearly always one short, and an alarm
on "the count is low" would ring all day. But hands and arms only ever
HIDE bars, they never add them. So the question asked is not "is the
count low now" but **"has every bar been seen at once, at least once, in
the last N seconds?"** A bar that is really gone never shows again; a bar
under an arm does as soon as the arm moves.

Every size is relative to the bars themselves (their median box area on
this camera), so nothing depends on resolution or how far the camera
sits from the desk.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np


@dataclass
class DeskSettings:
    missing_after_s: float = 25.0     # full count unseen this long -> alarm
    # ...AND over at least this many analysed frames. The full count shows
    # only in brief moments between arms; a starved CPU looking twice as
    # rarely misses more of them, and on a busy test box (0.4 fps instead
    # of 2) the time rule alone raised a false alarm on footage where
    # nothing went missing. 50 is 25 s at the intended 2 fps.
    missing_min_frames: float = 50
    # Below this many analysed frames a second, the missing-bar alarm is
    # PAUSED, not guessed. The full count shows only in brief moments, and
    # at 0.3 fps (a loaded test box) the app missed so many of them that
    # it raised a false alarm on footage where nothing went missing, even
    # with the frame minimum. Saying "too slow to verify" is honest; a
    # false high alarm on a gold desk is not.
    min_analysis_fps: float = 1.0
    extra_after_s: float = 25.0       # more bars than the start count this long
    learn_s: float = 30.0             # start count learned over this, when not given
    smooth_s: float = 3.0             # the displayed count is a median over this
    near_body_s: float = 1.0
    near_body_cooldown_s: float = 60.0
    tamper_s: float = 2.0
    # A bar not seen for this long has its spot checked: bare tray means
    # it has gone. Short, because a bar MOVED leaves its old track behind
    # until then and the desk reads one too many; a bar under an arm is
    # protected by its spot not being bare, not by this wait.
    hold_s: float = 0.5
    # An alarm clears only on the full count in this many frames running.
    clear_frames: float = 3
    # Gold-coloured pixels, as a fraction of one bar's area, that count as:
    hand_bar: float = 0.2             # a bar held in the handling zone
    body_bar: float = 0.2             # a bar close to the worker's body
    hidden_bar: float = 0.3           # part of a bar showing past an arm
    pan_fraction: float = 0.02        # of the balance zone: a bar on the pan
    default_bar_area: float = 0.0012  # of the frame, until bars have been seen

    @classmethod
    def from_config(cls, cfg: dict) -> "DeskSettings":
        out = cls()
        for name in out.__dataclass_fields__:
            value = (cfg or {}).get(name)
            if value is not None:
                setattr(out, name, float(value))
        return out


@dataclass
class Zones:
    """The operator's drawing, in pixels of the current frame."""
    tray: np.ndarray                      # polygon, required
    balance: np.ndarray | None = None
    display: np.ndarray | None = None
    handling: np.ndarray | None = None
    body: np.ndarray | None = None
    shape: tuple[int, int] = (0, 0)      # frame (h, w) these were built for
    _masks: dict = field(default_factory=dict, repr=False)

    def mask(self, name: str):
        """Boolean mask of a zone at this frame size (cached), or None."""
        poly = getattr(self, name)
        if poly is None or len(poly) < 3:
            return None
        if name not in self._masks:
            m = np.zeros(self.shape, np.uint8)
            cv2.fillPoly(m, [poly.astype(np.int32)], 1)
            self._masks[name] = m.astype(bool)
        return self._masks[name]

    def rect(self, name: str):
        poly = getattr(self, name)
        if poly is None or len(poly) < 3:
            return None
        x, y, w, h = cv2.boundingRect(poly.astype(np.int32))
        H, W = self.shape
        x0, y0 = max(0, x), max(0, y)
        return x0, y0, min(W, x + w), min(H, y + h)


def gold_mask(bgr, s_min=70, v_min=110):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    return (h >= 12) & (h <= 38) & (s >= s_min) & (v >= v_min)


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter or 1)


class BarTracker:
    """Keeps a bar on the count until its spot is seen empty.

    A hand over a tray hides bars, so "not detected" is not "gone". A
    bar is dropped only once the place it sat shows bare, dark tray — or
    when a freshly seen bar sits on top of it (put back a little off its
    old spot).
    """

    def __init__(self, hold_s: float = 0.5, confirm_hits: int = 2) -> None:
        self.tracks: dict[int, dict] = {}
        self.next_id = 1
        self.hold_s = hold_s
        self.confirm_hits = confirm_hits

    def update(self, boxes, frame, t: float) -> None:
        widths = [w for _, _, w, _ in boxes] or [40]
        match_px = 0.6 * float(np.median(widths))
        cents = [(x + w / 2, y + h / 2) for x, y, w, h in boxes]
        unmatched = set(range(len(boxes)))
        for tr in sorted(self.tracks.values(), key=lambda r: -r["hits"]):
            best, bd = None, match_px
            for i in unmatched:
                d = float(np.hypot(cents[i][0] - tr["c"][0], cents[i][1] - tr["c"][1]))
                if d < bd:
                    best, bd = i, d
            if best is not None:
                unmatched.discard(best)
                tr.update(c=cents[best], box=boxes[best], seen=t, hits=tr["hits"] + 1)
        for i in unmatched:
            self.tracks[self.next_id] = dict(c=cents[i], box=boxes[i], seen=t, hits=1)
            self.next_id += 1

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        for tid in list(self.tracks):
            tr = self.tracks[tid]
            if tr["seen"] == t:
                continue
            if tr["hits"] < self.confirm_hits:
                if t - tr["seen"] > 1.0:
                    del self.tracks[tid]            # a one-frame ghost
                continue
            if t - tr["seen"] < self.hold_s:
                continue
            x, y, w, h = tr["box"]
            patch = hsv[y + h // 4:y + 3 * h // 4, x + w // 4:x + 3 * w // 4]
            if patch.size and (patch[..., 2] < 75).mean() > 0.6:
                del self.tracks[tid]                # bare tray: really gone
                continue
            for oid, o in self.tracks.items():
                if oid != tid and o["seen"] == t and iou(o["box"], tr["box"]) > 0.25:
                    del self.tracks[tid]            # put back slightly off its spot
                    break

    def confirmed(self) -> dict[int, dict]:
        return {k: v for k, v in self.tracks.items() if v["hits"] >= self.confirm_hits}


class DeskMonitor:
    """One desk camera's rules.

    ``segment(crop) -> [(x, y, w, h), ...]`` finds bars in a crop of the
    tray zone. ``on_event(record)`` receives alerts and weighings as
    dicts with a ``type`` of ``"alert"`` or ``"weighing"``.
    """

    def __init__(self, segment: Callable, zones: Zones, settings: DeskSettings,
                 on_event: Callable[[dict], None], bars_at_start: int = 0) -> None:
        self.segment = segment
        self.zones = zones
        self.s = settings
        self.on_event = on_event
        self.tracker = BarTracker(hold_s=settings.hold_s)
        self.baseline: int | None = bars_at_start or None
        self.given_baseline = bool(bars_at_start)
        self.started_at: float | None = None
        self._learned: list[int] = []
        self._totals: deque = deque()            # (t, total), smoothing window
        self._recent: deque = deque()            # (t, total), alarm window
        self.full_seen_at: float | None = None
        self.frames_since_full = 0
        self.high_since: float | None = None
        self.count_alarm: str | None = None      # "missing" | "extra" | None
        self._pan_votes: deque = deque(maxlen=3)
        self.on_pan = False
        self.weighing: dict | None = None
        self.weighings = 0
        self.body_since: float | None = None
        self.body_alarm_at = -1e18
        self.ref_thumb = None
        self.tamper_since: float | None = None
        self.tamper_alarm = False
        self.bar_area: float | None = None
        self._prev_t: float | None = None
        self.frame_gap = 0.0                     # seconds since the previous look
        self._gaps: deque = deque(maxlen=10)
        self.too_slow = False                    # missing-bar alarm paused
        self.last: dict = {}

    # ── helpers ──

    def unit(self) -> float:
        """One bar's area in pixels on this camera."""
        if self.bar_area:
            return self.bar_area
        h, w = self.zones.shape
        return self.s.default_bar_area * h * w

    def recount(self) -> None:
        """Start the shift's count again from now (the start_shift action)."""
        self.baseline = None
        self.given_baseline = False
        self.started_at = None
        self._learned.clear()
        self._recent.clear()
        self.full_seen_at = None
        self.count_alarm = None
        self.high_since = None

    def set_baseline(self, bars: int) -> None:
        if bars and bars > 0:
            if bars != self.baseline:
                self.count_alarm = None
                self.full_seen_at = None
            self.baseline = int(bars)
            self.given_baseline = True

    def _alert(self, kind: str, severity: str, title: str, detail: str, t: float,
               frame, box=None) -> None:
        self.on_event({"type": "alert", "kind": kind, "severity": severity,
                       "title": title, "detail": detail, "t": t,
                       "frame": frame, "box": box})

    # ── one analysed frame ──

    def analyse(self, frame, t: float) -> dict:
        z = self.zones
        if self.started_at is None:
            self.started_at = t
        self.frame_gap = 0.0 if self._prev_t is None else max(0.0, t - self._prev_t)
        self._prev_t = t
        if self.frame_gap > 0:
            self._gaps.append(self.frame_gap)
        self._check_tamper(frame, t)

        # Bars in the trays.
        x0, y0, x1, y1 = z.rect("tray")
        tray_mask = z.mask("tray")
        boxes = []
        for bx, by, bw, bh in self.segment(frame[y0:y1, x0:x1]):
            gx, gy = bx + x0, by + y0
            cx, cy = int(gx + bw / 2), int(gy + bh / 2)
            if 0 <= cy < tray_mask.shape[0] and 0 <= cx < tray_mask.shape[1] \
                    and tray_mask[cy, cx]:
                boxes.append((int(gx), int(gy), int(bw), int(bh)))
        if boxes:
            area = float(np.median([w * h for _, _, w, h in boxes]))
            self.bar_area = area if self.bar_area is None else 0.9 * self.bar_area + 0.1 * area
        self.tracker.update(boxes, frame, t)
        tracks = self.tracker.confirmed()

        gold = gold_mask(frame)
        gold_open = cv2.morphologyEx(gold.astype(np.uint8), cv2.MORPH_OPEN,
                                     np.ones((5, 5), np.uint8)) > 0
        unit = self.unit()

        hidden, hidden_boxes = self._hidden_bars(frame, tracks, unit)

        # A bar on the precision balance: 2 of the last 3 frames agree.
        pan_mask = z.mask("balance")
        if pan_mask is not None:
            pan = gold_mask(frame, 60, 100) & pan_mask
            self._pan_votes.append(bool(pan.sum() > self.s.pan_fraction * pan_mask.sum()))
        # Plain Python values from here on: these end up in alert evidence,
        # and a numpy bool there made the alert unserialisable (it was lost).
        on_pan = bool(sum(self._pan_votes) >= 2)

        # Bars held off the trays and the balance.
        in_hand = 0
        hand_mask = z.mask("handling")
        if hand_mask is not None:
            px = int((gold_open & hand_mask).sum())
            if px > self.s.hand_bar * unit:
                in_hand = 2 if px > 1.6 * unit else 1

        total = len(tracks) + hidden + int(on_pan) + in_hand
        self._totals.append((t, total))
        while self._totals and t - self._totals[0][0] > self.s.smooth_s:
            self._totals.popleft()
        smooth = int(np.median([n for _, n in self._totals]))

        self._update_baseline(total, t)
        self._count_rules(total, smooth, t, frame)
        self._weighing(on_pan, t, frame)
        self._near_body(gold_open, unit, t, frame)

        self.last = dict(t=t, tracks=tracks, boxes=boxes, hidden_boxes=hidden_boxes,
                         in_trays=len(tracks) + hidden, on_pan=on_pan,
                         in_hand=in_hand, total=total, bars=smooth)
        return self.last

    def _hidden_bars(self, frame, tracks, unit):
        """Bars too covered by an arm for the segmenter to outline: bright
        gold inside the trays that no tracked bar accounts for, counted in
        whole-bar units. Arms are far darker than polished gold, so they
        do not count."""
        x0, y0, x1, y1 = self.zones.rect("tray")
        g = gold_mask(frame[y0:y1, x0:x1], 50, 120).astype(np.uint8)
        g &= self.zones.mask("tray")[y0:y1, x0:x1].astype(np.uint8)
        for tr in tracks.values():
            x, y, w, h = tr["box"]
            cv2.rectangle(g, (x - x0 - 4, y - y0 - 4), (x - x0 + w + 4, y - y0 + h + 4), 0, -1)
        g = cv2.morphologyEx(g, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
        n, _, st, _ = cv2.connectedComponentsWithStats(g)
        count, boxes = 0, []
        bar_unit = 0.8 * unit                     # bright gold share of a bar
        for x, y, w, h, a in st[1:]:
            if a < self.s.hidden_bar * bar_unit:
                continue
            count += max(1, int(round(a / bar_unit)))
            boxes.append((int(x + x0), int(y + y0), int(w), int(h)))
        return count, boxes

    def _update_baseline(self, total: int, t: float) -> None:
        if self.baseline is not None:
            return
        # Learning: occlusion only hides bars, so the start count is the
        # highest count seen in two frames running, not the typical one.
        self._learned.append(total)
        if t - self.started_at >= self.s.learn_s and len(self._learned) >= 2:
            pairs = [min(a, b) for a, b in zip(self._learned, self._learned[1:])]
            self.baseline = max(pairs)

    def analysis_fps(self) -> float | None:
        """Frames analysed per second, over the last ten."""
        if len(self._gaps) < 3:
            return None
        return len(self._gaps) / sum(self._gaps)

    def _count_rules(self, total: int, smooth: int, t: float, frame) -> None:
        if self.baseline is None:
            return
        fps = self.analysis_fps()
        self.too_slow = fps is not None and fps < self.s.min_analysis_fps
        if self.too_slow and self.count_alarm != "missing":
            # Too few looks to tell "hidden" from "gone". Start the window
            # again from now, so that when speed recovers it needs a full
            # window of good looks rather than inheriting sparse ones.
            self.full_seen_at = t
            self.frames_since_full = 0
        if self.full_seen_at is None or total >= self.baseline:
            self.full_seen_at = t
            self.frames_since_full = 0
        else:
            self.frames_since_full += 1
        self._recent.append((t, total))
        while self._recent and t - self._recent[0][0] > self.s.missing_after_s:
            self._recent.popleft()
        unseen = t - self.full_seen_at
        if unseen >= self.s.missing_after_s and \
                self.frames_since_full >= self.s.missing_min_frames and \
                self.count_alarm != "missing":
            self.count_alarm = "missing"
            since = [n for ts, n in self._recent if ts > self.full_seen_at] or [total]
            best = max(since)
            gone = self.baseline - best
            self._alert("bar_missing", "high", "Possible missing bar",
                        "Not all %d bars have been seen at once for %d s; the most "
                        "seen since was %d (in the trays, on the balance and in "
                        "hand), so up to %d bar%s unaccounted for." % (
                            self.baseline, unseen, best, gone,
                            "" if gone == 1 else "s"), t, frame)
        if smooth > self.baseline:
            self.high_since = self.high_since if self.high_since is not None else t
            if t - self.high_since >= self.s.extra_after_s and self.count_alarm != "extra":
                self.count_alarm = "extra"
                self._alert("bar_extra", "medium", "Extra bar on the desk",
                            "%d bars now, %d at the start: a bar was added, possibly "
                            "swapped for a lookalike." % (smooth, self.baseline), t, frame)
        else:
            self.high_since = None
        # Once raised, it clears only on the full count in several analysed
        # frames running. One or two frames of an extra count happen every
        # time a bar is moved (its old spot is checked a moment later),
        # and must not clear an alarm for a bar that is really gone.
        k = max(2, int(self.s.clear_frames))
        last = [n for _, n in list(self._recent)[-k:]]
        if self.count_alarm == "missing" and len(last) == k and \
                all(n >= self.baseline for n in last):
            self.count_alarm = None
        elif self.count_alarm == "extra" and smooth <= self.baseline:
            self.count_alarm = None

    def _weighing(self, on_pan: bool, t: float, frame) -> None:
        if on_pan and not self.on_pan:
            self.weighings += 1
            self.weighing = {"n": self.weighings, "start": t, "displays": []}
        if on_pan and self.weighing is not None:
            # The reading, once it has had a second to settle, and again
            # before the bar comes off: before and after a trim.
            if t - self.weighing["start"] >= 1.0 and len(self.weighing["displays"]) < 2:
                crop = self._display_crop(frame)
                if crop is not None:
                    self.weighing["displays"].append(crop)
        if not on_pan and self.on_pan and self.weighing is not None:
            w = self.weighing
            self.on_event({"type": "weighing", "n": w["n"], "t": w["start"],
                           "duration_s": round(t - w["start"], 1),
                           "displays": w["displays"]})
            self.weighing = None
        self.on_pan = on_pan

    def _display_crop(self, frame):
        r = self.zones.rect("display")
        if r is None:
            return None
        x0, y0, x1, y1 = r
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        return cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

    def _near_body(self, gold, unit: float, t: float, frame) -> None:
        body = self.zones.mask("body")
        if body is None:
            return
        hit = gold & body
        if hit.sum() > self.s.body_bar * unit:
            self.body_since = self.body_since if self.body_since is not None else t
            # How long it has been there: since the first look that saw it,
            # plus the gap since the look before. At 2 fps that needs two
            # looks half a second apart. On a starved box looking every
            # 2 s, the real event (a bar held up for 2 s) is seen ONCE, and
            # "held for a second" could never be satisfied — measured on a
            # loaded test box, this alert stopped firing at all.
            held = t - self.body_since + self.frame_gap
            # A small tolerance: frame times are not exact multiples.
            if held >= self.s.near_body_s - 0.05 and \
                    t - self.body_alarm_at >= self.s.near_body_cooldown_s:
                self.body_alarm_at = t
                ys, xs = np.where(hit)
                box = (int(xs.min()), int(ys.min()),
                       int(xs.max() - xs.min()), int(ys.max() - ys.min()))
                self._alert("bar_near_body", "medium", "Bar held away from the trays",
                            "A bar was held close to the worker, outside the trays and "
                            "the balance, for %.0f s. Worth a look." % max(held, 1.0),
                            t, frame, box)
        else:
            self.body_since = None

    def _check_tamper(self, frame, t: float) -> None:
        thumb = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 36),
                           interpolation=cv2.INTER_AREA).astype(np.float32)
        if self.ref_thumb is None:
            self.ref_thumb = thumb
        dark = thumb.mean() < 25
        # A person covers a fraction of the picture; a moved or covered
        # camera changes nearly all of it.
        moved = (np.abs(thumb - self.ref_thumb) > 40).mean() > 0.6
        if dark or moved:
            self.tamper_since = self.tamper_since if self.tamper_since is not None else t
            if t - self.tamper_since >= self.s.tamper_s and not self.tamper_alarm:
                self.tamper_alarm = True
                self._alert("camera_tamper", "high", "Desk camera blocked or moved",
                            "The view of the desk went dark or changed completely "
                            "for over %d s." % self.s.tamper_s, t, frame)
        else:
            self.tamper_since = None
            if self.tamper_alarm:
                self.tamper_alarm = False
                self.ref_thumb = thumb
            else:
                # Follow slow light changes through the day.
                self.ref_thumb = 0.98 * self.ref_thumb + 0.02 * thumb


def draw(frame, mon: DeskMonitor):
    """The frame with what the monitor sees on it, for evidence photos."""
    img = frame.copy()
    last = mon.last or {}
    for tr in (last.get("tracks") or {}).values():
        x, y, w, h = tr["box"]
        fresh = tr["seen"] == last.get("t")
        cv2.rectangle(img, (x, y), (x + w, y + h), (60, 220, 60) if fresh else (0, 200, 255), 2)
    for x, y, w, h in last.get("hidden_boxes") or []:
        cv2.rectangle(img, (x, y), (x + w, y + h), (255, 120, 255), 2)
    for name, colour in (("balance", (0, 200, 255)), ("body", (90, 90, 200))):
        poly = getattr(mon.zones, name)
        if poly is not None and len(poly) >= 3:
            cv2.polylines(img, [poly.astype(np.int32)], True, colour, 2)
    return img

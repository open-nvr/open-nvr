# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entry-screening logic: who is the guard, and did they scan this person.

Ported from the standalone prototype this app grew out of, and kept
close to it on purpose — the parts that look fussy are the parts that
were wrong first:

* the guard is whoever REACHES toward people, not whoever stands still
  longest (measured on real entrance footage: 61% reach rate for the
  guard, 3% for anyone else);
* one customer is one screening even when the tracker renames them
  three times in two minutes, which it does;
* a person who merely passes near the guard is not a skipped scan.

What changed in the port: everything is measured in SECONDS rather than
frames (the same rule meant different things on different hardware), the
scan zone is in unit coordinates rather than one camera's pixels, a
step can expire instead of latching for ever, and results leave through
sinks instead of being written to files.

Nothing here imports the platform. It takes decoded frames with
keypoints and hands back screenings, which is what lets the whole thing
be tested on a synthetic clock with no camera and no model.
"""
from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from .led import RedLightWatch

log = logging.getLogger("guard_scan.core")

#: The unit coordinate space zone polygons are drawn and stored in —
#: the platform's own convention, so a zone drawn once is right on every
#: resolution the camera might be reconfigured to.
UNIT_FRAME = 1000

# COCO keypoint indices, as the pose adapter emits them.
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
L_SHO, R_SHO, L_ELB, R_ELB, L_WRI, R_WRI = 5, 6, 7, 8, 9, 10
L_HIP, R_HIP = 11, 12
L_ANK, R_ANK = 15, 16

#: Verdict -> the alert kind the manifest declares and the catalog
#: documents. A verdict is how a screening SCORED; an alert kind is what
#: went wrong, and "partial" and "incomplete" are the same problem to
#: whoever is triaging the inbox — the guard did not scan the person
#: properly. Publishing the verdict raw is how the inbox came to hold
#: `partial` and `incomplete` while the manifest promised `improper_scan`,
#: so filtering by the documented type matched nothing.
ALERT_KIND = {
    "partial": "improper_scan",
    "incomplete": "improper_scan",
    "no_scan": "no_scan",
}

STEPS = ("left_arm", "right_arm", "front", "back")
STEP_LABEL = {
    "left_arm": "Left arm",
    "right_arm": "Right arm",
    "front": "Front",
    "back": "Back",
}


def _iso(ts):
    """A wall-clock stamp as ISO-8601 UTC."""
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(
        timespec="seconds")


def _pt(kps, idx, min_conf):
    """Keypoint idx as (x, y), or None when the model isn't confident."""
    x, y, c = kps[idx]
    return (float(x), float(y)) if c >= min_conf else None


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0.0:
        return 0.0
    union = ((a[2] - a[0]) * (a[3] - a[1])
             + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter / union if union > 0 else 0.0


def _mid(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _seg_dist(point, shape):
    """Distance from a point to a region's shape.

    A shape is either a point (the old circles, still used when the
    keypoints are too sparse to draw a segment) or a pair of points
    meaning the segment between them.
    """
    if not isinstance(shape[0], (tuple, list)):
        return _dist(point, shape)
    a, b = shape
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if span <= 1e-6:
        return _dist(point, a)
    # How far along the segment the closest point lies, clamped to it.
    t = max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / span))
    return _dist(point, (ax + t * dx, ay + t * dy))


def region_point(shape):
    """A point squarely on a region — its middle.

    Regions are segments now, so "where is this region" is no longer a
    single coordinate. Tests and overlays want one anyway.
    """
    if isinstance(shape[0], (tuple, list)):
        return _mid(shape[0], shape[1])
    return shape


def _limb(shoulder, elbow, wrist):
    """The line a limb occupies, from whichever joints were found."""
    ends = [p for p in (shoulder, elbow, wrist) if p is not None]
    if not ends:
        return None
    if len(ends) == 1:
        return ends[0]
    return (ends[0], ends[-1])


def _first(*points):
    for p in points:
        if p is not None:
            return p
    return None


class Body:
    """One person in one frame: keypoints plus the bits we reason about."""

    def __init__(self, track_id, box, kps, min_conf):
        self.track_id = track_id
        self.box = box  # x1, y1, x2, y2
        self.kps = kps
        self.min_conf = min_conf

        self.l_sho = _pt(kps, L_SHO, min_conf)
        self.r_sho = _pt(kps, R_SHO, min_conf)
        self.l_hip = _pt(kps, L_HIP, min_conf)
        self.r_hip = _pt(kps, R_HIP, min_conf)
        self.l_wri = _pt(kps, L_WRI, min_conf)
        self.r_wri = _pt(kps, R_WRI, min_conf)
        self.l_elb = _pt(kps, L_ELB, min_conf)
        self.r_elb = _pt(kps, R_ELB, min_conf)
        self.l_ank = _pt(kps, L_ANK, min_conf)
        self.r_ank = _pt(kps, R_ANK, min_conf)
        self.l_ank = _pt(kps, L_ANK, min_conf)
        self.r_ank = _pt(kps, R_ANK, min_conf)

        # Shoulder width is the scale everything else is measured in, so
        # the rules hold whether the person is near the camera or far.
        if self.l_sho and self.r_sho:
            self.scale = max(_dist(self.l_sho, self.r_sho), 20.0)
        else:
            self.scale = max((box[2] - box[0]) * 0.4, 20.0)

        shoulders = _first(
            _mid(self.l_sho, self.r_sho) if self.l_sho and self.r_sho else None,
            self.l_sho, self.r_sho,
        )
        hips = _first(
            _mid(self.l_hip, self.r_hip) if self.l_hip and self.r_hip else None,
            self.l_hip, self.r_hip,
        )
        if shoulders and hips:
            self.torso = _mid(shoulders, hips)
        else:
            self.torso = shoulders or hips or (
                (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)

    @property
    def stand_point(self):
        """Where this person is standing: their feet, or failing that the
        bottom of their box."""
        feet = [a for a in (self.l_ank, self.r_ank) if a]
        if feet:
            return (sum(f[0] for f in feet) / len(feet),
                    sum(f[1] for f in feet) / len(feet))
        return ((self.box[0] + self.box[2]) / 2.0, self.box[3])

    @property
    def facing_camera(self):
        """True when we can see the face, i.e. the person's front is to us."""
        face_conf = sum(float(self.kps[i][2]) for i in (NOSE, L_EYE, R_EYE))
        ear_conf = sum(float(self.kps[i][2]) for i in (L_EAR, R_EAR))
        return face_conf >= 0.9 or (face_conf >= 0.5 and face_conf > ear_conf)

    def regions(self):
        """The surfaces a wand has to cover, shaped like the body parts.

        These were three circles — one at each elbow, one at the chest —
        and a wand sweeping a person spends most of its time outside all
        three. Measured on entrance footage: during a real back pass the
        wand was judged ON the person for 14 seconds and inside a region
        for 0.4 of them. The steps then came down to whether a couple of
        frames happened to clip a circle, which is exactly as arbitrary
        as it sounds.

        An arm is a LINE from shoulder to wrist and a torso is the SLAB
        between the shoulders and the hips, so that is what these are:
        segments with a thickness, measured by distance to the segment
        rather than to one point on it.
        """
        out = {}
        # Shoulder to wrist, so the whole arm counts — but it sits
        # alongside the body region, and "nearest wins" keeps a sweep
        # down the chest from being credited as an arm.
        left = _limb(self.l_sho, self.l_elb, self.l_wri)
        right = _limb(self.r_sho, self.r_elb, self.r_wri)
        if left:
            out["left_arm"] = (left, self.scale * 0.55)
        if right:
            out["right_arm"] = (right, self.scale * 0.55)
        # The body itself: shoulders all the way DOWN, not just the
        # chest. "Front" and "back" mean the whole front and back of a
        # person, and a guard wanding someone spends much of the pass on
        # their legs — measured on entrance footage, the wand was on the
        # person but inside no region for 14 seconds of a single back
        # pass, because the model stopped at the hips. Seen from behind
        # this same shape is their back.
        top = (_mid(self.l_sho, self.r_sho) if self.l_sho and self.r_sho
               else _first(self.l_sho, self.r_sho))
        bottom = _first(
            _mid(self.l_ank, self.r_ank) if self.l_ank and self.r_ank else None,
            self.l_ank, self.r_ank,
            # No ankles in frame: fall back to the bottom of the box,
            # which is where the person's feet are anyway.
            ((self.box[0] + self.box[2]) / 2.0, self.box[3]),
        )
        if top and bottom:
            out["torso"] = ((top, bottom), self.scale * 0.65)
        else:
            out["torso"] = (self.torso, self.scale * 0.85)
        return out

    def head_box(self):
        """A face crop from the head keypoints, with a quality score."""
        pts, conf = [], 0.0
        for i in (NOSE, L_EYE, R_EYE, L_EAR, R_EAR):
            x, y, c = self.kps[i]
            if c >= 0.25:
                pts.append((float(x), float(y)))
                conf += float(c)
        if len(pts) < 2:
            return None, 0.0
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        pad = self.scale * 0.75
        box = (min(xs) - pad, min(ys) - pad * 1.2,
               max(xs) + pad, max(ys) + pad * 1.4)
        score = conf * (box[2] - box[0])  # confident AND large wins
        return box, score


def crop(frame, box, pad_frac=0.0):
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box
    pw, ph = (x2 - x1) * pad_frac, (y2 - y1) * pad_frac
    x1, y1 = max(0, int(x1 - pw)), max(0, int(y1 - ph))
    x2, y2 = min(w, int(x2 + pw)), min(h, int(y2 + ph))
    if x2 - x1 < 10 or y2 - y1 < 10:
        return None
    return frame[y1:y2, x1:x2].copy()


# scanner light -------------------------------------------------------

class SiteConfig:
    """What this particular camera is looking at.

    Kept apart from the scan rules: `rules.json` is the procedure, this
    is the room. Two cues, both optional, both worth far more than any
    behavioural guess when a site can supply them:

    uniform    the colour of the guard's shirt. Measured on the entrance
               footage, the light blue uniform matched in 75-100% of the
               guard's frames and 0.0% of any customer's.
    scan_zone  where the person being scanned stands -- here a raised
               platform, so their feet sit well above the guard's in the
               image. Whoever is standing in it is being screened, and so
               is not the guard. Matched 85-98% for customers, 0% for
               guards.

    With no site file both fall away and the guard is worked out from
    behaviour alone.
    """

    def __init__(self, data=None):
        d = data or {}
        u = d.get("uniform") or {}
        self.uniform_low = np.array(u.get("hsv_low", [0, 0, 0]), dtype=np.uint8)
        self.uniform_high = np.array(u.get("hsv_high", [0, 0, 0]), dtype=np.uint8)
        self.uniform_on = bool(u.get("hsv_low") and u.get("hsv_high"))
        self.uniform_min = float(u.get("min_fraction", 0.45))
        self.uniform_weight = float(u.get("weight", 2.0))
        self.uniform_required = bool(u.get("required", False))
        z = d.get("scan_zone") or {}
        # The polygon arrives in UNIT space (0..UNIT_FRAME), the same
        # coordinates the operator's zone editor draws in, and is scaled
        # to pixels per frame. It used to be stored in the pixels of one
        # particular 848x478 feed, which silently pointed at the wrong
        # part of the room on a camera of any other resolution.
        self._unit_zone = [tuple(pt) for pt in (z.get("polygon") or [])
                           if len(pt) >= 2]
        self.zone_min = float(z.get("min_fraction", 0.5))
        self._zone_cache = {}
        self.zone = None            # set per frame by zone_for()

    def zone_for(self, width, height):
        """The scan zone in this frame's pixels, or None."""
        if len(self._unit_zone) < 3:
            return None
        key = (int(width), int(height))
        hit = self._zone_cache.get(key)
        if hit is None:
            hit = np.array(
                [[int(round(x * width / UNIT_FRAME)),
                  int(round(y * height / UNIT_FRAME))]
                 for x, y in self._unit_zone], dtype=np.int32)
            self._zone_cache[key] = hit
        return hit

    def bind(self, frame):
        """Point ``zone`` at this frame's pixel geometry."""
        if frame is None:
            return
        h, w = frame.shape[:2]
        self.zone = self.zone_for(w, h)

    @classmethod
    def load(cls, path):
        if not path:
            return cls()
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except FileNotFoundError:
            raise SystemExit("site file not found: " + str(path))
        except json.JSONDecodeError as exc:
            raise SystemExit("site file is not valid JSON: " + str(exc))

    @property
    def zone_configured(self) -> bool:
        """Has the site told us where the scanned person stands?"""
        return len(self._unit_zone) >= 3

    @property
    def any_cue(self):
        return self.uniform_on or len(self._unit_zone) >= 3

    def uniform_fraction(self, frame, body):
        """How much of this person's shirt is the uniform colour."""
        if not self.uniform_on or frame is None:
            return None
        h, w = frame.shape[:2]
        cx, cy = body.torso
        r = body.scale * 0.45
        x1, y1 = int(max(0, cx - r)), int(max(0, cy - r * 0.9))
        x2, y2 = int(min(w, cx + r)), int(min(h, cy + r * 0.9))
        if x2 - x1 < 5 or y2 - y1 < 5:
            return None
        hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.uniform_low, self.uniform_high)
        return float(np.count_nonzero(mask)) / mask.size

    def in_scan_zone(self, body):
        if self.zone is None:
            return False
        x, y = body.stand_point
        return cv2.pointPolygonTest(self.zone, (float(x), float(y)), False) >= 0

    def draw(self, frame):
        if self.zone is not None:
            cv2.polylines(frame, [self.zone], True, (90, 130, 90), 1)


# guard ---------------------------------------------------------------

def _points_at(body, wrist, other, min_cos):
    """Is this person's arm pointing at `other`, not just near them?"""
    v1 = (wrist[0] - body.torso[0], wrist[1] - body.torso[1])
    v2 = (other.torso[0] - body.torso[0], other.torso[1] - body.torso[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 < 1e-6 or n2 < 1e-6:
        return True
    return (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2) >= min_cos


def _hand_out(body, other, max_d, min_cos):
    """The hand this person has out at `other`, or None.

    Two tests, both needed. Near their body, measured in *their* shoulder
    widths so it holds at any distance from the camera; and the arm
    actually pointing that way, which is what separates a guard sweeping
    a wand from a customer standing with their arms held out to be
    scanned. Distance to one's own torso is no use here -- a customer in
    that arms-out pose has their wrists as far from their own body as the
    guard does.
    """
    best = None
    for w in (body.l_wri, body.r_wri):
        if w is None:
            continue
        d = _dist(w, other.torso)
        if d > other.scale * max_d:
            continue
        if not _points_at(body, w, other, min_cos):
            continue
        if best is None or d < best[1]:
            best = (w, d)
    return best


def _reaching(body, others, max_d, min_cos):
    """True when this person has a hand out at somebody else."""
    return any(_hand_out(body, o, max_d, min_cos) for o in others)


class GuardPicker:
    """Works out which person is the guard.

    "Whoever has been there longest" is not enough on its own: a customer
    being screened stands there for minutes, which outlasts anyone, and
    when the tracker loses the guard behind somebody and hands back a new
    id the guard's count restarts at zero while the customer keeps theirs.

    Nor is "whoever moves least". Measured on entrance footage the guard
    moves about four times as much as the customer, who is standing still
    on the spot being scanned -- so stillness points at the wrong person.

    What does separate them is reaching: on that same footage a hand was
    out at somebody else in 61% of the guard's frames and 3% of the
    customer's. So that is what the score is built on, with presence only
    to break ties, and nobody is called the guard until they have
    actually reached at someone.

    The choice is sticky: a challenger has to stay ahead by a margin for
    a while before the label moves, so it cannot flicker mid-scan.
    """

    def __init__(self, args, site=None):
        self.args = args
        self.site = site or SiteConfig()
        self.tracks = {}
        self.guard_id = None
        self.manual = None
        self.challenger = None
        self.challenge = 0
        self.lost_anchor = None
        self.lost_at = 0.0
        self.lost_scale = 0.0
        self.last_at = None

    def frames_seen(self, tid):
        st = self.tracks.get(tid)
        return st["frames"] if st else 0

    def update(self, bodies, now, exclude=(), frame=None):
        dt = 0.0 if self.last_at is None else max(now - self.last_at, 0.0)
        self.last_at = now
        keep = 0.5 ** (dt / max(self.args.guard_window, 0.5)) if dt else 1.0

        for st in self.tracks.values():
            st["seen"] *= keep
            st["reach"] *= keep
            st["uni"] *= keep
            st["zone"] *= keep

        for b in bodies:
            st = self.tracks.get(b.track_id)
            if st is None:
                st = {"anchor": b.torso, "drift": 0.0, "seen": 0.0,
                      "reach": 0.0, "uni": 0.0, "zone": 0.0,
                      "frames": 0, "last": now, "scale": b.scale}
                self.tracks[b.track_id] = st
            st["frames"] += 1
            st["last"] = now
            st["seen"] += 1.0
            st["scale"] = 0.9 * st["scale"] + 0.1 * b.scale
            # A slow anchor, and the spread around it, is how much this
            # person actually moves about the frame.
            ax, ay = st["anchor"]
            st["drift"] = 0.95 * st["drift"] + 0.05 * _dist(b.torso, (ax, ay))
            st["anchor"] = (0.98 * ax + 0.02 * b.torso[0],
                            0.98 * ay + 0.02 * b.torso[1])
            if _reaching(b, [o for o in bodies if o.track_id != b.track_id],
                         self.args.reach_dist, self.args.reach_cos):
                st["reach"] += 1.0
            frac = self.site.uniform_fraction(frame, b)
            if frac is not None and frac >= self.site.uniform_min:
                st["uni"] += 1.0
            if self.site.in_scan_zone(b):
                st["zone"] += 1.0

        for tid in [t for t, st in self.tracks.items()
                    if now - st["last"] > 120.0 and t != self.guard_id]:
            self.tracks.pop(tid)

        return self._pick(bodies, now, set(exclude), frame)

    def _rate(self, tid, key):
        st = self.tracks.get(tid)
        return st[key] / max(st["seen"], 1.0) if st else 0.0

    def reach_rate(self, tid):
        return self._rate(tid, "reach")

    def uniform_rate(self, tid):
        return self._rate(tid, "uni")

    def zone_rate(self, tid):
        return self._rate(tid, "zone")

    def eligible(self, tid):
        """Could this person be the guard at all?"""
        if self.zone_rate(tid) >= self.site.zone_min:
            return False          # they are the one being scanned
        if self.site.uniform_on and self.site.uniform_required:
            return self.uniform_rate(tid) >= 0.35
        return True

    def _qualified(self, tid):
        """Enough evidence to hand someone the label."""
        if self.site.uniform_on and self.uniform_rate(tid) >= 0.35:
            return True           # the uniform says so on its own
        return self.reach_rate(tid) >= self.args.guard_min_reach

    def _score(self, body, top_seen):
        st = self.tracks[body.track_id]
        here = st["seen"] / top_seen if top_seen else 0.0
        score = 0.4 * here + 1.6 * self.reach_rate(body.track_id)
        if self.site.uniform_on:
            score += (self.site.uniform_weight
                      * self.uniform_rate(body.track_id))
        return score

    def _inherit(self, old_id, new_id):
        """Carry the guard's standing over when the tracker renames them.

        Everything here is about continuity of identity. The site cues
        are deliberately left out: what colour a shirt is, and whether
        somebody is stood on the platform, are facts about the body in
        the picture, not about the id it was given. Copying them across
        would let a re-id hand the customer the guard's uniform history
        and blind the very check meant to catch that.
        """
        old = self.tracks.get(old_id)
        if old is None:
            return
        new = self.tracks[new_id]
        # Re-base the cues onto the inherited history rather than copying
        # or diluting them, so the new body keeps its own uniform and
        # zone *rates* while taking on the old id's length of standing.
        rates = {k: new[k] / max(new["seen"], 1e-6) for k in ("uni", "zone")}
        for key in ("seen", "reach", "drift", "anchor", "scale"):
            new[key] = old[key]
        for k, rate in rates.items():
            new[k] = rate * new["seen"]
        new["frames"] = max(new["frames"], old["frames"])

    def _cue_ok(self, frame, body):
        """Do the site cues allow this person to be the guard, on this
        frame alone? Used when adopting a new track id, where there is no
        history to lean on yet."""
        if self.site.in_scan_zone(body):
            return False
        frac = self.site.uniform_fraction(frame, body)
        return frac is None or frac >= self.site.uniform_min

    def _pick(self, bodies, now, exclude=(), frame=None):
        visible = {b.track_id: b for b in bodies}
        # The person being scanned is not the one doing the scanning --
        # whether we worked that out from the wand or from the platform
        # they are standing on.
        pool = [b for b in bodies
                if b.track_id not in exclude and self.eligible(b.track_id)]

        # The g key wins, until that person has been gone a good while.
        if self.manual is not None:
            last = self.tracks.get(self.manual, {}).get("last", 0.0)
            if self.manual in visible or now - last < 10.0:
                self.guard_id = self.manual
                return self.guard_id
            self.manual = None

        # When the uniform says which one is the guard there is nothing
        # to work out: take it, without waiting out the re-id window. The
        # wait is only there to stop a one-frame tracker flicker handing
        # the label to a stray part-detection, and a shirt that matches
        # and feet off the platform is not a stray part-detection.
        if (self.site.uniform_on and frame is not None
                and self.guard_id not in visible):
            wearing = [b for b in pool
                       if (self.site.uniform_fraction(frame, b) or 0.0)
                       >= self.site.uniform_min]
            if len(wearing) == 1:
                b = wearing[0]
                if b.track_id != self.guard_id:
                    if self.guard_id is not None:
                        self._inherit(self.guard_id, b.track_id)
                    if self.frames_seen(b.track_id) >= 3:
                        self.guard_id = b.track_id
                        self.lost_anchor = None
                        self.challenger, self.challenge = None, 0
                if self.guard_id == b.track_id:
                    return self.guard_id

        if self.guard_id is not None and self.guard_id not in visible:
            # The guard went out of view. Remember the spot, so when the
            # tracker gives them a fresh id we can join it back up.
            st = self.tracks.get(self.guard_id)
            if st is not None and self.lost_anchor is None:
                self.lost_anchor, self.lost_at = st["anchor"], now
                self.lost_scale = st.get("scale", 0.0)
            gone = now - self.lost_at
            if gone > self.args.guard_reid:
                # Gone too long to still hold the label. Drop it and
                # elect again, rather than stay stuck on a dead track.
                log.info("guard track %s lost", self.guard_id)
                self.guard_id = None
                self.lost_anchor = None
                self.challenger, self.challenge = None, 0
            elif gone >= self.args.guard_reid_after and self.lost_anchor:
                # Same spot, about the same size, and not a track that
                # has been around all along -- otherwise a stray
                # part-detection nearby gets adopted as the guard.
                fresh = [b for b in pool
                         if _dist(b.torso, self.lost_anchor) < b.scale * 2.5
                         and self.tracks[b.track_id]["frames"] <= 8
                         and (not self.lost_scale
                              or 0.65 < b.scale / self.lost_scale < 1.55)
                         and self._cue_ok(frame, b)]
                if fresh:
                    b = min(fresh,
                            key=lambda c: _dist(c.torso, self.lost_anchor))
                    self._inherit(self.guard_id, b.track_id)
                    self.guard_id = b.track_id
                    self.lost_anchor = None
                    self.challenger, self.challenge = None, 0
                    return self.guard_id
            # Still within the grace window: keep the label where it is.
        elif self.guard_id in visible:
            self.lost_anchor = None

        if not pool:
            return self.guard_id

        top_seen = max(self.tracks[b.track_id]["seen"] for b in pool)
        scores = {b.track_id: self._score(b, top_seen) for b in pool}
        best = max(scores, key=scores.get)
        if self.guard_id is not None and not self.eligible(self.guard_id):
            self.guard_id = None
        if self.guard_id in exclude:
            self.guard_id = None

        if self.guard_id is None:
            # Nobody is the guard until they have actually reached at
            # someone -- unless the uniform already says who they are,
            # which needs far less watching to be sure of.
            need = self.args.guard_min_frames
            if self.site.uniform_on and self.uniform_rate(best) >= 0.6:
                need = min(need, 8)
            if self.frames_seen(best) >= need and self._qualified(best):
                self.guard_id = best
                log.info("guard is track %s", best)
            return self.guard_id

        if self.guard_id is None:
            return None
        if best == self.guard_id or self.guard_id not in visible:
            self.challenger, self.challenge = None, 0
            return self.guard_id

        # Someone scores higher -- but they have to hold that lead before
        # the label moves, so it cannot flicker in the middle of a scan.
        if (scores[best] > scores[self.guard_id] * self.args.guard_margin
                and self._qualified(best)
                and self.frames_seen(best) >= self.args.guard_min_frames):
            self.challenge = self.challenge + 1 if best == self.challenger else 1
            self.challenger = best
            if self.challenge >= self.args.guard_hold:
                log.info("guard track %s -> %s", self.guard_id, best)
                self.guard_id = best
                self.challenger, self.challenge = None, 0
        else:
            self.challenger, self.challenge = None, 0
        return self.guard_id


# rules ---------------------------------------------------------------

def _lcs(a, b):
    """How much of `a` runs in the same relative order as `b`."""
    m = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            m[i + 1][j + 1] = (m[i][j] + 1 if x == y
                               else max(m[i][j + 1], m[i + 1][j]))
    return m[-1][-1]


class ConfigError(ValueError):
    """The operator's configuration cannot be honoured.

    A real exception rather than SystemExit, because this is raised
    while BUILDING an engine from config the operator just saved — on a
    per-camera worker thread. SystemExit derives from BaseException, so
    `except Exception` does not catch it: a single typo in the procedure
    (``back_arm`` for ``back``) killed that camera's thread outright,
    with no log line, no health change and no screening on that
    entrance, while the app went on reporting itself well.
    """


class ScanRules:
    """What counts as a proper scan, and how a scan is graded.

    Kept out of the code because it is a site decision, not a technical
    one. One showroom wants all four surfaces in a fixed order; another
    only cares that nothing was missed. Both are expressed here.

    A scan is scored 0-100 and then graded. The score is the share of the
    required surfaces that were covered, optionally blended with how well
    the sequence matched -- `order_weight` 0 ignores the order entirely,
    1.0 makes it count as much as the coverage.
    """

    DEFAULT = {
        "steps": [
            {"name": "left_arm", "weight": 1.0},
            {"name": "right_arm", "weight": 1.0},
            {"name": "front", "weight": 1.0},
            {"name": "back", "weight": 1.0},
        ],
        # The sequence a scan is *expected* to follow. Only scored when
        # order_weight is above 0, which it is not by default: on the
        # entrance footage this was built against, guards covered every
        # surface but worked round whichever side they were standing on.
        "order": ["left_arm", "right_arm", "front", "back"],
        "order_weight": 0.0,
        "grades": [
            {"min": 100, "verdict": "compliant", "severity": None,
             "title": "Scan complete and correct"},
            {"min": 75, "verdict": "partial", "severity": "medium",
             "title": "Partial scan"},
            {"min": 1, "verdict": "incomplete", "severity": "high",
             "title": "Incomplete scan procedure"},
            {"min": 0, "verdict": "no_scan", "severity": "high",
             "title": "Person entered without a scan"},
        ],
    }

    def __init__(self, data=None):
        d = dict(self.DEFAULT)
        d.update(data or {})
        self.steps = [x["name"] for x in d["steps"]]
        self.weights = {x["name"]: float(x.get("weight", 1.0)) for x in d["steps"]}
        self.order = [x for x in d.get("order", self.steps) if x in self.weights]
        self.order_weight = max(0.0, min(1.0, float(d.get("order_weight", 0.0))))
        self.grades = sorted(d["grades"], key=lambda g: -g["min"])
        bad = [x for x in self.steps if x not in STEPS]
        if bad:
            raise ConfigError("unknown surface(s) " + ", ".join(bad)
                              + "; known surfaces are " + ", ".join(STEPS))
        if not self.grades:
            raise ConfigError("the procedure needs at least one grade band")

    @classmethod
    def load(cls, path):
        if not path:
            return cls()
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except ConfigError as exc:
            # Reading a rules file is a command-line act: a clean message
            # beats a traceback. In the app the same error propagates as
            # a ConfigError and is reported on the camera instead.
            raise SystemExit("rules: " + str(exc)) from exc
        except FileNotFoundError:
            raise SystemExit("rules file not found: " + str(path))
        except json.JSONDecodeError as exc:
            raise SystemExit("rules file is not valid JSON: " + str(exc))

    @property
    def pass_mark(self):
        return self.grades[0]["min"]

    def missing(self, done):
        return [STEP_LABEL[x] for x in self.steps if x not in done]

    def score(self, done_order):
        """Grade one scan. `done_order` is the steps, in the order done."""
        done = [x for x in done_order if x in self.weights]
        total = sum(self.weights.values()) or 1.0
        coverage = 100.0 * sum(self.weights[x] for x in set(done)) / total
        if not done:
            order = 100.0
        else:
            order = 100.0 * _lcs(done, self.order) / len(done)
        score = ((1.0 - self.order_weight) * coverage
                 + self.order_weight * order)
        # Nothing at all is "no scan", whatever the weights say.
        grade = self.grades[-1] if not done else next(
            (g for g in self.grades if score >= g["min"]), self.grades[-1])
        return {
            "score": round(score, 1),
            "coverage": round(coverage, 1),
            "order_score": round(order, 1),
            "verdict": grade["verdict"],
            "severity": grade["severity"],
            "title": grade["title"],
            "missing": self.missing(set(done)),
            "in_order": order >= 100.0,
        }


# sessions ------------------------------------------------------------

class ScanSession:
    """One customer being screened: which steps happened, and when."""

    def __init__(self, subject_id, started_at, rules):
        self.rules = rules
        self.id = uuid.uuid4().hex[:10]
        self.subject_id = subject_id
        self.started_at = started_at
        self.last_engaged = started_at    # wrist last on this person
        self.last_present = started_at    # last stood with the guard
        self.last_seen = started_at       # last seen by the tracker at all
        self.done = {}            # step -> timestamp
        self.order = []           # steps in the order they happened
        self.dwell = {}           # step -> seconds the hand has held it
        # When each done step was last actually seen, so a step the hand
        # left long ago can expire instead of latching for good.
        self.seen_at = {}
        self.last_tick = None
        self.in_zone = False      # were they ever on the scanned spot
        self.anchor = None        # where they were standing
        self.scale = 0.0          # and how big they looked
        self.aliases = []         # track ids this screening has had
        self.engaged_s = 0.0      # seconds the wand was actually on them
        self.flagged = False      # scanner light fired
        self.best_face = None     # (score, jpeg)
        self.best_body = None     # (score, jpeg)
        self.guard_face = None    # (score, jpeg) of who scanned them
        self.scene = None
        self.track = []           # keypoint log, for later training
        self.engaged_frames = 0

    def result(self):
        return self.rules.score(self.order)

    @property
    def complete(self):
        """Good enough to stop waiting and rule on it."""
        return self.result()["score"] >= self.rules.pass_mark

    @property
    def in_order(self):
        return self.result()["in_order"]

    def missing(self):
        return self.rules.missing(set(self.done))


# runner --------------------------------------------------------------

class ScanEngine:
    """One camera's screening logic: who is the guard, who is being
    scanned, and how each screening ended.

    Deliberately knows nothing about where frames come from or where
    results go. It is handed decoded frames with their keypoints, and
    hands back screenings and alerts through the sinks it was given —
    which is what makes it testable on a synthetic clock with no camera,
    no model and no platform (see ``tests/``).
    """

    def __init__(self, args, *, site=None, rules=None, camera="cam",
                 on_alert=None, on_screening=None, save_image=None,
                 on_session_log=None):
        self.args = args
        self.camera = camera
        self.source_name = str(camera)
        self.stop = False
        # Sinks. Defaults make the engine inert but harmless, so a test
        # can drive it without wiring anything up.
        self.on_alert = on_alert or (lambda record: None)
        self.on_screening = on_screening or (lambda summary: None)
        self.on_session_log = on_session_log or (lambda log: None)
        # Returns a stored path for one JPEG, or None. In the app this
        # uploads to core's evidence store; a test can keep bytes.
        self.save_image = save_image or (lambda name, jpeg: None)

        self.site = site if site is not None else SiteConfig.load(args.site)
        self.rules = rules if rules is not None else ScanRules.load(args.rules)
        self._apply_order_weight()
        if args.require:
            want = [x.strip() for x in args.require.split(",") if x.strip()]
            bad = [x for x in want if x not in STEPS]
            if bad:
                raise SystemExit("--require: unknown step(s) " + ", ".join(bad))
            self.rules.steps = want
            self.rules.weights = {x: self.rules.weights.get(x, 1.0) for x in want}
            self.rules.order = [x for x in self.rules.order if x in want]
        self.guard = GuardPicker(args, self.site)
        self.guard_id = None
        self.sessions = {}        # subject track id -> ScanSession
        self.light = RedLightWatch(
            ratio=args.led_ratio, hits=args.led_hits,
            window_s=args.led_window_s)
        self.alert_count = 0
        self.finished = {}        # track id -> when we ruled on them
        self.orphans = {}         # screenings whose track id vanished

    def _apply_order_weight(self) -> None:
        """Let the SETTING override the procedure's own order weight.

        `order_weight` reaches the rules by two routes — inside the
        `procedure` object, and as a setting in its own right — and the
        setting wins. Shared by __init__ and `retune` so the two cannot
        drift: a retune that forgot this would install a new procedure
        while silently keeping the old order weight, which is the
        hardest kind of half-applied setting to notice.
        """
        if self.args.order_weight is not None:
            self.rules.order_weight = max(0.0, min(1.0, self.args.order_weight))

    def retune(self, settings, *, site=None, rules=None) -> None:
        """Adopt new configuration without dropping what is in flight.

        An operator saves a setting and expects it to mean something.
        Until this existed the only way a running engine picked up a
        change was to be rebuilt, which happened when the camera's
        stream session ended — and on a healthy camera that is never.

        A screening ALREADY RUNNING is deliberately untouched: a
        ScanSession holds the rules object it was created with, so it is
        graded under the procedure it began under. Somebody half way
        through being wanded is not re-judged by rules that arrived
        while they stood there. The next screening gets the new ones.

        The guard picker keeps its election state — who the guard is and
        the evidence for it — and only changes the thresholds it judges
        by; rebuilding it would restart the election from nothing every
        time anybody pressed Save. The light keeps its rolling window
        for the same reason.

        `args.require` is not re-read: it is a command-line filter with
        no path through the catalog, and honouring it here would let a
        stale CLI flag quietly narrow a procedure the operator just set.
        """
        self.args = settings
        if site is not None:
            self.site = site
        if rules is not None:
            self.rules = rules
        self._apply_order_weight()

        self.guard.args = settings
        self.guard.site = self.site

        self.light.ratio = float(settings.led_ratio)
        self.light.hits = int(settings.led_hits)
        self.light.window_s = float(settings.led_window_s)

    # evidence
    def _remember_evidence(self, session, frame, body):
        head, score = body.head_box()
        if head is not None:
            face = crop(frame, head)
            if face is not None and (session.best_face is None
                                     or score > session.best_face[0]):
                ok, buf = cv2.imencode(".jpg", face,
                                       [cv2.IMWRITE_JPEG_QUALITY, 92])
                if ok:
                    session.best_face = (score, buf.tobytes())
        body_img = crop(frame, body.box, 0.08)
        if body_img is not None:
            area = body_img.shape[0] * body_img.shape[1]
            if session.best_body is None or area > session.best_body[0]:
                ok, buf = cv2.imencode(".jpg", body_img,
                                       [cv2.IMWRITE_JPEG_QUALITY, 88])
                if ok:
                    session.best_body = (area, buf.tobytes())
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            session.scene = buf.tobytes()

    def _remember_guard_face(self, session, frame, guard):
        """Keep the best look at the guard on duty for this screening.

        A procedure alert is about the GUARD, so a record showing only
        the customer asks the manager to take our word for who was
        careless.
        """
        if frame is None:
            return
        head, score = guard.head_box()
        if head is None:
            return
        face = crop(frame, head)
        if face is None or (session.guard_face is not None
                            and score <= session.guard_face[0]):
            return
        ok, buf = cv2.imencode(".jpg", face, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if ok:
            session.guard_face = (score, buf.tobytes())

    def _write_evidence(self, session):
        """Hand every held crop to the sink, and keep what it returns.

        The bytes never travel with the alert itself — an alert is a bus
        message with a payload ceiling, and a couple of crops exceed it,
        at which point the alert is dropped rather than shortened. So
        pictures are stored first and cited by path.
        """
        paths = {}
        held = {
            "face": session.best_face,
            "body": session.best_body,
            "scene": (0, session.scene) if session.scene else None,
            # Who was on duty, not just who was scanned: the procedure
            # alert is ABOUT the guard, and a compliance record that
            # cannot show them is hard to act on.
            "guard_face": session.guard_face,
        }
        for name, value in held.items():
            if not value or not value[1]:
                continue
            stored = self.save_image(f"{session.id}_{name}", value[1])
            if stored:
                paths[name] = stored
        return paths

    def _raise_alert(self, session, kind, severity, title, detail, images=None):
        record = {
            "id": session.id + "-" + kind,
            "kind": kind,
            "severity": severity,
            "title": title,
            "detail": detail,
            "at": _iso(session.last_engaged or session.started_at),
            "subject_track": session.subject_id,
            "steps_done": [STEP_LABEL[s] for s in session.order],
            "steps_missing": session.missing(),
            "score": session.result()["score"],
            "images": self._write_evidence(session) if images is None else images,
        }
        self.alert_count += 1
        log.warning("alert [%s] %s -- %s", severity, title, detail)
        self.on_alert(record)

    def _close_session(self, session, reason="end_of_run", now=None):
        now = time.time() if now is None else now
        res = session.result()
        verdict = res["verdict"]
        # Only rule on people who were actually screened. On entrance
        # footage a real screening ran 38s of wand-on-person; passers-by
        # and staff in the background picked up 0.1-0.4s as the wand
        # swung past them, and used to raise an alert each. A session
        # that never became a screening is dropped with no ruling -- and
        # no lock either, so a real scan later still counts.
        # Where the site has said WHERE a screening happens, someone who
        # never stood there was never being screened — they walked past
        # the guard, which is not a missed scan and must not be reported
        # as one. Without this a bystander who lingered three seconds
        # became "person entered without a scan".
        if self.site.zone_configured and not session.in_zone:
            self.sessions.pop(session.subject_id, None)
            return
        if session.engaged_s < self.args.min_screen and not session.complete:
            self.sessions.pop(session.subject_id, None)
            return
        if verdict == "no_scan" and session.engaged_frames < self.args.min_engaged:
            self.sessions.pop(session.subject_id, None)
            return
        # "They walked in without being scanned" is an accusation, and
        # it only holds when the wand was never on them. If it WAS on
        # them and we simply saw too little to credit a surface, that is
        # a fragment of a screening — on a real clip the same customer
        # appeared briefly before their screening proper, tracked, lost,
        # and tracked again, and those three seconds were published as
        # an unscanned entry while the scan itself was recorded,
        # correctly, moments later.
        if verdict == "no_scan" and session.engaged_s > self.args.no_scan_engaged:
            self.sessions.pop(session.subject_id, None)
            return
        self.finished[session.subject_id] = now
        # Photos for every screening now, not just the ones that alert:
        # the dashboard shows completed scans too, and a clean scan with
        # no picture of who was scanned is not much of a record.
        images = self._write_evidence(session)
        record = {
            "session": session.id,
            "camera": self.camera,
            "subject_track": session.subject_id,
            "verdict": verdict,
            "score": res["score"],
            "coverage": res["coverage"],
            "order_score": res["order_score"],
            "ended_by": reason,
            "track_ids": session.aliases,
            "engaged_frames": session.engaged_frames,
            "engaged_s": round(session.engaged_s, 1),
            "flagged": session.flagged,
            "steps": {k: round(v - session.started_at, 2)
                      for k, v in session.done.items()},
            "order": session.order,
            "duration_s": round(now - session.started_at, 2),
            "source": self.source_name,
            "images": images,
            "keypoints": session.track,  # the training data
        }
        # Keypoints per frame: the training set this collects as it
        # runs. It goes to the sink, which parks it somewhere with its
        # own pruning — NOT the evidence store, whose sweep only ever
        # deletes .jpg and would let these accumulate for ever.
        self.on_session_log(record)

        # A compact line per screening. The session file above carries
        # every keypoint and is far too heavy to read on every dashboard
        # refresh; this is what the dashboard and the reports run on.
        summary = {
            "session": session.id,
            "at": _iso(now),
            "ts": now,
            "source": self.source_name,
            "verdict": verdict,
            "score": res["score"],
            "coverage": res["coverage"],
            "order_score": res["order_score"],
            "severity": res["severity"],
            "title": res["title"],
            "duration_s": round(now - session.started_at, 2),
            "engaged_s": round(session.engaged_s, 1),
            "steps_done": [STEP_LABEL[x] for x in session.order],
            "steps_missing": res["missing"],
            "flagged": session.flagged,
            "ended_by": reason,
            "images": images,
            # The alarm this screening is about to raise, when it raises
            # one. The id is deterministic (session + kind, exactly as
            # _raise_alert builds it), so the ledger carries the link
            # without either side waiting on the other — otherwise a
            # flagged screening and the critical alert describing it sit
            # in two tables with nothing joining them.
            "alert_id": (None if res["severity"] is None
                         else session.id + "-" + ALERT_KIND.get(verdict, verdict)),
        }
        self.on_screening(summary)

        pct = "%d%%" % round(res["score"])
        if res["severity"] is None:
            log.info("session %s: %s %s (%s, %ds)", session.id, verdict,
                     pct, reason, int(now - session.started_at))
        else:
            missing = res["missing"]
            bits = []
            if missing:
                bits.append("Missing: " + ", ".join(missing))
            if not res["in_order"] and self.rules.order_weight > 0:
                bits.append("out of the expected order")
            detail = pct + " of the required scan. " + (
                "; ".join(bits) if bits else "No scanning steps detected")
            self._raise_alert(session, ALERT_KIND.get(verdict, verdict),
                              res["severity"], res["title"], detail, images)
        self.sessions.pop(session.subject_id, None)

    def _being_scanned(self):
        """Track ids we have actually watched the wand go over."""
        return [s.subject_id for s in self.sessions.values()
                if s.engaged_s >= self.args.scanned_lock]

    def _same_person(self, frame, a, b):
        """Are these two boxes really one person seen twice?

        Overlap alone is not enough. In this room the guard stands
        between the camera and the customer for much of a scan, so two
        different people overlap heavily. Before merging them, they have
        to be about the same size and the site cues have to agree -- one
        in uniform and one not is two people, however much they overlap.
        """
        if _iou(a.box, b.box) <= self.args.dup_iou:
            return False
        if b.scale and not (0.7 < a.scale / b.scale < 1.4):
            return False
        if self.site.in_scan_zone(a) != self.site.in_scan_zone(b):
            return False
        fa = self.site.uniform_fraction(frame, a)
        fb = self.site.uniform_fraction(frame, b)
        if fa is not None and fb is not None:
            if (fa >= self.site.uniform_min) != (fb >= self.site.uniform_min):
                return False
        return True

    def _dedupe(self, bodies, frame=None):
        """One person detected twice is one person.

        The tracker sometimes runs two ids over the same body for a
        second or two while it hands over. Left alone that opens a second
        screening for somebody already being scanned. Keep whichever id
        we have known longest, so identity stays put.
        """
        ranked = sorted(bodies, key=lambda b: -self.guard.frames_seen(b.track_id))
        kept = []
        for b in ranked:
            if any(self._same_person(frame, b, k) for k in kept):
                continue
            kept.append(b)
        return kept

    def _duplicate_of_live(self, sub, seen_ids):
        """Is another id already screening this same person?

        Two ids can run over one body for a second or two while the
        tracker hands over, and they do not always overlap enough for
        _dedupe to call it. Whichever id is still being tracked keeps the
        screening; this one is ignored until that id dies, at which point
        _adopt moves the screening across. Without this, a handover opens
        a second screening on somebody already being scanned and rules on
        the half of the scan each one saw.
        """
        for sid, sess in self.sessions.items():
            if sid == sub.track_id or sid not in seen_ids or sess.anchor is None:
                continue
            if _dist(sub.torso, sess.anchor) > sub.scale * self.args.subject_reid_dist:
                continue
            if sess.scale and not (0.7 < sub.scale / sess.scale < 1.45):
                continue
            return True
        return False

    def _adopt(self, sub, now, seen_ids):
        """A new id standing where a screening already is, is that person.

        The tracker renames people mid-screening -- on one 126s clip the
        customer came through as three ids in a row. Each rename used to
        start a fresh screening and rule on the fragment. Both a parked
        screening and a live one whose id has gone quiet this frame are
        fair game; a screening whose id is still being seen is somebody
        else, and is left alone.
        """
        cands = [(old, sess) for old, (sess, _a, _s, when) in self.orphans.items()
                 if now - when <= self.args.subject_reid]
        cands += [(sid, sess) for sid, sess in self.sessions.items()
                  if sid not in seen_ids]

        # THE PLATFORM HOLDS ONE PERSON AT A TIME. Where a site has told
        # us where the scanned person stands — here a raised platform —
        # that is worth more than any guess from position and size: a
        # new id inside it is the person who was being screened there,
        # renamed. Without this, one customer on a busy clip came out as
        # three screenings, each ruled on its fragment: "no scan",
        # "incomplete", and a partial.
        if self.site.in_scan_zone(sub):
            in_zone = [(old, sess) for old, sess in cands if sess.in_zone]
            if in_zone:
                cands = in_zone

        best = None
        zone_match = self.site.in_scan_zone(sub) and any(
            sess.in_zone for _o, sess in cands)
        for old, sess in cands:
            if sess.anchor is None:
                continue
            d = _dist(sub.torso, sess.anchor)
            # Both gates below are stand-ins for identity. When the site
            # has named the spot and both are on it, we have the real
            # thing and the stand-ins only get in the way.
            if not zone_match:
                if d > sub.scale * self.args.subject_reid_dist:
                    continue
                if sess.scale and not (0.7 < sub.scale / sess.scale < 1.45):
                    continue
            elif not sess.in_zone:
                continue
            if best is None or d < best[2]:
                best = (old, sess, d)
        if best is None:
            return None
        old, sess, _d = best
        self.orphans.pop(old, None)
        self.sessions.pop(old, None)
        sess.subject_id = sub.track_id
        if sub.track_id not in sess.aliases:
            sess.aliases.append(sub.track_id)
        sess.last_seen = now
        log.info("person %s is now track %s -- same screening",
                 old, sub.track_id)
        return sess

    def flush(self, now, reason="left"):
        """Rule on everyone still being screened, then start clean.

        Nothing else will: a screening whose person has walked away is
        parked in the orphan hold in case the tracker merely renamed
        them, and that hold is only checked while FRAMES ARE ARRIVING.
        When the feed ends — a clip finishing, a camera going away, the
        app shutting down — the last screening of the day sits in that
        hold, finished and unjudged, and is never heard of again.

        That is not a rare edge: it is the LAST person through the door
        every single time.
        """
        for key in list(self.orphans):
            self._close_session(self.orphans.pop(key)[0], reason, now)
        for session in list(self.sessions.values()):
            self._close_session(session, reason, now)
        self.finished.clear()

    def abandon(self, now, reason="feed_lost"):
        """The feed broke mid-screening.

        A scan we stopped watching must not be ruled incomplete — that
        blames the guard for our outage. But a scan that was already
        FINISHED before the feed died is a real result, and throwing it
        away loses evidence we actually have. So: rule the complete
        ones, drop the rest.
        """
        for holder in (self.sessions, self.orphans):
            for key in list(holder):
                value = holder.pop(key)
                session = value[0] if isinstance(value, tuple) else value
                if session.complete:
                    self._close_session(session, "complete", now)
                else:
                    log.info("session %s abandoned (%s)", session.id, reason)
        self.guard.reset() if hasattr(self.guard, "reset") else None
        self.light.reset()

    # per-frame logic
    def _handle(self, frame, bodies, now):
        # Zone polygons are stored in unit space; give them this
        # frame's pixels before anything asks whether a foot is inside.
        self.site.bind(frame)
        guard = next((b for b in bodies if b.track_id == self.guard_id), None)
        subjects = [b for b in bodies if b.track_id != self.guard_id]

        wrist, target = None, None
        if guard is not None:
            # The scanning hand is whichever of the guard's wrists is
            # nearest to somebody being screened.
            cands = [w for w in (guard.l_wri, guard.r_wri) if w]
            if cands and subjects:
                target = min(subjects,
                             key=lambda s: min(_dist(w, s.torso) for w in cands))
                wrist = min(cands, key=lambda w: _dist(w, target.torso))
            elif cands:
                wrist = cands[0]

        lit = self.light.update(frame, wrist,
                                guard.scale if guard else 60.0, now)

        seen_ids = set(b.track_id for b in bodies)
        engaged_now = None

        # A screening whose track id vanished but never came back.
        for old in [o for o, v in self.orphans.items()
                    if now - v[3] > self.args.subject_reid]:
            self._close_session(self.orphans.pop(old)[0], "left", now)

        for sub in subjects:
            # Scanning this person, rather than just standing near them:
            # the wand hand is on them and the arm is pointing that way.
            engaged = (wrist is not None and guard is not None
                       and _dist(wrist, sub.torso) < sub.scale * self.args.reach_dist
                       and _points_at(guard, wrist, sub, self.args.reach_cos))
            session = self.sessions.get(sub.track_id)
            if session is None:
                session = self._adopt(sub, now, seen_ids)
                if session is not None:
                    self.sessions[sub.track_id] = session
            if session is None and self._duplicate_of_live(sub, seen_ids):
                continue
            if session is None:
                if not engaged and self.guard.frames_seen(sub.track_id) < 5:
                    continue
                # Don't re-open a screening we have already ruled on.
                if now - self.finished.get(sub.track_id, -1e9) < self.args.rescan_lock:
                    continue
                session = ScanSession(sub.track_id, now, self.rules)
                session.aliases.append(sub.track_id)
                self.sessions[sub.track_id] = session

            session.last_seen = now
            session.anchor = sub.torso
            session.scale = sub.scale
            # Once they have stood on the scanned spot, that is the
            # strongest thing we know about who they are.
            if self.site.in_scan_zone(sub):
                session.in_zone = True
            # Standing with the guard is what holds a session open. Wrist
            # contact does not -- it drops out all through a real scan.
            if (guard is None or _dist(guard.torso, sub.torso)
                    < sub.scale * self.args.near_guard):
                session.last_present = now

            self._remember_evidence(session, frame, sub)
            session.track.append({
                "t": round(now - session.started_at, 3),
                "subject": [[round(float(v), 1) for v in kp]
                            for kp in sub.kps.tolist()],
                "guard_wrist": ([round(wrist[0], 1), round(wrist[1], 1)]
                                if wrist else None),
            })

            if engaged:
                if now - session.last_engaged < 1.0:
                    session.engaged_s += now - session.last_engaged
                session.last_engaged = now
                session.engaged_frames += 1
                if guard is not None:
                    self._remember_guard_face(session, frame, guard)
                if target is not None and sub.track_id == target.track_id:
                    engaged_now = sub.track_id
                self._check_steps(session, sub, wrist, now)
                if lit and not session.flagged:
                    session.flagged = True
                    self._raise_alert(
                        session, "scanner_flag", "critical",
                        "Scanner flagged this person",
                        "Red indicator seen on the hand scanner during the scan")

        # One place decides whether a screening is actually over.
        for session in list(self.sessions.values()):
            reason = self._session_over(session, now, seen_ids, engaged_now)
            if reason == "left" and self.args.subject_reid > 0:
                # They may only have been renamed by the tracker. Hold
                # the screening open off to one side for a moment.
                self.orphans[session.subject_id] = (
                    session, session.anchor, session.scale, now)
                self.sessions.pop(session.subject_id, None)
            elif reason:
                self._close_session(session, reason, now)

        return guard, wrist, lit

    def _session_over(self, session, now, seen_ids, engaged_now):
        """Has this screening finished?

        A screening runs for minutes and the wand leaves the customer
        again and again while it does: the guard lowers it, walks round
        to reach the back, or the arm passes behind the customer and the
        wrist keypoint drops out. None of that is the end of a session.
        What ends one is the customer leaving, the guard starting on the
        next person, or every step being done.
        """
        if session.subject_id not in seen_ids:
            # A short drop-out is occlusion or a tracker hiccup, not an exit.
            if now - session.last_seen > self.args.exit_grace:
                return "left"
            return None
        if session.complete and session.in_order:
            # Everything is done, in order. Nothing left to wait for.
            if now - session.last_engaged > self.args.settle:
                return "complete"
            return None
        if (engaged_now is not None and engaged_now != session.subject_id
                and session.engaged_frames >= self.args.min_engaged
                and now - session.last_engaged > self.args.handover):
            return "guard_moved_on"
        if now - session.last_present > self.args.session_gap:
            return "walked_off"
        if now - session.started_at > self.args.max_session:
            return "timeout"
        return None

    def _check_steps(self, session, sub, wrist, now):
        """Tick off a step once the scanning hand has stayed in its area.

        The arm circles and the torso circle overlap, so the hand counts
        towards the nearest one only. Crediting every circle it falls in
        would tick the front while the guard is still on an arm, and a
        properly ordered scan would come out as done out of order.
        """
        regions = sub.regions()
        ranked = sorted((_seg_dist(wrist, shape) / width, name)
                        for name, (shape, width) in regions.items())
        nearest = ranked[0][1] if ranked and ranked[0][0] <= 1.0 else None

        # Dwell is measured in SECONDS, not frames. Counting frames made
        # the rule mean different things on different hardware: three
        # frames is 0.4s on the box this was written on and 0.1s on a
        # faster one, so the same guard passed on one machine and failed
        # on the other.
        dt = max(0.0, min(now - (session.last_tick or now), 1.0))
        session.last_tick = now

        for name in regions:
            if name == "torso":
                step = "front" if sub.facing_camera else "back"
            else:
                step = name
            if name == nearest:
                if step in session.done:
                    session.seen_at[step] = now
                    continue
                session.dwell[step] = session.dwell.get(step, 0.0) + dt
                if session.dwell[step] >= self.args.dwell_s:
                    session.done[step] = now
                    session.seen_at[step] = now
                    session.order.append(step)
                    log.info("step %s done (person %s)",
                             STEP_LABEL[step], session.subject_id)
            elif step not in session.done:
                # Decay SLOWLY. A wand being swept is never still: on
                # real footage the torso is the nearest region in bursts
                # of a third of a second at a time, adding up to well
                # over a second across the pass. Decaying at full rate
                # erased each burst before the next one arrived, so the
                # front and back never credited at all — and the one
                # clip where they did credit passed by a single frame.
                #
                # What the rule is really asking is "how much of this
                # surface did the wand cover", not "did it hover on one
                # spot", so a brief excursion costs a fraction of the
                # progress rather than all of it.
                session.dwell[step] = max(
                    0.0, session.dwell.get(step, 0.0)
                    - dt * self.args.dwell_decay)

        self._expire_steps(session, now)

    def _expire_steps(self, session, now):
        """Un-tick a step the hand has long since left.

        A latched step was a one-way door: a wrist that clipped the
        torso circle for a moment credited "front" for the rest of the
        screening, so a scan that never touched the front still passed.
        A step now has to be re-earned if it goes unseen for long enough,
        unless the scan is already finished.
        """
        hold = float(getattr(self.args, "step_hold_s", 0.0) or 0.0)
        if hold <= 0:
            return
        for step in list(session.done):
            if now - session.seen_at.get(step, session.done[step]) > hold:
                session.done.pop(step, None)
                session.dwell[step] = 0.0
                if step in session.order:
                    session.order.remove(step)
                log.info("step %s expired (person %s)",
                         STEP_LABEL[step], session.subject_id)

    # drawing

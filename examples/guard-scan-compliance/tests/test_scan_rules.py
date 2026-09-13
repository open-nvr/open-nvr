# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The screening rules, driven on a fake clock.

No camera, no model, no platform: synthetic bodies go straight through
the real logic. These are the specification of correct behaviour, ported
from the prototype this app grew out of — every one of them pins down
something that was wrong first and is awkward to reproduce by hand:

* a screening that runs for two minutes with the wand off the customer
  for fifteen seconds of it is ONE screening, not three;
* a tracker that renames the customer mid-scan has not produced a second
  customer;
* the guard is whoever reaches toward people, and survives being renamed;
* the grading thresholds mean what the rules file says they mean.

The prototype's version of this file counted frames. Here everything is
seconds, because that is what the port changed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guard_scan import core as G  # noqa: E402
from guard_scan.settings import ScanSettings  # noqa: E402

FRAME = np.zeros((480, 960, 3), dtype=np.uint8)
FPS = 8.0
DT = 1.0 / FPS


def person(tid, cx, cy, sw=60.0, wrists=None, facing=True):
    """A standing person. wrists=(left, right), either may be None."""
    kp = np.zeros((17, 3), dtype=np.float32)

    def put(i, xy, c=0.9):
        if xy is not None:
            kp[i] = (xy[0], xy[1], c)

    put(G.L_SHO, (cx - sw / 2, cy - 40)); put(G.R_SHO, (cx + sw / 2, cy - 40))
    put(G.L_HIP, (cx - sw / 3, cy + 40)); put(G.R_HIP, (cx + sw / 3, cy + 40))
    put(G.L_ELB, (cx - sw / 2 - 8, cy)); put(G.R_ELB, (cx + sw / 2 + 8, cy))
    lw, rw = (wrists or (None, None))
    put(G.L_WRI, lw if lw else (cx - sw / 2 - 10, cy + 35))
    put(G.R_WRI, rw if rw else (cx + sw / 2 + 10, cy + 35))
    if facing:
        put(G.NOSE, (cx, cy - 70)); put(G.L_EYE, (cx - 8, cy - 75))
        put(G.R_EYE, (cx + 8, cy - 75))
    else:
        put(G.L_EAR, (cx - 14, cy - 72)); put(G.R_EAR, (cx + 14, cy - 72))
    box = (cx - sw, cy - 90, cx + sw, cy + 90)
    return G.Body(tid, box, kp, 0.35)


class Engine:
    """A ScanEngine with its sinks captured in lists."""

    def __init__(self, rules=None, site=None, **over):
        self.alerts: list[dict] = []
        self.screenings: list[dict] = []
        self.session_logs: list[dict] = []
        settings = ScanSettings(**over)
        self.engine = G.ScanEngine(
            settings,
            site=G.SiteConfig(site),
            rules=G.ScanRules(rules),
            camera="cam-test",
            on_alert=self.alerts.append,
            on_screening=self.screenings.append,
            on_session_log=self.session_logs.append,
            # Keep the bytes, so a test can assert a photo was taken.
            save_image=lambda name, jpeg: f"stored/{name}.jpg",
        )

    def run(self, script, cust_id=2, start=1000.0, frame=FRAME):
        """script: (seconds, region, facing, present) steps."""
        now = start
        for secs, target, facing, present in script:
            for _ in range(int(secs * FPS)):
                now += DT
                cust = person(cust_id, 560, 300, facing=facing)
                bodies = [cust] if present else []
                wrist = None
                if target and present:
                    regions = cust.regions()
                    if target in regions:
                        wrist = regions[target][0]
                bodies.insert(0, person(1, 400, 300,
                                        wrists=(wrist, None) if wrist else None))
                bodies = self.engine._dedupe(bodies, frame)
                self.engine.guard_id = self.engine.guard.update(
                    bodies, now, self.engine._being_scanned())
                self.engine._handle(frame, bodies, now)
        return now



def establish(e, *, fps=FPS, seconds=6.0, start=1000.0, region="right_arm"):
    """Run long enough for the guard to be elected.

    Nobody is the guard until they have been seen reaching for a while —
    deliberately, so a customer who happens to raise an arm cannot take
    the role. Tests that care about something else still have to pay it.
    """
    now, dt = start, 1.0 / fps
    cust = person(2, 560, 300)
    wrist = cust.regions()[region][0]
    while now < start + seconds:
        now += dt
        bodies = [person(1, 400, 300, wrists=(wrist, None)),
                  person(2, 560, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)
    assert e.engine.guard_id == 1, "the guard was never established"
    return now


FULL = [(20, "left_arm", True, True), (20, "right_arm", True, True),
        (15, "torso", True, True), (15, None, True, True),
        (20, "torso", False, True), (10, None, True, True),
        (8, None, True, False)]


# ── one screening, however messy the tracking ────────────────────────


def test_a_long_screening_with_a_dropout_is_one_compliant_screening():
    """108 seconds, with the wand off the customer for 15 of them."""
    e = Engine()
    e.run(FULL)
    assert len(e.screenings) == 1
    done = e.screenings[0]
    assert done["verdict"] == "compliant"
    assert done["score"] == 100.0
    assert e.session_logs[0]["order"] == ["left_arm", "right_arm", "front", "back"]
    assert e.alerts == [], "a compliant scan must raise no alert"


def test_a_skipped_back_is_a_partial_scan():
    e = Engine()
    e.run([(20, "left_arm", True, True), (20, "right_arm", True, True),
           (20, "torso", True, True), (15, None, True, True),
           (8, None, True, False)])
    assert len(e.screenings) == 1
    assert (e.screenings[0]["verdict"], e.screenings[0]["score"]) == ("partial", 75.0)
    assert len(e.alerts) == 1
    assert e.alerts[0]["severity"] == "medium"
    assert e.alerts[0]["steps_missing"] == ["Back"]


def test_one_arm_only_is_incomplete_and_loud():
    e = Engine()
    e.run([(20, "left_arm", True, True), (15, None, True, True),
           (8, None, True, False)])
    assert (e.screenings[0]["verdict"], e.screenings[0]["score"]) == ("incomplete", 25.0)
    assert e.alerts[0]["severity"] == "high"


def test_a_customer_renamed_mid_scan_is_still_one_screening():
    """The tracker renames people mid-clip. It did it three times to one
    customer in a two-minute clip; each rename used to start a new
    screening and rule the old one incomplete."""
    e = Engine()
    now = e.run(FULL[:3], cust_id=2)
    e.run(FULL[3:], cust_id=9, start=now)
    assert len(e.screenings) == 1
    assert e.screenings[0]["verdict"] == "compliant"
    assert e.session_logs[0]["track_ids"] == [2, 9]
    assert e.alerts == []


# ── who the guard is ─────────────────────────────────────────────────


def test_the_guard_is_not_stolen_by_a_customer_who_stands_still():
    e = Engine()
    now = 1000.0
    for _ in range(int(120 * FPS)):
        now += DT
        cust = person(2, 560, 300)
        g = person(1, 400, 300, wrists=(cust.regions()["torso"][0], None))
        bodies = e.engine._dedupe([g, cust], FRAME)
        e.engine.guard_id = e.engine.guard.update(
            bodies, now, e.engine._being_scanned())
        e.engine._handle(FRAME, bodies, now)
    assert e.engine.guard_id == 1


def test_the_guard_is_rejoined_after_the_tracker_renames_them():
    e = Engine()
    now = 1000.0
    for _ in range(int(40 * FPS)):
        now += DT
        cust = person(2, 560, 300)
        g = person(1, 400, 300, wrists=(cust.regions()["torso"][0], None))
        e.engine.guard_id = e.engine.guard.update([g, cust], now)
    assert e.engine.guard_id == 1
    for _ in range(int(1.0 * FPS)):                 # lost
        now += DT
        e.engine.guard_id = e.engine.guard.update([person(2, 560, 300)], now)
    for _ in range(int(20 * FPS)):                  # back, as id 7
        now += DT
        cust = person(2, 560, 300)
        g = person(7, 400, 300, wrists=(cust.regions()["torso"][0], None))
        e.engine.guard_id = e.engine.guard.update([g, cust], now)
    assert e.engine.guard_id == 7


# ── the rules file actually rules ────────────────────────────────────


def test_order_weight_required_steps_and_weights_all_bite():
    done = ["front", "right_arm", "left_arm", "back"]
    assert G.ScanRules().score(done)["verdict"] == "compliant"

    strict = G.ScanRules({"order_weight": 1.0}).score(done)
    assert strict["verdict"] == "incomplete" and strict["score"] == 50.0

    assert G.ScanRules({"order_weight": 0.3}).score(done)["verdict"] == "partial"

    arms = G.ScanRules({"steps": [{"name": "left_arm"}, {"name": "right_arm"}]})
    assert arms.score(["left_arm", "right_arm"])["verdict"] == "compliant"

    heavy = G.ScanRules({"steps": [{"name": "front", "weight": 3.0},
                                   {"name": "back", "weight": 1.0}]})
    assert heavy.score(["front"])["score"] == 75.0


def test_an_unknown_step_in_the_rules_is_refused():
    with pytest.raises(SystemExit):
        G.ScanRules({"steps": [{"name": "elbows"}]})


# ── what the port changed ────────────────────────────────────────────


@pytest.mark.parametrize("fps", [5.0, 30.0])
def test_dwell_is_seconds_so_the_rule_means_the_same_on_any_hardware(fps):
    """Counting frames made the same rule stricter on a slow box than a
    fast one: three frames was 0.4s here and 0.1s there. The wand rests
    on the left arm for 0.6s at six times the frame rate; the step has
    to count in both."""
    e = Engine(dwell_s=0.4)
    now = establish(e, fps=fps, region="right_arm")
    dt = 1.0 / fps
    cust = person(2, 560, 300)
    wrist = cust.regions()["left_arm"][0]
    stop = now + 0.6
    while now < stop:
        now += dt
        bodies = [person(1, 400, 300, wrists=(wrist, None)),
                  person(2, 560, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)
    session = e.engine.sessions.get(2)
    assert session is not None, f"no session at {fps} fps"
    assert "left_arm" in session.done, f"step missed at {fps} fps"


def test_a_step_can_expire_instead_of_latching_for_ever():
    """A wrist that clipped the torso once used to credit 'front' for
    the rest of the screening, so a scan that never touched the front
    still passed."""
    e = Engine(step_hold_s=2.0, dwell_s=0.2)
    now = establish(e, region="left_arm")
    cust = person(2, 560, 300)
    torso = cust.regions()["torso"][0]
    stop = now + 1.0                                # earn 'front'
    while now < stop:
        now += DT
        bodies = [person(1, 400, 300, wrists=(torso, None)), person(2, 560, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)
    assert "front" in e.engine.sessions[2].done

    stop = now + 4.0                                # hand goes elsewhere
    arm = cust.regions()["left_arm"][0]
    while now < stop:
        now += DT
        bodies = [person(1, 400, 300, wrists=(arm, None)), person(2, 560, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)
    assert "front" not in e.engine.sessions[2].done


def test_the_scan_zone_is_the_same_place_at_any_resolution():
    """The zone used to be stored in one feed's pixels, so it pointed
    somewhere else entirely on a camera of another size."""
    site = G.SiteConfig({"scan_zone": {"polygon": [[500, 200], [700, 200],
                                                   [700, 400], [500, 400]]}})
    small = site.zone_for(960, 480)
    big = site.zone_for(1920, 960)
    assert small is not None and big is not None
    # Same fraction of the frame, twice the pixels.
    assert list(big[0]) == [small[0][0] * 2, small[0][1] * 2]


def test_every_screening_is_reported_not_only_the_failures():
    """Compliance is complete scans over ALL screenings, so a clean one
    has to be recorded too."""
    e = Engine()
    e.run(FULL)
    assert len(e.screenings) == 1
    assert e.screenings[0]["verdict"] == "compliant"
    assert e.alerts == []
    assert e.screenings[0]["images"], "a screening should carry its photos"


def test_a_passer_by_is_not_a_skipped_scan():
    """Someone walking past the guard collects a fraction of a second of
    wand-near-them. Ruling on that produced an alert per passer-by."""
    e = Engine()
    now = 1000.0
    for _ in range(int(0.5 * FPS)):
        now += DT
        bodies = [person(1, 400, 300), person(3, 900, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)
    for _ in range(int(20 * FPS)):                  # they leave
        now += DT
        bodies = [person(1, 400, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)
    assert e.alerts == []
    assert e.screenings == []


def test_a_lost_feed_abandons_rather_than_blaming_the_guard():
    """The camera dropping out mid-screening is our outage, not the
    guard's mistake. Ruling 'incomplete' on a scan we stopped watching
    puts an alert on someone who may have done it perfectly."""
    e = Engine()
    now = establish(e)
    cust = person(2, 560, 300)
    arm = cust.regions()["left_arm"][0]
    stop = now + 5.0
    while now < stop:
        now += DT
        bodies = [person(1, 400, 300, wrists=(arm, None)), person(2, 560, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)
    assert e.engine.sessions, "nothing was being screened"

    e.engine.abandon(now)
    assert e.engine.sessions == {}
    assert e.alerts == [], "a dropped feed must not raise a procedure alert"
    assert e.screenings == []


def test_an_alert_carries_the_guards_face_as_well_as_the_customers():
    """A procedure alert is ABOUT the guard. A record that shows only
    the customer asks the manager to take our word for who was careless."""
    e = Engine()
    e.run([(20, "left_arm", True, True), (15, None, True, True),
           (8, None, True, False)])
    assert e.alerts, "expected an incomplete-scan alert"
    images = e.alerts[0]["images"]
    assert "face" in images and "body" in images
    assert "guard_face" in images, images

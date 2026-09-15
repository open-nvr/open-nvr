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
from guard_scan.core import region_point  # noqa: E402
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
                        wrist = region_point(regions[target][0])
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
    wrist = region_point(cust.regions()[region][0])
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


def test_alerts_are_raised_under_the_kinds_the_manifest_declares():
    """The catalog documents what this app emits, and the inbox filters
    on it. Publishing the VERDICT as the alert type put `partial` and
    `incomplete` in the inbox while the manifest promised
    `improper_scan`, so filtering by the documented type found nothing —
    and the two spellings never met, because nothing compared them.

    Read out of the source rather than imported, like the defaults-drift
    test above: this file is deliberately free of the SDK.
    """
    import re

    src = (Path(__file__).resolve().parents[1] / "guard_scan_compliance.py"
           ).read_text(encoding="utf-8")
    declared = set(re.findall(r'AlertType\("([a-z_]+)"', src))
    assert declared == {"scanner_flag", "improper_scan", "no_scan"}

    # Every kind the engine can raise must be one the manifest declares.
    assert set(G.ALERT_KIND.values()) | {"scanner_flag"} <= declared

    e = Engine()
    e.run([(20, "left_arm", True, True), (20, "right_arm", True, True),
           (20, "torso", True, True), (15, None, True, True),
           (8, None, True, False)])
    assert e.screenings[0]["verdict"] == "partial"       # how it scored
    assert e.alerts[0]["kind"] == "improper_scan"        # what went wrong

    thin = Engine()
    thin.run([(20, "left_arm", True, True), (15, None, True, True),
              (8, None, True, False)])
    assert thin.screenings[0]["verdict"] == "incomplete"
    assert thin.alerts[0]["kind"] == "improper_scan"


def test_a_screening_names_the_alarm_it_raised():
    """The ledger keeps every screening; the inbox keeps only the ones
    that went wrong. Without the alarm's id on the screening the two
    tables cannot be joined, and a manager looking at a critical alert
    has no way back to the record it came from — or the other way round.

    A clean scan raises nothing, so it names nothing."""
    bad = Engine()
    bad.run([(20, "left_arm", True, True), (20, "right_arm", True, True),
             (20, "torso", True, True), (15, None, True, True),
             (8, None, True, False)])
    assert bad.screenings[0]["alert_id"] == bad.alerts[0]["id"]

    good = Engine()
    good.run(FULL)
    assert good.alerts == []
    assert good.screenings[0]["alert_id"] is None


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
        g = person(1, 400, 300, wrists=(region_point(cust.regions()["torso"][0]), None))
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
        g = person(1, 400, 300, wrists=(region_point(cust.regions()["torso"][0]), None))
        e.engine.guard_id = e.engine.guard.update([g, cust], now)
    assert e.engine.guard_id == 1
    for _ in range(int(1.0 * FPS)):                 # lost
        now += DT
        e.engine.guard_id = e.engine.guard.update([person(2, 560, 300)], now)
    for _ in range(int(20 * FPS)):                  # back, as id 7
        now += DT
        cust = person(2, 560, 300)
        g = person(7, 400, 300, wrists=(region_point(cust.regions()["torso"][0]), None))
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


def test_an_unknown_surface_is_refused_without_killing_the_camera():
    """It must raise something `except Exception` can catch.

    This used to be SystemExit, which derives from BaseException — so
    the per-camera worker's handler did not catch it, the thread died
    on a typo in the procedure, that entrance silently stopped being
    screened, and the app went on reporting itself healthy.
    """
    with pytest.raises(G.ConfigError):
        G.ScanRules({"steps": [{"name": "elbows"}]})
    assert issubclass(G.ConfigError, Exception)


def test_a_bad_procedure_is_reported_not_fatal():
    """The whole point of the exception change, from the worker's side."""
    caught = None
    try:
        G.ScanRules({"steps": [{"name": "elbows"}]})
    except Exception as exc:        # exactly what CameraWorker._run does
        caught = exc
    assert caught is not None, "a config error must be catchable as Exception"
    assert "elbows" in str(caught)


def test_the_procedure_is_assembled_from_the_fields_the_form_collects():
    """`procedure` stays the engine's contract; the form no longer asks
    an operator to hand-write it. Read out of the app module's source is
    not possible here — this is the assembly itself, so it is exercised
    directly."""
    import importlib.util

    app_path = Path(__file__).resolve().parents[1] / "guard_scan_compliance.py"
    src = app_path.read_text(encoding="utf-8")
    # Pull the two helpers out without importing the module, which would
    # drag in the SDK this file is deliberately free of.
    ns = {"ScanRules": G.ScanRules}
    start = src.index("def _uniform_bounds(")
    end = src.index("class GuardScanApp(")
    exec(compile(src[start:end], "<helpers>", "exec"), ns)  # noqa: S102
    procedure, uniform = ns["_procedure"], ns["_uniform_bounds"]

    # Nothing configured -> the engine's own defaults, not an empty shell.
    assert procedure({}) is None

    # The surfaces an operator ticked, with the order switch they chose.
    built = procedure({"required_surfaces": ["front", "back"],
                       "order_weight": 0.3})
    rules = G.ScanRules(built)
    assert rules.steps == ["front", "back"]
    assert rules.order_weight == 0.3

    # Weights alone re-weight the shipped surfaces rather than doing nothing.
    weighted = G.ScanRules(procedure({"surface_weights": {"back": 3}}))
    assert weighted.weights["back"] == 3.0
    assert weighted.weights["front"] == 1.0

    # A whole `procedure` still wins outright — an install that set it
    # before the split must not change meaning.
    whole = {"steps": [{"name": "back", "weight": 2.0}], "order_weight": 1.0}
    assert procedure({"procedure": whole, "required_surfaces": ["front"]}) is whole

    # The picked colour wins over the two bare lists it replaced, and
    # the old lists still work on an install that never re-picked.
    assert uniform({"uniform_hsv": {"low": [1, 2, 3], "high": [4, 5, 6]},
                    "uniform_hsv_low": [9, 9, 9]}) == ([1, 2, 3], [4, 5, 6])
    assert uniform({"uniform_hsv_low": [1, 2, 3],
                    "uniform_hsv_high": [4, 5, 6]}) == ([1, 2, 3], [4, 5, 6])
    assert uniform({}) == ([], [])


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
    wrist = region_point(cust.regions()["left_arm"][0])
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
    torso = region_point(cust.regions()["torso"][0])
    stop = now + 1.0                                # earn 'front'
    while now < stop:
        now += DT
        bodies = [person(1, 400, 300, wrists=(torso, None)), person(2, 560, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)
    assert "front" in e.engine.sessions[2].done

    stop = now + 4.0                                # hand goes elsewhere
    arm = region_point(cust.regions()["left_arm"][0])
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
    arm = region_point(cust.regions()["left_arm"][0])
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


def test_the_last_person_through_the_door_is_still_ruled_on():
    """The bug that made a perfect scan produce no verdict at all.

    When somebody walks away their screening is parked in the orphan
    hold, in case the tracker only renamed them — and that hold is
    checked ONLY while frames keep arriving. So when the feed ends, the
    last screening of the day sits there finished and unjudged. That is
    not an edge case: it is the last person through the door, every
    time. On a looping test clip it was every screening.
    """
    e = Engine()
    now = e.run(FULL[:-1])                       # everything but their exit
    # They leave; the feed ends a moment later, before the hold expires.
    now = e.run([(2, None, True, False)], start=now)
    assert e.screenings == [], "precondition: nothing ruled yet"
    assert e.engine.orphans or e.engine.sessions, "nobody is waiting"

    e.engine.flush(now + 0.1, reason="left")
    assert len(e.screenings) == 1
    assert e.screenings[0]["verdict"] == "compliant"
    assert e.screenings[0]["score"] == 100.0


def test_a_scan_already_finished_survives_the_feed_dying():
    """Abandoning protects the guard from being blamed for our outage —
    but a scan that was COMPLETE before the feed died is a real result,
    and throwing it away loses evidence we actually have."""
    e = Engine()
    e.run(FULL[:-1])
    e.engine.abandon(1e9)
    assert len(e.screenings) == 1
    assert e.screenings[0]["verdict"] == "compliant"


def test_an_unfinished_scan_is_not_blamed_on_the_guard():
    e = Engine()
    now = establish(e)
    cust = person(2, 560, 300)
    arm = region_point(cust.regions()["left_arm"][0])
    stop = now + 5.0
    while now < stop:
        now += DT
        bodies = [person(1, 400, 300, wrists=(arm, None)), person(2, 560, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)
    e.engine.abandon(now)
    assert e.alerts == []
    assert e.screenings == []


def test_a_surface_is_credited_by_time_covered_not_by_hovering():
    """The rule that made a correct scan read as 50%.

    A wand being swept is never still. On real entrance footage each
    pass over the torso is the nearest region for about a third of a
    second at a time, adding up to well over a second across the pass —
    but the old rule wanted 0.4s UNBROKEN and wiped the progress out
    between bursts, so front and back never credited. The one clip
    where they did credit passed by a single frame, which is why it
    looked like it worked.
    """
    e = Engine(dwell_s=0.6, dwell_decay=0.25)
    now = establish(e)
    cust = person(2, 560, 300)
    torso = region_point(cust.regions()["torso"][0])
    arm = region_point(cust.regions()["right_arm"][0])

    # Four passes over the torso, 0.3s each, with the wand elsewhere
    # in between — exactly the shape the measurements showed.
    for _ in range(4):
        stop = now + 0.3
        while now < stop:
            now += DT
            bodies = [person(1, 400, 300, wrists=(torso, None)),
                      person(2, 560, 300)]
            e.engine.guard_id = e.engine.guard.update(bodies, now)
            e.engine._handle(FRAME, bodies, now)
        stop = now + 0.4
        while now < stop:
            now += DT
            bodies = [person(1, 400, 300, wrists=(arm, None)),
                      person(2, 560, 300)]
            e.engine.guard_id = e.engine.guard.update(bodies, now)
            e.engine._handle(FRAME, bodies, now)

    session = e.engine.sessions[2]
    assert "front" in session.done, (
        f"the torso was covered four times and still did not count: "
        f"{session.dwell}")


def test_a_wand_merely_travelling_past_does_not_credit_a_surface():
    """The other side of that coin: progress must still drain, or the
    hand crossing a surface on its way somewhere else would score it."""
    e = Engine(dwell_s=0.6, dwell_decay=0.25)
    now = establish(e)
    cust = person(2, 560, 300)
    torso = region_point(cust.regions()["torso"][0])
    arm = region_point(cust.regions()["left_arm"][0])

    # One brief crossing, then a long time elsewhere, repeatedly.
    for _ in range(3):
        now += DT                      # a single frame on the torso
        bodies = [person(1, 400, 300, wrists=(torso, None)), person(2, 560, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)
        stop = now + 3.0
        while now < stop:
            now += DT
            bodies = [person(1, 400, 300, wrists=(arm, None)),
                      person(2, 560, 300)]
            e.engine.guard_id = e.engine.guard.update(bodies, now)
            e.engine._handle(FRAME, bodies, now)

    session = e.engine.sessions[2]
    assert "front" not in session.done, "a passing wand credited the torso"


def test_the_three_copies_of_every_default_agree():
    """A threshold is written down in three places — the engine's
    settings, the app's config, and the manifest the catalog renders —
    and nothing keeps them in step. When they drifted, the app ran on a
    dwell nobody had chosen: tuned to 0.6 in the engine, shipped as 0.4
    by the manifest, and the clip that had just been made to pass failed
    again on the live stack.
    """
    import re

    src = (Path(__file__).resolve().parents[1] / "guard_scan_compliance.py"
           ).read_text(encoding="utf-8")
    settings = ScanSettings()
    for name in ("dwell_s", "dwell_decay", "no_scan_engaged", "step_hold_s",
                 "min_screen", "session_gap", "led_ratio", "led_window_s"):
        want = getattr(settings, name)
        in_manifest = re.search(
            rf'Param\("{name}",\s*\w+,\s*default=([0-9.]+)', src)
        assert in_manifest, f"{name} is not offered in the manifest"
        assert float(in_manifest.group(1)) == want, (
            f"manifest default for {name} is {in_manifest.group(1)}, "
            f"engine uses {want}")
        in_config = re.search(rf"\n    {name}: float = ([0-9.]+)", src)
        assert in_config, f"{name} missing from the app config"
        assert float(in_config.group(1)) == want, (
            f"app config default for {name} is {in_config.group(1)}, "
            f"engine uses {want}")


# ── live configuration ───────────────────────────────────────────────
#
# An operator saves a setting and expects it to mean something. It used
# to reach the app's config object and stop there: the running engine
# kept the rules it was built with, and the only thing that rebuilt one
# was a container restart. These pin down that a saved setting reaches
# the next screening — and that it never reaches one already in flight.


#: A pass that covers everything, in an order the procedure does not
#: expect. Worth 100% while order is ignored; less once it counts.
#: front, right arm, back, left arm — every surface covered, none in
#: the sequence the procedure expects. `torso` is the front while the
#: person faces the camera and the back once they turn, exactly as in
#: FULL above.
OUT_OF_ORDER = [(15, "torso", True, True), (20, "right_arm", True, True),
                (10, None, True, True), (20, "torso", False, True),
                (20, "left_arm", True, True), (15, None, True, True),
                (8, None, True, False)]


def test_a_saved_setting_reaches_the_next_screening():
    """The live case, exactly: order was not counted, an out-of-order
    pass scored 100%, the operator turned order on — and every later
    screening carried on saying 100% because nothing rebuilt the rules.
    """
    e = Engine()
    e.run(OUT_OF_ORDER)
    assert e.screenings[0]["verdict"] == "compliant"
    assert e.screenings[0]["score"] == 100.0

    e.engine.retune(ScanSettings(order_weight=0.3))

    e.run(OUT_OF_ORDER, cust_id=20, start=9000.0)
    after = e.screenings[1]
    assert after["verdict"] == "partial"
    assert after["score"] < 100.0
    assert after["coverage"] == 100.0      # every surface still covered


def test_a_screening_already_running_keeps_the_rules_it_began_under():
    """Nobody is re-judged half way through being wanded. The care the
    old comment described — just delivered without a restart."""
    e = Engine()
    # No trailing "walked off" step, so the screening is still open.
    e.run([(20, "left_arm", True, True), (20, "right_arm", True, True)])
    live = e.engine.sessions[2]
    assert live.done, "expected a screening in progress"
    before = live.rules

    e.engine.retune(ScanSettings(order_weight=1.0))

    # The session in flight still points at the rules it started with,
    # while the engine has moved on for whoever comes next.
    assert live.rules is before
    assert e.engine.rules is before or e.engine.rules.order_weight == 1.0
    assert live.rules.order_weight == before.order_weight


def test_order_weight_arriving_as_a_SETTING_survives_a_retune():
    """It reaches the rules by two routes — inside the procedure object
    and as a setting of its own — and the setting wins. A retune that
    only swapped the procedure would install new surfaces while quietly
    keeping the old order weight."""
    e = Engine()
    assert e.engine.rules.order_weight == 0.0

    # New procedure says 0.0; the setting beside it says 1.0.
    e.engine.retune(ScanSettings(order_weight=1.0),
                    rules=G.ScanRules({"order_weight": 0.0}))
    assert e.engine.rules.order_weight == 1.0


def test_retuning_also_moves_the_thresholds_the_guard_is_judged_by():
    """The picker and the light hold their own copy of the settings.
    Leaving them behind would apply half a saved config."""
    e = Engine()
    e.engine.retune(ScanSettings(led_ratio=0.5, led_hits=9, led_window_s=2.5))

    assert e.engine.guard.args.led_ratio == 0.5
    assert e.engine.light.ratio == 0.5
    assert e.engine.light.hits == 9
    assert e.engine.light.window_s == 2.5


def test_the_guard_election_survives_a_saved_setting():
    """Rebuilding the picker would restart the election from nothing
    every time anybody pressed Save, and the guard would be 'unknown'
    for seconds afterwards on a busy door."""
    e = Engine()
    establish(e)
    who = e.engine.guard.guard_id

    tracked = dict(e.engine.guard.tracks)
    e.engine.retune(ScanSettings(led_ratio=0.2))
    assert e.engine.guard.guard_id == who
    # The accumulated evidence is still there, not reset to nothing.
    assert set(e.engine.guard.tracks) == set(tracked)


# ── the confidence floor on front/back ─────────────────────────────
#
# The pose adapter NEVER omits an occluded keypoint: all 17 come back,
# every frame, each with its own confidence. `person()` above emits only
# 0.9 or 0.0, which is a shape real output never has — so these build
# their keypoints by hand at the confidences a real back-turned person
# produces.


def _head(nose=0.0, eyes=0.0, ears=0.0, cx=560.0, cy=300.0, min_conf=0.35):
    """A body whose head joints carry exactly the given confidences."""
    kp = np.zeros((17, 3), dtype=np.float32)
    kp[G.L_SHO] = (cx - 30, cy - 40, 0.9)
    kp[G.R_SHO] = (cx + 30, cy - 40, 0.9)
    kp[G.NOSE] = (cx, cy - 70, nose)
    kp[G.L_EYE] = (cx - 8, cy - 75, eyes)
    kp[G.R_EYE] = (cx + 8, cy - 75, eyes)
    kp[G.L_EAR] = (cx - 14, cy - 72, ears)
    kp[G.R_EAR] = (cx + 14, cy - 72, ears)
    return G.Body(1, (cx - 60, cy - 90, cx + 60, cy + 90), kp, min_conf)


def test_a_back_turned_person_is_not_facing_the_camera():
    """Nose and both eyes at 0.30 is the model saying it CANNOT see a
    face. Summed raw they reach 0.90 and used to clear the gate outright
    — before the ears, which the model can see perfectly well, were
    compared at all. The front then got credit for a pass down the back.
    """
    assert _head(nose=0.30, eyes=0.30, ears=0.88).facing_camera is False


def test_a_face_the_model_can_see_still_counts():
    assert _head(nose=0.95, eyes=0.90, ears=0.10).facing_camera is True


def test_a_half_seen_face_beats_unseen_ears():
    """Two confident face joints and no visible ears is still a front."""
    assert _head(nose=0.60, eyes=0.30, ears=0.10).facing_camera is True


def test_every_head_joint_below_the_floor_is_a_back():
    """Nothing visible at all must not read as a face."""
    assert _head(nose=0.2, eyes=0.2, ears=0.2).facing_camera is False


# ── the scanner light belongs to one person ────────────────────────


class _AlwaysLit:
    """Stands in for RedLightWatch: the wand's lamp is on, always.

    Real detection is HSV over a patch around the wrist; what is under
    test here is WHOSE session the reading is applied to, so the optics
    are replaced by a constant.
    """

    def __init__(self):
        self.resets = 0
        self.ratio = 0.08
        self.hits = 3
        self.window_s = 0.8

    def reset(self):
        self.resets += 1

    def update(self, frame, wrist, scale, now):
        return wrist is not None


def test_the_scanner_flag_does_not_leak_to_the_next_person():
    """A critical alert naming the wrong person is worse than no alert.

    One RedLightWatch serves the whole engine and `lit` used to be
    applied to every engaged subject in the frame, so with two people
    engaged at once both were flagged for one person's metal.
    """
    e = Engine(dwell_s=0.4)
    e.engine.light = _AlwaysLit()
    now = establish(e)

    # Two customers, the wand on the first one's torso.
    first = person(2, 560, 300)
    wrist = region_point(first.regions()["torso"][0])
    for _ in range(int(3.0 * FPS)):
        now += DT
        bodies = [person(1, 400, 300, wrists=(wrist, None)),
                  person(2, 560, 300), person(3, 760, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)

    flagged = [a for a in e.alerts if a.get("kind") == "scanner_flag"]
    assert flagged, "the person the wand was on should have been flagged"
    subjects = {a["subject_track"] for a in flagged}
    assert subjects == {2}, f"the flag reached someone else too: {subjects}"


def test_the_light_window_is_cleared_when_the_wand_changes_person():
    """window_s outlives a person stepping aside: without a reset, the
    hits collected on A are still inside the window when B steps up."""
    e = Engine(dwell_s=0.4)
    watch = _AlwaysLit()
    e.engine.light = watch
    now = establish(e)

    first = person(2, 560, 300)
    second = person(3, 760, 300)
    for target, cid in ((first, 2), (second, 3)):
        wrist = region_point(target.regions()["torso"][0])
        for _ in range(int(1.5 * FPS)):
            now += DT
            bodies = [person(1, 400, 300, wrists=(wrist, None)),
                      person(2, 560, 300), person(3, 760, 300)]
            e.engine.guard_id = e.engine.guard.update(bodies, now)
            e.engine._handle(FRAME, bodies, now)

    assert watch.resets >= 1, "the window was carried from one person to the next"


# ── abandon() actually lets go ─────────────────────────────────────


def test_abandon_resets_the_guard_election():
    """abandon() has always called guard.reset() behind a hasattr guard
    and GuardPicker never had the method, so a feed break kept an
    election — and a lost_anchor — for a track that is never coming
    back."""
    e = Engine()
    now = establish(e)
    assert e.engine.guard_id == 1
    assert e.engine.guard.tracks

    e.engine.abandon(now, reason="inference_down")

    assert e.engine.guard.guard_id is None
    assert e.engine.guard.tracks == {}
    assert e.engine.guard.lost_anchor is None


def test_abandon_drops_an_unfinished_screening_but_keeps_a_complete_one():
    """The point of abandon: our outage must not be graded as the
    guard's incomplete scan."""
    e = Engine(dwell_s=0.4, min_screen=0.0)
    now = establish(e)
    cust = person(2, 560, 300)
    wrist = region_point(cust.regions()["left_arm"][0])
    for _ in range(int(2.0 * FPS)):
        now += DT
        bodies = [person(1, 400, 300, wrists=(wrist, None)), person(2, 560, 300)]
        e.engine.guard_id = e.engine.guard.update(bodies, now)
        e.engine._handle(FRAME, bodies, now)

    assert e.engine.sessions, "expected a screening in flight"
    before = len(e.screenings)
    e.engine.abandon(now, reason="inference_down")

    assert not e.engine.sessions
    assert len(e.screenings) == before, (
        "an unfinished screening was published as a result")

# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alarm UX honesty: operators pick from what the detector can SEE, and
the camera is an explicit choice.

Field review findings: the rail Alarms form never asked WHICH camera (it
silently used the main screen's checkbox state, so a porch alarm could
quietly arm fleet-wide); the two arm surfaces (rail card vs camera-screen
popover) had drifted apart; and the free-text target invited labels the
detector can't see (the server rejects those, but discovering the
vocabulary by trial and error is hostile). GET /alarm-targets now serves
the install's real detectable vocabulary; a shared <datalist> backs every
target input; the rail form gained a camera picker; the popover gained
the missing name field.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec


def _runtime(**over):
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True,
                    cameras=[CameraSpec(camera_id="cam1",
                                        frame_url="http://x/1.jpg", role="r")],
                    **over)
    return CameraAgentRuntime(cfg)


# ── GET /alarm-targets ──────────────────────────────────────────────────

def test_targets_serve_detectable_vocabulary_common_first():
    d = TestClient(build_app(_runtime())).get("/alarm-targets").json()
    targets = d["targets"]
    # Security-relevant labels lead the list…
    assert targets[0] == "person"
    assert targets.index("car") < targets.index("bench")
    # …and the full COCO-80 vocabulary is present.
    assert {"dog", "truck", "bicycle", "bench"} <= set(targets)


def test_targets_include_operator_extra_labels():
    rt = _runtime(detector_extra_labels=["snake"])
    d = TestClient(build_app(rt)).get("/alarm-targets").json()
    assert "snake" in d["targets"]


def test_targets_special_intents_kept_separate():
    # fire/smoke are armable intents but need a dedicated detector/app —
    # they must NOT be offered as ordinary pickable targets.
    d = TestClient(build_app(_runtime())).get("/alarm-targets").json()
    assert "fire" in d["special"] and "smoke" in d["special"]
    assert "fire" not in d["targets"]


# ── demo page: the two arm surfaces agree ───────────────────────────────

_HTML = (Path(__file__).resolve().parents[1] / "demo" / "index.html").read_text()


def test_rail_form_asks_which_camera():
    assert 'id="alarmCam"' in _HTML
    assert '<option value="all">All cameras</option>' in _HTML
    # The submit reads the explicit picker, not the hidden checkbox state.
    assert 'camera_id:(camSel&&camSel.value)||"all"' in _HTML


def test_every_target_input_shares_the_picklist():
    assert 'id="targetOpts"' in _HTML
    assert "loadAlarmTargets" in _HTML
    assert _HTML.count('list="targetOpts"') == 3   # alarm rail + alarm pop + watch pop


def test_popover_matches_rail_form():
    # Same components both places the operator can arm from: optional name,
    # target with pick-list, ring level, time window.
    assert 'id="paName"' in _HTML
    for surface in ("alarm", "pa"):
        assert f'id="{surface}Ring"' in _HTML
        assert f'id="{surface}When"' in _HTML
        assert f'id="{surface}After"' in _HTML

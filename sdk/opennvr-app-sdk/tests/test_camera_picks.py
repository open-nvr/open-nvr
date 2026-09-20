# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Apps work on the cameras picked for them — and nothing else.

A camera is picked for an app in the app's own configuration. Core
serves the app its picks (the roster route) and delivers them on the
live config poll. These tests pin the SDK half:

* ``roster()`` tells "nothing picked" (``[]``) apart from "core could not
  be asked" (``None``) — the difference between stopping and keeping on;
* a pick change reaches the app even though no config key changed;
* a subscriber drops other cameras' events, but only once it is
  connected and only for apps that take picks at all;
* one camera has three spellings, and a zone saved under one is found
  under the others.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx

from opennvr_app_sdk import (
    Alert, AlertDispatcher, AppManifest, Detector, FrameApp, OpenNVR,
    camera_key, per_camera_value,
)
from opennvr_app_sdk import contract as contract_mod


class _Recorder:
    name = "recorder"

    def __init__(self):
        self.alerts = []

    def send(self, alert):
        self.alerts.append(alert)
        return True


MANIFEST = AppManifest(id="picky", name="Picky", version="0.1.0",
                       description="test", category="test")
EXEMPT = AppManifest(id="relay", name="Relay", version="0.1.0",
                     description="test", category="test", camera_picker=False)


class _Det(Detector):
    manifest = MANIFEST

    def on_detections(self, camera_id, detections, event):
        yield Alert(title="hit", description="d", camera_id=camera_id)


class _ExemptDet(_Det):
    manifest = EXEMPT


def _cfg(**kw):
    base = {"contract_port": 0, "contract_bind_host": "127.0.0.1"}
    base.update(kw)
    return SimpleNamespace(**base)


def _event(camera):
    return {"camera_id": camera, "result": {"detections": [{"label": "person"}]}}


def _connected(det):
    # What start_config_poll leaves behind; the thread itself is not
    # needed to exercise the rule.
    det._config_poll_thread = object()
    return det


# ── one camera, three spellings ────────────────────────────────────


def test_camera_key_reads_every_spelling():
    assert camera_key(3) == 3
    assert camera_key("3") == 3
    assert camera_key("cam3") == 3
    assert camera_key("cam-3") == 3
    assert camera_key(SimpleNamespace(id=3)) == 3


def test_camera_key_never_raises_on_junk():
    for junk in (None, True, "", "front-door", "camx", [], {}):
        assert camera_key(junk) is None


def test_a_zone_saved_under_the_numeric_id_is_found_by_handle():
    """The catalog's zone editor saves {"3": [...]}; apps look up "cam3".
    Before this, the lookup missed and a drawn zone never applied."""
    zones = {"3": [[0, 0], [1, 0], [1, 1]]}
    assert per_camera_value(zones, "cam3") == zones["3"]
    assert per_camera_value({"cam3": "z"}, 3) == "z"
    assert per_camera_value(zones, "cam4") is None
    assert per_camera_value(zones, "cam4", default="none") == "none"


# ── roster(): nothing picked vs. could not ask ─────────────────────


def test_roster_is_none_when_core_cannot_be_asked():
    nvr = OpenNVR("http://core:8000", token="k")
    nvr._http.get_json = lambda *a, **k: None
    assert nvr.roster() is None
    assert nvr.cameras() == []


def test_roster_is_empty_when_nothing_is_picked():
    nvr = OpenNVR("http://core:8000", token="k")
    nvr._http.get_json = lambda *a, **k: {"cameras": []}
    assert nvr.roster() == []


def test_roster_lists_the_picked_cameras():
    nvr = OpenNVR("http://core:8000", token="k")
    nvr._http.get_json = lambda *a, **k: {"cameras": [
        {"camera_id": "cam2", "open_nvr_camera_id": 2, "name": "Gate"}]}
    assert [c.handle for c in nvr.roster()] == ["cam2"]


# ── picks arrive on the config poll ────────────────────────────────


class _FakeGet:
    def __init__(self, bodies):
        self.bodies = list(bodies)

    def __call__(self, url, headers=None, timeout=None, trust_env=None):
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))


class _Watching(_Det):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.picks_seen = []

    def on_cameras_update(self, camera_ids):
        self.picks_seen.append(set(camera_ids))


def test_a_pick_reaches_the_app_even_though_no_config_changed(monkeypatch):
    """Picking a camera is a claim on the camera, not an edit to this
    app's settings — so the config is identical on both polls, and the
    early return for an unchanged config must not swallow the pick."""
    monkeypatch.setattr(contract_mod.httpx, "get", _FakeGet([
        {"config": {"x": 1}, "cameras": []},
        {"config": {"x": 1}, "cameras": [2]},
        {"config": {"x": 1}, "cameras": [2]},
    ]))
    app = _Watching(_cfg(opennvr_url="http://reg:8000"), AlertDispatcher([_Recorder()]))
    url, headers = app._config_poll_target()
    for _ in range(3):
        app._config_poll_once(url, headers)
    assert app.picks_seen == [set(), {2}]
    assert app.picked_cameras == frozenset({2})


def test_an_old_core_without_picks_changes_nothing(monkeypatch):
    monkeypatch.setattr(contract_mod.httpx, "get", _FakeGet([{"config": {"x": 1}}]))
    app = _Watching(_cfg(opennvr_url="http://reg:8000"), AlertDispatcher([_Recorder()]))
    app._config_poll_once(*app._config_poll_target())
    assert app.picks_seen == []
    assert app.picked_cameras is None


# ── a subscriber acts only on picked cameras ───────────────────────


def test_standalone_detector_keeps_every_camera():
    """No core, no poll, no picks to wait for: a standalone run keeps
    working on its own YAML cameras."""
    det = _Det(_cfg(), AlertDispatcher([_Recorder()]))
    assert det.handle_event(_event("cam1"))


def test_connected_detector_fails_closed_until_picks_arrive():
    det = _connected(_Det(_cfg(), AlertDispatcher([_Recorder()])))
    assert det.handle_event(_event("cam1")) == []


def test_connected_detector_acts_only_on_picked_cameras():
    det = _connected(_Det(_cfg(), AlertDispatcher([_Recorder()])))
    det.picked_cameras = frozenset({2})
    assert det.handle_event(_event("cam1")) == []
    assert det.handle_event(_event("cam2"))
    assert det.handle_event(_event("2"))


def test_nothing_picked_means_nothing_acted_on():
    det = _connected(_Det(_cfg(), AlertDispatcher([_Recorder()])))
    det.picked_cameras = frozenset()
    assert det.handle_event(_event("cam1")) == []


def test_an_app_without_a_picker_is_never_filtered():
    """gate-controller and footage-search are Detectors too. An app that
    declares camera_picker=False acts on other apps' alerts and must not
    go deaf because it has no picks."""
    det = _connected(_ExemptDet(_cfg(), AlertDispatcher([_Recorder()])))
    det.picked_cameras = frozenset()
    assert det.handle_event(_event("cam1"))


def test_the_manifest_carries_the_picker_flag():
    assert MANIFEST.to_dict()["camera_picker"] is True
    assert EXEMPT.to_dict()["camera_picker"] is False


# ── a frame app follows its picks ──────────────────────────────────


class _Frames:
    def __init__(self):
        self.asked = []

    def get_frame(self, camera_id):
        self.asked.append(camera_id)
        return b"jpeg"


class _Poller(FrameApp):
    manifest = MANIFEST

    def on_frame(self, camera_id, frame_bytes):
        return None


def test_a_frame_app_polls_exactly_its_picked_cameras():
    frames = _Frames()
    app = _Poller(_cfg(), AlertDispatcher([_Recorder()]),
                  frame_source=frames, cameras=["front-door"])
    app.on_cameras_update(frozenset({3, 1}))
    app.handle_tick()
    assert frames.asked == ["cam1", "cam3"]


def test_a_frame_app_with_nothing_picked_fetches_nothing():
    frames = _Frames()
    app = _Poller(_cfg(), AlertDispatcher([_Recorder()]),
                  frame_source=frames, cameras=["front-door"])
    app.on_cameras_update(frozenset())
    app.handle_tick()
    assert frames.asked == []


# ── the operator's switch ──────────────────────────────────────────
#
# Disabling an app in the catalog used to change nothing it could feel:
# it kept its cameras, kept pulling their streams and kept driving its
# adapters, so the only real off switch was `docker stop`. The switch
# rides the same poll the picks do.


class _Switched(_Det):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.switches = []

    def on_enabled_update(self, enabled):
        self.switches.append(enabled)


def test_the_switch_arrives_on_the_config_poll(monkeypatch):
    monkeypatch.setattr(contract_mod.httpx, "get", _FakeGet([
        {"config": {"x": 1}, "enabled": True, "cameras": [2]},
        {"config": {"x": 1}, "enabled": False, "cameras": []},
        {"config": {"x": 1}, "enabled": False, "cameras": []},
    ]))
    app = _Switched(_cfg(opennvr_url="http://reg:8000"), AlertDispatcher([_Recorder()]))
    url, headers = app._config_poll_target()
    for _ in range(3):
        app._config_poll_once(url, headers)
    assert app.switches == [True, False]   # told once per change, not per poll
    assert app.app_enabled is False


def test_an_old_core_without_the_switch_leaves_the_app_working(monkeypatch):
    """``None`` is "not told", never "off" — an app must not stop
    because it is talking to a core that predates the flag."""
    monkeypatch.setattr(contract_mod.httpx, "get",
                        _FakeGet([{"config": {"x": 1}, "cameras": [2]}]))
    app = _Switched(_cfg(opennvr_url="http://reg:8000"), AlertDispatcher([_Recorder()]))
    app._config_poll_once(*app._config_poll_target())
    assert app.app_enabled is None and app.switches == []
    assert app.handle_event(_event("cam2"))


def test_a_disabled_app_acts_on_no_camera_it_still_holds():
    det = _connected(_Det(_cfg(), AlertDispatcher([_Recorder()])))
    det.picked_cameras = frozenset({2})
    assert det.handle_event(_event("cam2"))
    det.app_enabled = False
    assert det.handle_event(_event("cam2")) == []
    det.app_enabled = True
    assert det.handle_event(_event("cam2"))


def test_a_disabled_app_without_a_picker_stops_too():
    """The apps exempt from camera selection (relays, gateways) have no
    empty roster to stop them — the switch is all there is."""
    det = _connected(_ExemptDet(_cfg(), AlertDispatcher([_Recorder()])))
    det.app_enabled = False
    assert det.handle_event(_event("cam1")) == []


def test_a_disabled_frame_app_fetches_nothing():
    frames = _Frames()
    app = _Poller(_cfg(), AlertDispatcher([_Recorder()]),
                  frame_source=frames, cameras=["front-door"])
    app.app_enabled = False
    app.handle_tick()
    assert frames.asked == []


def test_a_disabled_app_says_so_on_health():
    det = _Det(_cfg(), AlertDispatcher([_Recorder()]))
    assert "enabled" not in det.health_snapshot()
    det.app_enabled = False
    health = det.health_snapshot()
    # Switched off is not a fault: /health stays ready and says why it
    # is quiet.
    assert health["enabled"] is False and health["ready"] is True

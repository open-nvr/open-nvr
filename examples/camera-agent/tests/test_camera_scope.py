# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""auth_mode="opennvr": per-camera RBAC.

The main server assigns cameras to users (ownership + CameraPermission
grants). The agent honours the SAME assignment: a guard who can see the
gate and not the yard gets the gate's roster entry, frames, events,
plates, alarms and app alerts — and "no such camera" for the yard, from
the HTTP routes and from the LLM's tools alike. Superusers see the
fleet; background loops (alarms, monitors) always see the fleet."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import (
    AlertRecord, CameraSpec, EventRecord, PlateRecord, camera_scope,
    set_camera_scope, spawn_unscoped,
)

USERS = {
    "tok-guard": {"username": "guard", "is_superuser": False, "role_name": "operator"},
    "tok-super": {"username": "root", "is_superuser": True, "role_name": "admin"},
    "tok-nobody": {"username": "nobody", "is_superuser": False, "role_name": "viewer"},
}
# Server camera ids each user may see (what GET /api/v1/cameras returns).
SCOPES = {"tok-guard": {1}, "tok-nobody": set()}


class _FakeAuth:
    def __init__(self):
        self.scope_calls = 0

    async def me(self, token):
        return USERS.get(token)

    async def visible_cameras(self, token, user=None):
        self.scope_calls += 1
        if user and user.get("is_superuser"):
            return None
        return set(SCOPES.get(token, set()))

    async def device_allowed(self, device_token):
        return True

    async def aclose(self):
        pass


def _client():
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        auth_mode="opennvr", opennvr_api_url="http://srv",
        cameras=[
            CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg",
                       role="the gate", opennvr_camera_id=1),
            CameraSpec(camera_id="cam2", frame_url="http://x/2.jpg",
                       role="the yard", opennvr_camera_id=2),
            # A config-only camera the server does not know: superuser-only.
            CameraSpec(camera_id="cam9", frame_url="http://x/9.jpg",
                       role="the workshop"),
        ],
    )
    rt = CameraAgentRuntime(cfg)
    rt.auth = _FakeAuth()
    return rt, TestClient(build_app(rt))


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _ids(resp):
    return sorted(c["camera_id"] for c in resp.json()["cameras"])


# ── the roster ─────────────────────────────────────────────────────────


def test_roster_is_the_users_assigned_cameras():
    _, c = _client()
    assert _ids(c.get("/cameras", headers=_h("tok-guard"))) == ["cam1"]
    assert _ids(c.get("/cameras", headers=_h("tok-super"))) == ["cam1", "cam2", "cam9"]
    assert _ids(c.get("/cameras", headers=_h("tok-nobody"))) == []


def test_unassigned_camera_reads_as_unknown_not_forbidden():
    """404, never 403: the guard must not learn the yard exists."""
    _, c = _client()
    assert c.get("/timeline/cam1", headers=_h("tok-guard")).status_code == 200
    assert c.get("/timeline/cam2", headers=_h("tok-guard")).status_code == 404
    assert c.get("/timeline/cam9", headers=_h("tok-guard")).status_code == 404
    assert c.get("/timeline/cam2", headers=_h("tok-super")).status_code == 200
    assert c.get("/frame/cam2", headers=_h("tok-guard")).status_code == 404


def test_scope_is_bound_only_for_the_request():
    """Outside a request (background alarm loops) the fleet is whole."""
    rt, c = _client()
    assert c.get("/cameras", headers=_h("tok-guard")).status_code == 200
    assert camera_scope() is None
    assert [x.camera_id for x in rt.context.cameras] == ["cam1", "cam2", "cam9"]


# ── the rings the tools read ───────────────────────────────────────────


def test_rings_and_prompt_roster_follow_the_scope():
    rt, _ = _client()
    now = time.time()
    rt.context.record_event(EventRecord(
        received_at=now, camera_id="cam1", adapter="tier0", summary="1 car"))
    rt.context.record_event(EventRecord(
        received_at=now, camera_id="cam2", adapter="tier0", summary="1 person"))
    rt.context.record_plate(PlateRecord(
        received_at=now, camera_id="cam2", plate_text="KA01AB1234", confidence=0.9))
    rt.context.record_app_alert(AlertRecord(
        received_at=now, app_id="lpr", camera_id="cam2", title="Unknown vehicle",
        severity="high", summary="on the yard"))
    rt.context.record_app_alert(AlertRecord(
        received_at=now, app_id="site", camera_id="", title="Backup done",
        severity="low", summary="site-wide notice"))

    token = set_camera_scope({"cam1"})
    try:
        assert {e.camera_id for e in rt.context.recent_events(
            camera_id=None, window_seconds=60)} == {"cam1"}
        assert rt.context.recent_events(camera_id="cam2", window_seconds=60) == []
        assert rt.context.latest_inference("cam2") is None
        assert rt.context.recent_plates(window_seconds=60) == []
        titles = [a.title for a in rt.context.recent_app_alerts(window_seconds=60)]
        assert titles == ["Backup done"]          # the camera-less notice only
        assert rt.context.known_camera("cam2") is False
        assert rt.context.get_camera("cam2") is None
        assert "the yard" not in rt.build_system_prompt()
        assert "the gate" in rt.build_system_prompt()
        # The LLM's camera resolver: "all" means all of MINE.
        assert rt.tools._resolve_cameras({"camera_id": "all"}) == ["cam1"]
        assert rt.tools._resolve_cameras({"camera_id": "cam2"}).startswith("ERROR")
    finally:
        from context import reset_camera_scope
        reset_camera_scope(token)
    # Unscoped again: everything is back.
    assert len(rt.context.recent_plates(window_seconds=60)) == 1


# ── alarms / monitors / the events feed ────────────────────────────────


def test_alarms_and_monitors_are_per_camera(monkeypatch):
    rt, c = _client()
    # The superuser arms one alarm on each camera and a watch on the yard.
    assert c.post("/alarms", headers=_h("tok-super"),
                  json={"name": "Gate", "target": "person", "camera_ids": ["cam1"]}
                  ).status_code == 202
    assert c.post("/alarms", headers=_h("tok-super"),
                  json={"name": "Yard", "target": "person", "camera_ids": ["cam2"]}
                  ).status_code == 202
    assert c.post("/monitors", headers=_h("tok-super"),
                  json={"kind": "notify", "target": "car", "camera_ids": ["cam2"]}
                  ).status_code == 202

    got = c.get("/alarms", headers=_h("tok-guard")).json()
    assert [a["name"] for a in got["alarms"]] == ["Gate"]
    assert c.get("/monitors", headers=_h("tok-guard")).json()["monitors"] == []
    assert len(c.get("/alarms", headers=_h("tok-super")).json()["alarms"]) == 2

    # Arming on a camera you cannot see fails as "unknown camera".
    r = c.post("/alarms", headers=_h("tok-guard"),
               json={"name": "Sneaky", "target": "person", "camera_ids": ["cam2"]})
    assert r.status_code == 400 and "cam2" in r.json()["error"]

    # Deleting someone else's alarm is a no-op, and it is still armed.
    yard_id = next(a["id"] for a in c.get("/alarms", headers=_h("tok-super")).json()["alarms"]
                   if a["name"] == "Yard")
    assert c.delete(f"/alarms/{yard_id}", headers=_h("tok-guard")).json()["stopped"] is False
    assert len(c.get("/alarms", headers=_h("tok-super")).json()["alarms"]) == 2

    # "Silence everything" silences only MY ringing alarms.
    for a in rt.alarms._alarms.values():
        a.triggered = True
    assert c.post("/alarms/ack", headers=_h("tok-guard"), json={}).json()["silenced"] == 1
    assert next(a for a in rt.alarms._alarms.values() if a.name == "Yard").triggered is True


def test_events_feed_is_scoped():
    rt, c = _client()
    now = time.time()
    rt.context.record_app_alert(AlertRecord(
        received_at=now, app_id="lpr", camera_id="cam2", title="Yard alert",
        severity="high", summary=""))
    rt.context.record_app_alert(AlertRecord(
        received_at=now, app_id="lpr", camera_id="cam1", title="Gate alert",
        severity="high", summary=""))
    rt.context.record_app_alert(AlertRecord(
        received_at=now, app_id="site", camera_id="", title="Notice",
        severity="low", summary=""))
    titles = lambda tok: sorted(e["title"] for e in c.get("/events", headers=_h(tok)).json()["events"])
    assert titles("tok-guard") == ["Gate alert", "Notice"]
    assert titles("tok-nobody") == ["Notice"]
    assert titles("tok-super") == ["Gate alert", "Notice", "Yard alert"]


def test_scope_lookup_is_cached_per_token():
    rt, c = _client()
    c.get("/cameras", headers=_h("tok-guard"))
    c.get("/cameras", headers=_h("tok-guard"))
    # The fake counts calls; the real client caches per token (see
    # OpennvrAuthClient.visible_cameras) — here we only assert the gate
    # asks the auth client rather than deciding on its own.
    assert rt.auth.scope_calls >= 1


def test_background_work_spawned_from_a_scoped_request_sees_the_fleet():
    """A task inherits its creator's context — an alarm loop started
    by a guard's POST /alarms must NOT stay pinned to the guard's
    cameras for the rest of its life."""
    import asyncio

    async def _probe():
        return camera_scope()

    async def main():
        token = set_camera_scope({"cam1"})
        try:
            inherited = await asyncio.create_task(_probe())
            cleared = await spawn_unscoped(_probe())
        finally:
            from context import reset_camera_scope
            reset_camera_scope(token)
        return inherited, cleared

    inherited, cleared = asyncio.run(main())
    assert inherited == frozenset({"cam1"})
    assert cleared is None

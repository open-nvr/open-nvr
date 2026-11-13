# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-114: server-described entities.

* what a caller sees is scoped: each descriptor's required_scope and its
  camera; tokens by their scopes and allow-list;
* conditional descriptors: the recording switch only with the pause flag,
  zone devices per zone, PTZ controls only for PTZ-capable cameras;
* app descriptors come from the manifest ``entities:``; malformed ones and
  controls naming undeclared actions are skipped; per-camera entities
  follow the app's cameras;
* states are resolved on the server (live state, event store, app /state);
* commands run only for descriptors the caller can see, re-check the
  camera, and are audited; app actions get the camera handle;
* the publisher pushes only what changed, v2-only, scope-tagged.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from services import entity_descriptors as ed
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    import services.live_state as ls_mod
    from services import entity_state_publisher as pub, ptz_presets_cache

    monkeypatch.setattr(ls_mod, "_instance", ls_mod.LiveState())
    monkeypatch.setattr(pub, "_states", {})
    monkeypatch.setattr(pub, "_meta", {})
    monkeypatch.setattr(pub, "_etag", None)
    monkeypatch.setattr(ptz_presets_cache, "_presets", {})
    monkeypatch.setattr(ed, "_app_state", {})


def _keys(env, headers):  # noqa: F811
    r = env.client.get("/api/v1/entities", headers=headers)
    assert r.status_code == 200, r.text
    return {e["key"]: e for e in r.json()["entities"]}


def test_admin_sees_site_and_every_camera_with_an_etag(env):  # noqa: F811
    r = env.client.get("/api/v1/entities", headers=env.jwt("admin"))
    keys = {e["key"] for e in r.json()["entities"]}
    assert {"site.alerts_unacknowledged", "site.storage_used", "camera.1.motion",
            "camera.3.count.person", "camera.2.detection"} <= keys
    assert "camera.1.recording" not in keys           # pause flag is off
    etag = r.headers["etag"]
    again = env.client.get("/api/v1/entities",
                           headers={**env.jwt("admin"), "If-None-Match": etag})
    assert again.status_code == 304
    d = next(e for e in r.json()["entities"] if e["key"] == "camera.1.detection")
    assert d["command"] == {"type": "core_control", "control": "detection"}
    assert d["descriptor_version"] == ed.DESCRIPTOR_VERSION and "state_path" not in d


def test_scoping_by_permission_camera_and_token(env):  # noqa: F811
    vera = _keys(env, env.jwt("vera"))            # camera 3, no alerts.view/cameras.manage
    assert all(k.startswith(("site.", "camera.3.")) for k in vera)
    assert "site.storage_used" in vera and "site.alerts_unacknowledged" not in vera
    assert "camera.3.detection" not in vera and "camera.3.motion" in vera
    tok = _mint(env, scopes=["cameras.view"], camera_ids=[1])["token"]
    got = _keys(env, _as(tok))
    assert got and all(k.startswith("camera.1.") for k in got)
    assert "camera.1.last_plate" not in got      # needs recordings.view


def test_conditional_descriptors(env):  # noqa: F811
    from services import ptz_presets_cache, site_settings

    s = env.Session()
    site_settings.set_json(s, site_settings.RECORDING_PAUSE_KEY, True)
    s.add(env.models.CameraZone(camera_id=1, name="drive",
                                polygon=[[0, 0], [1, 0], [1, 1]], labels=["car"]))
    s.add(env.models.CameraCapability(camera_id=2, supported_areas={"ptz": True}))
    s.commit()
    zid = s.query(env.models.CameraZone).one().id
    s.close()
    ptz_presets_cache.put(2, [{"token": "1", "name": "Gate"}])
    keys = _keys(env, env.jwt("admin"))
    assert keys["camera.1.recording"]["enabled_default"] is False
    assert keys[f"zone.{zid}.count.car"]["device"]["camera_id"] == 1
    assert f"zone.{zid}.count.person" not in keys          # zone is cars only
    assert keys["camera.2.ptz_preset"]["options"] == ["Gate"]
    assert "camera.2.ptz_up" in keys and "camera.1.ptz_up" not in keys


def _install_app(env, entities, actions=None, enabled=True):  # noqa: F811
    s = env.Session()
    s.add(env.models.InstalledApp(
        id="abandoned-object", name="Abandoned object", category="perimeter",
        version="1.1.0", url="http://abandoned:8080", enabled=enabled,
        manifest_json={"id": "abandoned-object", "entities": entities,
                       "actions": actions if actions is not None else [
                           {"name": "acknowledge", "params": [{"name": "camera"},
                                                              {"name": "track"}]}]}))
    s.commit()
    s.close()


def test_app_descriptors_from_the_manifest(env, monkeypatch):  # noqa: F811
    import services.app_keys as ak

    monkeypatch.setattr(ak, "app_camera_ids", lambda db, row: {1, 3})
    _install_app(env, [
        {"key": "unattended_now", "platform": "sensor", "name": "Unattended",
         "state_path": "unattended_now"},
        {"key": "unattended", "platform": "sensor", "name": "Here", "per_camera": True,
         "state_path": "per_camera[camera={camera}].unattended"},
        {"key": "acknowledge", "platform": "button", "name": "Ack", "per_camera": True,
         "action": "acknowledge"},
        {"key": "launch", "platform": "button", "name": "x", "action": "launch"},   # undeclared
        {"key": "odd", "platform": "lock", "name": "x"},                            # unknown
        {"key": "Bad-Key", "platform": "sensor", "name": "x", "state_path": "a"},  # bad key
    ])
    keys = _keys(env, env.jwt("admin"))
    app_keys = sorted(k for k in keys if k.startswith("app."))
    assert app_keys == ["app.abandoned-object.1.acknowledge", "app.abandoned-object.1.unattended",
                        "app.abandoned-object.3.acknowledge", "app.abandoned-object.3.unattended",
                        "app.abandoned-object.unattended_now"]
    assert keys["app.abandoned-object.3.unattended"]["device"] == {"kind": "camera", "id": 3}
    assert keys["app.abandoned-object.1.acknowledge"]["required_scope"] == "apps.actions"
    # vera: no apps.view in her role → no app entities.
    assert not any(k.startswith("app.") for k in _keys(env, env.jwt("vera")))


def test_disabled_apps_have_no_entities(env):  # noqa: F811
    _install_app(env, [{"key": "n", "platform": "sensor", "name": "n", "state_path": "n"}],
                 enabled=False)
    assert not any(k.startswith("app.") for k in _keys(env, env.jwt("admin")))


def test_states_are_resolved_on_the_server(env, monkeypatch):  # noqa: F811
    import services.app_keys as ak
    import services.live_state as ls_mod

    monkeypatch.setattr(ak, "app_camera_ids", lambda db, row: {3})
    _install_app(env, [{"key": "unattended", "platform": "sensor", "name": "Here",
                        "per_camera": True,
                        "state_path": "per_camera[camera={camera}].unattended"}])
    ed.set_app_state("abandoned-object", {"per_camera": [{"camera": "cam3", "unattended": 2}]})
    ls_mod.get_live_state().update(1, {"frame": {"w": 100, "h": 100}, "tracks": [
        {"id": 1, "label": "person", "box": [1, 1, 10, 10], "score": 0.9, "matched": True}]})
    s = env.Session()
    s.add(env.models.AppAlert(alert_id="x", fired_at=datetime.now(UTC), severity="critical",
                              title="t"))
    s.commit()
    s.close()
    states = env.client.get("/api/v1/entities/states", headers=env.jwt("admin")).json()["states"]
    assert states["camera.1.count.person"]["state"] == 1
    assert states["camera.1.occupancy.person"]["state"] is True
    assert states["camera.2.occupancy.all"]["state"] is False
    assert states["camera.1.motion"]["state"] is True
    assert states["camera.1.detection"]["state"] is True
    assert states["site.alerts_highest_severity"]["state"] == "critical"
    assert states["site.alerts_unacknowledged_count"]["state"] == 1
    assert states["app.abandoned-object.3.unattended"]["state"] == 2
    assert "camera.1.detections" not in states          # event entities have no state


def test_commands(env, monkeypatch):  # noqa: F811
    A = env.jwt("admin")
    r = env.client.post("/api/v1/entities/camera.1.detection/command", headers=A,
                        json={"value": False})
    assert r.status_code == 200, r.text
    s = env.Session()
    assert s.get(env.models.Camera, 1).detection_enabled is False
    s.add(env.models.AppAlert(alert_id="y", fired_at=datetime.now(UTC), severity="high",
                              title="t"))
    s.commit()
    s.close()
    r = env.client.post("/api/v1/entities/site.ack_all_alerts/command", headers=A, json={})
    assert r.json()["result"] == {"acknowledged": 1}
    r = env.client.post("/api/v1/entities/camera.2.manual_event/command", headers=A,
                        json={"args": {"label": "doorbell"}})
    assert r.status_code == 200 and r.json()["result"]["event_id"]
    s = env.Session()
    audits = s.query(env.models.AuditLog).filter_by(action="entity.command").count()
    s.close()
    assert audits == 3
    # Unknown key, a key the caller can't see, a sensor (no command): all 404.
    assert env.client.post("/api/v1/entities/camera.9.detection/command", headers=A,
                           json={}).status_code == 404
    assert env.client.post("/api/v1/entities/camera.3.detection/command",
                           headers=env.jwt("vera"), json={"value": True}).status_code == 404
    assert env.client.post("/api/v1/entities/camera.1.motion/command", headers=A,
                           json={}).status_code == 404


def test_a_token_commands_only_within_its_scopes(env):  # noqa: F811
    view = _mint(env, scopes=["cameras.view"], camera_ids=[1])["token"]
    assert env.client.post("/api/v1/entities/camera.1.detection/command", headers=_as(view),
                           json={"value": False}).status_code == 404
    manage = _mint(env, name="m", scopes=["cameras.view", "cameras.manage"],
                   camera_ids=[1])["token"]
    assert env.client.post("/api/v1/entities/camera.1.detection/command", headers=_as(manage),
                           json={"value": False}).status_code == 200
    assert env.client.post("/api/v1/entities/camera.2.detection/command", headers=_as(manage),
                           json={"value": False}).status_code == 404


def test_app_action_gets_the_camera_handle(env, monkeypatch):  # noqa: F811
    import routers.apps as apps
    import services.app_keys as ak

    monkeypatch.setattr(ak, "app_camera_ids", lambda db, row: {3})
    _install_app(env, [{"key": "acknowledge", "platform": "button", "name": "Ack",
                        "per_camera": True, "action": "acknowledge"}])
    calls = []

    async def fake(app_id, action_name, params, current_user, db):
        calls.append((app_id, action_name, params))
        return {"ok": True}

    monkeypatch.setattr(apps, "invoke_app_action", fake)
    r = env.client.post("/api/v1/entities/app.abandoned-object.3.acknowledge/command",
                        headers=env.jwt("admin"), json={"args": {"track": "7", "evil": 1}})
    assert r.status_code == 200, r.text
    assert calls == [("abandoned-object", "acknowledge", {"track": "7", "camera": "cam3"})]


def test_publisher_pushes_only_changes(env, monkeypatch):  # noqa: F811
    import services.live_state as ls_mod
    from services import entity_state_publisher as pub, event_bus_service as ebs

    sent = []

    async def fake(event):
        sent.append(event)

    bus = ebs.EventBus()
    monkeypatch.setattr(ebs, "_event_bus_instance", bus)
    monkeypatch.setattr(bus, "publish", fake)
    pub._forget()
    # Cold (first pass after idling): primes the cache and publishes nothing.
    # A burst of every state would overflow subscribers' queues; they got
    # these states in their snapshot instead.
    asyncio.run(pub.tick())
    assert sent == [] and pub.is_warm() and "camera.1.detection" in pub.current_states()
    asyncio.run(pub.tick())
    assert sent == []
    ls_mod.get_live_state().update(2, {"frame": {"w": 100, "h": 100}, "tracks": [
        {"id": 1, "label": "car", "box": [1, 1, 10, 10], "score": 0.9, "matched": True}]})
    asyncio.run(pub.tick())
    changed = {e["payload"]["key"] for e in sent}
    assert "camera.2.count.car" in changed and all(k.startswith("camera.2.") for k in changed)
    assert all(e["v2_only"] and e["required_scope"] for e in sent)
    sent.clear()
    s = env.Session()
    s.add(env.models.CameraZone(camera_id=1, name="z", polygon=[[0, 0], [1, 0], [1, 1]]))
    s.commit()
    s.close()
    asyncio.run(pub.tick())
    assert any(e["event_type"] == "descriptors_changed" for e in sent)


def test_entity_state_is_v2_only_and_scope_filtered():
    from routers.events import _v2_may_send
    from services import event_bus_service as ebs

    ev = {"event_type": "entity_state", "v2_only": True, "site_wide": True,
          "required_scope": "alerts.view", "payload": {}}
    assert not ebs._Subscriber(10, None, None).matches(ev)             # v1
    assert ebs._Subscriber(10, None, None, with_seq=True).matches(ev)  # v2
    assert _v2_may_send(ev, {"alerts.view"}) and not _v2_may_send(ev, {"cameras.view"})


# ── M1 review fixes ───────────────────────────────────────────────────


def test_site_alert_counts_are_fleet_only(env):  # noqa: F811
    """A caller limited to some cameras must not learn other cameras' alert counts."""
    admin = _keys(env, env.jwt("admin"))
    assert "site.alerts_unacknowledged_count" in admin
    tok = _mint(env, scopes=["cameras.view", "alerts.view"], camera_ids=[1])["token"]
    assert not any(k.startswith("site.alerts_") for k in _keys(env, _as(tok)))


def test_command_values_are_validated(env, monkeypatch):  # noqa: F811
    from services import site_settings

    s = env.Session()
    site_settings.set_json(s, site_settings.RECORDING_PAUSE_KEY, True)
    s.close()
    A = env.jwt("admin")
    cmd = lambda key, body: env.client.post(f"/api/v1/entities/{key}/command",  # noqa: E731
                                            headers=A, json=body).status_code
    assert cmd("camera.1.detection", {}) == 422                       # no value
    assert cmd("camera.1.detection", {"value": "yes"}) == 422
    for bad in (5, -1, 10**15, "3600", True):
        assert cmd("camera.1.recording", {"value": False,
                                          "args": {"resume_after_s": bad}}) == 422
    assert cmd("camera.1.manual_event", {"args": {"label": "<b>"}}) == 422
    assert cmd("camera.1.manual_event", {"args": {"note": "x" * 501}}) == 422


def test_app_action_works_for_an_admin_owned_token(env, monkeypatch):  # noqa: F811
    """invoke_app_action compared a camera against scope=None (unrestricted)
    and raised TypeError for every admin-owned token."""
    import routers.apps as apps
    import services.app_keys as ak

    monkeypatch.setattr(ak, "app_camera_ids", lambda db, row: {3})
    _install_app(env, [{"key": "acknowledge", "platform": "button", "name": "Ack",
                        "per_camera": True, "action": "acknowledge"}])

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(apps.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(apps, "validate_app_url", lambda url: None)
    tok = _mint(env, scopes=["cameras.view", "apps.view", "apps.actions"])["token"]
    r = env.client.post("/api/v1/entities/app.abandoned-object.3.acknowledge/command",
                        headers=_as(tok), json={})
    assert r.status_code == 200, r.text


def test_publisher_is_idle_until_someone_uses_entities(env, monkeypatch):  # noqa: F811
    from services import entity_state_publisher as pub, event_bus_service as ebs

    monkeypatch.setattr(ebs, "_event_bus_instance", ebs.EventBus())
    monkeypatch.setattr(pub, "_rest_used_at", None)
    assert pub.wanted() is False
    env.client.get("/api/v1/entities/states", headers=env.jwt("admin"))
    assert pub.wanted() is True


def test_a_turned_off_camera_keeps_its_entities(env):  # noqa: F811
    """Off is a state, not an absence: a client that dropped the entities would
    lose the user's names and automations, and could not turn it back on."""
    s = env.Session()
    s.get(env.models.Camera, 2).is_active = False
    s.commit()
    s.close()
    keys = _keys(env, env.jwt("admin"))
    assert "camera.2.online" in keys and "camera.2.detection" in keys


def test_last_plate_and_object_are_batched_across_cameras(env):  # noqa: F811
    """Review: resolve_states ran two ``LIMIT 1`` queries PER camera on every
    publisher tick (2 s while any HA bridge is connected). The statement
    count must not grow with the camera count, and each camera's answer
    must still be its own newest plate and newest evidenced visit."""
    from datetime import timedelta

    from sqlalchemy import event

    from models import TimelineEvent
    from services.timeline_service import record_track_visit

    s = env.Session()
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    expect = {}
    for cid in (1, 2, 3):
        # Newest row per camera has no plate and no evidence, so the two
        # answers are distinct rows and neither is "just the newest".
        first = record_track_visit(s, camera_id=cid, label="car", started_at=t0,
                                   evidence_path="a/b.jpg")
        if cid != 3:
            first.plate_text = f"P{cid}"
            s.commit()
        second = record_track_visit(s, camera_id=cid, label="truck",
                                    started_at=t0 + timedelta(minutes=1),
                                    evidence_path="c/d.jpg")
        record_track_visit(s, camera_id=cid, label="person",
                           started_at=t0 + timedelta(minutes=2))
        expect[cid] = (first.id, second.id)
    # Two visits with the SAME started_at: the tie goes to the higher id.
    tie_a = record_track_visit(s, camera_id=1, label="bus", started_at=t0 + timedelta(minutes=5),
                               evidence_path="e/f.jpg")
    tie_b = record_track_visit(s, camera_id=1, label="van", started_at=t0 + timedelta(minutes=5),
                               evidence_path="g/h.jpg")
    assert tie_b.id > tie_a.id
    expect[1] = (expect[1][0], tie_b.id)
    # Outside the lookback: a plate older than 7 days must not answer for
    # a camera whose only recent visits carry none.
    record_track_visit(s, camera_id=3, label="ghost",
                       started_at=datetime.now(UTC) - timedelta(days=30),
                       evidence_path="x/y.jpg").plate_text = "OLD"
    s.commit()

    statements: list[str] = []
    engine = env.Session.kw["bind"]

    def _on(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _on)
    try:
        descs = ed.all_descriptors(s)
        one = [d for d in descs if d.camera_id in (None, 1)]
        statements.clear()
        got_one = ed.resolve_states(s, one)
        n_one = len(statements)
        statements.clear()
        got_all = ed.resolve_states(s, descs)
        n_all = len(statements)
        # The helper on its own: one statement per call, for 1 or 3 cameras.
        statements.clear()
        ed.latest_events(s, [1], TimelineEvent.plate_text.isnot(None))
        assert len(statements) == 1
        statements.clear()
        ed.latest_events(s, [1, 2, 3], TimelineEvent.plate_text.isnot(None))
        assert len(statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", _on)
        s.close()
    assert n_all == n_one, (n_one, n_all)
    assert got_one["camera.1.last_plate"] == got_all["camera.1.last_plate"]
    for cid, (plate_id, obj_id) in expect.items():
        if cid != 3:
            assert got_all[f"camera.{cid}.last_plate"] == {
                "state": f"P{cid}", "attributes": {"event_id": plate_id}}
        assert got_all[f"camera.{cid}.last_object"]["attributes"]["event_id"] == obj_id
    assert got_all["camera.3.last_plate"] == {"state": None, "attributes": {}}
    assert got_all["camera.1.last_object"]["attributes"]["label"] == "van"

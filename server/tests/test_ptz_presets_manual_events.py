# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-107: PTZ presets, manual events and protecting an event's footage.

What is pinned:

* preset XML is parsed across vendor dialects, preset tokens are validated
  before they reach the SOAP body and names are escaped;
* PTZ move/stop/presets need ``ptz.control`` on top of the camera check;
* manual events: create (open or with a duration), end, and the rules
  around them (only manual events end, at most once; no future dates);
* a token reaches these only with the right scope and only on its cameras,
  including the camera named in the POST body;
* protect flags every clip OVERLAPPING the event window, including the
  segment that began before the event did.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

# ── ONVIF presets (pure) ──────────────────────────────────────────────────


def test_presets_parse_across_vendor_dialects():
    from services.onvif_digest_service import _parse_presets

    text = """
    <tptz:GetPresetsResponse>
      <tptz:Preset token="1"><tt:Name>Gate</tt:Name><tt:PTZPosition/></tptz:Preset>
      <Preset fixed="false" token="Preset_2"><Name>Drive &amp; path</Name></Preset>
      <ns2:Preset token="3"/>
      <tptz:Preset><tt:Name>no token, skipped</tt:Name></tptz:Preset>
    </tptz:GetPresetsResponse>"""
    assert _parse_presets(text) == [
        {"token": "1", "name": "Gate"},
        {"token": "Preset_2", "name": "Drive & path"},
        {"token": "3", "name": "3"},
    ]


def test_a_bad_preset_token_never_reaches_the_camera(monkeypatch):
    import services.onvif_digest_service as od

    async def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("SOAP request made with an unchecked token")

    monkeypatch.setattr(od, "_onvif_request", boom)
    monkeypatch.setattr(od, "get_ptz_service_url", boom)
    for bad in ("1</tptz:PresetToken><x>", "", "a" * 65, "sp ace"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(od.ptz_goto_preset_digest("10.0.0.1", "u", "p", "prof", bad))
        assert exc.value.status_code == 422


def test_preset_names_are_escaped_into_the_soap_body(monkeypatch):
    import services.onvif_digest_service as od

    sent = {}

    async def fake_url(*a, **k):
        return "http://cam/onvif/ptz"

    async def fake_req(url, body, *a, **k):
        sent["body"] = body
        return 200, "<tptz:SetPresetResponse><tptz:PresetToken>7</tptz:PresetToken></tptz:SetPresetResponse>"

    monkeypatch.setattr(od, "get_ptz_service_url", fake_url)
    monkeypatch.setattr(od, "_onvif_request", fake_req)
    out = asyncio.run(od.ptz_set_preset_digest("10.0.0.1", "u", "p", "prof", "<Gate> & co"))
    assert "&lt;Gate&gt; &amp; co" in sent["body"] and "<Gate>" not in sent["body"]
    assert out == {"token": "7", "name": "<Gate> & co"}


# ── routes ────────────────────────────────────────────────────────────────


@pytest.fixture()
def ptz(env, monkeypatch):  # noqa: F811
    """Camera 1 gets ONVIF credentials; the camera calls are faked."""
    from services.ptz_service import PTZService

    s = env.Session()
    cam = s.get(env.models.Camera, 1)
    cam.username, cam.password = "onvif", "secret"
    s.commit()
    s.close()
    calls = []

    async def presets(**kw):
        calls.append(("presets", kw["camera_id"]))
        return [{"token": "1", "name": "Gate"}]

    async def goto(**kw):
        calls.append(("goto", kw["camera_id"], kw["preset_token"]))
        return {"status": "moving", "preset": kw["preset_token"]}

    async def save(**kw):
        calls.append(("save", kw["camera_id"], kw["name"], kw["preset_token"]))
        return {"token": kw["preset_token"] or "9", "name": kw["name"]}

    async def move(**kw):
        calls.append(("move", kw["camera_id"]))
        return {"success": True}

    monkeypatch.setattr(PTZService, "presets", staticmethod(presets))
    monkeypatch.setattr(PTZService, "goto_preset", staticmethod(goto))
    monkeypatch.setattr(PTZService, "set_preset", staticmethod(save))
    monkeypatch.setattr(PTZService, "move", staticmethod(move))
    return calls


def _last_audit(env, action):
    s = env.Session()
    try:
        row = (s.query(env.models.AuditLog).filter_by(action=action)
               .order_by(env.models.AuditLog.id.desc()).first())
        return row, (json.loads(row.details) if row and row.details else {})
    finally:
        s.close()


def test_presets_list_goto_and_save(env, ptz):  # noqa: F811
    A = env.jwt("admin")
    r = env.client.get("/api/v1/cameras/1/ptz/presets", headers=A)
    assert r.status_code == 200 and r.json()["presets"] == [{"token": "1", "name": "Gate"}]
    r = env.client.post("/api/v1/cameras/1/ptz/presets/1/goto", headers=A)
    assert r.status_code == 200 and r.json()["preset"] == "1"
    r = env.client.post("/api/v1/cameras/1/ptz/presets", headers=A, json={"name": " Drive "})
    assert r.status_code == 200 and r.json()["token"] == "9"
    assert ("save", 1, "Drive", None) in ptz
    row, details = _last_audit(env, "ptz.preset_goto")
    assert row is not None and details["token"] == "1"
    assert _last_audit(env, "ptz.preset_save")[1]["name"] == "Drive"


def test_ptz_needs_the_ptz_control_permission(env, ptz):  # noqa: F811
    """vera's role has live.view but not ptz.control in this fixture."""
    s = env.Session()
    s.get(env.models.Camera, 3).owner_id = env.ids["viewer"]
    s.get(env.models.Camera, 3).username = "u"
    s.get(env.models.Camera, 3).password = "p"
    s.commit()
    s.close()
    V = env.jwt("vera")
    assert env.client.post("/api/v1/cameras/3/ptz/move?x=0.5", headers=V).status_code == 403
    assert env.client.get("/api/v1/cameras/3/ptz/presets", headers=V).status_code == 403
    assert not any(c[0] in ("move", "presets") for c in ptz)


def test_a_token_steers_only_its_cameras_with_the_scope(env, ptz):  # noqa: F811
    tok = _mint(env, scopes=["cameras.view", "ptz.control"], camera_ids=[1])["token"]
    r = env.client.post("/api/v1/cameras/1/ptz/presets/1/goto",
                        headers={**_as(tok), "X-Correlation-Id": "ha-preset-1"})
    assert r.status_code == 200, r.text
    row, details = _last_audit(env, "ptz.preset_goto")
    assert details["actor"].startswith("token:") and row.correlation_id == "ha-preset-1"
    assert env.client.post("/api/v1/cameras/2/ptz/presets/1/goto",
                           headers=_as(tok)).status_code == 403
    no_scope = _mint(env, name="noptz", scopes=["cameras.view"])["token"]
    assert env.client.post("/api/v1/cameras/1/ptz/presets/1/goto",
                           headers=_as(no_scope)).status_code == 403


def test_manual_event_lifecycle(env):  # noqa: F811
    A = env.jwt("admin")
    r = env.client.post("/api/v1/events", headers=A,
                        json={"camera_id": 1, "label": "Doorbell", "note": "pressed"})
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["source"] == "manual" and ev["label"] == "doorbell" and ev["ended_at"] is None
    assert ev["payload"] == {"note": "pressed", "created_by": "user:admin"}
    listed = env.client.get("/api/v1/events?source=manual", headers=A).json()["events"]
    assert [e["id"] for e in listed] == [ev["id"]]

    r = env.client.put(f"/api/v1/events/{ev['id']}/end", headers=A)
    assert r.status_code == 200 and r.json()["ended_at"] is not None
    assert env.client.put(f"/api/v1/events/{ev['id']}/end", headers=A).status_code == 409

    r = env.client.post("/api/v1/events", headers=A,
                        json={"camera_id": 1, "duration_s": 30})
    assert r.status_code == 201 and r.json()["ended_at"] is not None
    assert _last_audit(env, "event.create")[0] is not None


def test_only_manual_events_can_be_ended(env):  # noqa: F811
    from services.timeline_service import record_track_visit

    s = env.Session()
    row = record_track_visit(s, camera_id=1, label="person",
                             started_at=datetime.now(UTC) - timedelta(minutes=1))
    rid = row.id
    s.close()
    assert env.client.put(f"/api/v1/events/{rid}/end",
                          headers=env.jwt("admin")).status_code == 409


@pytest.mark.parametrize("body, status", [
    # Times are offsets from NOW, resolved when the test runs, not when
    # pytest collects it (a long suite made "5 minutes ahead" the past).
    ({"camera_id": 1, "started_in": timedelta(minutes=5)}, 422),
    ({"camera_id": 1, "started_in": -timedelta(hours=25)}, 422),
    ({"camera_id": 1, "label": "<script>"}, 422),
    ({"camera_id": 1, "duration_s": 0}, 422),
    ({"camera_id": 999}, 404),
])
def test_manual_event_validation(env, body, status):  # noqa: F811
    body = dict(body)
    if "started_in" in body:
        body["started_at"] = (datetime.now(UTC) + body.pop("started_in")).isoformat()
    assert env.client.post("/api/v1/events", headers=env.jwt("admin"),
                           json=body).status_code == status


def test_manual_events_need_the_permission_and_the_camera(env):  # noqa: F811
    # vera has no events.create.
    assert env.client.post("/api/v1/events", headers=env.jwt("vera"),
                           json={"camera_id": 3}).status_code == 403
    tok = _mint(env, scopes=["cameras.view", "events.create"], camera_ids=[1])["token"]
    assert env.client.post("/api/v1/events", headers=_as(tok),
                           json={"camera_id": 1}).status_code == 201
    # The camera is in the BODY: the gate can't see it, the route must.
    assert env.client.post("/api/v1/events", headers=_as(tok),
                           json={"camera_id": 2}).status_code == 403
    no_scope = _mint(env, name="ro", scopes=["cameras.view"])["token"]
    assert env.client.post("/api/v1/events", headers=_as(no_scope),
                           json={"camera_id": 1}).status_code == 403


def test_protect_flags_every_clip_overlapping_the_event(env):  # noqa: F811
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    s = env.Session()
    R = env.models.Recording

    def clip(cam, start, secs):
        s.add(R(camera_id=cam, filename=f"{cam}-{start:%H%M%S}.mp4",
                file_path=f"/rec/{cam}/{start:%H%M%S}.mp4",
                start_time=start, end_time=start + timedelta(seconds=secs) if secs else None))

    clip(1, t0 - timedelta(seconds=50), 60)   # began before the event: holds its start
    clip(1, t0 + timedelta(seconds=10), 60)   # inside
    clip(1, t0 + timedelta(minutes=5), 60)    # well after: not protected
    clip(2, t0, 60)                           # other camera
    s.commit()
    s.close()

    A = env.jwt("admin")
    ev = env.client.post("/api/v1/events", headers=A, json={
        "camera_id": 1, "started_at": t0.isoformat(), "duration_s": 30}).json()
    r = env.client.post(f"/api/v1/events/{ev['id']}/protect", headers=A,
                        json={"pre_s": 5, "post_s": 5})
    assert r.status_code == 200, r.text
    assert r.json()["updated_clips"] == 2
    s = env.Session()
    flagged = sorted((c.camera_id, c.start_time.replace(tzinfo=UTC)) for c in
                     s.query(R).filter(R.is_flagged.is_(True)).all())
    s.close()
    assert flagged == [(1, t0 - timedelta(seconds=50)), (1, t0 + timedelta(seconds=10))]
    assert _last_audit(env, "recording.protect")[1]["updated_clips"] == 2


def test_protect_is_scoped_to_what_the_caller_sees(env):  # noqa: F811
    ev = env.client.post("/api/v1/events", headers=env.jwt("admin"),
                         json={"camera_id": 1, "duration_s": 5}).json()
    # vera sees only camera 3: 404, not 403 (don't confirm it exists).
    assert env.client.post(f"/api/v1/events/{ev['id']}/protect",
                           headers=env.jwt("vera")).status_code == 404

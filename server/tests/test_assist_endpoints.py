# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""What Home Assistant and Assist ask core (HA-116, HA-501/502).

* main's plain-language ``GET /search`` (its own tests are test_search.py):
  the ``zone`` filter added for Home Assistant, and API-token scoping;
* ``GET /search/summary`` counts events by label and alerts by severity per
  camera, scoped like ``/search``;
* ``POST /cameras/{id}/describe`` words from a caption/VQA adapter, audited
  and rate-limited.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from services import scene_description
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

T0 = datetime(2026, 9, 18, 8, 0, tzinfo=UTC)
SEARCH_ACTION = {"name": "search", "params": [{"name": "query", "type": "str"},
                                              {"name": "limit", "type": "int"}]}


@pytest.fixture()
def data(env):  # noqa: F811
    s = env.Session()
    Z = env.models.CameraZone
    drive = Z(camera_id=1, name="Driveway", polygon=[[0, 0], [1, 0], [1, 1]])
    porch = Z(camera_id=2, name="Porch", polygon=[[0, 0], [1, 0], [1, 1]])
    s.add_all([drive, porch])
    s.commit()
    E = env.models.TimelineEvent

    def ev(cam, label, minutes, zones=None, plate=None):
        s.add(E(camera_id=cam, source="tier0", event_type="track", label=label,
                started_at=T0 + timedelta(minutes=minutes),
                ended_at=T0 + timedelta(minutes=minutes, seconds=20),
                zone_ids=zones, plate_text=plate))

    ev(1, "car", 0, [drive.id], plate="KA01AB1234")
    ev(1, "person", 5, [3, drive.id])
    ev(1, "person", 10, [drive.id + 10])       # zone 1x, not zone x
    ev(2, "person", 15, [porch.id])
    ev(3, "dog", 20)
    s.add(env.models.AppAlert(alert_id="a1", fired_at=T0 + timedelta(minutes=7),
                              severity="critical", title="Person loitering at gate",
                              camera_id="cam1", source_name="loitering"))
    s.add(env.models.AppAlert(alert_id="a2", fired_at=T0 + timedelta(minutes=8),
                              severity="low", title="Plate read", camera_id="cam2",
                              source_name="anpr"))
    s.commit()
    ids = {"drive": drive.id, "porch": porch.id}
    s.close()
    return ids


def _search(env, headers=None, status=200, **params):  # noqa: F811
    r = env.client.get("/api/v1/search", headers=headers or env.jwt("admin"), params=params)
    assert r.status_code == status, r.text
    return r.json()


def test_zone_filter_by_id_or_name_never_by_prefix(env, data):  # noqa: F811
    drive = _search(env, parse=False, zone=str(data["drive"]))
    # The car (only zone) and the person (last of two zones); NOT the event in
    # zone drive+10, which a prefix match would also have returned.
    assert [r["label"] for r in drive["results"]] == ["person", "car"]
    assert drive["interpretation"]["zone_id"] == data["drive"]
    assert "zone_id" in drive["interpretation"]["overridden"]
    assert [r["camera_id"] for r in _search(env, parse=False, zone="porch")["results"]] == [2]
    _search(env, status=404, parse=False, zone="garden")
    _search(env, status=404, parse=False, zone="drive%")          # no wildcards
    # vera (camera 3 only) cannot probe the Porch zone on camera 2.
    _search(env, env.jwt("vera"), status=404, parse=False, zone="Porch")


def test_tokens_need_recordings_view_and_keep_to_their_cameras(env, data):  # noqa: F811
    tok = _mint(env, scopes=["cameras.view", "recordings.view"], camera_ids=[2])["token"]
    got = _search(env, _as(tok), parse=False)
    assert {r["camera_id"] for r in got["results"]} == {2}
    no_rec = _mint(env, name="n", scopes=["cameras.view", "alerts.view"])["token"]
    _search(env, _as(no_rec), status=403, parse=False)


# ── summary ─────────────────────────────────────────────────────────────


def _summary(env, headers=None, status=200, **params):  # noqa: F811
    params.setdefault("from", T0.isoformat())
    params.setdefault("to", (T0 + timedelta(hours=1)).isoformat())
    r = env.client.get("/api/v1/search/summary", headers=headers or env.jwt("admin"),
                       params=params)
    assert r.status_code == status, r.text
    return r.json()


def test_summary_counts_per_camera(env, data):  # noqa: F811
    s = env.Session()
    s.add(env.models.AppAlert(alert_id="site", fired_at=T0 + timedelta(minutes=9),
                              severity="high", title="Disk", camera_id=None))
    s.commit()
    s.close()
    out = _summary(env)
    cams = {c["camera_id"]: c for c in out["cameras"]}
    assert cams[1]["events"] == {"car": 1, "person": 2} and cams[1]["alerts"] == {"critical": 1}
    assert cams[1]["first_event"].startswith("2026-09-18T08:00")
    assert cams[1]["last_event"].startswith("2026-09-18T08:10")
    assert cams[2]["events"] == {"person": 1} and cams[2]["alerts"] == {"low": 1}
    assert cams[3]["events"] == {"dog": 1} and out["cameras"][0]["camera_id"] == 1
    assert out["site_alerts"] == {"high": 1}
    assert out["totals"] == {"events": 5, "alerts": 3}
    # A narrower window and one camera.
    one = _summary(env, camera_id=2)
    assert [c["camera_id"] for c in one["cameras"]] == [2] and one["site_alerts"] == {}
    late = _summary(env, **{"from": (T0 + timedelta(minutes=12)).isoformat()})
    assert late["totals"] == {"events": 2, "alerts": 0}


def test_summary_scope_and_limits(env, data):  # noqa: F811
    # vera: camera 3, recordings.view only.
    out = _summary(env, env.jwt("vera"))
    assert [c["camera_id"] for c in out["cameras"]] == [3] and out["totals"]["alerts"] == 0
    _summary(env, env.jwt("vera"), status=404, camera_id=1)
    tok = _mint(env, scopes=["cameras.view", "alerts.view"], camera_ids=[1])["token"]
    out = _summary(env, _as(tok))
    assert [(c["camera_id"], c["event_count"], c["alert_count"]) for c in out["cameras"]] == \
        [(1, 0, 1)]
    _summary(env, status=422, to=(T0 - timedelta(minutes=1)).isoformat())
    _summary(env, status=422, to=(T0 + timedelta(days=40)).isoformat())


# ── describe ────────────────────────────────────────────────────────────


def test_pick_and_text_of():
    adapters = [{"name": "blip", "tasks_advertised": ["scene_caption"]},
                {"name": "moondream", "tasks_advertised": ["visual_qa", "scene_caption"]}]
    assert scene_description.pick(adapters, None) == ("blip", "scene_caption")
    assert scene_description.pick(adapters, "is the gate open?") == ("moondream", "visual_qa")
    assert scene_description.pick(adapters[:1], "is the gate open?") == ("blip", "scene_caption")
    assert scene_description.pick([{"name": "yolo", "tasks_advertised": ["x"]}], None) is None
    assert scene_description.text_of({"result": {"answer": " Yes. "}}) == "Yes."
    assert scene_description.text_of({"caption": "a car"}) == "a car"
    assert scene_description.text_of({"result": {}}) is None


def test_rate_limit_is_per_caller():
    scene_description._calls.clear()
    for i in range(scene_description.PER_MINUTE):
        assert scene_description._allow("a", now=100.0 + i)
    assert not scene_description._allow("a", now=110.0)
    assert scene_description._allow("b", now=110.0)
    assert scene_description._allow("a", now=161.0)         # the first has aged out


def test_describe_route(env, monkeypatch):  # noqa: F811
    calls = []

    async def fake(camera, caller, question=None):
        calls.append((camera.id, caller, question))
        if question == "slow down":
            raise scene_description.RateLimited()
        return {"available": True, "description": "a red car in the drive",
                "model": "ollamavlm", "task": "visual_qa" if question else "scene_caption"}

    monkeypatch.setattr(scene_description, "describe", fake)
    r = env.client.post("/api/v1/cameras/1/describe", headers=env.jwt("admin"),
                        json={"question": "any cars?"})
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "a red car in the drive" and r.json()["camera_id"] == 1
    r = env.client.post("/api/v1/cameras/1/describe", headers=env.jwt("admin"))
    assert r.status_code == 200 and calls[-1][2] is None
    assert env.client.post("/api/v1/cameras/1/describe", headers=env.jwt("admin"),
                           json={"question": "slow down"}).status_code == 429
    # A token needs live.view and the camera.
    tok = _mint(env, scopes=["cameras.view", "live.view"], camera_ids=[1])
    assert env.client.post("/api/v1/cameras/1/describe",
                           headers=_as(tok["token"])).status_code == 200
    assert calls[-1][1] == "user:1"   # the token's owner: the limit is per person
    assert env.client.post("/api/v1/cameras/2/describe",
                           headers=_as(tok["token"])).status_code == 403
    no_live = _mint(env, name="n", scopes=["cameras.view"])["token"]
    assert env.client.post("/api/v1/cameras/1/describe",
                           headers=_as(no_live)).status_code == 403
    s = env.Session()
    try:
        rows = s.query(env.models.AuditLog).filter_by(action="camera.describe").all()
        details = [json.loads(r.details) if isinstance(r.details, str) else r.details
                   for r in rows]
        assert len(rows) == 3 and details[0]["question"] is True
        assert "any cars" not in json.dumps(details) and "red car" not in json.dumps(details)
    finally:
        s.close()


def test_describe_without_an_adapter(monkeypatch):
    import asyncio

    async def none():
        return []

    monkeypatch.setattr(scene_description, "_registry", none)
    scene_description._calls.clear()
    cam = type("C", (), {"id": 1, "rtsp_url": "rtsp://x"})()
    out = asyncio.run(scene_description.describe(cam, "t"))
    assert out == {"available": False, "description": None, "model": None, "task": None}


_ = datetime, UTC


# ── review hardening (HA-503) ───────────────────────────────────────────


def test_call_app_action_as_a_token(env, monkeypatch):  # noqa: F811
    """The factored-out transport, called with a TokenPrincipal: posts the
    params to the app's action and returns its JSON, with the timeout given."""
    import routers.apps as apps_router
    from services import api_tokens

    seen = {}

    class FakeClient:
        def __init__(self, timeout=None, verify=None):
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            seen.update(url=url, json=json, headers=headers)
            import httpx

            return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(apps_router.httpx, "AsyncClient", FakeClient)
    minted = _mint(env, scopes=["cameras.view", "recordings.view"], camera_ids=[1])
    s = env.Session()
    try:
        s.add(env.models.InstalledApp(
            id="fs", name="x", version="1", url="http://footage-search:9215", enabled=True,
            manifest_json={"actions": [SEARCH_ACTION]}, config_json={}))
        s.commit()
        row = s.get(env.models.InstalledApp, "fs")
        token = s.get(env.models.ApiToken, minted["id"])
        owner = s.get(env.models.User, token.owner_user_id)
        principal = api_tokens._principal(owner, token)
        import asyncio

        out = asyncio.run(apps_router.call_app_action(s, row, "search", {"query": "x"},
                                                      principal, timeout=8.0))
    finally:
        s.close()
    assert out == {"results": []} and seen["timeout"] == 8.0
    assert seen["url"] == "http://footage-search:9215/actions/search"
    assert seen["json"] == {"query": "x"}


def test_describe_calls_kai_c_one_at_a_time(monkeypatch):
    import asyncio

    import httpx

    import services.kai_c_service as kcs

    async def registry():
        return [{"name": "moondream", "tasks_advertised": ["visual_qa"]}]

    class Capture:
        async def capture_frame_bytes(self, url, cid):
            return b"\xff\xd8jpeg"

    calls = []

    class FakeClient:
        def __init__(self, timeout=None, trust_env=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            calls.append((url, json["params"] if "params" in json else json))
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"result": {"answer": "Yes, a van."}})

    monkeypatch.setattr(scene_description, "_registry", registry)
    monkeypatch.setattr(kcs, "get_kai_c_service", lambda: Capture())
    monkeypatch.setattr(scene_description.httpx if hasattr(scene_description, "httpx")
                        else httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(scene_description, "MAX_WAITING", 1)
    scene_description._calls.clear()
    cam = type("C", (), {"id": 4, "rtsp_url": ""})()

    async def scenario():
        return await asyncio.gather(
            scene_description.describe(cam, "a", "a van?"),
            scene_description.describe(cam, "b", "a van?"),
            scene_description.describe(cam, "c", "a van?"), return_exceptions=True)

    out = asyncio.run(scenario())
    answered = [o for o in out if isinstance(o, dict)]
    assert answered and all(o["description"] == "Yes, a van." for o in answered)
    assert any(isinstance(o, scene_description.Busy) for o in out)   # the queue is full
    assert calls[0][0].endswith("/api/v1/infer/moondream")


def test_describe_route_releases_and_reports_busy(env, monkeypatch):  # noqa: F811
    async def busy(camera, caller, question=None):
        raise scene_description.Busy()

    monkeypatch.setattr(scene_description, "describe", busy)
    r = env.client.post("/api/v1/cameras/1/describe", headers=env.jwt("admin"))
    assert r.status_code == 503 and r.headers.get("Retry-After") == "30"

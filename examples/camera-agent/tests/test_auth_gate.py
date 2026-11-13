# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""auth_mode="opennvr": bearer gate, permission tiers, login/refresh
proxies, token-validation caching, and the viewer toolset. Default
("none") stays wide open — the rest of the suite is that regression."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec

USERS = {
    "tok-viewer": {"username": "v", "is_superuser": False, "role_name": "viewer"},
    "tok-op": {"username": "o", "is_superuser": False, "role_name": "operator"},
    "tok-admin": {"username": "a", "is_superuser": False, "role_name": "admin"},
    "tok-super": {"username": "s", "is_superuser": True, "role_name": "viewer"},
}


class _FakeAuth:
    def __init__(self):
        self.me_calls = 0
        self.device_calls = 0
        self.device_ok = True   # device firewall allows by default

    async def me(self, token):
        self.me_calls += 1
        return USERS.get(token)

    async def visible_cameras(self, token, user=None):
        return None   # unrestricted — per-camera scope has its own tests

    async def device_allowed(self, device_token):
        self.device_calls += 1
        return self.device_ok

    async def login(self, username, password, totp_code=None, **kw):
        if (username, password) == ("admin", "pw"):
            return 200, {"access_token": "tok-admin", "refresh_token": "r1",
                         "token_type": "bearer"}
        return 401, {"detail": "Incorrect username or password"}

    async def refresh(self, refresh_token):
        if refresh_token == "r1":
            return 200, {"access_token": "tok-admin", "refresh_token": "r2",
                         "token_type": "bearer"}
        return 401, {"detail": "Invalid or expired refresh token"}

    async def aclose(self):
        pass


def _client(auth_mode="opennvr"):
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        auth_mode=auth_mode, opennvr_api_url="http://srv",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front")],
    )
    rt = CameraAgentRuntime(cfg)
    rt.auth = _FakeAuth()
    return rt, TestClient(build_app(rt))


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── the gate ───────────────────────────────────────────────────────────


def test_data_endpoints_401_without_token_page_shell_open():
    _, c = _client()
    assert c.get("/cameras").status_code == 401
    assert c.get("/monitors").status_code == 401
    assert c.post("/ask", json={"text": "x"}).status_code == 401
    # the shell that RENDERS the login stays reachable
    assert c.get("/health").status_code == 200
    assert c.get("/demo").status_code == 200
    assert c.get("/demo/camera/cam1").status_code == 200
    assert c.get("/agent").json()["auth_mode"] == "opennvr"


def test_none_mode_stays_open():
    _, c = _client(auth_mode="none")
    assert c.get("/cameras").status_code == 200
    assert c.post("/auth/login", json={"username": "a", "password": "b"}).status_code == 404


# ── tiers ──────────────────────────────────────────────────────────────


def test_viewer_can_look_but_not_touch():
    _, c = _client()
    assert c.get("/cameras", headers=_h("tok-viewer")).status_code == 200
    assert c.get("/monitors", headers=_h("tok-viewer")).status_code == 200
    r = c.post("/monitors", headers=_h("tok-viewer"),
               json={"kind": "notify", "target": "car", "camera_ids": ["cam1"]})
    assert r.status_code == 403 and "operator" in r.json()["error"]
    assert c.post("/skills/see/disable", headers=_h("tok-viewer")).status_code == 403


def test_operator_arms_but_cannot_govern():
    _, c = _client()
    r = c.post("/monitors", headers=_h("tok-op"),
               json={"kind": "notify", "target": "car", "camera_ids": ["cam1"]})
    assert r.status_code == 202
    assert c.post("/alarms", headers=_h("tok-op"),
                  json={"name": "A", "target": "person", "camera_ids": ["cam1"]}
                  ).status_code == 202
    r = c.post("/skills/see/disable", headers=_h("tok-op"))
    assert r.status_code == 403 and "admin" in r.json()["error"]


def test_admin_and_superuser_govern():
    _, c = _client()
    assert c.post("/skills/see/disable", headers=_h("tok-admin")).status_code == 200
    assert c.post("/skills/restore", headers=_h("tok-super")).status_code == 200


# ── login / refresh proxies ────────────────────────────────────────────


def test_login_proxy_passthrough():
    _, c = _client()
    ok = c.post("/auth/login", json={"username": "admin", "password": "pw"})
    assert ok.status_code == 200 and ok.json()["access_token"] == "tok-admin"
    assert ok.json()["refresh_token"] == "r1"          # mobile needs the pair
    bad = c.post("/auth/login", json={"username": "admin", "password": "no"})
    assert bad.status_code == 401
    assert c.post("/auth/login", json={}).status_code == 400
    ref = c.post("/auth/refresh", json={"refresh_token": "r1"})
    assert ref.status_code == 200 and ref.json()["refresh_token"] == "r2"


# ── viewer toolset (chat can't arm anything) ───────────────────────────


def test_viewer_chat_toolset_has_no_mutating_verbs(monkeypatch):
    rt, c = _client()
    seen = {}

    async def fake_turn(runtime, history, text, *, tool_definitions=None, **kw):
        seen["tools"] = {t["function"]["name"] for t in (tool_definitions or [])}
        return "ok"

    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)
    assert c.post("/ask", json={"text": "arm an alarm"},
                  headers=_h("tok-viewer")).status_code == 200
    assert seen["tools"], "viewer turn ran with an empty toolset"
    forbidden = {"create_alarm", "stop_alarm", "create_monitor", "stop_monitor",
                 "create_report", "stop_report", "create_background_task",
                 "enroll_face", "forget_face"}
    assert not (seen["tools"] & forbidden), seen["tools"] & forbidden
    # operators get the full set
    assert c.post("/ask", json={"text": "x"}, headers=_h("tok-op")).status_code == 200
    assert "create_alarm" in seen["tools"]


# ── validation cache (real client, stubbed transport) ──────────────────


def test_me_validation_is_cached_per_token():
    from adapter_clients import OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv")
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        def json(self):
            return {"username": "v", "role_name": "viewer", "is_superuser": False}

    class _Http:
        async def get(self, url, headers=None):
            calls["n"] += 1
            return _Resp()
        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError

    client._client = lambda: _Http()
    for _ in range(5):
        assert asyncio.run(client.me("tok"))["role_name"] == "viewer"
    assert calls["n"] == 1                     # 4 hits served from cache


# ── a slow core is not a logout (#540) ─────────────────────────────────
#
# `me()` answers None only when the SERVER rejected the token. When the
# server could not be asked, it raises AuthUnavailable and the gate says
# 503 — "ask again" — instead of 401, which told the page the session was
# over and sent the operator back to the login card.


def _unreachable_client(**kw):
    """A real auth client whose transport always times out."""
    from adapter_clients import OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv", **kw)

    class _Http:
        async def get(self, url, headers=None):
            raise TimeoutError()          # str() is "" — as in the logs

        async def post(self, *a, **k):
            raise TimeoutError()

    client._client = lambda: _Http()
    return client


def test_unreachable_core_is_not_an_invalid_token():
    from adapter_clients import AuthUnavailable

    client = _unreachable_client()
    try:
        asyncio.run(client.me("tok"))
    except AuthUnavailable:
        pass
    else:                                   # pragma: no cover
        raise AssertionError("a timeout must not read as a rejected token")


def test_a_transport_failure_is_never_cached_as_a_verdict():
    """The bug: the None from a timeout went into the cache next to a
    real 401, so one slow round-trip 401'd every request for a TTL."""
    from adapter_clients import AuthUnavailable, OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv", unavailable_backoff=0.0)
    state = {"fail": True}

    class _Resp:
        status_code = 200

        def json(self):
            return {"username": "v", "role_name": "viewer", "is_superuser": False}

    class _Http:
        async def get(self, url, headers=None):
            if state["fail"]:
                raise TimeoutError()
            return _Resp()

        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError

    client._client = lambda: _Http()
    try:
        asyncio.run(client.me("tok"))
    except AuthUnavailable:
        pass
    state["fail"] = False
    # Recovery is immediate: nothing about the failure was remembered.
    assert asyncio.run(client.me("tok"))["role_name"] == "viewer"


def test_a_validated_token_rides_out_a_blip():
    """A live session must not end because one round-trip was slow."""
    from adapter_clients import OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv", ttl_seconds=0.0,
                               unavailable_backoff=0.0, grace_seconds=120.0)
    state = {"fail": False}

    class _Resp:
        status_code = 200

        def json(self):
            return {"username": "v", "role_name": "viewer", "is_superuser": False}

    class _Http:
        async def get(self, url, headers=None):
            if state["fail"]:
                raise TimeoutError()
            return _Resp()

        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError

    client._client = lambda: _Http()
    assert asyncio.run(client.me("tok"))["role_name"] == "viewer"
    state["fail"] = True
    assert asyncio.run(client.me("tok"))["role_name"] == "viewer"


def test_a_rejected_token_stays_rejected_while_core_is_down():
    """Riding out a blip is for tokens the server ALREADY validated. A
    token it turned down must not be admitted by an outage."""
    from adapter_clients import AuthUnavailable, OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv", ttl_seconds=0.0,
                               unavailable_backoff=0.0)
    state = {"fail": False}

    class _Resp:
        status_code = 401

        def json(self):  # pragma: no cover
            return {}

    class _Http:
        async def get(self, url, headers=None):
            if state["fail"]:
                raise TimeoutError()
            return _Resp()

        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError

    client._client = lambda: _Http()
    assert asyncio.run(client.me("bad")) is None
    state["fail"] = True
    try:
        asyncio.run(client.me("bad"))
    except AuthUnavailable:
        pass
    else:                                   # pragma: no cover
        raise AssertionError("an outage must not upgrade a rejected token")


def test_the_gate_answers_503_not_401_when_core_cannot_be_asked():
    from adapter_clients import AuthUnavailable

    rt, c = _client()

    class _Unreachable(_FakeAuth):
        async def me(self, token):
            raise AuthUnavailable("core down")

    rt.auth = _Unreachable()
    r = c.get("/cameras", headers=_h("tok-viewer"))
    assert r.status_code == 503
    assert r.json()["error"] == "core_unreachable"
    assert r.headers.get("Retry-After") == "2"


def test_the_refresh_proxy_says_try_again_not_refused():
    """The 502 the page got here read as a refused refresh, so the next
    401 raised the login card on a core that was merely busy."""
    client = _unreachable_client()
    status, data = asyncio.run(client.refresh("r1"))
    assert status == 503
    assert data["error"] == "core_unreachable"


def test_the_login_proxy_says_try_again_not_refused():
    client = _unreachable_client()
    status, data = asyncio.run(client.login("admin", "pw"))
    assert status == 503
    assert data["error"] == "core_unreachable"


def test_the_page_does_not_log_out_on_a_transient_refresh_failure():
    """Source lockstep with demo/index.html: a refresh the server could
    not answer must not end the session."""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "demo" / "index.html").read_text(
        encoding="utf-8")
    body = html[html.index("async function _tryRefresh()"):]
    body = body[:body.index("window.fetch=async function")]
    # Any 5xx on the refresh route: the exchange did not complete. Only a
    # 4xx is the server refusing the refresh token.
    assert 'if(r.status>=500) return "unavailable";' in body
    wrapper = html[html.index("window.fetch=async function"):]
    wrapper = wrapper[:wrapper.index("// Device-firewall denial")]
    # A core-unreachable 503 is a reconnect, never the login card.
    assert 'if(await _coreUnreachable(r)){ showCoreOffline(); return r; }' in wrapper
    assert 'if(outcome==="unavailable"){ showCoreOffline(); return r; }' in wrapper
    assert "showLogin()" in wrapper


def test_the_page_does_not_blame_core_for_an_offline_camera():
    """/frame answers 503 for a camera that is merely off, and the page
    polls it constantly. Only the gate's marker means "core is down"."""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "demo" / "index.html").read_text(
        encoding="utf-8")
    fn = html[html.index("async function _coreUnreachable(r)"):]
    fn = fn[:fn.index("async function _tryRefresh()")]
    assert 'd.error==="core_unreachable"' in fn
    # Never the bare status.
    wrapper = html[html.index("window.fetch=async function"):]
    wrapper = wrapper[:wrapper.index("// Device-firewall denial")]
    assert "r.status===503" not in wrapper


def test_a_frame_503_is_not_a_core_outage():
    """The other half of the lockstep: the agent's own 503 for an
    unreachable camera carries no body, so it can't be mistaken for the
    gate's."""
    import camera_agent as _ca

    src = (_ca.__file__)
    body = open(src, encoding="utf-8").read()
    frame = body[body.index('@app.get("/frame/{camera_id}")'):]
    frame = frame[:frame.index('@app.get("/timeline/{camera_id}")')]
    assert "Response(status_code=503)" in frame
    assert "core_unreachable" not in frame


def test_the_camera_scope_rides_out_a_blip_too():
    """Staying signed in is no use if every camera vanishes from the
    page for the duration. The reused scope is what the server itself
    gave us — an outage can never widen it."""
    from adapter_clients import OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv", ttl_seconds=0.0)
    state = {"fail": False}

    class _Resp:
        status_code = 200

        def json(self):
            return {"cameras": [{"id": 7}, {"id": 9}]}

    class _Http:
        async def get(self, url, params=None, headers=None):
            if state["fail"]:
                raise TimeoutError()
            return _Resp()

        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError

    client._client = lambda: _Http()
    assert asyncio.run(client.visible_cameras("tok")) == {7, 9}
    state["fail"] = True
    assert asyncio.run(client.visible_cameras("tok")) == {7, 9}


def test_an_unknown_token_still_fails_closed_when_core_is_down():
    """No scope was ever handed out for this token, so there is nothing
    to ride — empty, never "everything"."""
    client = _unreachable_client()
    assert asyncio.run(client.visible_cameras("never-seen")) == set()


# ── a 5xx from core is "could not ask", not "refused" ──────────────────
#
# Behind a reverse proxy a slow core usually surfaces as 502/504, which
# is a RESPONSE, not a transport exception — so catching only exceptions
# left the logout intact in its most likely shape.


class _Clock:
    """A monotonic clock the test drives, so grace windows are exact."""

    def __init__(self, t=1000.0):
        self.t = t

    def monotonic(self):
        return self.t


def _status_client(status, monkeypatch=None, **kw):
    from adapter_clients import OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv", **kw)
    calls = {"n": 0}

    class _Resp:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            if self.status_code != 200:      # pragma: no cover
                raise AssertionError("body read on a non-200")
            return {"username": "v", "role_name": "viewer", "is_superuser": False}

    class _Http:
        async def get(self, url, params=None, headers=None):
            calls["n"] += 1
            return _Resp(status["code"])

        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError

    client._client = lambda: _Http()
    return client, calls


def test_a_gateway_timeout_from_core_is_not_an_invalid_token():
    from adapter_clients import AuthUnavailable

    status = {"code": 504}
    client, _ = _status_client(status)
    try:
        asyncio.run(client.me("tok"))
    except AuthUnavailable:
        pass
    else:                                   # pragma: no cover
        raise AssertionError("a 504 must not read as a rejected token")


def test_a_gateway_timeout_is_never_cached_as_a_verdict():
    from adapter_clients import AuthUnavailable

    status = {"code": 502}
    client, _ = _status_client(status, unavailable_backoff=0.0)
    try:
        asyncio.run(client.me("tok"))
    except AuthUnavailable:
        pass
    status["code"] = 200
    assert asyncio.run(client.me("tok"))["role_name"] == "viewer"


def test_a_validated_token_rides_out_a_gateway_timeout():
    status = {"code": 200}
    client, _ = _status_client(status, ttl_seconds=0.0, unavailable_backoff=0.0)
    assert asyncio.run(client.me("tok"))["role_name"] == "viewer"
    status["code"] = 504
    assert asyncio.run(client.me("tok"))["role_name"] == "viewer"


def test_a_401_from_core_is_still_a_rejection():
    """Guard against over-correcting: only 5xx is 'could not ask'."""
    status = {"code": 401}
    client, _ = _status_client(status)
    assert asyncio.run(client.me("tok")) is None


def test_the_grace_window_does_not_renew_itself(monkeypatch):
    """The ride-out is cached, so it must be measured from the last
    answer the SERVER gave — otherwise an outage renews its own grace
    for as long as it lasts, and a revoked token never expires."""
    import adapter_clients as ac

    clock = _Clock()
    monkeypatch.setattr(ac.time, "monotonic", clock.monotonic)

    status = {"code": 200}
    client, calls = _status_client(status, ttl_seconds=60.0, grace_seconds=120.0)
    assert asyncio.run(client.visible_cameras("tok")) == set()

    status["code"] = 504
    clock.t += 61                            # past the TTL
    asyncio.run(client.visible_cameras("tok"))
    clock.t += 11                            # past the cached ride
    asyncio.run(client.visible_cameras("tok"))
    clock.t += 60                            # now 132s past the last answer
    before = calls["n"]
    # Grace is spent: it fails closed instead of riding forever.
    assert asyncio.run(client.visible_cameras("tok")) == set()
    assert calls["n"] > before               # it did re-ask


def test_the_scope_ride_is_cached_so_it_stops_paying_the_timeout(monkeypatch):
    """Riding out a blip is pointless if every request behind it still
    blocks on the doomed call."""
    import adapter_clients as ac

    clock = _Clock()
    monkeypatch.setattr(ac.time, "monotonic", clock.monotonic)

    from adapter_clients import OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv", ttl_seconds=60.0)
    state = {"fail": False}
    calls = {"n": 0}

    class _Resp:
        status_code = 200

        def json(self):
            return {"cameras": [{"id": 4}]}

    class _Http:
        async def get(self, url, params=None, headers=None):
            calls["n"] += 1
            if state["fail"]:
                raise TimeoutError()
            return _Resp()

        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError

    client._client = lambda: _Http()
    assert asyncio.run(client.visible_cameras("tok")) == {4}
    state["fail"] = True
    clock.t += 61
    assert asyncio.run(client.visible_cameras("tok")) == {4}
    asked = calls["n"]
    # Every request in the next few seconds is served from the ride.
    for _ in range(5):
        clock.t += 1
        assert asyncio.run(client.visible_cameras("tok")) == {4}
    assert calls["n"] == asked


def test_a_refused_scope_is_not_ridden_after_an_outage(monkeypatch):
    """Core answered ABOUT this token and it was not a scope (401). A
    later outage must not resurrect what the server has since refused."""
    import adapter_clients as ac

    clock = _Clock()
    monkeypatch.setattr(ac.time, "monotonic", clock.monotonic)

    from adapter_clients import OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv", ttl_seconds=60.0)
    state = {"code": 200, "fail": False}

    class _Resp:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return {"cameras": [{"id": 4}]}

    class _Http:
        async def get(self, url, params=None, headers=None):
            if state["fail"]:
                raise TimeoutError()
            return _Resp(state["code"])

        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError

    client._client = lambda: _Http()
    assert asyncio.run(client.visible_cameras("tok")) == {4}
    state["code"] = 401
    clock.t += 61
    assert asyncio.run(client.visible_cameras("tok")) == set()
    state["fail"] = True
    clock.t += 11
    assert asyncio.run(client.visible_cameras("tok")) == set()


def test_the_backoff_does_not_renew_itself(monkeypatch):
    """Requests arriving inside the backoff must not push its deadline
    forward, or a busy page keeps core from ever being retried."""
    import adapter_clients as ac

    clock = _Clock()
    monkeypatch.setattr(ac.time, "monotonic", clock.monotonic)

    status = {"code": 504}
    client, calls = _status_client(status, unavailable_backoff=2.0)
    from adapter_clients import AuthUnavailable

    def _ask():
        try:
            return asyncio.run(client.me("tok"))
        except AuthUnavailable:
            return None

    _ask()
    asked = calls["n"]
    for _ in range(10):                      # a busy page, inside the backoff
        clock.t += 0.1
        _ask()
    assert calls["n"] == asked               # served by the backoff, not core
    clock.t += 2.1                           # the backoff has expired
    status["code"] = 200
    assert _ask()["role_name"] == "viewer"   # and core IS retried


def test_an_unreadable_body_is_not_a_verdict():
    """A 200 we cannot parse says nothing about the token — and must not
    crash the gate either, which is where the parse used to sit."""
    from adapter_clients import AuthUnavailable, OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv")

    class _Resp:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    class _Http:
        async def get(self, url, params=None, headers=None):
            return _Resp()

        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError

    client._client = lambda: _Http()
    try:
        asyncio.run(client.me("tok"))
    except AuthUnavailable:
        pass
    else:                                   # pragma: no cover
        raise AssertionError("an unreadable body must not read as a verdict")
    assert asyncio.run(client.visible_cameras("tok")) == set()

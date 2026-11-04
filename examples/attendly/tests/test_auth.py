"""The login proxy and the gate, against a mocked core."""
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from attendly.auth import AuthUnavailable, OpenNVRAuth, public_user, user_tier
from attendly.gate import AuthGate, is_api_path, required_tier, set_gate

CORE = "http://core.test"
ADMIN = {"id": 1, "username": "root", "email": "root@example.com", "role_name": "admin", "is_superuser": True,
         "mfa_enabled": True, "created_at": "2026-01-01T00:00:00Z"}
VIEWER = {"id": 2, "username": "guard", "email": "", "role_name": "viewer", "is_superuser": False, "mfa_enabled": False}
OPERATOR = {"id": 3, "username": "teacher", "email": "t@example.com", "role_name": "operator", "is_superuser": False}


def test_tiers_and_public_shape():
    assert user_tier(ADMIN) == "admin" and user_tier(VIEWER) == "viewer" and user_tier(OPERATOR) == "operator"
    u = public_user(ADMIN)
    assert u == {"id": "1", "username": "root", "email": "root@example.com", "mfa_enabled": True, "is_admin": True,
                 "role_name": "admin", "tier": "admin", "created_at": "2026-01-01T00:00:00Z"}


def test_required_tier_rules():
    assert required_tier("GET", "/students/") == "viewer"
    assert required_tier("POST", "/students/register_new") == "operator"
    assert required_tier("PUT", "/classrooms/3") == "operator"
    assert required_tier("POST", "/registry/student/AT1/soft_delete") == "admin"
    assert required_tier("DELETE", "/registry/student/AT1/faces") == "admin"
    assert required_tier("POST", "/attendance/refinalize/all") == "admin"
    assert is_api_path("/students") and is_api_path("/api/auth/me") and not is_api_path("/manage-rooms")
    assert not is_api_path("/assets/app.js") and not is_api_path("/")


@respx.mock
def test_login_proxy_passthrough_and_mfa_202():
    auth = OpenNVRAuth(base_url=CORE)
    route = respx.post(f"{CORE}/api/v1/auth/login-json")
    route.mock(return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "token_type": "bearer"}))
    status, data = auth.login("root", "pw", device_token="dev-1", user_agent="UA/1")
    assert status == 200 and data["access_token"] == "a"
    req = route.calls[0].request
    assert req.headers["x-device-token"] == "dev-1" and req.headers["user-agent"] == "UA/1"
    import json

    assert json.loads(req.content) == {"username": "root", "password": "pw"}

    route.mock(return_value=httpx.Response(401, json={"detail": {"message": "Invalid or missing MFA code", "remaining_attempts": 4}}))
    status, data = auth.login("root", "pw")
    assert status == 202 and "MFA" in data["detail"]
    # with a wrong code it stays a 401
    status, _ = auth.login("root", "pw", "000000")
    assert status == 401
    assert json.loads(route.calls[-1].request.content)["code"] == "000000"

    route.mock(side_effect=httpx.ConnectError("down"))
    assert auth.login("root", "pw")[0] == 503


@respx.mock
def test_me_caches_and_rides_out_outages():
    auth = OpenNVRAuth(base_url=CORE, ttl_seconds=60, grace_seconds=120)
    me = respx.get(f"{CORE}/api/v1/auth/me")
    me.mock(return_value=httpx.Response(200, json=ADMIN))
    assert auth.me("tok")["username"] == "root"
    assert auth.me("tok")["username"] == "root"
    assert me.call_count == 1  # cached
    me.mock(return_value=httpx.Response(401, json={"detail": "nope"}))
    assert auth.me("bad") is None
    assert auth.me("bad") is None and me.call_count == 2
    # a never-seen token during an outage is "cannot say", not "rejected"
    me.mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(AuthUnavailable):
        auth.me("fresh")
    # a known-good token rides the outage within the grace window
    auth._cache["tok"] = (auth._cache["tok"][0] - 61, auth._cache["tok"][1])
    assert auth.me("tok")["username"] == "root"
    me.mock(return_value=httpx.Response(502, text="bad gateway"))
    assert auth.me("tok")["username"] == "root"


@pytest.fixture
def gated_client(env):
    import attendly_app

    set_gate(AuthGate(auth_mode="opennvr", auth=OpenNVRAuth(base_url=CORE)))
    app = attendly_app.build_app(start_engine=False, register=False)
    with TestClient(app) as c:
        yield c
    set_gate(None)


@respx.mock
def test_gate_open_paths_and_401(gated_client):
    c = gated_client
    assert c.get("/health").status_code == 200
    assert c.get("/manifest").status_code == 200
    assert c.get("/state").status_code == 200
    r = c.get("/students/")
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"
    assert c.get("/api/auth/me").status_code == 401


@respx.mock
def test_gate_tiers(gated_client):
    c = gated_client
    me = respx.get(f"{CORE}/api/v1/auth/me")

    def by_token(request):
        tok = request.headers["Authorization"].split()[1]
        return httpx.Response(200, json={"v": VIEWER, "o": OPERATOR, "a": ADMIN}[tok])

    me.mock(side_effect=by_token)
    v, o, a = ({"Authorization": f"Bearer {t}"} for t in ("v", "o", "a"))
    assert c.get("/students/", headers=v).status_code == 200
    r = c.post("/courses/", json={"code": "MBA-1"}, headers=v)
    assert r.status_code == 403 and "operator" in r.json()["detail"]
    assert c.post("/courses/", json={"code": "MBA-1"}, headers=o).status_code == 200
    r = c.post("/attendance/refinalize/all", headers=o)
    assert r.status_code == 403 and "admin" in r.json()["detail"]
    assert c.post("/attendance/refinalize/all", headers=a).status_code == 200
    me_out = c.get("/api/auth/me", headers=o).json()
    assert me_out["username"] == "teacher" and me_out["tier"] == "operator" and me_out["is_admin"] is False


@respx.mock
def test_gate_login_endpoints_proxy(gated_client):
    c = gated_client
    respx.post(f"{CORE}/api/v1/auth/login-json").mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "r", "token_type": "bearer"}))
    r = c.post("/api/auth/login-json", json={"username": "root", "password": "pw"})
    assert r.status_code == 200 and r.json()["refresh_token"] == "r"
    # the old UI's OAuth2 form shape still works
    r = c.post("/api/auth/login", data={"username": "root", "password": "pw", "grant_type": ""})
    assert r.status_code == 200
    assert c.post("/api/auth/login-json", json={"username": "", "password": ""}).status_code == 400
    respx.post(f"{CORE}/api/v1/auth/refresh").mock(return_value=httpx.Response(200, json={
        "access_token": "a2", "refresh_token": "r2", "token_type": "bearer"}))
    assert c.post("/api/auth/refresh", json={"refresh_token": "r"}).json()["access_token"] == "a2"


@respx.mock
def test_gate_core_outage_is_503_not_401(gated_client):
    respx.get(f"{CORE}/api/v1/auth/me").mock(side_effect=httpx.ConnectError("down"))
    r = gated_client.get("/students/", headers={"Authorization": "Bearer x"})
    assert r.status_code == 503


def test_websocket_requires_operator_token(gated_client):
    from attendly.gate import ws_user_allowed

    with respx.mock:
        respx.get(f"{CORE}/api/v1/auth/me").mock(return_value=httpx.Response(200, json=VIEWER))
        assert ws_user_allowed("v", "operator") is False
        respx.get(f"{CORE}/api/v1/auth/me").mock(return_value=httpx.Response(200, json=OPERATOR))
        assert ws_user_allowed("o", "operator") is True
        assert ws_user_allowed(None) is False

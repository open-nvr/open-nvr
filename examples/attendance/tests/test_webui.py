# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web UI's pass-through to core: what it forwards, and what it won't."""
from __future__ import annotations

import json

import httpx
import pytest
from starlette.testclient import TestClient

from attendance.webui import NO_DEVICE, SECURITY_HEADERS, build_web_app, core_path
from attendance_app import MANIFEST, AppConfig, build_manifest


class FakeCore:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.body: dict = {"ok": True}
        self.headers: dict = {}
        self.fail: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.fail is not None:
            raise self.fail
        return httpx.Response(self.status, json=self.body, headers=self.headers)


@pytest.fixture
def dist(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (tmp_path / "assets" / "app-1a2b.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return tmp_path


@pytest.fixture
def core():
    return FakeCore()


@pytest.fixture
def client(core, dist):
    app = build_web_app("http://opennvr-core:8000", dist, transport=httpx.MockTransport(core))
    with TestClient(app) as c:
        yield c


# ── which paths reach core ───────────────────────────────────────────


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "attendance/people", "/api/v1/attendance/people"),
    ("POST", "attendance/people/3/faces", "/api/v1/attendance/people/3/faces"),
    ("PATCH", "attendance/sessions/9/marks/4", "/api/v1/attendance/sessions/9/marks/4"),
    ("DELETE", "attendance/groups/2", "/api/v1/attendance/groups/2"),
    ("GET", "cameras", "/api/v1/cameras/"),
    ("GET", "users/me/permissions", "/api/v1/users/me/permissions"),
    ("GET", "auth/me", "/api/v1/auth/me"),
])
def test_the_attendance_routes_and_the_pages_reads_go_through(method, path, expected):
    assert core_path(method, path) == expected


@pytest.mark.parametrize("method,path", [
    ("GET", "users"),                       # not the page's business
    ("POST", "cameras"),                    # cameras are read, never changed, from here
    ("DELETE", "users/me/permissions"),
    ("GET", "internal/app/attendance/active"),
    ("GET", "attendance/internal/anything"),
    ("GET", "attendance/../users"),
    ("GET", "attendance//people"),
    ("OPTIONS", "attendance/people"),
    ("GET", "apps/attendance/config"),
])
def test_everything_else_is_refused(method, path):
    assert core_path(method, path) is None


def test_a_refused_path_never_reaches_core(client, core):
    r = client.get("/api/users", headers={"authorization": "Bearer t"})
    assert r.status_code == 404
    assert core.calls == []


# ── what is forwarded ────────────────────────────────────────────────


def test_the_users_token_query_and_device_go_to_core_as_they_came(client, core):
    r = client.get("/api/attendance/sessions?from=2026-11-01&group_id=2",
                   headers={"authorization": "Bearer user-jwt", "x-device-token": "dev-1",
                            "cookie": "opennvr_device=other"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    sent = core.calls[0]
    assert sent.url.path == "/api/v1/attendance/sessions"
    assert sent.url.query == b"from=2026-11-01&group_id=2"
    assert sent.headers["authorization"] == "Bearer user-jwt"
    assert sent.headers["x-device-token"] == "dev-1"
    # Nothing that would let core see this as anyone but the user.
    assert "cookie" not in sent.headers
    assert "x-internal-api-key" not in sent.headers
    assert r.headers["cache-control"] == "no-store"


def test_a_browser_with_no_device_token_is_not_taken_for_a_sibling_service(client, core):
    client.get("/api/attendance/groups", headers={"authorization": "Bearer user-jwt"})
    assert core.calls[0].headers["x-device-token"] == NO_DEVICE


def test_core_answers_pass_back_verbatim_errors_included(client, core):
    core.status, core.body = 403, {"detail": "Missing permission attendance.manage"}
    r = client.post("/api/attendance/groups", json={"name": "A"},
                    headers={"authorization": "Bearer user-jwt"})
    assert r.status_code == 403
    assert r.json() == {"detail": "Missing permission attendance.manage"}
    assert json.loads(core.calls[0].content) == {"name": "A"}


def test_a_report_file_keeps_its_name(client, core):
    core.headers = {"content-disposition": 'attachment; filename="attendance.xlsx"'}
    r = client.get("/api/attendance/report?group_id=1&from=2026-11-01&to=2026-11-02&format=xlsx",
                   headers={"authorization": "Bearer t"})
    assert r.headers["content-disposition"] == 'attachment; filename="attendance.xlsx"'


def test_cameras_come_back_as_names_never_stream_credentials(client, core):
    core.body = {"cameras": [{"id": 1, "name": "Room 101", "location": "Block A",
                              "rtsp_url": "rtsp://admin:secret@10.0.0.5/1",
                              "password": "secret", "username": "admin"}],
                 "total": 1}
    r = client.get("/api/cameras", headers={"authorization": "Bearer t"})
    assert r.json() == {"cameras": [{"id": 1, "name": "Room 101", "location": "Block A"}]}
    assert b"secret" not in r.content


def test_photos_go_through_as_multipart(client, core):
    r = client.post("/api/attendance/people/3/faces",
                    files=[("photos", ("a.jpg", b"\xff\xd8jpeg", "image/jpeg"))],
                    headers={"authorization": "Bearer t"})
    assert r.status_code == 200
    sent = core.calls[0]
    assert sent.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b"\xff\xd8jpeg" in sent.content


def test_a_body_past_the_cap_is_refused_before_core(client, core, monkeypatch):
    import attendance.webui as webui

    monkeypatch.setattr(webui, "MAX_BODY_BYTES", 10)
    app = build_web_app("http://core", ".", transport=httpx.MockTransport(core))
    with TestClient(app) as c:
        r = c.post("/api/attendance/people", content=b"x" * 11,
                   headers={"authorization": "Bearer t"})
    assert r.status_code == 413
    assert core.calls == []


def test_core_down_is_try_again_not_signed_out(client, core):
    core.fail = httpx.ConnectError("refused")
    r = client.get("/api/attendance/groups", headers={"authorization": "Bearer t"})
    assert r.status_code == 503 and r.headers["retry-after"] == "2"


# ── signing in ───────────────────────────────────────────────────────


def test_login_goes_to_core_with_the_browsers_own_device(client, core):
    core.body = {"access_token": "a", "refresh_token": "r", "device_token": "new"}
    r = client.post("/auth/login", json={"username": "u", "password": "p", "code": "123456"},
                    headers={"user-agent": "Firefox/140"})
    assert r.json()["device_token"] == "new"
    sent = core.calls[0]
    assert sent.url.path == "/api/v1/auth/login-json"
    assert json.loads(sent.content) == {"username": "u", "password": "p", "code": "123456"}
    assert sent.headers["user-agent"] == "Firefox/140"
    # Core mints a token for a browser that has none; a stand-in would
    # stop it from doing so.
    assert "x-device-token" not in sent.headers


def test_core_says_why_a_login_failed(client, core):
    core.status, core.body = 401, {"detail": {"message": "Invalid or missing MFA code"}}
    core.headers = {"www-authenticate": "Bearer"}
    r = client.post("/auth/login", json={"username": "u", "password": "p"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_refresh_goes_to_core(client, core):
    client.post("/auth/refresh", json={"refresh_token": "r"})
    assert core.calls[0].url.path == "/api/v1/auth/refresh"


# ── the page ─────────────────────────────────────────────────────────


def test_every_route_of_the_page_is_the_page(client):
    for path in ("/", "/people", "/attendance/sessions/4"):
        r = client.get(path)
        assert r.status_code == 200 and "id=root" in r.text
        assert r.headers["cache-control"] == "no-cache"


def test_built_files_are_served_and_cached_for_good(client):
    r = client.get("/assets/app-1a2b.js")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert client.get("/favicon.svg").text == "<svg/>"


def test_a_path_out_of_the_build_is_the_page_not_a_file(client):
    r = client.get("/..%2F..%2Fattendance_app.py")
    assert "id=root" in r.text


def test_every_response_carries_the_security_headers(client):
    for r in (client.get("/"), client.get("/api/nope"), client.get("/health")):
        for name, value in SECURITY_HEADERS.items():
            assert r.headers[name] == value


def test_an_image_without_the_build_says_so(core, tmp_path):
    app = build_web_app("http://core", tmp_path, transport=httpx.MockTransport(core))
    with TestClient(app) as c:
        assert c.get("/").status_code == 503
        assert c.get("/health").json() == {"status": "ok", "ui_built": False}


# ── the manifest ─────────────────────────────────────────────────────


def test_no_web_port_no_ui_in_the_catalog():
    assert build_manifest(AppConfig()) is MANIFEST
    assert MANIFEST.has_ui is False


def test_a_web_port_makes_it_an_external_app():
    m = build_manifest(AppConfig(web_port=9218)).to_dict()
    assert m["has_ui"] is True and m["ui_mode"] == "external"
    assert m["ui_url"] == "http://{host}:9218/"
    m = build_manifest(AppConfig(web_port=9218, web_ui_url="https://{host}:9443/")).to_dict()
    assert m["ui_url"] == "https://{host}:9443/"

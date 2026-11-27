# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Remote sites and their agents (services/sites.py, routers/sites.py).

What these pin:
- the one-time code: shown once, single use, expires, only its hash stored;
- the credential: only its hash stored, constant-time check, replaced on
  re-enrolment, gone on revoke (and revoke ends the site's streams);
- every agent-route failure is the same 404;
- the agent gets only its own site's cameras, with credentials inside the
  LAN URLs, and a publish token only for those;
- a site camera is a pushed camera whose LAN URLs carry no credentials;
- the long poll answers at once on a different etag and is woken by a
  change.
"""
from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

import asyncio
import json
import os
import secrets
import sys
import types as _types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules["core.logging_config"] = _lm

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.auth as core_auth  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, Permission, Role, RolePermission, Site, User  # noqa: E402
from services import sites as svc  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    from routers import sites as sites_router
    from services import audit_service
    from services import mediamtx_jwt_service as jwt_mod
    from services.mediamtx_admin_service import MediaMtxAdminService

    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(audit_service, "audit_request", lambda *a, **k: None)
    monkeypatch.setattr(
        jwt_mod.MediaMtxJwtService, "create_publish_token",
        classmethod(lambda cls, **kw: json.dumps({"path": kw["camera_path"],
                                                  "extra": kw.get("extra_paths"),
                                                  "minutes": kw["expiry_minutes"]})),
    )
    kicked: list[list[str]] = []

    async def fake_kick(paths):
        kicked.append(list(paths))
        return {"status": "ok", "kicked": list(paths), "errors": []}

    monkeypatch.setattr(MediaMtxAdminService, "kick_path_sessions", staticmethod(fake_kick))
    from services.mediamtx_startup_service import MediaMtxStartupService

    provisioned: list[int] = []

    async def fake_provision(camera_id, force=False):
        provisioned.append(camera_id)
        return {"status": "ok"}

    monkeypatch.setattr(MediaMtxStartupService, "provision_camera_by_id", staticmethod(fake_provision))
    sites_router._calls.clear()

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng, expire_on_commit=False)
    db = SessionLocal()
    manage = Permission(name="cameras.manage", description="")
    role = Role(name="operator", description="")
    viewer_role = Role(name="viewer", description="")
    db.add_all([manage, role, viewer_role])
    db.flush()
    db.add(RolePermission(role_id=role.id, permission_id=manage.id))
    users = {}
    for name, r in (("operator", role), ("viewer", viewer_role)):
        u = User(username=name, email=f"{name}@x", hashed_password="x", is_active=True,
                 is_superuser=False, password_set=True, role_id=r.id)
        db.add(u)
        db.flush()
        users[name] = u
    owner = users["operator"].id
    cams = {
        "pushed": Camera(name="Gate", ip_address="0.0.0.0", port=554, owner_id=owner,
                         is_active=True, source_type="publisher"),
        "pushed2": Camera(name="Yard", ip_address="0.0.0.0", port=554, owner_id=owner,
                          is_active=True, source_type="publisher"),
        "webcam": Camera(name="Desk", ip_address="0.0.0.0", port=554, owner_id=owner,
                         is_active=True, source_type="publisher", source_device="dshow:hp"),
        "ip": Camera(name="Lobby", ip_address="10.0.0.7", port=554, owner_id=owner,
                     is_active=True, rtsp_url="rtsp://10.0.0.7/s"),
    }
    db.add_all(cams.values())
    db.commit()
    ids = {k: c.id for k, c in cams.items()}
    for u in users.values():
        db.refresh(u)
        _ = [p.name for p in (u.role.permissions or [])]
        db.expunge(u)
    db.close()

    app = FastAPI()
    app.include_router(sites_router.router, prefix="/api/v1")
    app.include_router(sites_router.agent_router, prefix="/api/v1")

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    current = {"user": users["operator"]}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[core_auth.get_current_active_user] = lambda: current["user"]
    with TestClient(app, base_url="https://nvr.example.com") as tc:
        yield SimpleNamespace(tc=tc, ids=ids, users=users, current=current,
                              SessionLocal=SessionLocal, kicked=kicked, provisioned=provisioned)


def _create(env, name="Shop 12"):
    r = env.tc.post("/api/v1/sites", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _enrol(env, code):
    return env.tc.post("/api/v1/site-agent/enrol", json={"code": code, "version": "0.2.0"})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


REPORT = {"version": "0.2.0", "os": "linux", "arch": "arm64", "hostname": "shop12-pi"}


# ── admin side ─────────────────────────────────────────────────────────


def test_only_camera_managers_see_sites(env):
    env.current["user"] = env.users["viewer"]
    assert env.tc.get("/api/v1/sites").status_code == 403
    assert env.tc.post("/api/v1/sites", json={"name": "x"}).status_code == 403


def test_create_shows_the_code_once_and_stores_only_its_hash(env):
    site = _create(env)
    code = site["enrol_code"]
    assert len(code) == 11 and code[5] == "-"
    assert code in site["enrol_command"] and "https://nvr.example.com" in site["enrol_command"]
    with env.SessionLocal() as s:
        row = s.get(Site, site["id"])
        assert row.enrol_code_hash and code.replace("-", "") not in row.enrol_code_hash
    listed = env.tc.get("/api/v1/sites").json()
    assert "enrol_code" not in listed[0] and listed[0]["code_pending"] is True


# ── enrolment ──────────────────────────────────────────────────────────


def test_a_code_enrols_once_and_is_case_and_dash_insensitive(env):
    site = _create(env)
    code = site["enrol_code"].lower().replace("-", " ")
    r = _enrol(env, code)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith(f"site_{site['id']}_")
    assert body["ingest"] == {"host": "nvr.example.com", "port": 443, "alpn": "opennvr-rtsps"}
    # Single use.
    assert _enrol(env, site["enrol_code"]).status_code == 404
    with env.SessionLocal() as s:
        row = s.get(Site, site["id"])
        secret = body["token"].split("_", 2)[2]
        assert row.credential_hash and secret not in row.credential_hash
        assert row.enrol_code_hash is None


def test_an_expired_or_unknown_code_is_a_plain_404(env, monkeypatch):
    site = _create(env)
    assert _enrol(env, "AAAAA-AAAAA").status_code == 404
    assert _enrol(env, "short").status_code == 404
    with env.SessionLocal() as s:
        row = s.get(Site, site["id"])
        row.enrol_expires_at = _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1)
        s.commit()
    assert _enrol(env, site["enrol_code"]).status_code == 404


def test_enrolment_attempts_are_rate_limited_per_address(env):
    _create(env)
    codes = [_enrol(env, f"BBBBB-BBBB{i}").status_code for i in range(7)]
    assert codes[:5] == [404] * 5 and codes[-1] == 429


def test_re_enrolling_replaces_the_credential(env):
    site = _create(env)
    old = _enrol(env, site["enrol_code"]).json()["token"]
    code = env.tc.post(f"/api/v1/sites/{site['id']}/enrol-code").json()["enrol_code"]
    # The old credential works until the new code is used...
    assert env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(old)).status_code == 200
    new = _enrol(env, code).json()["token"]
    # ...and not after.
    assert env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(old)).status_code == 404
    assert env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(new)).status_code == 200


# ── agent auth ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("header", [
    None, "Bearer ", "Bearer site_1_wrong", "Bearer site_x_abc", "Basic abc", "Bearer ohk_abc",
])
def test_every_agent_failure_is_the_same_404(env, header):
    site = _create(env)
    _enrol(env, site["enrol_code"])
    headers = {"Authorization": header} if header else {}
    for method, path in (("post", "/api/v1/site-agent/sync"), ("get", "/api/v1/site-agent/wait"),
                         ("post", f"/api/v1/site-agent/publish-token/{env.ids['pushed']}")):
        kw = {"json": REPORT} if path.endswith("sync") else {}
        r = getattr(env.tc, method)(path, headers=headers, **kw)
        assert r.status_code == 404, (path, header, r.status_code)


def test_revoke_cuts_the_agent_off_and_ends_its_streams(env):
    site = _create(env)
    token = _enrol(env, site["enrol_code"]).json()["token"]
    env.tc.put(f"/api/v1/sites/{site['id']}/cameras/{env.ids['pushed']}",
               json={"main_url": "rtsp://192.168.1.64:554/main"})
    r = env.tc.delete(f"/api/v1/sites/{site['id']}/credential")
    assert r.status_code == 200 and r.json()["enrolled"] is False
    with env.SessionLocal() as s:
        expected = list(svc.stream_names(s.get(Camera, env.ids["pushed"])))
    assert env.kicked and env.kicked[-1] == expected
    assert env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).status_code == 404
    # The camera stays (recordings with it).
    with env.SessionLocal() as s:
        assert s.get(Camera, env.ids["pushed"]).site_id == site["id"]


# ── cameras ────────────────────────────────────────────────────────────


def test_link_accepts_only_pushed_cameras_and_clean_urls(env):
    site = _create(env)
    put = lambda cam, body: env.tc.put(f"/api/v1/sites/{site['id']}/cameras/{env.ids[cam]}", json=body)  # noqa: E731
    assert put("webcam", {"main_url": "rtsp://192.168.1.64/main"}).status_code == 422
    assert put("ip", {"main_url": "rtsp://192.168.1.64/main"}).status_code == 422
    assert put("pushed", {"main_url": "http://192.168.1.64/main"}).status_code == 422
    r = put("pushed", {"main_url": "rtsp://admin:pw@192.168.1.64/main"})
    assert r.status_code == 422 and "credentials" in r.json()["detail"]
    r = put("pushed", {"main_url": "rtsp://192.168.1.64:554/main", "sub_url": "rtsp://192.168.1.64:554/sub",
                       "username": "admin", "password": "p@ss:word"})
    assert r.status_code == 200, r.text
    cam = r.json()["cameras"][0]
    assert cam["lan_host"] == "192.168.1.64" and "pw" not in json.dumps(r.json())
    # Not another site's.
    other = _create(env, "Shop 13")
    r = env.tc.put(f"/api/v1/sites/{other['id']}/cameras/{env.ids['pushed']}",
                   json={"main_url": "rtsp://10.1.1.1/x"})
    assert r.status_code == 422


def test_sync_hands_out_only_this_sites_cameras_with_credentials(env):
    a = _create(env, "A")
    b = _create(env, "B")
    token_a = _enrol(env, a["enrol_code"]).json()["token"]
    token_b = _enrol(env, b["enrol_code"]).json()["token"]
    env.tc.put(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed']}",
               json={"main_url": "rtsp://192.168.1.64:554/main", "sub_url": "rtsp://192.168.1.64:554/sub",
                     "username": "admin", "password": "p@ss:word"})
    env.tc.put(f"/api/v1/sites/{b['id']}/cameras/{env.ids['pushed2']}",
               json={"main_url": "rtsp://10.9.0.5/live"})

    d = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token_a)).json()
    assert [x["camera_id"] for x in d["desired"]] == [env.ids["pushed"]]
    entry = d["desired"][0]
    assert entry["main_url"] == "rtsp://admin:p%40ss%3Aword@192.168.1.64:554/main"
    assert entry["sub_url"].endswith("@192.168.1.64:554/sub")
    assert entry["stream_name"].endswith(str(env.ids["pushed"])) and entry["sub_stream_name"]
    assert d["ingest"]["alpn"] == "opennvr-rtsps"

    d_b = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token_b)).json()
    assert [x["camera_id"] for x in d_b["desired"]] == [env.ids["pushed2"]]
    assert d_b["desired"][0]["sub_url"] is None and d_b["desired"][0]["sub_stream_name"] is None


def test_publish_tokens_only_for_the_sites_own_cameras(env):
    a = _create(env, "A")
    token = _enrol(env, a["enrol_code"]).json()["token"]
    env.tc.put(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed']}",
               json={"main_url": "rtsp://192.168.1.64/main", "sub_url": "rtsp://192.168.1.64/sub"})
    r = env.tc.post(f"/api/v1/site-agent/publish-token/{env.ids['pushed']}", headers=_auth(token))
    assert r.status_code == 200
    tok = json.loads(r.json()["token"])
    assert tok["minutes"] == 5 and tok["extra"] == [r.json()["sub_stream_name"]]
    for other in ("pushed2", "webcam", "ip"):
        assert env.tc.post(f"/api/v1/site-agent/publish-token/{env.ids[other]}",
                           headers=_auth(token)).status_code == 404


def test_the_report_is_kept_and_discovered_devices_listed_until_linked(env):
    a = _create(env)
    token = _enrol(env, a["enrol_code"]).json()["token"]
    report = {**REPORT, "onvif": {"scanned_at": 1.0, "devices": [
        {"ip": "192.168.1.64", "xaddr": "http://192.168.1.64/onvif/device_service", "name": "Gate"},
        {"ip": "192.168.1.65", "xaddr": "http://192.168.1.65/onvif/device_service", "name": "Yard"},
    ]}}
    assert env.tc.post("/api/v1/site-agent/sync", json=report, headers=_auth(token)).status_code == 200
    view = env.tc.get(f"/api/v1/sites/{a['id']}").json()
    assert view["online"] is True and view["hostname"] == "shop12-pi"
    assert {d["ip"] for d in view["discovered"]} == {"192.168.1.64", "192.168.1.65"}
    env.tc.put(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed']}",
               json={"main_url": "rtsp://192.168.1.64/main"})
    view = env.tc.get(f"/api/v1/sites/{a['id']}").json()
    assert {d["ip"] for d in view["discovered"]} == {"192.168.1.65"}


def test_a_site_with_cameras_cannot_be_deleted(env):
    a = _create(env)
    env.tc.put(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed']}",
               json={"main_url": "rtsp://192.168.1.64/main"})
    assert env.tc.delete(f"/api/v1/sites/{a['id']}").status_code == 409
    assert env.tc.delete(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed']}").status_code == 200
    assert env.tc.delete(f"/api/v1/sites/{a['id']}").status_code == 204


# ── long poll ──────────────────────────────────────────────────────────


def test_wait_answers_at_once_on_a_different_etag(env):
    a = _create(env)
    token = _enrol(env, a["enrol_code"]).json()["token"]
    r = env.tc.get("/api/v1/site-agent/wait", params={"etag": "stale"}, headers=_auth(token))
    assert r.status_code == 200 and r.json()["etag"] != "stale"


@pytest.mark.asyncio
async def test_a_change_wakes_the_waiter():
    ev = svc.arm(4242)
    waiter = asyncio.create_task(svc.wait(ev, timeout=5))
    await asyncio.sleep(0.05)
    svc.notify(4242)
    assert await asyncio.wait_for(waiter, 2) is True
    ev = svc.arm(4242)
    assert await svc.wait(ev, timeout=0.05) is False


# ── server pin ─────────────────────────────────────────────────────────


def _self_signed(path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "opennvr-ui")])
    now = _dt.datetime.now(_dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + _dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key


def test_the_enrol_command_carries_the_servers_pin(env, monkeypatch, tmp_path):
    import base64
    import hashlib

    from cryptography.hazmat.primitives import serialization

    cert = tmp_path / "server.crt"
    key = _self_signed(cert)
    monkeypatch.setenv("NGINX_CERT_PATH", str(cert))
    spki = key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    want = "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode()
    assert svc.server_pin() == want
    site = _create(env)
    assert f"-pin {want} https://nvr.example.com " in site["enrol_command"]
    assert site["server_pin"] == want


def test_no_readable_certificate_no_pin(env, monkeypatch, tmp_path):
    monkeypatch.setenv("NGINX_CERT_PATH", str(tmp_path / "missing.crt"))
    site = _create(env)
    assert site["server_pin"] is None and "-pin" not in site["enrol_command"]


# ── camera requests for the agent (site_relay) ──────────────────────────


def test_wait_hands_queued_camera_requests_to_the_agent_at_once(env):
    from services import site_relay

    a = _create(env)
    token = _enrol(env, a["enrol_code"]).json()["token"]
    etag = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).json()["etag"]
    site_relay._pending[a["id"]] = {"r1": {"id": "r1", "method": "GET", "url": "http://192.168.1.64/x"}}
    try:
        r = env.tc.get("/api/v1/site-agent/wait", params={"etag": etag}, headers=_auth(token))
        assert r.status_code == 200 and [x["id"] for x in r.json()["rpc"]] == ["r1"]
        # Taken: not handed out twice.
        assert not site_relay.has_pending(a["id"])
    finally:
        site_relay._pending.clear()


def test_an_rpc_result_for_an_unknown_request_is_a_404(env):
    a = _create(env)
    token = _enrol(env, a["enrol_code"]).json()["token"]
    r = env.tc.post("/api/v1/site-agent/rpc/r999/result", json={"status": 200}, headers=_auth(token))
    assert r.status_code == 404


def test_a_separate_control_host_is_used_and_sent_to_the_agent(env):
    """An NVR channel or an encoder: the video comes from one box, the
    control API (ONVIF, PTZ) answers on another."""
    a = _create(env)
    token = _enrol(env, a["enrol_code"]).json()["token"]
    put = lambda body: env.tc.put(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed']}", json=body)  # noqa: E731
    assert put({"main_url": "rtsp://192.168.1.10/ch1", "control_host": "http://x"}).status_code == 422
    r = put({"main_url": "rtsp://192.168.1.10/ch1", "control_host": "192.168.1.20"})
    assert r.status_code == 200 and r.json()["cameras"][0]["lan_host"] == "192.168.1.20"
    d = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).json()
    assert d["desired"][0]["control_host"] == "192.168.1.20"


# ── bandwidth (C6) ─────────────────────────────────────────────────────


def test_sub_only_sends_the_sub_stream_into_the_main_path(env):
    a = _create(env)
    token = _enrol(env, a["enrol_code"]).json()["token"]
    env.tc.put(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed']}",
               json={"main_url": "rtsp://192.168.1.64/main", "sub_url": "rtsp://192.168.1.64/sub"})
    # A camera without a sub stream keeps its main stream under sub_only.
    env.tc.put(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed2']}",
               json={"main_url": "rtsp://192.168.1.65/main"})
    before = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).json()
    r = env.tc.patch(f"/api/v1/sites/{a['id']}", json={"live_policy": "sub_only", "uplink_cap_kbps": 4000})
    assert r.status_code == 200 and r.json()["live_policy"] == "sub_only" and r.json()["uplink_cap_kbps"] == 4000
    after = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).json()
    assert after["etag"] != before["etag"] and after["uplink_cap_kbps"] == 4000
    by_id = {d["camera_id"]: d for d in after["desired"]}
    one, two = by_id[env.ids["pushed"]], by_id[env.ids["pushed2"]]
    assert one["main_url"].endswith("/sub") and one["sub_url"] is None and one["sub_stream_name"] is None
    assert one["main_from_sub"] is True
    assert two["main_url"].endswith("/main") and two["main_from_sub"] is False


def test_only_the_supported_live_policies_are_accepted(env):
    a = _create(env)
    assert env.tc.patch(f"/api/v1/sites/{a['id']}", json={"live_policy": "on_demand_main"}).status_code == 422
    assert env.tc.patch(f"/api/v1/sites/{a['id']}", json={"uplink_cap_kbps": -5}).status_code == 422
    # 0 or null clears the cap.
    env.tc.patch(f"/api/v1/sites/{a['id']}", json={"uplink_cap_kbps": 3000})
    assert env.tc.patch(f"/api/v1/sites/{a['id']}", json={"uplink_cap_kbps": 0}).json()["uplink_cap_kbps"] is None


def test_linking_provisions_the_cameras_paths_and_a_sub_gets_its_sub_path(env):
    from services.local_webcams import provisioning_for

    a = _create(env)
    env.tc.put(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed']}",
               json={"main_url": "rtsp://192.168.1.64/main", "sub_url": "rtsp://192.168.1.64/sub"})
    assert env.provisioned == [env.ids["pushed"]]
    with env.SessionLocal() as s:
        cam = s.get(Camera, env.ids["pushed"])
        assert provisioning_for(cam) == {"substream": True}
        other = s.get(Camera, env.ids["pushed2"])  # a browser webcam: main path only
        assert provisioning_for(other) == {}
    env.tc.delete(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed']}")
    assert env.provisioned[-1] == env.ids["pushed"]

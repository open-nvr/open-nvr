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


# ── agent downloads ────────────────────────────────────────────────────


@pytest.fixture
def agent_dir(tmp_path, monkeypatch):
    for rel, body in (("linux-amd64/opennvr-host", b"linux-bin"),
                      ("linux-arm64/opennvr-host", b"pi-bin"),
                      ("windows-amd64/opennvr-host.exe", b"gui-bin"),
                      ("windows-amd64/console/opennvr-host.exe", b"console-bin"),
                      ("windows-arm64/opennvr-host.exe", b"old-gui-only")):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    (tmp_path / "BUILD").write_text("0.1.12+abc1234\n")
    monkeypatch.setenv("OPENNVR_HOST_AGENT_DIR", str(tmp_path))
    svc._sum_cache.clear()
    return tmp_path


def test_the_agent_downloads_without_a_login(env, agent_dir):
    env.current["user"] = None  # the site machine has no credential yet
    r = env.tc.get("/api/v1/site-agent/download/linux-arm64")
    assert r.status_code == 200 and r.content == b"pi-bin"
    assert 'filename="opennvr-host"' in r.headers["content-disposition"]


def test_windows_gets_the_console_build(env, agent_dir):
    r = env.tc.get("/api/v1/site-agent/download/windows-amd64")
    assert r.content == b"console-bin"
    assert 'filename="opennvr-host.exe"' in r.headers["content-disposition"]
    # An image without the console build still serves what it has.
    assert env.tc.get("/api/v1/site-agent/download/windows-arm64").content == b"old-gui-only"


def test_unknown_or_missing_platforms_are_404(env, agent_dir):
    for p in ("darwin-arm64", "linux-mips", "..%2F..%2Fetc%2Fpasswd", "BUILD"):
        assert env.tc.get(f"/api/v1/site-agent/download/{p}").status_code == 404, p


def test_the_listing_carries_sums_and_the_build(env, agent_dir):
    import hashlib

    body = env.tc.get("/api/v1/site-agent/download").json()
    assert body["build"] == "0.1.12+abc1234"
    files = {f["platform"]: f for f in body["files"]}
    assert set(files) == {"linux-amd64", "linux-arm64", "windows-amd64", "windows-arm64"}
    assert files["windows-amd64"]["sha256"] == hashlib.sha256(b"console-bin").hexdigest()
    assert files["linux-amd64"]["size"] == len(b"linux-bin")


def test_downloads_are_rate_limited(env, agent_dir):
    codes = [env.tc.get("/api/v1/site-agent/download/linux-amd64").status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[-1] == 429


def test_setup_commands_fetch_from_this_server_with_the_pin(env, agent_dir, monkeypatch):
    monkeypatch.setattr(svc, "server_pin", lambda: "sha256/AbC+/=")
    site = _create(env)
    setup = {s["os"]: s for s in site["setup"]}
    linux, win = setup["linux"], setup["windows"]
    base = "https://nvr.example.com/api/v1/site-agent/download/"
    assert base + "$(uname -s" in linux["download"]
    # Chain first, then the pinned key; curl's pin format has two slashes.
    assert linux["download"].index("curl -fso") < linux["download"].index('--pinnedpubkey "sha256//AbC+/="')
    assert linux["download"].endswith("&& chmod +x opennvr-host")
    assert linux["enrol"] == f"./opennvr-host enrol -pin sha256/AbC+/= https://nvr.example.com {site['enrol_code']}"
    assert base + "windows-$($env:PROCESSOR_ARCHITECTURE.ToLower())" in win["download"]
    assert "$LASTEXITCODE" in win["download"] and win["enrol"].startswith(".\opennvr-host.exe enrol -pin ")
    # A fresh code carries fresh commands.
    again = env.tc.post(f"/api/v1/sites/{site['id']}/enrol-code").json()
    assert again["enrol_code"] in again["setup"][0]["enrol"]


def test_setup_without_a_pin_trusts_the_chain_only(env, monkeypatch):
    monkeypatch.setattr(svc, "server_pin", lambda: None)
    site = _create(env)
    for s in site["setup"]:
        assert "pinnedpubkey" not in s["download"] and "-k" not in s["download"].split()
        assert " -pin " not in s["enrol"]


# ── hardening (S0): no pool held, one code once, the old agent cut off ──


def test_wait_lets_go_of_the_database_before_it_waits(env, monkeypatch):
    """Held across the 25 s wait, ~30 polling sites took core's whole pool."""
    import core.database as database
    from routers import sites as sites_router

    a = _create(env)
    token = _enrol(env, a["enrol_code"]).json()["token"]
    etag = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).json()["etag"]
    events: list[str] = []
    real_release = database.release
    monkeypatch.setattr(database, "release", lambda db: (events.append("release"), real_release(db)))

    async def fake_wait(ev, timeout=svc.WAIT_MAX_S):
        events.append("wait")
        return False

    monkeypatch.setattr(svc, "wait", fake_wait)
    r = env.tc.get("/api/v1/site-agent/wait", params={"etag": etag}, headers=_auth(token))
    assert r.status_code == 200
    assert events[:2] == ["release", "wait"], events
    assert sites_router  # imported for its patched module


def test_an_agent_replaced_while_it_waits_gets_nothing_more(env, monkeypatch):
    a = _create(env)
    old = _enrol(env, a["enrol_code"]).json()["token"]
    etag = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(old)).json()["etag"]

    async def re_enrol_meanwhile(ev, timeout=svc.WAIT_MAX_S):
        with env.SessionLocal() as s:
            code, _ = svc.issue_enrol_code(s, s.get(Site, a["id"]))
            assert svc.enrol(s, code, ip=None, version=None) is not None
        return True

    monkeypatch.setattr(svc, "wait", re_enrol_meanwhile)
    r = env.tc.get("/api/v1/site-agent/wait", params={"etag": etag}, headers=_auth(old))
    assert r.status_code == 404


def test_one_code_enrols_once_even_when_two_agents_race(env):
    from sqlalchemy.orm import Session as _Session

    a = _create(env)
    code = a["enrol_code"]
    engine = env.SessionLocal.kw["bind"]
    raced = {"done": False}

    class Racing(_Session):
        def execute(self, statement, *args, **kwargs):
            # The other agent's enrol lands between this one's read and its
            # UPDATE.
            if not raced["done"] and getattr(statement, "is_dml", False):
                raced["done"] = True
                with env.SessionLocal() as other:
                    assert svc.enrol(other, code, ip=None, version=None) is not None
            return super().execute(statement, *args, **kwargs)

    with Racing(bind=engine, expire_on_commit=False) as s:
        assert svc.enrol(s, code, ip=None, version=None) is None
    assert raced["done"]


def test_re_enrolling_ends_the_old_agents_streams(env):
    a = _create(env)
    _enrol(env, a["enrol_code"])
    env.tc.put(f"/api/v1/sites/{a['id']}/cameras/{env.ids['pushed']}",
               json={"main_url": "rtsp://192.168.1.64:554/main"})
    env.kicked.clear()
    code = env.tc.post(f"/api/v1/sites/{a['id']}/enrol-code").json()["enrol_code"]
    assert _enrol(env, code).status_code == 200
    with env.SessionLocal() as s:
        expected = list(svc.stream_names(s.get(Camera, env.ids["pushed"])))
    assert env.kicked == [expected]


def test_a_first_enrolment_kicks_nothing(env):
    a = _create(env)
    env.kicked.clear()
    assert _enrol(env, a["enrol_code"]).status_code == 200
    assert env.kicked == []


def test_enrolment_is_also_limited_across_all_addresses(env, monkeypatch):
    from routers import sites as sites_router

    monkeypatch.setattr(sites_router, "ENROL_GLOBAL_PER_MINUTE", 2)
    codes = [_create(env, f"S{i}")["enrol_code"] for i in range(3)]
    assert [_enrol(env, c).status_code for c in codes[:2]] == [200, 200]
    assert _enrol(env, codes[2]).status_code == 429


def test_camera_answers_do_not_use_up_the_control_budget(env, monkeypatch):
    from routers import sites as sites_router

    monkeypatch.setattr(sites_router, "AGENT_PER_MINUTE", 3)
    a = _create(env)
    token = _enrol(env, a["enrol_code"]).json()["token"]
    for _ in range(10):  # unknown ids: 404, but authenticated and counted
        env.tc.post("/api/v1/site-agent/rpc/rx/result", json={"status": 200}, headers=_auth(token))
    assert env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).status_code == 200


@pytest.mark.asyncio
async def test_a_newer_poll_wakes_the_older():
    ev1 = svc.arm(4343)
    first = asyncio.create_task(svc.wait(ev1, timeout=5))
    await asyncio.sleep(0.05)
    ev2 = svc.arm(4343)
    assert await asyncio.wait_for(first, 2) is True
    assert await svc.wait(ev2, timeout=0.05) is False
    assert svc.holding() == 0


@pytest.mark.asyncio
async def test_over_the_global_cap_a_poll_answers_at_once(monkeypatch):
    monkeypatch.setattr(svc, "WAIT_GLOBAL_MAX", 0)
    ev = svc.arm(4444)
    assert await asyncio.wait_for(svc.wait(ev, timeout=5), 1) is False


# ── repin: the server proves a new certificate key ──────────────────────


def test_the_repin_proof_matches_the_agents():
    """The same vector is in host-agent/internal/site/s0_test.go."""
    import hashlib

    cred = hashlib.sha256(b"s3cret").hexdigest()
    assert svc.repin_proof(cred, "nonce-0123456789ab", "sha256/AAA=") == (
        "d6faf2169657c81d6db1db0eca51a418d94cde01ad2d99ef7b38295ab46726ce")


def test_repin_proves_only_for_a_live_site(env, monkeypatch):
    monkeypatch.setattr(svc, "server_pin", lambda: "sha256/AAA=")
    a = _create(env)
    token = _enrol(env, a["enrol_code"]).json()["token"]
    secret = token.split("_", 2)[2]
    nonce = "nonce-0123456789ab"
    want = svc.repin_proof(svc._sha256(secret), nonce, "sha256/AAA=")
    r = env.tc.post("/api/v1/site-agent/repin", json={"site_id": a["id"], "nonce": nonce})
    assert r.status_code == 200 and r.json()["proof"] == want
    # No credential is needed (the agent sends none to a server it does not
    # trust yet), and an unknown or revoked site gets a proof of the same
    # shape that proves nothing.
    unknown = env.tc.post("/api/v1/site-agent/repin", json={"site_id": 999, "nonce": nonce}).json()["proof"]
    env.tc.delete(f"/api/v1/sites/{a['id']}/credential")
    revoked = env.tc.post("/api/v1/site-agent/repin", json={"site_id": a["id"], "nonce": nonce}).json()["proof"]
    for p in (unknown, revoked):
        assert len(p) == 64 and p != want
    assert env.tc.post("/api/v1/site-agent/repin", json={"site_id": a["id"], "nonce": "short"}).status_code == 422


# ── setup links (S1) ───────────────────────────────────────────────────


@pytest.fixture
def links(env, monkeypatch):
    """A server that can offer links: a public name, a trusted certificate,
    no front proxy (the TLS checks are faked)."""
    from services import site_setup

    monkeypatch.setattr(svc.settings, "public_hostname", "nvr.example.com", raising=False)
    monkeypatch.setattr(svc, "server_pin", lambda: "sha256/AAA=")
    monkeypatch.setattr(site_setup, "_tls_pin", lambda host, port, name, timeout: "sha256/AAA=")
    site_setup._ready_cache = None
    yield site_setup
    site_setup._ready_cache = None


def _link(env, site_id, **body):
    r = env.tc.post(f"/api/v1/sites/{site_id}/setup-links", json=body or None)
    assert r.status_code == 201, r.text
    return r.json()


def _token_of(link):
    return link["url"].split("#", 1)[1]


def test_setup_link_derivations_match_the_agents():
    """The same vectors are in host-agent/internal/site/setup_test.go."""
    from services import site_setup

    secret = "abcdefghijklmnopqrstuvwxyz"
    assert site_setup.proof_key(secret) == "0073dced0b2b7dc0c88e80f5805becf5783a9ba8ec488629ff2a169b30ec7b50"
    assert site_setup.hello_proof(site_setup.proof_key(secret), "nonce-0123456789ab", "sha256/AAA=") == (
        "57ab763d66a28479da86d02d3ff73be1064196cf37bd22dfc256a204735aadf4")
    assert site_setup.enrol_hash(secret) == "e4bd37be8329db8fa590f0ed7003033677635065b20e7be542932739c0d9108d"


@pytest.mark.parametrize("case", ["no_name", "untrusted", "front_proxy"])
def test_no_links_without_a_trusted_certificate_of_our_own(env, monkeypatch, case):
    from services import site_setup

    monkeypatch.setattr(svc, "server_pin", lambda: "sha256/AAA=")
    monkeypatch.setattr(svc.settings, "public_hostname", "" if case == "no_name" else "nvr.example.com",
                        raising=False)

    def tls(host, port, name, timeout):
        if case == "untrusted":
            raise OSError("certificate verify failed: self-signed certificate")
        return "sha256/PROXY=" if host == "nvr.example.com" else "sha256/AAA="

    monkeypatch.setattr(site_setup, "_tls_pin", tls)
    site_setup._ready_cache = None
    want = {"no_name": "no_public_hostname", "untrusted": "certificate_not_trusted",
            "front_proxy": "front_proxy_ends_tls"}[case]
    status = env.tc.get("/api/v1/sites/setup-links/status").json()
    assert status["ready"] is False and status["reason"] == want
    site = _create(env)
    r = env.tc.post(f"/api/v1/sites/{site['id']}/setup-links")
    assert r.status_code == 409 and r.json()["detail"]["reason"] == want
    site_setup._ready_cache = None


def test_a_link_keeps_its_secret_in_the_fragment_and_out_of_the_database(env, links):
    from models import SiteSetupToken

    site = _create(env)
    link = _link(env, site["id"])
    assert link["url"].startswith("https://nvr.example.com/site-setup#")
    tid, secret = _token_of(link).split(".")
    assert link["setup_code"] == f"nvr.example.com#{tid}.{secret}"
    assert len(secret) == 26 and tid not in secret
    with env.SessionLocal() as s:
        row = s.query(SiteSetupToken).filter_by(token_id=tid).one()
        for col in (row.enrol_hash, row.proof_key_enc):
            assert secret not in col
    listed = env.tc.get(f"/api/v1/sites/{site['id']}/setup-links").json()
    assert [x["state"] for x in listed] == ["active"] and "url" not in listed[0]


def test_the_setup_page_lookup_never_uses_a_link_up(env, links):
    site = _create(env, "Shop 7")
    tid = _token_of(_link(env, site["id"])).split(".")[0]
    for _ in range(3):
        r = env.tc.post("/api/v1/site-agent/setup-info", json={"token_id": tid})
        assert r.status_code == 200 and r.json()["site_name"] == "Shop 7" and "files" in r.json()
    assert env.tc.post("/api/v1/site-agent/setup-info", json={"token_id": "nosuchid"}).status_code == 404


def test_hello_proves_only_an_active_link(env, links):
    site = _create(env)
    token = _token_of(_link(env, site["id"]))
    tid, secret = token.split(".")
    nonce = "nonce-0123456789ab"
    want = links.hello_proof(links.proof_key(secret), nonce, "sha256/AAA=")
    r = env.tc.post("/api/v1/site-agent/hello", json={"token_id": tid, "nonce": nonce})
    assert r.status_code == 200 and r.json()["proof"] == want
    other = env.tc.post("/api/v1/site-agent/hello", json={"token_id": "nosuchid", "nonce": nonce}).json()["proof"]
    assert len(other) == 64 and other != want
    assert env.tc.post("/api/v1/site-agent/enrol", json={"token": token}).status_code == 200
    used = env.tc.post("/api/v1/site-agent/hello", json={"token_id": tid, "nonce": nonce}).json()["proof"]
    assert used != want


def test_a_link_enrols_once_and_voids_the_sites_other_ways_in(env, links):
    site = _create(env)
    first, second = _token_of(_link(env, site["id"])), _token_of(_link(env, site["id"]))
    r = env.tc.post("/api/v1/site-agent/enrol", json={"token": first, "hostname": "shop-pi"})
    assert r.status_code == 200 and r.json()["token"].startswith("site_")
    assert env.tc.post("/api/v1/site-agent/enrol", json={"token": first}).status_code == 404
    assert env.tc.post("/api/v1/site-agent/enrol", json={"token": second}).status_code == 404
    assert _enrol(env, site["enrol_code"]).status_code == 404  # the short code too
    listed = env.tc.get(f"/api/v1/sites/{site['id']}/setup-links").json()
    assert {x["state"] for x in listed} == {"used", "revoked"}
    used = next(x for x in listed if x["state"] == "used")
    assert used["used_hostname"] == "shop-pi"


def test_a_wrong_secret_a_revoked_or_an_expired_link_enrol_nothing(env, links):
    import datetime as dt

    from models import SiteSetupToken

    site = _create(env)
    token = _token_of(_link(env, site["id"]))
    tid = token.split(".")[0]
    wrong = tid + "." + "x" * 26
    assert env.tc.post("/api/v1/site-agent/enrol", json={"token": wrong}).status_code == 404
    revoked = _token_of(_link(env, site["id"]))
    assert env.tc.delete(f"/api/v1/sites/{site['id']}/setup-links/{revoked.split('.')[0]}").status_code == 204
    assert env.tc.post("/api/v1/site-agent/enrol", json={"token": revoked}).status_code == 404
    with env.SessionLocal() as s:
        row = s.query(SiteSetupToken).filter_by(token_id=tid).one()
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        s.commit()
    assert env.tc.post("/api/v1/site-agent/enrol", json={"token": token}).status_code == 404


def test_one_link_enrols_once_even_when_two_agents_race(env, links):
    from sqlalchemy.orm import Session as _Session

    site = _create(env)
    token = _token_of(_link(env, site["id"]))
    engine = env.SessionLocal.kw["bind"]
    raced = {"done": False}

    class Racing(_Session):
        def execute(self, statement, *args, **kwargs):
            if not raced["done"] and getattr(statement, "is_dml", False):
                raced["done"] = True
                with env.SessionLocal() as other:
                    assert links.consume(other, token, ip=None, hostname=None) is not None
            return super().execute(statement, *args, **kwargs)

    with Racing(bind=engine, expire_on_commit=False) as s:
        assert links.consume(s, token, ip=None, hostname=None) is None
    assert raced["done"]


def test_links_are_bounded_in_lifetime_and_number(env, links):
    site = _create(env)
    assert env.tc.post(f"/api/v1/sites/{site['id']}/setup-links", json={"hours": 0}).status_code == 422
    assert env.tc.post(f"/api/v1/sites/{site['id']}/setup-links", json={"hours": 24 * 8}).status_code == 422
    for _ in range(links.LINKS_PER_SITE_MAX + 2):
        _link(env, site["id"], hours=1)
    listed = env.tc.get(f"/api/v1/sites/{site['id']}/setup-links").json()
    assert len([x for x in listed if x["state"] == "active"]) == links.LINKS_PER_SITE_MAX


def test_revoking_the_site_voids_its_unused_links(env, links):
    site = _create(env)
    token = _token_of(_link(env, site["id"]))
    env.tc.delete(f"/api/v1/sites/{site['id']}/credential")
    assert env.tc.post("/api/v1/site-agent/enrol", json={"token": token}).status_code == 404


def test_a_replaced_agent_gets_no_cameras_until_approved(env, links):
    site = _create(env)
    _enrol(env, site["enrol_code"])
    env.tc.put(f"/api/v1/sites/{site['id']}/cameras/{env.ids['pushed']}",
               json={"main_url": "rtsp://192.168.1.64:554/main", "username": "admin", "password": "pw"})
    body = {"token": _token_of(_link(env, site["id"])), "hostname": "new-box"}
    new = env.tc.post("/api/v1/site-agent/enrol", json=body).json()["token"]
    ans = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(new)).json()
    assert ans["desired"] == []
    view = env.tc.get(f"/api/v1/sites/{site['id']}").json()
    assert view["approval_pending"] is True and view["pending_agent"]["hostname"] == "new-box"
    assert env.tc.post(f"/api/v1/sites/{site['id']}/approve-agent").status_code == 200
    ans = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(new)).json()
    assert [d["camera_id"] for d in ans["desired"]] == [env.ids["pushed"]]
    assert env.tc.post(f"/api/v1/sites/{site['id']}/approve-agent").status_code == 409


def test_a_first_agent_needs_no_approval(env, links):
    site = _create(env)
    r = env.tc.post("/api/v1/site-agent/enrol", json={"token": _token_of(_link(env, site["id"]))})
    assert r.status_code == 200
    assert env.tc.get(f"/api/v1/sites/{site['id']}").json()["approval_pending"] is False


def test_enrol_takes_a_code_or_a_token_not_both(env, links):
    site = _create(env)
    token = _token_of(_link(env, site["id"]))
    r = env.tc.post("/api/v1/site-agent/enrol", json={"code": site["enrol_code"], "token": token})
    assert r.status_code == 422
    assert env.tc.post("/api/v1/site-agent/enrol", json={}).status_code == 404


def test_the_report_says_which_relay_carries_each_stream(env):
    site = _create(env)
    token = _enrol(env, site["enrol_code"]).json()["token"]
    report = {**REPORT, "publishers": [
        {"camera_id": env.ids["pushed"], "stream": "main", "state": "live", "relay": "ffmpeg"},
        {"camera_id": env.ids["pushed"], "stream": "sub", "state": "live", "relay": "native"},
    ]}
    assert env.tc.post("/api/v1/site-agent/sync", json=report, headers=_auth(token)).status_code == 200
    pubs = env.tc.get(f"/api/v1/sites/{site['id']}").json()["publishers"]
    assert [p["relay"] for p in pubs] == ["ffmpeg", "native"]
    bad = {**REPORT, "publishers": [{"camera_id": 1, "state": "live", "relay": "vlc"}]}
    assert env.tc.post("/api/v1/site-agent/sync", json=bad, headers=_auth(token)).status_code == 422


def test_32_bit_raspberry_pi_os_gets_its_own_build(env, agent_dir):
    p = agent_dir / "linux-arm" / "opennvr-host"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"armv7-bin")
    r = env.tc.get("/api/v1/site-agent/download/linux-arm")
    assert r.status_code == 200 and r.content == b"armv7-bin"
    setup = svc.setup_commands("nvr.example.com", "ABCDE-FGHJK", None)
    linux = next(s for s in setup if s["os"] == "linux")
    assert "s/armv7l/arm/" in linux["download"]
    assert linux["run"] == "sudo ./opennvr-host install-service"


def test_the_signed_update_manifest_is_relayed_to_enrolled_agents_only(env, agent_dir):
    import base64

    site = _create(env)
    token = _enrol(env, site["enrol_code"]).json()["token"]
    # Unsigned builds (no manifest): nothing to offer.
    assert env.tc.get("/api/v1/site-agent/update", headers=_auth(token)).status_code == 404
    (agent_dir / "manifest.json").write_bytes(b"{\"format\": 1}")
    (agent_dir / "manifest.sig").write_bytes(b"{\"key_id\": \"k\", \"sig\": \"s\"}")
    r = env.tc.get("/api/v1/site-agent/update", headers=_auth(token))
    assert r.status_code == 200
    assert base64.b64decode(r.json()["manifest"]) == b"{\"format\": 1}"
    assert base64.b64decode(r.json()["signature"]) == b"{\"key_id\": \"k\", \"sig\": \"s\"}"
    # Not for anyone without the site credential.
    assert env.tc.get("/api/v1/site-agent/update").status_code == 404


# ── finding and adding a site's cameras (S5) ───────────────────────────


def _with_report(env, site_id, report):
    with env.SessionLocal() as s:
        site = s.get(Site, site_id)
        site.report = json.dumps(report)
        s.commit()


def _enrolled_with_devices(env, monkeypatch):
    from services import site_relay

    monkeypatch.setattr(site_relay, "SessionLocal", env.SessionLocal, raising=False)
    import core.database as database

    monkeypatch.setattr(database, "SessionLocal", env.SessionLocal)
    site = _create(env)
    token = _enrol(env, site["enrol_code"]).json()["token"]
    _with_report(env, site["id"], {
        "onvif": {"devices": [{"ip": "192.168.1.64", "name": "Gate cam"}]},
        "scan": {"id": 1, "devices": [{"ip": "192.168.1.70", "ports": [554, 37777], "rtsp": True}]},
    })
    return site, token


def test_onvif_service_addresses_stay_on_the_site_route():
    from services import onvif_digest_service as ods

    route = "d192-168-1-64.s3.opennvr-site.invalid"
    assert ods.route_xaddr("http://192.168.1.64/onvif/Media", route) == f"http://{route}/onvif/Media"
    assert ods.route_xaddr("https://192.168.1.64:8443/onvif/ptz", route) == f"https://{route}:8443/onvif/ptz"
    # A camera on this server's own network: unchanged.
    assert ods.route_xaddr("http://10.0.0.7/onvif/Media", "10.0.0.7") == "http://10.0.0.7/onvif/Media"


def test_a_device_route_reaches_only_what_the_agent_reported(env, monkeypatch):
    from services import site_relay

    site, _ = _enrolled_with_devices(env, monkeypatch)
    sid = site["id"]
    assert site_relay.parse_device_host(site_relay.device_host(sid, "192.168.1.64")) == ("192.168.1.64", sid)
    assert site_relay.resolve_route(site_relay.device_host(sid, "192.168.1.64")) == (sid, "192.168.1.64")
    assert site_relay.resolve_route(site_relay.device_host(sid, "192.168.1.70")) == (sid, "192.168.1.70")
    for ip in ("192.168.1.1", "10.0.0.5"):  # the router, anything not reported
        assert site_relay.resolve_route(site_relay.device_host(sid, ip)) is None
    assert site_relay.parse_device_host("d300-1-1-1.s1.opennvr-site.invalid") is None


def _fake_camera(monkeypatch, *, password="pw", path="/Streaming/Channels/101"):
    """rtsp_exchange answering like a Hikvision camera: Digest, one path."""
    from services import site_relay

    sent: list[bytes] = []

    async def exchange(host, port, request, timeout=6.0):
        sent.append(request)
        line = request.split(b"\r\n", 1)[0].decode()
        if port != 554:
            return None
        if path.encode() not in line.encode():
            return b"RTSP/1.0 404 Not Found\r\nCSeq: 1\r\n\r\n"
        if b"Authorization: Digest" not in request:
            return b'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: Digest realm="cam", nonce="n1"\r\n\r\n'
        if password and b'response="' in request and getattr(exchange, "ok", True):
            return b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Type: application/sdp\r\n\r\n"
        return b'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: Digest realm="cam", nonce="n2"\r\n\r\n'

    monkeypatch.setattr(site_relay, "rtsp_exchange", exchange)
    return exchange, sent


@pytest.mark.asyncio
async def test_a_camera_without_onvif_is_found_by_its_vendor_path(env, monkeypatch):
    from services import onvif_digest_service as ods
    from services import site_probe

    site, _ = _enrolled_with_devices(env, monkeypatch)

    async def no_onvif(*a, **k):
        raise RuntimeError("no ONVIF")

    monkeypatch.setattr(ods, "connect_and_get_profiles", no_onvif)
    exchange, sent = _fake_camera(monkeypatch)
    res = await site_probe.probe_device(site["id"], "192.168.1.70", "admin", "pw")
    assert res["status"] == "ok" and res["via"] == "rtsp"
    ch = res["channels"][0]
    # The camera's LAN address, no credentials, and its sub stream derived.
    assert ch["main_url"] == "rtsp://192.168.1.70:554/Streaming/Channels/101"
    assert ch["sub_url"] == "rtsp://192.168.1.70:554/Streaming/Channels/102"
    assert all(b"opennvr-site.invalid" not in r for r in sent), "a site route leaked into a request"
    exchange.ok = False
    res = await site_probe.probe_device(site["id"], "192.168.1.70", "admin", "wrong")
    assert res["status"] == "wrong_password"


@pytest.mark.asyncio
async def test_an_onvif_camera_gives_its_channels_on_its_lan_address(env, monkeypatch):
    from services import onvif_digest_service as ods
    from services import site_probe

    site, _ = _enrolled_with_devices(env, monkeypatch)

    async def profiles(host, user, pw, *a, **k):
        assert host.endswith(".opennvr-site.invalid")
        return {"port": 80, "scheme": "http", "device_info": {"manufacturer": "Hikvision", "model": "DS-2CD"},
                "channels": [{"index": 1, "name": "Ch 1",
                              "main": {"token": "p1", "width": 1920, "height": 1080,
                                       "stream_uri": "rtsp://admin:pw@192.168.1.64:554/Streaming/Channels/101"},
                              "sub": {"token": "p2", "stream_uri": "rtsp://192.168.1.64:554/Streaming/Channels/102"}}]}

    async def snap(*a, **k):
        return "SlBFRw=="

    monkeypatch.setattr(ods, "connect_and_get_profiles", profiles)
    monkeypatch.setattr(site_probe, "_snapshot", snap)
    res = await site_probe.probe_device(site["id"], "192.168.1.64", "admin", "pw")
    assert res["status"] == "ok" and res["model"] == "DS-2CD"
    ch = res["channels"][0]
    assert ch["main_url"] == "rtsp://192.168.1.64:554/Streaming/Channels/101"  # credentials stripped
    assert ch["snapshot_b64"] == "SlBFRw==" and ch["width"] == 1920


def test_probing_takes_only_devices_the_site_reported(env, monkeypatch):
    from services import site_probe

    site, _ = _enrolled_with_devices(env, monkeypatch)
    seen = {}

    async def fake(site_id, ips, username, password):
        seen["ips"], seen["user"] = ips, username
        return [{"ip": ip, "status": "ok", "channels": []} for ip in ips]

    monkeypatch.setattr(site_probe, "probe_devices", fake)
    r = env.tc.post(f"/api/v1/sites/{site['id']}/probe", json={"ips": ["192.168.1.1"], "username": "admin", "password": "x"})
    assert r.status_code == 422  # the router: never reported, never probed
    r = env.tc.post(f"/api/v1/sites/{site['id']}/probe",
                    json={"ips": ["192.168.1.64", "192.168.1.70", "192.168.1.64"], "username": "admin", "password": "x"})
    assert r.status_code == 200 and seen["ips"] == ["192.168.1.64", "192.168.1.70"]


def test_a_scan_request_reaches_the_agent_and_its_result_is_listed(env, monkeypatch):
    site, token = _enrolled_with_devices(env, monkeypatch)
    before = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).json()
    rid = env.tc.post(f"/api/v1/sites/{site['id']}/scan").json()["requested"]
    report = {**REPORT, "scan": {"id": rid, "scanned_at": 1.0, "subnets": ["192.168.1.0/24"],
                                 "devices": [{"ip": "192.168.1.70", "ports": [554], "rtsp": True}]}}
    after = env.tc.post("/api/v1/site-agent/sync", json={**REPORT}, headers=_auth(token)).json()
    assert after["scan_request"] == rid and after["etag"] != before["etag"]
    assert env.tc.get(f"/api/v1/sites/{site['id']}").json()["scan"]["pending"] is True
    env.tc.post("/api/v1/site-agent/sync", json=report, headers=_auth(token))
    view = env.tc.get(f"/api/v1/sites/{site['id']}").json()
    assert view["scan"]["pending"] is False and view["scan"]["subnets"] == ["192.168.1.0/24"]
    assert [d["ip"] for d in view["found"]] == ["192.168.1.70"] and view["found"][0]["rtsp"] is True


def test_cameras_are_added_in_one_go_or_not_at_all(env):
    site = _create(env)
    bad = {"cameras": [{"name": "A", "main_url": "rtsp://192.168.1.64/a"},
                       {"name": "B", "main_url": "http://192.168.1.65/b"}], "username": "admin", "password": "pw"}
    assert env.tc.post(f"/api/v1/sites/{site['id']}/cameras/bulk", json=bad).status_code == 422
    with env.SessionLocal() as s:
        assert s.query(Camera).filter(Camera.site_id == site["id"]).count() == 0
    good = {"cameras": [{"name": "Gate", "main_url": "rtsp://192.168.1.64:554/Streaming/Channels/101",
                         "sub_url": "rtsp://192.168.1.64:554/Streaming/Channels/102"},
                        {"name": "Yard", "main_url": "rtsp://192.168.1.70:554/cam/realmonitor?channel=1&subtype=0"}],
            "username": "admin", "password": "pw"}
    r = env.tc.post(f"/api/v1/sites/{site['id']}/cameras/bulk", json=good)
    assert r.status_code == 201, r.text
    added = r.json()["added"]
    assert len(added) == 2 and env.provisioned[-2:] == added  # one after another
    names = sorted(c["name"] for c in r.json()["cameras"])
    assert names == ["Gate", "Yard"]


# ── uplink test and camera passwords (S5b) ─────────────────────────────


@pytest.fixture
def fresh_uplink():
    svc._uplink_requests.clear()
    svc._uplink_done.clear()
    yield
    svc._uplink_requests.clear()
    svc._uplink_done.clear()


def test_the_uplink_is_tested_only_when_asked_and_once(env, fresh_uplink):
    site = _create(env)
    token = _enrol(env, site["enrol_code"]).json()["token"]
    # Not asked for: the agent cannot upload.
    assert env.tc.post("/api/v1/site-agent/speedtest/1", content=b"x" * 1000, headers=_auth(token)).status_code == 404
    rid = env.tc.post(f"/api/v1/sites/{site['id']}/uplink-test").json()["requested"]
    assert env.tc.post(f"/api/v1/sites/{site['id']}/uplink-test").status_code == 429  # once an hour
    ans = env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).json()
    assert ans["uplink_test"] == rid
    assert env.tc.get(f"/api/v1/sites/{site['id']}").json()["uplink_test"]["pending"] is True
    r = env.tc.post(f"/api/v1/site-agent/speedtest/{rid}", content=b"x" * 250_000, headers=_auth(token))
    assert r.status_code == 200 and r.json()["bytes"] == 250_000 and r.json()["kbps"] > 0
    # Used up: a second upload for the same test is refused.
    assert env.tc.post(f"/api/v1/site-agent/speedtest/{rid}", content=b"x", headers=_auth(token)).status_code == 404
    # The agent's next report does not wipe the result.
    env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token))
    view = env.tc.get(f"/api/v1/sites/{site['id']}").json()["uplink_test"]
    assert view["pending"] is False and view["kbps"] == r.json()["kbps"]
    # Another site's credential gets nothing.
    other = _create(env, "Other")
    other_token = _enrol(env, other["enrol_code"]).json()["token"]
    assert env.tc.post(f"/api/v1/site-agent/speedtest/{rid}", content=b"x", headers=_auth(other_token)).status_code == 404


def test_a_new_camera_password_reaches_the_agent(env):
    site = _create(env)
    token = _enrol(env, site["enrol_code"]).json()["token"]
    for key in ("pushed", "pushed2"):
        env.tc.put(f"/api/v1/sites/{site['id']}/cameras/{env.ids[key]}",
                   json={"main_url": "rtsp://192.168.1.64:554/main", "username": "admin", "password": "old"})
    assert env.tc.post(f"/api/v1/sites/{site['id']}/cameras/credentials", json={}).status_code == 422
    r = env.tc.post(f"/api/v1/sites/{site['id']}/cameras/credentials",
                    json={"camera_ids": [env.ids["pushed"]], "password": "n3w"})
    assert r.status_code == 200 and r.json()["changed"] == [env.ids["pushed"]]
    urls = {d["camera_id"]: d["main_url"] for d in
            env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).json()["desired"]}
    assert urls[env.ids["pushed"]] == "rtsp://admin:n3w@192.168.1.64:554/main"
    assert urls[env.ids["pushed2"]] == "rtsp://admin:old@192.168.1.64:554/main"
    # All of them, user too.
    env.tc.post(f"/api/v1/sites/{site['id']}/cameras/credentials", json={"username": "ops", "password": "p2"})
    urls = [d["main_url"] for d in
            env.tc.post("/api/v1/site-agent/sync", json=REPORT, headers=_auth(token)).json()["desired"]]
    assert urls == ["rtsp://ops:p2@192.168.1.64:554/main"] * 2

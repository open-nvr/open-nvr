# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HTTP to a remote site's cameras through its agent (services/site_relay.py).

What these pin: a site camera's control host is a synthetic name that only
camera_http_client routes, to that site's agent; the agent gets the camera's
real LAN host and the request's headers and body; a digest exchange (401,
then the authorised retry) works across the relay; an agent that does not
answer is a timeout and one that fails is a connection error; an answer for
another site's request is refused.
"""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
try:
    from cryptography.fernet import Fernet

    os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:  # pragma: no cover
    pass

from services import site_relay as relay  # noqa: E402

CAM = SimpleNamespace(id=7, site_id=3, site_lan_host="192.168.1.64", ip_address="0.0.0.0")
HOST = "c7.s3.opennvr-site.invalid"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    relay._pending.clear()
    relay._inflight.clear()
    relay._routes.clear()
    relay.prime(CAM)
    monkeypatch.setattr(relay, "PICKUP_MAX_S", 0.5)
    from services import sites

    monkeypatch.setattr(sites, "notify", lambda site_id: None)
    yield


def test_control_host_and_parsing():
    assert relay.control_host(CAM) == HOST
    assert relay.parse_host(HOST) == (7, 3)
    assert relay.parse_host("HOST".lower()) is None
    assert relay.parse_host("192.168.1.64") is None
    assert relay.parse_host("c7.s3.opennvr-site.invalid.evil.com") is None
    plain = SimpleNamespace(id=1, site_id=None, site_lan_host=None, ip_address="10.0.0.7")
    assert relay.control_host(plain) == "10.0.0.7"


async def _agent(site_id: int, answer, seen: list, rounds: int = 1):
    """A fake agent: takes the site's queued requests and answers them."""
    done = 0
    while done < rounds:
        await asyncio.sleep(0.01)
        for req in relay.take_pending(site_id):
            seen.append(req)
            relay.complete(site_id, req["id"], answer(req))
            done += 1


@pytest.mark.asyncio
async def test_a_request_goes_to_the_cameras_lan_host_through_the_agent():
    seen: list = []
    agent = asyncio.create_task(_agent(3, lambda r: {
        "status": 200, "headers": {"Content-Type": ["application/soap+xml"]},
        "body_b64": base64.b64encode(b"<ok/>").decode(),
    }, seen))
    async with relay.camera_http_client(timeout=5, verify=False) as c:
        r = await c.post(f"http://{HOST}:8080/onvif/device_service", content=b"<soap/>",
                         headers={"Content-Type": "application/soap+xml"})
    await agent
    assert r.status_code == 200 and r.text == "<ok/>"
    req = seen[0]
    assert req["url"] == "http://192.168.1.64:8080/onvif/device_service"
    assert base64.b64decode(req["body_b64"]) == b"<soap/>"
    assert req["headers"]["content-type"] == ["application/soap+xml"]
    assert "host" not in {k.lower() for k in req["headers"]}


@pytest.mark.asyncio
async def test_digest_auth_works_across_the_relay():
    seen: list = []
    challenge = 'Digest realm="cam", nonce="abc", qop="auth", algorithm=MD5'

    def answer(req):
        if not any(k.lower() == "authorization" for k in req["headers"]):
            return {"status": 401, "headers": {"WWW-Authenticate": [challenge]}, "body_b64": ""}
        return {"status": 200, "headers": {}, "body_b64": base64.b64encode(b"granted").decode()}

    agent = asyncio.create_task(_agent(3, answer, seen, rounds=2))
    async with relay.camera_http_client(timeout=5, verify=False) as c:
        r = await c.get(f"http://{HOST}/ISAPI/System/deviceInfo", auth=httpx.DigestAuth("admin", "pw"))
    await agent
    assert r.status_code == 200 and r.text == "granted"
    assert len(seen) == 2 and seen[1]["headers"]["authorization"][0].startswith("Digest ")


@pytest.mark.asyncio
async def test_no_agent_is_a_timeout_and_the_request_is_withdrawn():
    async with relay.camera_http_client(timeout=0.2, verify=False) as c:
        with pytest.raises(httpx.ReadTimeout):
            await c.get(f"http://{HOST}/x")
    assert not relay.has_pending(3) and not relay._inflight


@pytest.mark.asyncio
async def test_an_agent_side_failure_is_a_connection_error():
    agent = asyncio.create_task(_agent(3, lambda r: {"error": "dial tcp 192.168.1.64:80: refused"}, []))
    async with relay.camera_http_client(timeout=5, verify=False) as c:
        with pytest.raises(httpx.ConnectError, match="refused"):
            await c.get(f"http://{HOST}/x")
    await agent


@pytest.mark.asyncio
async def test_an_answer_from_another_site_is_refused():
    async def bad_agent():
        await asyncio.sleep(0.01)
        reqs = relay.take_pending(3)
        assert reqs
        assert relay.complete(4, reqs[0]["id"], {"status": 200}) is False  # site 4 cannot answer
        assert relay.complete(3, reqs[0]["id"], {"status": 204}) is True

    agent = asyncio.create_task(bad_agent())
    async with relay.camera_http_client(timeout=5, verify=False) as c:
        r = await c.get(f"http://{HOST}/x")
    await agent
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_a_camera_that_is_no_longer_the_sites_is_not_reached(monkeypatch):
    relay._routes.clear()
    monkeypatch.setattr(relay, "_lan_host", lambda cam, site: None)
    async with relay.camera_http_client(timeout=1, verify=False) as c:
        with pytest.raises(httpx.ConnectError):
            await c.get(f"http://{HOST}/x")
    assert not relay.has_pending(3)

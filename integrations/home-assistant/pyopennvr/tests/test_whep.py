# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""WHEP: offer → answer, session URL rebuilt under /webrtc, trickle, close."""

from __future__ import annotations

import pytest

from pyopennvr import OpenNVRAuthError, OpenNVRClient, OpenNVRNotFoundError, Whep
from pyopennvr.whep import candidate_fragment, resolve_session_url

from .conftest import fake_site

INFO = {"camera_id": 1, "stream_name": "cam-1", "token": "streamjwt",
        "urls": {"webrtc": "https://localhost/webrtc/cam-1/whep", "rtsps": None}}


def test_location_gets_the_proxy_prefix_back():
    whep = "https://nvr.local/webrtc/cam-1/whep"
    assert resolve_session_url("/cam-1/whep/abc", whep) == "https://nvr.local/webrtc/cam-1/whep/abc"
    # Another origin would receive the stream token on PATCH/DELETE: refused.
    assert resolve_session_url("https://m/x/whep/abc", whep) is None
    assert resolve_session_url("//evil.example/x", whep) is None
    assert resolve_session_url("https://nvr.local/webrtc/cam-1/whep/abc", whep) == (
        "https://nvr.local/webrtc/cam-1/whep/abc")
    assert resolve_session_url("abc", whep) == "https://nvr.local/webrtc/cam-1/abc"
    assert resolve_session_url(None, whep) is None


def test_candidate_fragment():
    frag = candidate_fragment("candidate:1 1 UDP 1 10.0.0.2 5000 typ host", "0")
    assert frag.endswith("a=candidate:1 1 UDP 1 10.0.0.2 5000 typ host\r\n")
    assert "a=mid:0" in frag


async def test_offer_answer_trickle_close():
    routes = {
        ("GET", "/api/v1/streams/1/info"): INFO,
        ("POST", "/webrtc/cam-1/whep"): (201, "v=0 answer", {"Location": "/cam-1/whep/s1"}),
        ("PATCH", "/webrtc/cam-1/whep/s1"): (204, ""),
        ("DELETE", "/webrtc/cam-1/whep/s1"): (200, ""),
    }
    async with fake_site(routes) as (base, session, rec):
        whep = Whep(OpenNVRClient(base, "onvr_x", session), session)
        s = await whep.offer(1, "v=0 offer")
        await whep.add_candidate(s, "candidate:1 1 UDP 1 10.0.0.2 5000 typ host", "0")
        await whep.close(s)
    assert s.answer_sdp == "v=0 answer" and s.session_url == f"{base}/webrtc/cam-1/whep/s1"
    post, patch, delete = rec.requests[1:]
    assert post["headers"]["Authorization"] == "Bearer streamjwt"
    assert post["headers"]["Content-Type"] == "application/sdp" and post["body"] == b"v=0 offer"
    assert patch["headers"]["Content-Type"] == "application/trickle-ice-sdpfrag"
    assert delete["method"] == "DELETE" and delete["path"] == "/webrtc/cam-1/whep/s1"


@pytest.mark.parametrize("status, error", [(404, OpenNVRNotFoundError),
                                           (400, OpenNVRAuthError), (401, OpenNVRAuthError)])
async def test_offer_errors(status, error):
    routes = {("GET", "/api/v1/streams/1/info"): INFO,
              ("POST", "/webrtc/cam-1/whep"): (status, "")}
    async with fake_site(routes) as (base, session, _):
        with pytest.raises(error):
            await Whep(OpenNVRClient(base, "onvr_x", session), session).offer(1, "v=0 offer")

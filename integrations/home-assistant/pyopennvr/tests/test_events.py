# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""EventStream against a real (in-process) websocket server speaking v2."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from pyopennvr import EventStream, OpenNVRClient


class FakeServer:
    """Scripted v2 server. ``scripts`` is one list of frames per connection;
    a frame may be ("close", code) to close the socket."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.connections: list[dict] = []
        self.ticket_status = 200

    async def ticket(self, request):
        if self.ticket_status != 200:
            return web.json_response({"detail": "no"}, status=self.ticket_status)
        return web.json_response({"ticket": "t", "expires_in": 30, "kind": "token"})

    async def ws(self, request):
        self.connections.append(dict(request.query))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        script = self.scripts.pop(0) if self.scripts else []
        for frame in script:
            if isinstance(frame, tuple) and frame[0] == "close":
                await ws.close(code=frame[1])
                return ws
            await ws.send_str(json.dumps(frame))
        await ws.close()
        return ws


async def _run(server: FakeServer, *, until=lambda frames: False, max_s=5.0):
    app = web.Application()
    app.router.add_post("/api/v1/events/ws-ticket", server.ticket)
    app.router.add_get("/api/v1/events/ws", server.ws)
    frames, states = [], []
    async with TestServer(app) as ts, aiohttp.ClientSession() as session:
        client = OpenNVRClient(str(ts.make_url("")), "onvr_x", session)
        stream = None

        async def on_frame(f):
            frames.append(f)
            if until(frames):
                await stream.stop()

        stream = EventStream(client, session, on_frame, on_state=states.append,
                             min_backoff=0.01, max_backoff=0.05)
        await asyncio.wait_for(stream.run(), max_s)
    return stream, frames, states


def hello(epoch="E1", seq=10, resumed=False):
    return {"v": 2, "event_type": "subscribed", "epoch": epoch, "seq": seq, "filters": {},
            "resumed": resumed}


def snap(seq=10):
    return {"v": 2, "event_type": "state_snapshot", "seq": seq, "resync": False, "cameras": [],
            "site_mode": None, "entity_states": {}}


async def test_resume_after_a_drop_asks_for_what_was_missed():
    server = FakeServer([
        [hello(), snap(), {"v": 2, "seq": 11, "event_type": "live_state"},
         {"v": 2, "seq": 12, "event_type": "heartbeat"}],        # then the socket drops
        [hello(resumed=True, seq=12), {"v": 2, "seq": 12, "event_type": "app_alert"}],
    ])
    stream, frames, states = await _run(
        server, until=lambda fs: any(f.get("event_type") == "app_alert" for f in fs))
    assert server.connections[0].get("since") is None
    # Resumed from the last EVENT (11), not the heartbeat's seq (12).
    assert server.connections[1]["since"] == "11" and server.connections[1]["epoch"] == "E1"
    assert stream.last_seq == 12
    assert "connected" in states and states[-1] == "stopped"


async def test_a_new_epoch_resets_the_resume_point():
    server = FakeServer([
        [hello(epoch="E1"), snap(), {"v": 2, "seq": 11, "event_type": "live_state"}],
        [hello(epoch="E2", seq=0), snap(seq=0)],
        [hello(epoch="E2", seq=0), {"v": 2, "seq": 1, "event_type": "live_state"}],
    ])
    stream, frames, _ = await _run(server, until=lambda fs: len(fs) >= 6)
    assert server.connections[1]["since"] == "11"          # asked; server restarted
    assert server.connections[2]["since"] == "0" and server.connections[2]["epoch"] == "E2"


async def test_revoked_token_stops_the_stream():
    server = FakeServer([[hello(), snap(), ("close", 4401)]])
    stream, _frames, states = await _run(server)
    assert stream.state == "auth_failed" and states[-1] == "auth_failed"
    assert len(server.connections) == 1                      # no retry with a dead token


async def test_ticket_refused_is_auth_failure():
    server = FakeServer([])
    server.ticket_status = 401
    stream, _frames, _ = await _run(server)
    assert stream.state == "auth_failed" and server.connections == []


async def test_junk_frames_are_skipped_and_unknown_types_passed_on():
    server = FakeServer([[hello(), "not json", [1, 2], {"v": 2, "seq": 11,
                                                         "event_type": "from_the_future"}]])
    _stream, frames, _ = await _run(
        server, until=lambda fs: any(f.get("event_type") == "from_the_future" for f in fs))
    assert [f["event_type"] for f in frames] == ["subscribed", "from_the_future"]


@pytest.mark.parametrize("n", [3])
async def test_it_keeps_reconnecting_with_backoff(n):
    server = FakeServer([[] for _ in range(n)] + [[hello(), snap()]])
    _stream, frames, _ = await _run(server, until=lambda fs: any(
        f.get("event_type") == "state_snapshot" for f in fs))
    assert len(server.connections) == n + 1

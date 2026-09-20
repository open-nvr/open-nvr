# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""EventStream against a real (in-process) websocket server speaking v2."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from pyopennvr import EventStream, OpenNVRClient


class FakeServer:
    """Scripted v2 server. ``scripts`` is one list of frames per connection;
    a frame may be ("close", code) to close the socket, or ("hang", seconds)
    to go silent without closing (a half-open connection, as the client
    sees it)."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.connections: list[dict] = []
        #: ``time.monotonic()`` when each connection was accepted.
        self.connected_at: list[float] = []
        self.ticket_status = 200
        self.ticket_headers: dict[str, str] = {}
        self.tickets = 0
        #: After this many tickets, refuse the rest with ``ticket_status``.
        self.refuse_after: int | None = None
        #: Seconds the ticket endpoint takes to answer.
        self.ticket_delay = 0.0
        #: Refuse the websocket upgrade itself with this status.
        self.ws_status: int | None = None

    async def ticket(self, request):
        self.tickets += 1
        if self.ticket_delay:
            await asyncio.sleep(self.ticket_delay)
        refuse = (self.refuse_after is not None and self.tickets > self.refuse_after) or (
            self.refuse_after is None and self.ticket_status != 200)
        if refuse:
            return web.json_response({"detail": "no"}, status=self.ticket_status,
                                     headers=self.ticket_headers)
        return web.json_response({"ticket": "t", "expires_in": 30, "kind": "token"})

    async def ws(self, request):
        self.connections.append(dict(request.query))
        self.connected_at.append(time.monotonic())
        if self.ws_status is not None:
            return web.Response(status=self.ws_status)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        script = self.scripts.pop(0) if self.scripts else []
        for frame in script:
            if isinstance(frame, tuple) and frame[0] == "close":
                await ws.close(code=frame[1])
                return ws
            if isinstance(frame, tuple) and frame[0] == "hang":
                await asyncio.sleep(frame[1])
                continue
            await ws.send_str(json.dumps(frame))
        await ws.close()
        return ws


def _app(server: FakeServer) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/v1/events/ws-ticket", server.ticket)
    app.router.add_get("/api/v1/events/ws", server.ws)
    return app


async def _run(server: FakeServer, *, until=lambda frames: False, max_s=5.0, **kwargs):
    frames, states = [], []
    async with TestServer(_app(server)) as ts, aiohttp.ClientSession() as session:
        client = OpenNVRClient(str(ts.make_url("")), "onvr_x", session)
        stream = None

        async def on_frame(f):
            frames.append(f)
            if until(frames):
                await stream.stop()

        stream = EventStream(client, session, on_frame, on_state=states.append,
                             **{"min_backoff": 0.01, "max_backoff": 0.05, **kwargs})
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


async def test_4401_reconnects_and_a_dead_token_then_stops():
    """4401 also means "what you may see changed": reconnect. A revoked token
    then fails to mint a ticket, and only that stops the stream."""
    server = FakeServer([[hello(), snap(), ("close", 4401)]])
    server.ticket_status, server.refuse_after = 401, 1
    stream, _frames, states = await _run(server)
    assert stream.state == "auth_failed" and states[-1] == "auth_failed"
    assert server.tickets == 2 and len(server.connections) == 1


async def test_4401_with_the_token_still_good_resumes():
    server = FakeServer([
        [hello(), snap(), {"v": 2, "seq": 11, "event_type": "live_state"}, ("close", 4401)],
        [hello(resumed=True, seq=11), {"v": 2, "seq": 12, "event_type": "app_alert"}],
    ])
    stream, frames, _ = await _run(
        server, until=lambda fs: any(f.get("event_type") == "app_alert" for f in fs))
    assert server.connections[1]["since"] == "11" and stream.state == "stopped"


async def test_an_address_refusal_is_retried_not_auth_failed():
    server = FakeServer([[hello(), snap()]])
    server.ticket_status, server.refuse_after = 403, 0
    server.ticket_headers = {"X-OpenNVR-Error": "token_address"}
    states = []
    async with TestServer(_app(server)) as ts, aiohttp.ClientSession() as session:
        client = OpenNVRClient(str(ts.make_url("")), "onvr_x", session)
        stream = EventStream(client, session, lambda f: None, on_state=states.append,
                             min_backoff=0.01, max_backoff=0.02)
        task = asyncio.create_task(stream.run())
        while server.tickets < 3:
            await asyncio.sleep(0.01)
        await stream.stop()
        await asyncio.wait_for(task, 2)
    assert "auth_failed" not in states


async def test_a_consumer_error_does_not_end_the_stream():
    server = FakeServer([[hello(), snap(), {"v": 2, "seq": 11, "event_type": "boom"},
                          {"v": 2, "seq": 12, "event_type": "app_alert"}]])
    seen = []
    async with TestServer(_app(server)) as ts, aiohttp.ClientSession() as session:
        client = OpenNVRClient(str(ts.make_url("")), "onvr_x", session)
        stream = None

        def on_frame(f):
            seen.append(f["event_type"])
            if f["event_type"] == "boom":
                raise ValueError("bad value")
            if f["event_type"] == "app_alert":
                asyncio.get_running_loop().create_task(stream.stop())

        stream = EventStream(client, session, on_frame, min_backoff=0.01, max_backoff=0.02)
        await asyncio.wait_for(stream.run(), 5)
    assert seen[-2:] == ["boom", "app_alert"]


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


async def test_a_connection_that_dies_by_timeout_resets_the_backoff():
    """A half-open TCP connection surfaces as a receive timeout, i.e. an
    exception out of the connection, not a normal close. The connection had
    been established, so the next attempt must come after the MINIMUM
    backoff, not one doubled from before it (which would then never shrink
    again, since every later drop looks the same)."""
    server = FakeServer([[], [], [],                              # ratchets up
                         [hello(), snap(), ("hang", 1.0)],        # then goes silent
                         [hello(), snap()]])
    _stream, frames, states = await _run(
        server, min_backoff=0.05, max_backoff=1.0, silence_timeout=0.1,
        until=lambda fs: sum(f.get("event_type") == "state_snapshot" for f in fs) == 2)
    assert len(server.connections) == 5
    # Silence timeout + minimum backoff (with jitter): well under the 0.8 s a
    # doubled backoff would have waited.
    assert server.connected_at[4] - server.connected_at[3] < 0.5
    assert states.count("connected") == 2


async def test_stop_interrupts_the_backoff_sleep():
    server = FakeServer([[]])                       # closes at once: a long backoff follows
    async with TestServer(_app(server)) as ts, aiohttp.ClientSession() as session:
        client = OpenNVRClient(str(ts.make_url("")), "onvr_x", session)
        stream = EventStream(client, session, lambda f: None, min_backoff=30, max_backoff=60)
        task = asyncio.create_task(stream.run())
        while not (server.connections and stream.state == "disconnected"):
            await asyncio.sleep(0.01)               # "disconnected" is also the initial state
        await stream.stop()
        await asyncio.wait_for(task, 1)             # not 30 s
    assert stream.state == "stopped" and len(server.connections) == 1


async def test_stop_during_the_ticket_round_trip_opens_no_socket():
    server = FakeServer([[hello(), snap()]])
    server.ticket_delay = 0.2
    async with TestServer(_app(server)) as ts, aiohttp.ClientSession() as session:
        client = OpenNVRClient(str(ts.make_url("")), "onvr_x", session)
        stream = EventStream(client, session, lambda f: None, min_backoff=0.01)
        task = asyncio.create_task(stream.run())
        while server.tickets < 1:
            await asyncio.sleep(0.01)
        await stream.stop()                         # the ticket is still being minted
        await asyncio.wait_for(task, 2)
    assert server.connections == [] and stream.state == "stopped"


async def test_a_refused_handshake_is_logged_without_the_ticket(caplog):
    """aiohttp's handshake error prints the URL, and ours carries the
    single-use ticket."""
    server = FakeServer([[hello(), snap()]])
    server.ws_status = 403
    with caplog.at_level(logging.DEBUG, logger="pyopennvr.events"):
        async with TestServer(_app(server)) as ts, aiohttp.ClientSession() as session:
            client = OpenNVRClient(str(ts.make_url("")), "onvr_x", session)
            stream = EventStream(client, session, lambda f: None, min_backoff=0.01,
                                 max_backoff=0.02)
            task = asyncio.create_task(stream.run())
            while len(server.connections) < 2:
                await asyncio.sleep(0.01)
            await stream.stop()
            await asyncio.wait_for(task, 2)
    failed = [r.getMessage() for r in caplog.records if "connection failed" in r.getMessage()]
    assert failed and all("403" in m for m in failed)
    assert not any("ticket=" in r.getMessage() for r in caplog.records)

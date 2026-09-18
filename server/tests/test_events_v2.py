# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-111: events WebSocket v2 (numbered, resumable) without touching v1.

Bus:
* every event gets the next seq and lands in the replay ring, even when
  nobody is subscribed (a disconnected client is who needs replay);
* replay is complete only when the ring still holds everything after
  ``since``; a gap, a pruned range or a future ``since`` says "resync";
* v1 subscribers receive the bare event dict, exactly as before;
* the client's ``types`` filter narrows, entitlements still apply.

Socket:
* v2 opens with ``subscribed`` (epoch, seq) then a ``state_snapshot``;
* reconnecting with ``since`` + the same epoch replays what was missed and
  no snapshot; a different epoch (restart) or a pruned range → snapshot
  with ``resync: true``;
* replay honours the token's cameras and event types.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from services import event_bus_service as ebs
from services.event_bus_service import EventBus
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture


def _ev(kind="camera_event", cam=1, **kw):
    return {"event_type": kind, "camera_id": cam, "task": "tier0", "payload": kw or {"n": 1}}


# ── bus ───────────────────────────────────────────────────────────────


def test_seq_and_ring_even_without_subscribers():
    bus = EventBus()
    for i in range(3):
        asyncio.run(bus.publish(_ev(n=i)))
    assert bus.current_seq == 3 and [s for s, _t, _e in bus._ring] == [1, 2, 3]


def test_v1_subscribers_get_the_bare_event_unchanged():
    """Pins v1: no seq, no v, the same keys as before HA-111."""
    bus = EventBus()

    async def run():
        async with bus.subscribe() as sub:
            await bus.publish(_ev())
            return await sub.queue.get()

    got = asyncio.run(run())
    assert isinstance(got, dict)
    assert set(got) == {"event_type", "camera_id", "task", "payload", "timestamp"}


def test_replay_complete_gap_and_future():
    bus = EventBus()

    async def run():
        for i in range(5):
            await bus.publish(_ev(n=i))
        async with bus.subscribe(with_seq=True) as sub:
            full, ok = await bus.replay(sub, 2)
            nothing, ok_now = await bus.replay(sub, 5)
            future = await bus.replay(sub, 9)
            bus._ring.popleft()           # seq 1 aged out
            bus._ring.popleft()           # seq 2 aged out
            gap = await bus.replay(sub, 1)
            edge = await bus.replay(sub, 2)
            return full, ok, nothing, ok_now, future, gap, edge

    full, ok, nothing, ok_now, future, gap, edge = asyncio.run(run())
    assert ok and [s for s, _ in full] == [3, 4, 5]
    assert ok_now and nothing == []
    assert future == ([], False)
    assert gap == ([], False)
    assert edge[1] is True and [s for s, _ in edge[0]] == [3, 4, 5]


def test_ring_prunes_by_age(monkeypatch):
    bus = EventBus()
    clock = [1000.0]
    monkeypatch.setattr(ebs.time, "monotonic", lambda: clock[0])
    asyncio.run(bus.publish(_ev()))
    clock[0] += ebs.RING_SECONDS + 1
    asyncio.run(bus.publish(_ev()))
    assert [s for s, _t, _e in bus._ring] == [2]


def test_types_filter_and_entitlement():
    bus = EventBus()

    async def run():
        async with bus.subscribe(with_seq=True, event_types=["live_state"],
                                 allowed_camera_ids={1}) as sub:
            await bus.publish(_ev("tracks", 1))
            await bus.publish(_ev("live_state", 2))
            await bus.publish(_ev("live_state", 1))
            return [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]

    got = asyncio.run(run())
    assert [(s, e["event_type"], e["camera_id"]) for s, e in got] == [(3, "live_state", 1)]


# ── socket ────────────────────────────────────────────────────────────


@pytest.fixture()
def bus(monkeypatch):
    fresh = EventBus()
    monkeypatch.setattr(ebs, "_event_bus_instance", fresh)
    import services.live_state as ls_mod

    monkeypatch.setattr(ls_mod, "_instance", ls_mod.LiveState())
    return fresh


def _ticket(env, headers):  # noqa: F811
    r = env.client.post("/api/v1/events/ws-ticket", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


def _open(env, headers, **q):  # noqa: F811
    qs = "&".join([f"ticket={_ticket(env, headers)}", "v=2"] +
                  [f"{k}={v}" for k, v in q.items() if v is not None])
    return env.client.websocket_connect(f"/api/v1/events/ws?{qs}")


def test_v2_opens_with_hello_and_snapshot(env, bus):  # noqa: F811
    asyncio.run(bus.publish(_ev()))
    with _open(env, env.jwt("admin")) as ws:
        hello = ws.receive_json()
        snap = ws.receive_json()
    assert hello["v"] == 2 and hello["epoch"] == bus.epoch and hello["seq"] == 1
    assert hello["resumed"] is False
    assert snap["event_type"] == "state_snapshot" and snap["resync"] is False
    assert [c["camera_id"] for c in snap["cameras"]] == [1, 2, 3]
    assert {"objects", "zones", "motion", "online"} <= set(snap["cameras"][0])


def test_resume_replays_what_was_missed_and_no_snapshot(env, bus):  # noqa: F811
    with _open(env, env.jwt("admin")) as ws:
        hello = ws.receive_json()
        ws.receive_json()
    # Disconnected: these must be replayable.
    asyncio.run(bus.publish(_ev("camera_event", 1)))
    asyncio.run(bus.publish(_ev("app_alert", 2)))
    with _open(env, env.jwt("admin"), since=hello["seq"], epoch=hello["epoch"]) as ws:
        again = ws.receive_json()
        frames = [ws.receive_json(), ws.receive_json()]
    assert again["resumed"] is True and again["seq"] == hello["seq"] + 2
    assert [(f["seq"], f["event_type"]) for f in frames] == [
        (hello["seq"] + 1, "camera_event"), (hello["seq"] + 2, "app_alert")]
    assert all(f["v"] == 2 for f in frames)


def test_a_new_epoch_or_a_pruned_range_means_resync(env, bus):  # noqa: F811
    asyncio.run(bus.publish(_ev()))
    with _open(env, env.jwt("admin"), since=0, epoch="restarted") as ws:
        assert ws.receive_json()["resumed"] is False
        snap = ws.receive_json()
    assert snap["event_type"] == "state_snapshot" and snap["resync"] is True
    asyncio.run(bus.publish(_ev()))
    bus._ring.clear()
    with _open(env, env.jwt("admin"), since=0, epoch=bus.epoch) as ws:
        ws.receive_json()
        assert ws.receive_json()["resync"] is True


def test_replay_keeps_the_tokens_cameras_and_types(env, bus):  # noqa: F811
    tok = _mint(env, scopes=["cameras.view", "live.view"], camera_ids=[1])["token"]
    with _open(env, _as(tok)) as ws:
        hello = ws.receive_json()
        snap = ws.receive_json()
    assert [c["camera_id"] for c in snap["cameras"]] == [1]
    asyncio.run(bus.publish(_ev("inference_result", 2)))  # other camera
    asyncio.run(bus.publish(_ev("app_alert", 1)))         # no alerts.view
    asyncio.run(bus.publish(_ev("live_state", 1)))        # filtered out by types
    asyncio.run(bus.publish(_ev("inference_result", 1)))  # the one to get
    with _open(env, _as(tok), since=hello["seq"], epoch=hello["epoch"],
               types="inference_result") as ws:
        ws.receive_json()
        frame = ws.receive_json()
    assert (frame["event_type"], frame["camera_id"], frame["seq"]) == (
        "inference_result", 1, hello["seq"] + 4)


def test_v1_socket_is_unchanged(env, bus):  # noqa: F811
    with env.client.websocket_connect(
            f"/api/v1/events/ws?ticket={_ticket(env, env.jwt('admin'))}") as ws:
        hello = ws.receive_json()
    assert hello == {"event_type": "subscribed", "filters": {"camera_id": None, "task": None}}
    assert json.dumps(hello)  # v1 hello carries no epoch/seq/v


def test_live_boxes_are_not_kept_for_replay():
    """M1 review: tracks frames filled the ring (and memory) on every install."""
    bus = EventBus()
    asyncio.run(bus.publish(_ev("tracks", 1)))
    asyncio.run(bus.publish(_ev("app_alert", 1)))
    assert [e["event_type"] for _s, _t, e in bus._ring] == ["app_alert"]
    assert bus.current_seq == 2       # still numbered: live clients see the gap as filtered

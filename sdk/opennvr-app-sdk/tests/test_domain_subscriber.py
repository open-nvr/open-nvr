# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DomainEventSubscriber + InferStream + the multi-subject NATS loop."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from opennvr_app_sdk import (
    DomainEvent, DomainEventSubscriber, InferStream, parse_domain_event,
)
from opennvr_app_sdk.domain_subscriber import subscription_subject
from opennvr_app_sdk.frame_app import KaiCError
from opennvr_app_sdk.nats_loop import NatsSubscriberMixin


def _envelope(schema="plate.recognized.v1", camera="cam3", **payload):
    return {"id": "evt_1", "schema": schema, "correlation_id": None,
            "camera_id": camera, "ts": "2026-09-05T10:00:00+00:00",
            "producer": "kai-c", "payload": payload or {"plate_text": "KA01AB1234"}}


def test_parse_and_subjects():
    ev = parse_domain_event(json.dumps(_envelope()).encode(),
                            subject="opennvr.events.plate.recognized.v1.cam3")
    assert ev == DomainEvent(id="evt_1", schema="plate.recognized.v1", camera_id="cam3",
                             ts="2026-09-05T10:00:00+00:00",
                             payload={"plate_text": "KA01AB1234"}, producer="kai-c",
                             correlation_id=None,
                             subject="opennvr.events.plate.recognized.v1.cam3")
    assert parse_domain_event(b"not json") is None
    assert parse_domain_event({"schema": "x", "camera_id": "cam1"}) is None   # no payload
    assert subscription_subject("plate.recognized.v1") == "opennvr.events.plate.recognized.v1.>"
    assert subscription_subject("opennvr.events.occupancy.changed.v1.cam2") == \
        "opennvr.events.occupancy.changed.v1.cam2"


class _Gate(DomainEventSubscriber):
    subscriptions = ["plate.recognized.v1", "access.decided.v1"]

    def setup(self):
        self.seen = []

    def on_event(self, event):
        if event.payload.get("boom"):
            raise RuntimeError("handler bug")
        self.seen.append((event.schema, event.camera_id))


def _cfg(**over):
    base = {"nats_url": "nats://x", "nats_token": None, "contract_port": None}
    base.update(over)
    return SimpleNamespace(**base)


def test_subscriber_dispatches_and_isolates():
    app = _Gate(_cfg())
    assert app._nats_subjects() == ["opennvr.events.plate.recognized.v1.>",
                                    "opennvr.events.access.decided.v1.>"]
    assert app._handle_raw(json.dumps(_envelope()).encode(), subject="s") is True
    assert app._handle_raw(b"{bad", subject="s") is False
    assert app._handle_raw(json.dumps(_envelope(boom=True)).encode(), subject="s") is False
    assert app.seen == [("plate.recognized.v1", "cam3")]
    # cfg.subject_pattern overrides the class list; an empty list is an error.
    assert _Gate(_cfg(subject_pattern="opennvr.events.>"))._nats_subjects() == ["opennvr.events.>"]

    class _Empty(DomainEventSubscriber):
        def on_event(self, event):
            pass

    with pytest.raises(ValueError):
        _Empty(_cfg())._nats_subjects()


def test_nats_loop_subscribes_to_every_subject(monkeypatch):
    """The shared loop funnels N subscriptions through one handler."""
    subscribed: list[str] = []

    class _Sub:
        async def unsubscribe(self):
            pass

    class _NC:
        async def subscribe(self, subject, cb=None):
            subscribed.append(subject)
            if subject.startswith("opennvr.events.plate"):
                await cb(SimpleNamespace(data=json.dumps(_envelope()).encode(),
                                         subject=subject))
            return _Sub()

        async def drain(self):
            pass

    async def _connect(**kw):
        return _NC()

    import sys
    monkeypatch.setitem(sys.modules, "nats", SimpleNamespace(connect=_connect))
    app = _Gate(_cfg())
    asyncio.run(app._run_nats_loop(once=True))
    assert subscribed == app._nats_subjects()
    assert app.seen == [("plate.recognized.v1", "cam3")]


# ── InferStream ────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, replies):
        self.sent = []
        self.replies = list(replies)
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def recv(self, timeout=None):
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        self.closed = True


def test_infer_stream_handshake_frames_and_teardown():
    conns = []

    def factory(url, headers):
        assert url == "ws://kaic:8100/api/v1/infer/yolov8/stream"
        assert ("X-Internal-Api-Key", "k") in headers
        c = _Conn([json.dumps({"type": "handshake_ack"}),
                   json.dumps({"type": "result", "inference_ms": 7,
                               "result": {"detections": [{"label": "person"}]}}),
                   ConnectionError("gone")])
        conns.append(c)
        return c

    s = InferStream("http://kaic:8100", "k", adapter="yolov8", camera_id="cam1",
                    websocket_factory=factory)
    with s:
        out = s.infer(b"\xff\xd8")
        assert out["result"]["detections"][0]["label"] == "person"
        assert out["inference_ms"] == 7 and out["correlation_id"] == s.correlation_id
        frame_hdr = json.loads(conns[0].sent[1])
        assert frame_hdr == {"type": "frame", "seq": 1, "ts_ms": frame_hdr["ts_ms"],
                             "content_type": "image/jpeg"}
        assert json.loads(conns[0].sent[0])["camera_id"] == "cam1"
        # A transport failure closes the session and raises; the next
        # call reconnects with a fresh sequence.
        with pytest.raises(KaiCError):
            s.infer(b"\xff\xd8")
        assert conns[0].closed
    assert len(conns) == 1


def test_infer_stream_rejects_bad_handshake():
    def factory(url, headers):
        return _Conn([json.dumps({"type": "error", "detail": "unknown adapter"})])

    s = InferStream("https://kaic", None, adapter="x", camera_id="cam1",
                    websocket_factory=factory)
    assert s.url == "wss://kaic/api/v1/infer/x/stream"
    with pytest.raises(KaiCError):
        s.open()

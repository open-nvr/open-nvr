# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Alert dispatch tests — stdout + webhook + NATS + failure isolation.

Ported from ``examples/loitering-detection/tests/test_alerts.py`` with
only import paths (and monkeypatch targets, which are import paths)
adjusted — the content is otherwise identical, because that suite is
the §11.5 behavior spec the SDK promotion must preserve. The default
``source.name == "loitering-detection"`` assertions are satisfied by
the autouse fixture in ``conftest.py``.
"""
from __future__ import annotations

import json

import httpx
import pytest

from opennvr_app_sdk.alerts import (
    Alert,
    AlertChannel,
    AlertDispatcher,
    AlertSource,
    StdoutChannel,
    WebhookChannel,
    build_dispatcher,
)


def _alert() -> Alert:
    return Alert(
        title="Person in restricted zone 'front-yard'",
        description="Detected person.",
        camera_id="cam-front",
        correlation_id="corr-123",
        tags=["intrusion", "person"],
    )


# ── Alert wire shape ───────────────────────────────────────────────


def test_alert_to_wire_includes_all_required_fields():
    alert = _alert()
    wire = alert.to_wire()
    for key in (
        "alert_id", "fired_at", "title", "description", "severity",
        "source", "camera_id", "correlation_id", "evidence", "tags",
    ):
        assert key in wire, f"missing {key}"


def test_alert_id_is_unique_prefix():
    a1, a2 = _alert(), _alert()
    assert a1.alert_id != a2.alert_id
    assert a1.alert_id.startswith("alrt_")


def test_alert_source_defaults_to_app_kind():
    alert = _alert()
    assert alert.source.kind == "app"
    assert alert.source.name == "loitering-detection"


# ── Stdout channel ─────────────────────────────────────────────────


def test_stdout_channel_prints_one_line(capsys):
    channel = StdoutChannel()
    assert channel.send(_alert())
    out = capsys.readouterr().out
    assert "ALERT" in out
    assert "HIGH" in out
    assert "corr-123" in out
    # Single line
    assert out.count("\n") == 1


# ── Webhook channel ────────────────────────────────────────────────


class _CapturingTransport(httpx.BaseTransport):
    """Captures every request for assertions; returns a fixed status."""

    def __init__(self, status_code: int = 200) -> None:
        self.calls: list[dict] = []
        self._status_code = status_code

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = bytes(request.read())
        self.calls.append({
            "url": str(request.url),
            "method": request.method,
            "body": body,
        })
        return httpx.Response(self._status_code, json={"ok": True})


def test_webhook_channel_posts_alert_json(monkeypatch):
    transport = _CapturingTransport(200)

    def _patched(url, **kwargs):
        # ``trust_env=`` lives on the Client constructor, not on
        # per-request .post() — strip when proxying through a stub.
        kwargs.pop("trust_env", None)
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr("opennvr_app_sdk.alerts.httpx.post", _patched)
    channel = WebhookChannel("https://example.invalid/hook")
    assert channel.send(_alert())
    assert len(transport.calls) == 1
    parsed = json.loads(transport.calls[0]["body"])
    assert parsed["title"].startswith("Person in restricted zone")
    assert parsed["camera_id"] == "cam-front"


def test_webhook_channel_returns_false_on_http_error(monkeypatch):
    transport = _CapturingTransport(500)

    def _patched(url, **kwargs):
        kwargs.pop("trust_env", None)
        return httpx.Client(transport=transport).post(url, **kwargs)

    monkeypatch.setattr("opennvr_app_sdk.alerts.httpx.post", _patched)
    channel = WebhookChannel("https://example.invalid/hook")
    assert not channel.send(_alert())  # but no raise


def test_webhook_channel_swallows_transport_exception(monkeypatch):
    def _raises(*args, **kwargs):
        raise RuntimeError("DNS lookup failed")
    monkeypatch.setattr("opennvr_app_sdk.alerts.httpx.post", _raises)
    channel = WebhookChannel("https://example.invalid/hook")
    assert not channel.send(_alert())  # MUST NOT raise


# ── Dispatcher ─────────────────────────────────────────────────────


def test_dispatcher_requires_at_least_one_channel():
    with pytest.raises(ValueError):
        AlertDispatcher([])


def test_dispatcher_fires_all_channels(capsys):
    fired_through: dict[str, list[Alert]] = {}

    class RecordingChannel:
        def __init__(self, name: str) -> None:
            self.name = name
            fired_through[name] = []

        def send(self, alert: Alert) -> bool:
            fired_through[self.name].append(alert)
            return True

    dispatcher = AlertDispatcher([RecordingChannel("a"), RecordingChannel("b")])
    alert = _alert()
    report = dispatcher.fire(alert)
    assert report == {"a": True, "b": True}
    assert fired_through["a"] == [alert]
    assert fired_through["b"] == [alert]


def test_dispatcher_isolates_channel_exceptions():
    class BrokenChannel:
        name = "broken"

        def send(self, alert: Alert) -> bool:
            raise RuntimeError("kaboom")

    class WorkingChannel:
        name = "working"
        delivered = False

        def send(self, alert: Alert) -> bool:
            WorkingChannel.delivered = True
            return True

    dispatcher = AlertDispatcher([BrokenChannel(), WorkingChannel()])
    report = dispatcher.fire(_alert())
    assert report["broken"] is False  # caught
    assert report["working"] is True  # downstream channel still fired
    assert WorkingChannel.delivered


# ── build_dispatcher factory ───────────────────────────────────────


def test_build_dispatcher_without_webhook_has_only_stdout():
    dispatcher = build_dispatcher(webhook_url=None)
    report = dispatcher.fire(_alert())
    assert list(report.keys()) == ["stdout"]


def test_build_dispatcher_with_webhook_has_both():
    dispatcher = build_dispatcher(webhook_url="https://example.invalid/hook")
    # We don't actually send the webhook here (no monkeypatching);
    # we just verify both channels are present.
    assert {"stdout", "webhook"} <= set(
        getattr(ch, "name", ch.__class__.__name__) for ch in dispatcher._channels
    )


# ── NATS alert channel ─────────────────────────────────────────────
#
# These tests mock the nats-py module so they run without a broker.
# The interesting axes are subject derivation (with sanitization) and
# the failure-isolation contract (publish errors return False, never
# raise, drop the cached connection so the next call retries).

from opennvr_app_sdk.alerts import (  # noqa: E402  — keep grouped with the channel tests
    DEFAULT_ALERT_SUBJECT_PREFIX,
    NatsAlertChannel,
    alert_subject,
)


def test_alert_subject_default_prefix():
    subject = alert_subject(_alert())
    assert subject == "opennvr.alerts.app.loitering-detection.cam-front"


def test_alert_subject_custom_prefix():
    subject = alert_subject(_alert(), prefix="org.example.alerts")
    assert subject == "org.example.alerts.app.loitering-detection.cam-front"


def test_alert_subject_sanitizes_disallowed_chars():
    # Spaces, dots, slashes, NATS reserved (*, >) all collapse to _.
    a = Alert(
        title="t",
        description="d",
        camera_id="cam back/shed 2.0",
        source=AlertSource(kind="app", name="my app *star*", version="1"),
    )
    subject = alert_subject(a)
    assert subject == "opennvr.alerts.app.my_app__star.cam_back_shed_2_0"
    # No NATS reserved chars survived
    assert "*" not in subject
    assert ">" not in subject
    assert " " not in subject


def test_alert_subject_empty_segments_become_unknown():
    a = Alert(
        title="t",
        description="d",
        camera_id="",  # empty
        source=AlertSource(kind="!!!", name="", version="1"),  # all-sanitized-away
    )
    subject = alert_subject(a)
    assert subject == f"{DEFAULT_ALERT_SUBJECT_PREFIX}.unknown.unknown.unknown"


class _FakeNatsClient:
    """Records publishes; honors a publish_error knob for failure tests."""

    def __init__(self, publish_error: BaseException | None = None) -> None:
        self.published: list[tuple[str, bytes]] = []
        self._publish_error = publish_error
        self.drained = False

    async def publish(self, subject: str, payload: bytes) -> None:
        if self._publish_error is not None:
            raise self._publish_error
        self.published.append((subject, payload))

    async def drain(self) -> None:
        self.drained = True


class _FakeNatsModule:
    """Stand-in for the ``nats`` module the channel does ``import nats``
    on. ``connect`` returns a configurable fake client and records the
    connect kwargs so tests can assert auth wiring."""

    def __init__(
        self,
        client: _FakeNatsClient | None = None,
        connect_error: BaseException | None = None,
    ) -> None:
        self.client = client or _FakeNatsClient()
        self._connect_error = connect_error
        self.connect_calls: list[dict] = []

    async def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        if self._connect_error is not None:
            raise self._connect_error
        return self.client


def _install_fake_nats(monkeypatch, fake: _FakeNatsModule) -> None:
    import sys
    monkeypatch.setitem(sys.modules, "nats", fake)


def test_nats_channel_publishes_to_derived_subject(monkeypatch):
    fake = _FakeNatsModule()
    _install_fake_nats(monkeypatch, fake)
    channel = NatsAlertChannel("nats://localhost:4222")
    try:
        assert channel.send(_alert()) is True
        assert len(fake.client.published) == 1
        subject, payload = fake.client.published[0]
        assert subject == "opennvr.alerts.app.loitering-detection.cam-front"
        # Payload is the exact §11.5 wire JSON
        parsed = json.loads(payload)
        assert parsed["camera_id"] == "cam-front"
        assert parsed["correlation_id"] == "corr-123"
        assert parsed["source"]["name"] == "loitering-detection"
    finally:
        channel.close()


def test_nats_channel_passes_token_through(monkeypatch):
    fake = _FakeNatsModule()
    _install_fake_nats(monkeypatch, fake)
    channel = NatsAlertChannel(
        "nats://localhost:4222", token="s3cret",
    )
    try:
        channel.send(_alert())
        assert fake.connect_calls[0]["token"] == "s3cret"
    finally:
        channel.close()


def test_nats_channel_never_gives_up_reconnecting(monkeypatch):
    """Field outage (2026-09-04): a 95-second broker restart killed the
    alert channel FOREVER — max_reconnect_attempts=5 exhausted in ~5s,
    and nats-py buffers publishes silently while reconnecting, so
    nothing errored until the client was already closed. Two days of
    alarms went into a zombie. A bus channel owned by a long-lived
    daemon must retry forever (-1); this pins that so a future 'tidy'
    finite value fails loudly."""
    fake = _FakeNatsModule()
    _install_fake_nats(monkeypatch, fake)
    channel = NatsAlertChannel("nats://localhost:4222")
    try:
        channel.send(_alert())
        assert fake.connect_calls[0]["max_reconnect_attempts"] == -1
    finally:
        channel.close()


def test_nats_channel_omits_token_when_unset(monkeypatch):
    fake = _FakeNatsModule()
    _install_fake_nats(monkeypatch, fake)
    channel = NatsAlertChannel("nats://localhost:4222")  # no token
    try:
        channel.send(_alert())
        assert "token" not in fake.connect_calls[0]
    finally:
        channel.close()


def test_nats_channel_returns_false_on_connect_error(monkeypatch):
    fake = _FakeNatsModule(connect_error=ConnectionRefusedError("nope"))
    _install_fake_nats(monkeypatch, fake)
    channel = NatsAlertChannel("nats://localhost:4222")
    try:
        # MUST NOT raise; returns False and the detector loop carries on.
        assert channel.send(_alert()) is False
    finally:
        channel.close()


def test_nats_channel_returns_false_on_publish_error(monkeypatch):
    client = _FakeNatsClient(publish_error=RuntimeError("broker hung up"))
    fake = _FakeNatsModule(client=client)
    _install_fake_nats(monkeypatch, fake)
    channel = NatsAlertChannel("nats://localhost:4222")
    try:
        assert channel.send(_alert()) is False
        # Cached connection was dropped — second send retries the connect.
        assert channel._nc is None
    finally:
        channel.close()


def test_nats_channel_uses_custom_subject_prefix(monkeypatch):
    fake = _FakeNatsModule()
    _install_fake_nats(monkeypatch, fake)
    channel = NatsAlertChannel(
        "nats://localhost:4222",
        subject_prefix="org.example.alerts",
    )
    try:
        channel.send(_alert())
        subject, _ = fake.client.published[0]
        assert subject == "org.example.alerts.app.loitering-detection.cam-front"
    finally:
        channel.close()


def test_build_dispatcher_includes_nats_when_url_set():
    dispatcher = build_dispatcher(
        webhook_url=None,
        nats_alerts_url="nats://localhost:4222",
    )
    names = {
        getattr(ch, "name", ch.__class__.__name__) for ch in dispatcher._channels
    }
    assert "stdout" in names
    assert "nats" in names
    assert "webhook" not in names  # not configured this call


def test_build_dispatcher_skips_nats_when_url_blank():
    dispatcher = build_dispatcher(
        webhook_url=None,
        nats_alerts_url=None,
    )
    names = {
        getattr(ch, "name", ch.__class__.__name__) for ch in dispatcher._channels
    }
    assert names == {"stdout"}


def test_build_dispatcher_orders_stdout_first_then_webhook_then_nats():
    dispatcher = build_dispatcher(
        webhook_url="https://example.invalid/hook",
        nats_alerts_url="nats://localhost:4222",
    )
    names = [
        getattr(ch, "name", ch.__class__.__name__) for ch in dispatcher._channels
    ]
    assert names == ["stdout", "webhook", "nats"]


# ── Punch-list regression tests ────────────────────────────────────


def test_nats_channel_rejects_empty_subject_prefix():
    """Peer-review L6: an empty (or all-dots) prefix is operator
    misconfiguration; fail at construction, not silently at publish."""
    with pytest.raises(ValueError, match="subject_prefix"):
        NatsAlertChannel("nats://localhost:4222", subject_prefix="")
    with pytest.raises(ValueError, match="subject_prefix"):
        NatsAlertChannel("nats://localhost:4222", subject_prefix="...")


def test_nats_channel_rejects_subject_prefix_with_invalid_chars():
    """Peer-review L6: spaces / wildcards / other NATS-invalid chars
    in the prefix would cause every publish to ErrBadSubject. Fail
    at construction so operators see it immediately."""
    for bad in ("open nvr.alerts", "opennvr.alerts.*", "opennvr.alerts.>"):
        with pytest.raises(ValueError, match="subject_prefix"):
            NatsAlertChannel("nats://localhost:4222", subject_prefix=bad)


def test_nats_channel_accepts_multi_token_prefix():
    """Multi-token prefixes are legal — operators may want to nest
    OpenNVR alerts under an org-specific top-level subject."""
    # Should not raise.
    ch = NatsAlertChannel(
        "nats://localhost:4222", subject_prefix="org.example.opennvr.alerts",
    )
    assert ch._subject_prefix == "org.example.opennvr.alerts"


def test_dispatcher_close_calls_channel_close(monkeypatch):
    """Peer-review H2: AlertDispatcher.close must propagate to any
    channel that has its own close (in practice: NatsAlertChannel)."""
    closed = {"n": 0}

    class _ClosableChannel:
        name = "closable"

        def send(self, alert):
            return True

        def close(self):
            closed["n"] += 1

    dispatcher = AlertDispatcher([StdoutChannel(), _ClosableChannel()])
    dispatcher.close()
    assert closed["n"] == 1


def test_dispatcher_close_isolates_close_failures(capsys):
    """One channel's close raising must not stop subsequent channels
    from being closed."""
    closed_b = {"flag": False}

    class _BadClose:
        name = "bad"

        def send(self, alert):
            return True

        def close(self):
            raise RuntimeError("close kaboom")

    class _GoodClose:
        name = "good"

        def send(self, alert):
            return True

        def close(self):
            closed_b["flag"] = True

    dispatcher = AlertDispatcher([_BadClose(), _GoodClose()])
    # Must not raise.
    dispatcher.close()
    assert closed_b["flag"] is True


def test_nats_channel_close_is_reinit_safe(monkeypatch):
    """Peer-review M1: after close(), the channel must reset state
    so a subsequent send() spins a fresh loop + connection rather
    than scheduling on a stopped loop and hanging to timeout."""
    fake = _FakeNatsModule()
    _install_fake_nats(monkeypatch, fake)
    channel = NatsAlertChannel("nats://localhost:4222")
    assert channel.send(_alert()) is True
    assert channel._loop is not None
    channel.close()
    # Internal state must be reset.
    assert channel._loop is None
    assert channel._thread is None
    assert channel._nc is None
    # And a second send works — re-spawns the thread, re-connects.
    assert channel.send(_alert()) is True
    assert len(fake.client.published) == 2
    channel.close()


# ── SDK-only: configurable default source ──────────────────────────


def test_set_default_source_changes_new_alerts_only():
    """The process-wide default stamps NEW AlertSource instances;
    explicitly-constructed sources are untouched."""
    from opennvr_app_sdk.alerts import set_default_source

    set_default_source(kind="app", name="my-cool-app", version="2.3.4")
    fresh = Alert(title="t", description="d", camera_id="c")
    assert fresh.source.name == "my-cool-app"
    assert fresh.source.version == "2.3.4"
    explicit = Alert(
        title="t", description="d", camera_id="c",
        source=AlertSource(kind="adapter", name="yolov8", version="9"),
    )
    assert explicit.source.name == "yolov8"

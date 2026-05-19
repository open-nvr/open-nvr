# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert dispatch tests — stdout + webhook + failure isolation."""
from __future__ import annotations

import json

import httpx
import pytest

from alerts import (
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
    assert alert.source.name == "intrusion-detection"


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

    monkeypatch.setattr("alerts.httpx.post", _patched)
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

    monkeypatch.setattr("alerts.httpx.post", _patched)
    channel = WebhookChannel("https://example.invalid/hook")
    assert not channel.send(_alert())  # but no raise


def test_webhook_channel_swallows_transport_exception(monkeypatch):
    def _raises(*args, **kwargs):
        raise RuntimeError("DNS lookup failed")
    monkeypatch.setattr("alerts.httpx.post", _raises)
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

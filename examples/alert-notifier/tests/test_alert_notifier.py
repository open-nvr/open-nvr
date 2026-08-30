# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AlertNotifier — severity gate, flood guards, delivery channels."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import alert_notifier as an
from alert_notifier import AlertNotifier, AppConfig, load_config, severity_rank


def _config(**overrides) -> AppConfig:
    base = AppConfig(nats_url="nats://test:4222",
                     telegram_bot_token="TOK", telegram_chat_id="42")
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _notifier(**overrides) -> AlertNotifier:
    return AlertNotifier(_config(**overrides))


def _alert(severity="high", *, title="Unknown vehicle XX99ZZ0001",
           camera="cam1", wrap_envelope=True):
    body = {
        "alert_id": "al_1", "fired_at": "2026-08-30T10:00:00+00:00",
        "title": title, "description": "desc", "severity": severity,
        "camera_id": camera, "correlation_id": "c1",
        "source": {"kind": "app", "name": "license-plate-recognition"},
        "evidence": {}, "tags": [],
    }
    if not wrap_envelope:
        return body
    return {"id": "evt_1", "schema": "alert.fired.v1", "camera_id": camera,
            "ts": body["fired_at"], "producer": "app:lpr",
            "correlation_id": "c1", "payload": body}


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


@pytest.fixture()
def posts(monkeypatch):
    calls = []
    monkeypatch.setattr(
        an.httpx, "post",
        lambda url, json=None, timeout=None: calls.append(
            {"url": url, "json": json}) or _Resp())
    return calls


def test_high_alert_reaches_telegram(posts):
    n = _notifier()
    n.on_alert(_alert(), "opennvr.events.alert.fired.v1.cam1")
    assert len(posts) == 1
    assert posts[0]["url"] == "https://api.telegram.org/botTOK/sendMessage"
    assert posts[0]["json"]["chat_id"] == "42"
    assert "[HIGH] Unknown vehicle XX99ZZ0001" in posts[0]["json"]["text"]
    assert "camera cam1" in posts[0]["json"]["text"]
    assert n.state_snapshot()["forwarded_total"] == 1


def test_plain_alert_shape_also_accepted(posts):
    """Legacy plumbing subjects deliver the bare §11.5 dict."""
    n = _notifier()
    n.on_alert(_alert(wrap_envelope=False), "opennvr.alerts.app.lpr.cam1")
    assert len(posts) == 1


def test_below_the_bar_is_suppressed(posts):
    n = _notifier(min_severity="high")
    n.on_alert(_alert("low"), "s")
    n.on_alert(_alert("medium"), "s")
    assert posts == []
    assert n.state_snapshot()["suppressed_total"] == 2


def test_severity_rank_tolerates_junk():
    assert severity_rank("HIGH") == 3
    assert severity_rank(None) == 1
    assert severity_rank("apocalyptic") == 1


def test_repeat_cooldown_one_alarm_one_push(posts, monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["now"])
    n = _notifier(repeat_cooldown_seconds=300.0)
    n.on_alert(_alert(), "s")
    t["now"] += 30.0
    n.on_alert(_alert(), "s")                       # same alarm — quiet
    n.on_alert(_alert(title="Barrier FAULT"), "s")  # different alarm — push
    assert len(posts) == 2
    t["now"] += 400.0
    n.on_alert(_alert(), "s")                       # window over — push
    assert len(posts) == 3


def test_per_minute_ceiling(posts, monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["now"])
    n = _notifier(max_per_minute=2, repeat_cooldown_seconds=0)
    for i in range(4):
        n.on_alert(_alert(title=f"alarm {i}"), "s")
    assert len(posts) == 2
    snap = n.state_snapshot()
    assert snap["forwarded_total"] == 2 and snap["suppressed_total"] == 2
    t["now"] += 61.0
    n.on_alert(_alert(title="later"), "s")
    assert len(posts) == 3


def test_delivery_failure_counted_and_retriable(monkeypatch):
    def boom(url, json=None, timeout=None):
        raise an.httpx.ConnectError("no route")
    monkeypatch.setattr(an.httpx, "post", boom)
    t = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["now"])
    n = _notifier(repeat_cooldown_seconds=300.0)
    n.on_alert(_alert(), "s")
    assert n.state_snapshot()["failure_total"] == 1
    # Failure must NOT start the cooldown — the next firing retries.
    calls = []
    monkeypatch.setattr(an.httpx, "post",
                        lambda url, json=None, timeout=None: calls.append(url) or _Resp())
    t["now"] += 1.0
    n.on_alert(_alert(), "s")
    assert calls and n.state_snapshot()["forwarded_total"] == 1


def test_no_channel_configured_counts_as_failure(posts):
    n = _notifier(telegram_bot_token="", telegram_chat_id="",
                  notify_webhook_url="")
    n.on_alert(_alert(), "s")
    assert posts == []
    assert n.state_snapshot()["failure_total"] == 1


def test_webhook_channel_gets_message_and_alert(posts):
    n = _notifier(telegram_bot_token="", telegram_chat_id="",
                  notify_webhook_url="http://gw/sms")
    n.on_alert(_alert(), "s")
    assert posts[0]["url"] == "http://gw/sms"
    assert "message" in posts[0]["json"] and "alert" in posts[0]["json"]
    assert posts[0]["json"]["alert"]["severity"] == "high"


def test_both_channels_must_succeed(monkeypatch):
    """Telegram ok + webhook down = failure (partial delivery visible)."""
    def post(url, json=None, timeout=None):
        if "telegram" in url:
            return _Resp()
        return _Resp(500)
    monkeypatch.setattr(an.httpx, "post", post)
    n = _notifier(notify_webhook_url="http://gw/sms")
    n.on_alert(_alert(), "s")
    assert n.state_snapshot()["failure_total"] == 1


def test_live_config_update(posts):
    n = _notifier(min_severity="critical")
    n.on_alert(_alert("high"), "s")
    assert posts == []
    n.on_config_update({"min_severity": "low",
                        "telegram_bot_token": "TOK2",
                        "telegram_chat_id": "99"})
    n.on_alert(_alert("high", title="after update"), "s")
    assert posts[-1]["url"] == "https://api.telegram.org/botTOK2/sendMessage"
    assert posts[-1]["json"]["chat_id"] == "99"


def test_manifest_declares_the_contract():
    m = AlertNotifier.manifest
    assert m.subscribes == "opennvr.events.alert.fired.v1.>"
    assert m.requires_tasks == []
    assert "events:alert.fired" in m.requires_scopes


def test_load_config_requires_nats_url(tmp_path: Path):
    p = tmp_path / "c.yml"
    p.write_text("min_severity: high\n")
    with pytest.raises(ValueError, match="nats_url"):
        load_config(p)


def test_load_config_parses(tmp_path: Path):
    p = tmp_path / "c.yml"
    p.write_text(
        "nats_url: nats://x:4222\n"
        "min_severity: medium\n"
        "telegram_bot_token: T\n"
        "telegram_chat_id: '7'\n"
        "notify_webhook_url: http://gw\n"
        "max_per_minute: 5\n")
    cfg = load_config(p)
    assert cfg.min_severity == "medium"
    assert cfg.telegram_chat_id == "7"
    assert cfg.max_per_minute == 5

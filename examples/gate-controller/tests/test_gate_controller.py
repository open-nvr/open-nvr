# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GateController — decisions in, relay pulses out, fail closed."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gate_controller as gc
from gate_controller import AppConfig, GateController, load_config, parse_relays


def _config(**overrides) -> AppConfig:
    base = AppConfig(nats_url="nats://test:4222",
                     relays={"1": "http://relay/open"})
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _controller(**overrides) -> tuple[GateController, MagicMock]:
    dispatcher = MagicMock()
    return GateController(_config(**overrides), dispatcher), dispatcher


def _decision(decision="allow", *, camera="cam1", plate="MH12DE1433",
              reason="registered", schema="access.decided.v1", **extra):
    env = {
        "id": "evt_0123456789ab",
        "schema": schema,
        "correlation_id": "corr-1",
        "camera_id": camera,
        "ts": "2026-08-30T10:00:00+00:00",
        "producer": "app:license-plate-recognition",
        "payload": {"plate_text": plate, "decision": decision,
                    "reason": reason, "owner": "A. Sharma", "unit": "B-402",
                    "confidence": 0.9},
    }
    env.update(extra)
    return env


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


def test_allow_pulses_relay_and_alerts_low(monkeypatch):
    calls = []
    monkeypatch.setattr(gc.httpx, "get",
                        lambda url, timeout: calls.append(url) or _Resp())
    ctl, dispatcher = _controller()
    fired = ctl.handle_event(_decision())
    assert calls == ["http://relay/open"]
    assert fired[0].severity == "low"
    assert "Barrier opened" in fired[0].title
    assert dispatcher.fire.call_count == 1
    assert ctl.state_snapshot()["opened_total"] == 1


def test_deny_and_unknown_decisions_fail_closed(monkeypatch):
    monkeypatch.setattr(gc.httpx, "get",
                        lambda *a, **k: pytest.fail("relay must not be called"))
    ctl, dispatcher = _controller()
    assert ctl.handle_event(_decision("deny")) == []
    # A decision value invented by a future producer: still closed.
    assert ctl.handle_event(_decision("allow_with_escort")) == []
    assert dispatcher.fire.call_count == 0
    assert ctl.state_snapshot()["denied_total"] == 2


def test_cooldown_one_car_one_pulse(monkeypatch):
    calls = []
    monkeypatch.setattr(gc.httpx, "get",
                        lambda url, timeout: calls.append(url) or _Resp())
    t = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["now"])
    ctl, _ = _controller(pulse_cooldown_seconds=5.0)
    ctl.handle_event(_decision())
    t["now"] += 2.0
    ctl.handle_event(_decision(plate="KA05MJ6021"))  # boom already up
    assert len(calls) == 1
    t["now"] += 10.0
    ctl.handle_event(_decision(plate="TS09EA7788"))
    assert len(calls) == 2


def test_relay_fault_alerts_high_and_is_retriable(monkeypatch):
    def boom(url, timeout):
        raise gc.httpx.ConnectError("relay unreachable")
    monkeypatch.setattr(gc.httpx, "get", boom)
    t = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["now"])
    ctl, _ = _controller(pulse_cooldown_seconds=5.0)
    fired = ctl.handle_event(_decision())
    assert fired[0].severity == "high"
    assert "FAULT" in fired[0].title
    # A failed pulse must NOT start the cooldown — the next decision
    # retries immediately.
    ok_calls = []
    monkeypatch.setattr(gc.httpx, "get",
                        lambda url, timeout: ok_calls.append(url) or _Resp())
    t["now"] += 1.0
    fired2 = ctl.handle_event(_decision())
    assert ok_calls and fired2[0].severity == "low"
    snap = ctl.state_snapshot()
    assert snap["fault_total"] == 1 and snap["opened_total"] == 1


def test_dry_run_alerts_without_calling_relay(monkeypatch):
    monkeypatch.setattr(gc.httpx, "get",
                        lambda *a, **k: pytest.fail("dry run must not call"))
    ctl, _ = _controller(dry_run=True)
    fired = ctl.handle_event(_decision())
    assert fired[0].severity == "low"
    assert "[dry run]" in fired[0].title
    assert fired[0].evidence["dry_run"] is True


def test_allow_at_unwired_camera_is_quiet(monkeypatch):
    monkeypatch.setattr(gc.httpx, "get",
                        lambda *a, **k: pytest.fail("no relay for cam9"))
    ctl, dispatcher = _controller()
    assert ctl.handle_event(_decision(camera="cam9")) == []
    assert dispatcher.fire.call_count == 0


def test_non_2xx_relay_answer_is_a_fault(monkeypatch):
    monkeypatch.setattr(gc.httpx, "get", lambda url, timeout: _Resp(503))
    ctl, _ = _controller()
    fired = ctl.handle_event(_decision())
    assert fired[0].severity == "high"


def test_post_method_supported(monkeypatch):
    posts = []
    monkeypatch.setattr(gc.httpx, "post",
                        lambda url, timeout: posts.append(url) or _Resp())
    ctl, _ = _controller(relays={"cam1": {"url": "http://r/o", "method": "post"}})
    ctl.handle_event(_decision())
    assert posts == ["http://r/o"]


def test_foreign_and_malformed_events_ignored():
    ctl, dispatcher = _controller()
    assert ctl.handle_event({"schema": "plate.recognized.v1"}) == []
    assert ctl.handle_event("not a dict") == []
    assert ctl.handle_event({"schema": "access.decided.v1"}) == []  # no camera
    assert dispatcher.fire.call_count == 0


def test_parse_relays_normalises_and_skips_bad():
    got = parse_relays({
        "1": "http://a/open",
        "cam2": {"url": "http://b/open", "method": "POST"},
        "3": {"method": "GET"},          # no url — skipped
        "4": 42,                          # junk — skipped
        "": "http://never",               # no key — skipped
        "5": {"url": "http://c", "method": "DELETE"},  # bad method → GET
    })
    assert got == {
        "cam1": {"url": "http://a/open", "method": "GET"},
        "cam2": {"url": "http://b/open", "method": "POST"},
        "cam5": {"url": "http://c", "method": "GET"},
    }


def test_live_config_update():
    ctl, _ = _controller()
    ctl.on_config_update({"relays": {"7": "http://new/open"},
                          "dry_run": True, "pulse_cooldown_seconds": 1})
    assert ctl._relays == {"cam7": {"url": "http://new/open", "method": "GET"}}
    assert ctl._dry_run is True and ctl._cooldown == 1.0


def test_manifest_declares_the_contract():
    m = GateController.manifest
    assert m.subscribes == "opennvr.events.access.decided.v1.>"
    assert m.requires_tasks == []
    assert "events:access.decided" in m.requires_scopes
    assert {p.name for p in m.params} >= {"relays", "pulse_cooldown_seconds", "dry_run"}


def test_load_config_requires_nats_url(tmp_path: Path):
    p = tmp_path / "c.yml"
    p.write_text("relays: {}\n")
    with pytest.raises(ValueError, match="nats_url"):
        load_config(p)


def test_load_config_parses(tmp_path: Path):
    p = tmp_path / "c.yml"
    p.write_text(
        "nats_url: nats://x:4222\n"
        "relays:\n  '1': http://r/open\n"
        "pulse_cooldown_seconds: 7\n"
        "dry_run: true\n")
    cfg = load_config(p)
    assert cfg.relays == {"1": "http://r/open"}
    assert cfg.pulse_cooldown_seconds == 7.0
    assert cfg.dry_run is True

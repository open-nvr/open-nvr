# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The apps-bus leaf link: rendered correctly by the nats-apps entrypoint,
and watched by core so a dead link is loud instead of "alerts stopped"."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_app_credentials import (  # noqa: F401 — fixture re-export
    _manifest, _site, env,
)

import core.auth as auth_mod  # noqa: E402
from models import AppAlert as AppAlertRow  # noqa: E402
from services import apps_bus_watch as w  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "nats" / "apps-entrypoint.sh"
TEMPLATE = REPO / "nats" / "apps.conf"


@pytest.fixture(autouse=True)
def _fresh():
    w.reset_for_tests()
    yield
    w.reset_for_tests()


# ── the entrypoint renders the leaf password into the URL ──────────────


def _config_lines(text: str) -> str:
    """The template's comments explain the bug and quote the literal; only
    the lines nats-server acts on count."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def _render(key: str) -> str:
    env_ = {**os.environ, "INTERNAL_API_KEY": key, "APPS_CONF_TEMPLATE": str(TEMPLATE)}
    return subprocess.run(["sh", str(ENTRYPOINT), "--render"], env=env_,
                          capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")
def test_template_has_no_dollar_variable_in_the_leaf_url():
    # nats-server does not expand $VAR inside a URL — that is the bug
    # this file guards against. The template carries a placeholder only.
    live = _config_lines(TEMPLATE.read_text())
    assert "$INTERNAL_API_KEY" not in live
    assert "@@INTERNAL_API_KEY_URL@@" in live


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")
def test_render_puts_the_hex_key_in_the_leaf_url():
    out = _render("c0ffee1234deadbeef")
    assert 'url: "nats://opennvr-apps-bus:c0ffee1234deadbeef@nats:7422"' in out
    live = _config_lines(out)
    assert "@@INTERNAL_API_KEY_URL@@" not in live and "$INTERNAL_API_KEY" not in live
    assert 'include "/var/lib/opennvr/nats/users.conf"' in out


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")
def test_render_percent_encodes_a_base64_key():
    # `openssl rand -base64 32` (the .env.example advice) yields '/', '+', '='
    out = _render("l/3WTLMGLPazDFuLBjajSFHehrj0I3e25wcQt4amHGA=")
    assert "opennvr-apps-bus:l%2F3WTLMGLPazDFuLBjajSFHehrj0I3e25wcQt4amHGA%3D@nats:7422" in out


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")
def test_render_encodes_every_url_special_character():
    out = _render("a@b:c#d?e&f|g h%i")
    assert "opennvr-apps-bus:a%40b%3Ac%23d%3Fe%26f%7Cg%20h%25i@nats:7422" in out


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")
def test_render_refuses_without_a_key():
    env_ = {k: v for k, v in os.environ.items() if k != "INTERNAL_API_KEY"}
    env_["APPS_CONF_TEMPLATE"] = str(TEMPLATE)
    r = subprocess.run(["sh", str(ENTRYPOINT), "--render"], env=env_, capture_output=True, text=True)
    assert r.returncode == 1 and "INTERNAL_API_KEY is not set" in r.stderr


# ── the watchdog ───────────────────────────────────────────────────────


def test_monitor_url_is_derived_from_the_apps_bus_url():
    assert w.monitor_url("nats://nats-apps:4222") == "http://nats-apps:8222/leafz"
    assert w.monitor_url("nats://10.0.0.5:4222", "http://mon:9999") == "http://mon:9999/leafz"
    assert w.monitor_url("") == ""


def test_parse_leafz():
    assert w.parse_leafz({"leafs": []}) == {"linked": False, "leafs": 0, "remotes": []}
    got = w.parse_leafz({"leafs": [{"name": "opennvr-nats", "is_spoke": True}]})
    assert got == {"linked": True, "leafs": 1, "remotes": ["opennvr-nats"]}
    assert w.parse_leafz("junk")["linked"] is False


def test_unlinked_counts_only_after_the_grace_period_and_alerts_hourly():
    t0 = 1_000_000.0
    assert w.observe({"linked": False, "remotes": []}, now=t0) == {"transition": None, "alert": False}
    assert w.observe({"linked": False, "remotes": []}, now=t0 + 60) == {"transition": None, "alert": False}
    lost = w.observe({"linked": False, "remotes": []}, now=t0 + w.GRACE_S)
    assert lost == {"transition": "lost", "alert": True}
    # while it lasts: no re-alert inside the hour, one after
    assert w.observe({"linked": False, "remotes": []}, now=t0 + w.GRACE_S + 600) == {"transition": None, "alert": False}
    assert w.observe({"linked": False, "remotes": []}, now=t0 + w.GRACE_S + w.ALERT_EVERY_S) == {"transition": None, "alert": True}
    snap = w.state()
    assert snap["linked"] is False and snap["unlinked_for_s"] > 0
    # recovery
    back = w.observe({"linked": True, "remotes": ["opennvr-nats"]}, now=t0 + w.GRACE_S + 4000)
    assert back == {"transition": "restored", "alert": False}
    assert w.state()["linked"] is True and w.state()["remotes"] == ["opennvr-nats"]


def test_unreachable_monitor_is_not_treated_as_unlinked():
    t0 = 2_000_000.0
    for i in range(10):
        assert w.observe({"error": "ConnectError: boom"}, now=t0 + i * 60) == {"transition": None, "alert": False}
    snap = w.state()
    assert snap["reachable"] is False and snap["linked"] is None and "boom" in snap["error"]


def test_check_once_writes_the_inbox_alert(monkeypatch, env):
    tc, _ids, SessionLocal = env
    t0 = 3_000_000.0
    monkeypatch.setattr(w, "fetch_leafz", lambda url, timeout=3.0: {"linked": False, "leafs": 0, "remotes": []})
    clock = {"now": t0}
    monkeypatch.setattr(w.time, "time", lambda: clock["now"])
    s = SessionLocal()
    try:
        assert w.check_once("http://nats-apps:8222/leafz", db=s)["alert"] is False
        clock["now"] = t0 + w.GRACE_S
        assert w.check_once("http://nats-apps:8222/leafz", db=s)["alert"] is True
        s.commit()
        rows = s.query(AppAlertRow).filter(AppAlertRow.alert_id.like("apps-bus-unlinked-%")).all()
        assert len(rows) == 1
        assert "not linked to the platform bus" in rows[0].title
        assert rows[0].severity == "high"
    finally:
        s.close()


def test_bus_endpoint_reports_the_link(monkeypatch, env):
    tc, _ids, _ = env
    from core.config import settings

    monkeypatch.setattr(settings, "nats_apps_url", "nats://nats-apps:4222", raising=False)
    tc.app.dependency_overrides[auth_mod.get_current_active_user] = \
        tc.app.dependency_overrides[auth_mod.get_current_superuser]
    w.observe({"linked": True, "remotes": ["opennvr-nats"]})
    r = tc.get("/apps/bus")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True and body["monitor_url"] == "http://nats-apps:8222/leafz"
    assert body["linked"] is True and body["remotes"] == ["opennvr-nats"]

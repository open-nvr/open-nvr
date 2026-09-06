# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Egress policy (services/app_egress.py) and its routes.

Apps sit on an internal network; the egress proxy asks core per
connection. Core answers from the listing's ``network_egress`` plus the
operator's allow list, identifies the app by the address its contract
URL resolves to, and turns a refusal into a log line, a counter and —
once an hour per destination — an inbox alert.

Run with:
    cd server && pytest tests/test_app_egress.py -v
"""
from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

from types import SimpleNamespace

import pytest

from tests.test_app_credentials import (  # noqa: F401 — fixture re-export
    SITE_KEY, _manifest, _site, env,
)

from models import AppAlert as AppAlertRow  # noqa: E402
from services import app_egress as eg  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    eg.clear_denials()
    eg.forget_resolution()
    yield
    eg.clear_denials()
    eg.forget_resolution()


# ── rules ───────────────────────────────────────────────────────────────


def test_rule_grammar_hosts_wildcards_ips_cidrs_ports():
    ok = {
        "api.telegram.org": ("api.telegram.org", None),
        "API.Telegram.org ": ("api.telegram.org", None),
        "*.ntfy.sh": ("*.ntfy.sh", None),
        "192.168.1.50": ("192.168.1.50", None),
        "192.168.1.0/24": ("192.168.1.0/24", None),
        "homeassistant.local:8123": ("homeassistant.local", 8123),
        "10.0.0.7:80": ("10.0.0.7", 80),
    }
    for text, (host, port) in ok.items():
        rule = eg.parse_rule(text)
        assert rule is not None and (rule.host, rule.port) == (host, port), text
    for bad in ["", "the webhook URL the operator configures", "https://x.y/z",
                "x.y/path", "999.1.1.1", "10.0.0.0/99", "a b", "host:0",
                "host:70000", "-bad.example", None, 42, "x" * 300]:
        assert eg.parse_rule(bad) is None, bad


def test_rule_matching():
    r = eg.parse_rule
    assert r("api.telegram.org").matches("API.telegram.org.", 443)
    assert not r("api.telegram.org").matches("evil-api.telegram.org", 443)
    assert r("*.ntfy.sh").matches("push.ntfy.sh", 443)
    assert not r("*.ntfy.sh").matches("ntfy.sh", 443)          # the apex is not a subdomain
    assert r("192.168.1.0/24").matches("192.168.1.77", 80)
    assert not r("192.168.1.0/24").matches("192.168.2.1", 80)
    assert not r("192.168.1.0/24").matches("home.local", 80)
    assert r("ha.local:8123").matches("ha.local", 8123)
    assert not r("ha.local:8123").matches("ha.local", 443)


def test_operator_rules_are_validated_and_normalised():
    assert eg.validate_operator_rules(None) == []
    assert eg.validate_operator_rules(["HA.local:8123", "ha.local:8123", "10.0.0.5"]) == \
        ["ha.local:8123", "10.0.0.5"]
    with pytest.raises(ValueError, match="not a host rule"):
        eg.validate_operator_rules(["http://ha.local:8123/api"])
    with pytest.raises(ValueError, match="list"):
        eg.validate_operator_rules("ha.local")
    with pytest.raises(ValueError, match="at most"):
        eg.validate_operator_rules([f"h{i}.example" for i in range(eg.MAX_RULES + 1)])


def test_listing_prose_is_a_note_not_a_rule():
    row = SimpleNamespace(id="alert-notifier", egress_allow=["ntfy.example:80"])
    rules = eg.rules_for(row, declared=["api.telegram.org",
                                        "the webhook URL the operator configures"])
    assert [(r.text(), r.source) for r in rules] == [
        ("api.telegram.org", "listing"), ("ntfy.example:80", "operator"),
    ]
    assert eg.match(rules, "api.telegram.org", 443).source == "listing"
    assert eg.match(rules, "ntfy.example", 80).source == "operator"
    assert eg.match(rules, "ntfy.example", 443) is None


# ── identity by address ─────────────────────────────────────────────────


def test_app_for_address_matches_the_contract_host(monkeypatch):
    monkeypatch.setattr(eg, "_resolve", lambda host: frozenset(
        {"loitering": {"172.29.0.5"}, "notifier": {"172.29.0.9", "172.28.0.9"}}.get(host, set())))
    rows = [SimpleNamespace(id="loitering-detection", url="http://loitering:9200"),
            SimpleNamespace(id="alert-notifier", url="http://notifier:9211"),
            SimpleNamespace(id="broken", url=None)]
    assert eg.app_for_address(rows, "172.29.0.5").id == "loitering-detection"
    assert eg.app_for_address(rows, "172.28.0.9").id == "alert-notifier"
    assert eg.app_for_address(rows, "172.29.0.99") is None
    assert eg.app_for_address(rows, "not-an-ip") is None


def test_resolution_is_cached_and_literal_ips_pass_through(monkeypatch):
    calls = []

    def fake_getaddrinfo(host, port):
        calls.append(host)
        return [(None, None, None, None, ("10.1.1.1", 0))]

    monkeypatch.setattr(eg.socket, "getaddrinfo", fake_getaddrinfo)
    assert eg._resolve("app-host") == {"10.1.1.1"}
    assert eg._resolve("app-host") == {"10.1.1.1"}
    assert calls == ["app-host"]
    assert eg._resolve("172.29.0.4") == {"172.29.0.4"} and calls == ["app-host"]


# ── the decision, end to end ────────────────────────────────────────────


def _install(tc, app_id="alert-notifier", url="http://notifier:9211"):
    body = {"url": url, "manifest": _manifest(app_id=app_id, provides=())}
    r = tc.post("/apps/register", json=body, headers=_site())
    assert r.status_code == 200, r.text
    return r.json()


def test_check_allows_declared_hosts_and_denies_the_rest(env, monkeypatch):
    tc, _ids, SessionLocal = env
    _install(tc)
    monkeypatch.setattr(eg, "_resolve", lambda host: frozenset({"172.29.0.9"}) if host == "notifier" else frozenset())
    monkeypatch.setattr(eg, "listing_egress", lambda app_id: ["api.telegram.org"] if app_id == "alert-notifier" else [])

    def ask(host, port=443, ip="172.29.0.9"):
        r = tc.post("/apps/egress/check", json={"client_ip": ip, "host": host, "port": port},
                    headers=_site())
        assert r.status_code == 200, r.text
        return r.json()

    assert ask("api.telegram.org") == {"allowed": True, "app_id": "alert-notifier",
                                       "reason": "listing: api.telegram.org"}
    denied = ask("evil.example")
    assert denied["allowed"] is False and denied["app_id"] == "alert-notifier"
    # Unknown client: refused, attributed to nobody.
    assert ask("api.telegram.org", ip="172.29.0.250") == {
        "allowed": False, "app_id": None, "reason": "unknown client"}
    # The site key is the only credential the check accepts.
    assert tc.post("/apps/egress/check", json={"client_ip": "1.2.3.4", "host": "x", "port": 1},
                   headers={"X-Internal-Api-Key": "nope"}).status_code == 401
    assert tc.post("/apps/egress/check", json={"client_ip": "1.2.3.4", "host": "x", "port": 1}).status_code == 401

    # The denial is remembered on the app, and raised ONCE in the inbox.
    ask("evil.example")
    ask("evil.example", port=80)
    view = tc.get("/apps/alert-notifier/egress", headers=_site()).json()
    assert view["declared"] == ["api.telegram.org"] and view["enforced"] == ["api.telegram.org"]
    assert [(d["host"], d["port"], d["count"]) for d in view["denied"]] == [
        ("evil.example", 80, 1), ("evil.example", 443, 2)]
    s = SessionLocal()
    try:
        alerts = s.query(AppAlertRow).filter(AppAlertRow.alert_id.like("egress-%")).all()
        titles = sorted(a.title for a in alerts)
    finally:
        s.close()
    assert titles == ["Alert-Notifier tried to reach evil.example:443",
                      "Alert-Notifier tried to reach evil.example:80"]
    assert all(a.severity == "medium" and a.source_name == "egress-proxy" for a in alerts)
    # GET /apps carries the same view on every card.
    card = next(a for a in tc.get("/apps", headers=_site()).json() if a["id"] == "alert-notifier")
    assert card["egress"]["declared"] == ["api.telegram.org"] and len(card["egress"]["denied"]) == 2


def test_operator_allow_list_opens_a_host_and_clears_its_denials(env, monkeypatch):
    tc, _ids, SessionLocal = env
    _install(tc)
    monkeypatch.setattr(eg, "_resolve", lambda host: frozenset({"172.29.0.9"}))
    monkeypatch.setattr(eg, "listing_egress", lambda app_id: [])

    def ask(host, port):
        return tc.post("/apps/egress/check", json={"client_ip": "172.29.0.9", "host": host,
                                                   "port": port}, headers=_site()).json()

    assert ask("ha.local", 8123)["allowed"] is False
    assert len(tc.get("/apps/alert-notifier/egress", headers=_site()).json()["denied"]) == 1

    r = tc.put("/apps/alert-notifier/egress", json={"allow": ["HA.local:8123", "10.0.0.0/8"]})
    assert r.status_code == 200, r.text
    assert r.json()["allow"] == ["ha.local:8123", "10.0.0.0/8"]
    assert r.json()["denied"] == []                       # cleared: the operator acted
    assert ask("ha.local", 8123) == {"allowed": True, "app_id": "alert-notifier",
                                      "reason": "operator: ha.local:8123"}
    assert ask("10.20.30.40", 80)["allowed"] is True
    assert ask("ha.local", 443)["allowed"] is False       # port-scoped rule

    bad = tc.put("/apps/alert-notifier/egress", json={"allow": ["http://ha.local"]})
    assert bad.status_code == 400 and "not a host rule" in bad.json()["detail"]
    assert tc.put("/apps/nope/egress", json={"allow": []}).status_code == 404
    # Persisted on the row.
    s = SessionLocal()
    try:
        from models import InstalledApp
        assert s.get(InstalledApp, "alert-notifier").egress_allow == ["ha.local:8123", "10.0.0.0/8"]
    finally:
        s.close()


def test_inbox_alert_is_throttled_per_destination(monkeypatch):
    assert eg._remember_denial("a", "h:1") is True
    assert eg._remember_denial("a", "h:1") is False
    assert eg._remember_denial("a", "h:2") is True
    assert eg._remember_denial("b", "h:1") is True
    monkeypatch.setattr(eg, "ALERT_EVERY_S", 0.0)
    assert eg._remember_denial("a", "h:1") is True
    assert eg.denials_view("a")[0]["count"] >= 1
    eg.clear_denials("a")
    assert eg.denials_view("a") == [] and eg.denials_view("b")


def test_denial_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(eg, "MAX_DENIALS_PER_APP", 3)
    for i in range(6):
        eg._remember_denial("a", f"h{i}:1")
    assert len(eg.denials_view("a")) == 3

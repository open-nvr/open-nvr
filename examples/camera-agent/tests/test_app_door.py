# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The app door — read-only orchestration of installed catalog apps.

The OpenNVR Agent discovers container apps it can't import and RELAYS
their state conversationally (list + status), completing "every catalog
app is a conversational skill" for apps outside the bundled rule library.

Read-only by construction: the agent surfaces ``list_apps`` / ``app_status``
tools and an ``apps`` skill (plus one skill entry per installed app), but
NEVER enables / disables / configures an app — those stay operator actions.

Covered here:

* ``AppRegistryClient``: cached (TTL + negative cache), 5s timeout, threads
  X-Internal-Api-Key, never raises, ``apps_cached`` sync view.
* the two tools' outputs (installed apps, one app's status) and their
  graceful messages when the registry is unset / unreachable.
* ``skills_payload`` gains source:"app" entries when apps are installed +
  reachable, and none when the door is unwired / unreachable.
* the read-only boundary: no code path enables/disables/configures an app.
"""
from __future__ import annotations

import asyncio

import httpx

from adapter_clients import AppRegistryClient
from camera_agent import SKILL_TOOLS, AppConfig, CameraAgentRuntime
from context import CameraSpec


# ── fixtures / helpers ─────────────────────────────────────────────────


_APPS_PAYLOAD = [
    {
        "id": "loitering-detection",
        "name": "Loitering Detection",
        "category": "perimeter",
        "enabled": True,
        "manifest": {
            "summary": "Alerts when a watched object dwells in a zone.",
            "category": "perimeter",
            "emits": [{"name": "loitering", "severity": "high"}],
        },
    },
    {
        "id": "occupancy-counting",
        "name": "Occupancy Counting",
        "category": "analytics",
        "enabled": False,           # installed but NOT enabled → skipped
        "manifest": {"summary": "Counts people in a zone.", "emits": []},
    },
]

_STATUS_PAYLOAD = {
    "health": {"ready": True, "uptime_s": 42},
    "state": {"active_tracks": 3, "dwelling": 1},
}


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


class _FakeHttp:
    """Drop-in for the mixin's httpx.AsyncClient: canned per-URL payloads
    (or a raised error). Records calls + last headers."""

    def __init__(self, *, apps=None, status=None, error: Exception | None = None) -> None:
        self._apps = apps
        self._status = status
        self.error = error
        self.calls = 0
        self.last_headers: dict | None = None
        self.last_url: str | None = None

    async def get(self, url: str, headers: dict | None = None) -> _FakeResponse:
        self.calls += 1
        self.last_headers = headers
        self.last_url = url
        if self.error is not None:
            raise self.error
        if url.endswith("/status"):
            return _FakeResponse(self._status)
        return _FakeResponse(self._apps)


def _client(**kw) -> AppRegistryClient:
    return AppRegistryClient(base_url="http://nvr:8000/", api_key="sekrit", **kw)


def _runtime(*, api_url: str | None = "http://nvr:8000") -> CameraAgentRuntime:
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        opennvr_api_url=api_url,
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front door")],
    )
    return CameraAgentRuntime(cfg)


# ── AppRegistryClient ──────────────────────────────────────────────────


def test_list_apps_fetches_with_internal_key_and_v1_url():
    c = _client()
    c._http = _FakeHttp(apps=_APPS_PAYLOAD)
    apps = asyncio.run(c.list_apps())
    assert [a["id"] for a in apps] == ["loitering-detection", "occupancy-counting"]
    assert c._http.last_headers == {"X-Internal-Api-Key": "sekrit"}
    assert c._http.last_url == "http://nvr:8000/api/v1/apps"
    assert c.apps_cached == apps          # sync view mirrors last fetch


def test_app_status_fetches_status_route():
    c = _client()
    c._http = _FakeHttp(status=_STATUS_PAYLOAD)
    status = asyncio.run(c.app_status("loitering-detection"))
    assert status == _STATUS_PAYLOAD
    assert c._http.last_url == "http://nvr:8000/api/v1/apps/loitering-detection/status"
    assert c._http.last_headers == {"X-Internal-Api-Key": "sekrit"}


def test_no_key_sends_no_header():
    c = AppRegistryClient(base_url="http://nvr:8000", api_key="")
    c._http = _FakeHttp(apps=_APPS_PAYLOAD)
    asyncio.run(c.list_apps())
    assert c._http.last_headers == {}


def test_list_apps_caches_within_ttl():
    c = _client(ttl_seconds=60)
    c._http = _FakeHttp(apps=_APPS_PAYLOAD)

    async def go():
        await c.list_apps()
        await c.list_apps()
        await c.list_apps()

    asyncio.run(go())
    assert c._http.calls == 1              # served from cache after the first


def test_list_apps_refetches_after_ttl():
    c = _client(ttl_seconds=0)
    c._http = _FakeHttp(apps=_APPS_PAYLOAD)

    async def go():
        await c.list_apps()
        await c.list_apps()

    asyncio.run(go())
    assert c._http.calls == 2


def test_app_status_caches_per_app_within_ttl():
    c = _client(ttl_seconds=60)
    c._http = _FakeHttp(status=_STATUS_PAYLOAD)

    async def go():
        await c.app_status("a")
        await c.app_status("a")
        await c.app_status("b")           # different app → its own fetch

    asyncio.run(go())
    assert c._http.calls == 2


def test_list_apps_unreachable_yields_none_and_never_raises():
    c = _client()
    c._http = _FakeHttp(error=httpx.ConnectError("registry down"))
    assert asyncio.run(c.list_apps()) is None
    assert c.apps_cached is None


def test_app_status_unreachable_yields_none_and_never_raises():
    c = _client()
    c._http = _FakeHttp(error=httpx.ConnectError("registry down"))
    assert asyncio.run(c.app_status("x")) is None


def test_list_apps_negative_caches_failures():
    c = _client(ttl_seconds=60)
    c._http = _FakeHttp(error=httpx.ConnectError("registry down"))

    async def go():
        await c.list_apps()
        await c.list_apps()

    asyncio.run(go())
    assert c._http.calls == 1              # down registry retried once per TTL


def test_default_timeout_is_five_seconds():
    assert _client()._timeout == 5.0


# ── list_apps tool ─────────────────────────────────────────────────────


def _stub_registry(rt: CameraAgentRuntime, http: _FakeHttp) -> None:
    """Point the runtime's real AppRegistryClient at a fake transport."""
    rt.app_registry._http = http


def test_list_apps_tool_reports_installed_enabled_apps():
    rt = _runtime()
    _stub_registry(rt, _FakeHttp(apps=_APPS_PAYLOAD))
    out = asyncio.run(rt._handle_list_apps({}))
    assert "Loitering Detection" in out
    assert "perimeter" in out
    assert "dwells in a zone" in out
    assert "loitering" in out             # emitted alert type
    # The disabled app is omitted.
    assert "Occupancy Counting" not in out


def test_list_apps_tool_when_no_apps_enabled():
    rt = _runtime()
    disabled_only = [{"id": "x", "name": "X", "enabled": False, "manifest": {}}]
    _stub_registry(rt, _FakeHttp(apps=disabled_only))
    out = asyncio.run(rt._handle_list_apps({}))
    assert "none are currently enabled" in out


def test_list_apps_tool_when_registry_unreachable_is_graceful():
    rt = _runtime()
    _stub_registry(rt, _FakeHttp(error=httpx.ConnectError("down")))
    out = asyncio.run(rt._handle_list_apps({}))
    assert "couldn't reach the app registry" in out.lower()


def test_list_apps_tool_when_door_unwired_is_graceful():
    rt = _runtime(api_url=None)            # no opennvr_api_url
    assert rt.app_registry is None
    out = asyncio.run(rt._handle_list_apps({}))
    assert "couldn't reach the app registry" in out.lower()


# ── app_status tool ────────────────────────────────────────────────────


def test_app_status_tool_reports_health_and_state():
    rt = _runtime()
    _stub_registry(rt, _FakeHttp(status=_STATUS_PAYLOAD))
    out = asyncio.run(rt._handle_app_status({"app_id": "loitering-detection"}))
    assert "loitering-detection" in out
    assert "ready" in out.lower()
    assert "active_tracks=3" in out


def test_app_status_tool_needs_app_id():
    rt = _runtime()
    _stub_registry(rt, _FakeHttp(status=_STATUS_PAYLOAD))
    out = asyncio.run(rt._handle_app_status({}))
    assert "which app" in out.lower()


def test_app_status_tool_when_unreachable_is_graceful():
    rt = _runtime()
    _stub_registry(rt, _FakeHttp(error=httpx.ConnectError("down")))
    out = asyncio.run(rt._handle_app_status({"app_id": "x"}))
    assert "couldn't reach the app registry" in out.lower()


# ── tool advertisement gating ──────────────────────────────────────────


def test_app_tools_advertised_when_door_wired():
    rt = _runtime()
    names = {t["function"]["name"] for t in rt.tool_definitions}
    assert "list_apps" in names
    assert "app_status" in names


def test_app_tools_absent_when_door_unwired():
    rt = _runtime(api_url=None)
    names = {t["function"]["name"] for t in rt.tool_definitions}
    assert "list_apps" not in names
    assert "app_status" not in names


# ── skills_payload: generic "apps" skill + per-app source:"app" entries ─


def _by_id(rt: CameraAgentRuntime) -> dict[str, dict]:
    return {s["id"]: s for s in rt.skills_payload()}


def test_generic_apps_skill_enabled_when_door_wired():
    rt = _runtime()
    apps = _by_id(rt)["apps"]
    assert apps["enabled"] is True
    assert apps["available"] is True


def test_generic_apps_skill_present_but_disabled_when_unwired():
    rt = _runtime(api_url=None)
    apps = _by_id(rt)["apps"]
    assert apps["enabled"] is False
    assert apps["available"] is False
    assert "opennvr_api_url" in apps["hint"]


def test_skills_payload_includes_app_entries_when_installed_and_reachable():
    rt = _runtime()
    # Prime the cache exactly as the /skills endpoint does.
    _stub_registry(rt, _FakeHttp(apps=_APPS_PAYLOAD))
    asyncio.run(rt.app_registry.list_apps())
    skills = _by_id(rt)
    assert "app:loitering-detection" in skills
    entry = skills["app:loitering-detection"]
    assert entry["source"] == "app"
    assert entry["app_id"] == "loitering-detection"
    assert entry["name"] == "Loitering Detection"
    assert entry["uses"].startswith("installed app — ")
    assert entry["enabled"] is True
    assert entry["read_only"] is False   # ✕ mutes it in the agent; the app keeps running
    assert entry["emits"] == ["loitering"]
    # A disabled installed app is NOT surfaced as a skill.
    assert "app:occupancy-counting" not in skills


def test_no_app_entries_when_registry_unreachable():
    rt = _runtime()
    _stub_registry(rt, _FakeHttp(error=httpx.ConnectError("down")))
    asyncio.run(rt.app_registry.list_apps())        # negative-cached → None
    skills = _by_id(rt)
    assert not [s for s in skills.values() if s.get("source") == "app"]
    # ...but the GENERIC apps capability skill still exists.
    assert "apps" in skills


def test_no_app_entries_when_door_unwired():
    rt = _runtime(api_url=None)
    skills = _by_id(rt)
    assert not [s for s in skills.values() if s.get("source") == "app"]


def test_app_entries_omitted_before_any_fetch():
    # apps_cached is None until the first list_apps() refresh — the panel
    # simply shows no app entries yet (never crashes).
    rt = _runtime()
    skills = _by_id(rt)
    assert not [s for s in skills.values() if s.get("source") == "app"]


# ── the read-only boundary ─────────────────────────────────────────────


def test_apps_skill_maps_only_to_read_tools():
    # The whole slice is read-only: the "apps" skill exposes only query /
    # relay tools (list + status + the alert relay) — no enable/disable/
    # config tool exists.
    assert SKILL_TOOLS["apps"] == [
        "list_apps", "app_status", "recent_app_alerts"
    ]


def test_no_registry_write_tool_or_handler_exists():
    rt = _runtime()
    # No tool advertises an app-mutation verb.
    names = {t["function"]["name"] for t in rt.tool_definitions}
    for forbidden in ("enable_app", "disable_app", "configure_app",
                      "register_app", "update_app_config"):
        assert forbidden not in names
    # No handler is registered for one either.
    for forbidden in ("enable_app", "disable_app", "configure_app"):
        assert forbidden not in rt.tool_handlers
    # The client exposes no write method (only the two reads + sync view).
    assert not hasattr(rt.app_registry, "enable_app")
    assert not hasattr(rt.app_registry, "disable_app")
    assert not hasattr(rt.app_registry, "update_config")

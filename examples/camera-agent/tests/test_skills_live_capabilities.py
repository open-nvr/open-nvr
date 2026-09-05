# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Skills derived live from KAI-C (blueprint "skills as capabilities").

``KaicCapabilitiesClient`` fetches KAI-C's aggregated capabilities
(``GET /api/v1/ai/capabilities``) with a 60s TTL and NEVER raises;
``skills_payload()`` intersects each skill's ``backing_tasks`` with the
live ``tasks_advertised`` union into an advisory ``tasks_available``
flag. ``skill_requirement_met`` stays the enable gate — a briefly
unreachable KAI-C must not disable (or grey out) working tools.
"""
from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from adapter_clients import KaicCapabilitiesClient
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec


def _runtime() -> CameraAgentRuntime:
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front door")],
    )
    return CameraAgentRuntime(cfg)


def _by_id(runtime: CameraAgentRuntime) -> dict[str, dict]:
    return {s["id"]: s for s in runtime.skills_payload()}


class _StubCaps:
    """Stands in for KaicCapabilitiesClient on the runtime: a canned
    ``tasks_advertised`` set (or None = unreachable) + a refresh counter."""

    def __init__(self, tasks: set[str] | None) -> None:
        self.tasks = tasks
        self.refreshes = 0

    @property
    def tasks_advertised(self) -> set[str] | None:
        return self.tasks

    async def refresh(self) -> set[str] | None:
        self.refreshes += 1
        return self.tasks


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    """Drop-in for the mixin's httpx.AsyncClient: canned payload or error."""

    def __init__(self, payload: dict | None = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0
        self.last_headers: dict | None = None

    async def get(self, url: str, headers: dict | None = None) -> _FakeResponse:
        self.calls += 1
        self.last_headers = headers
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload or {})


_CAPS_PAYLOAD = {
    "contract_version": "1",
    "sovereignty_mode": "local_only",
    "adapters": {
        "yolov8": {"tasks_advertised": ["object_detection"]},
        "blip": {"tasks_advertised": ["image_captioning"]},
        "insightface": {"tasks_advertised": ["face_recognition"]},
    },
}


# ── KaicCapabilitiesClient: fetch, auth header, TTL, failure modes ─────


def test_capabilities_client_fetches_union_of_tasks():
    client = KaicCapabilitiesClient(kaic_url="http://k/", api_key="sekrit")
    client._http = _FakeHttp(payload=_CAPS_PAYLOAD)
    tasks = asyncio.run(client.refresh())
    assert tasks == {"object_detection", "image_captioning", "face_recognition"}
    assert client.tasks_advertised == tasks
    # Internal key threaded exactly like the infer path; URL is the v1 route.
    assert client._http.last_headers == {"X-Internal-Api-Key": "sekrit"}
    assert client._url == "http://k/api/v1/ai/capabilities"


def test_capabilities_client_no_key_sends_no_header():
    client = KaicCapabilitiesClient(kaic_url="http://k", api_key="")
    client._http = _FakeHttp(payload=_CAPS_PAYLOAD)
    asyncio.run(client.refresh())
    assert client._http.last_headers == {}


def test_capabilities_client_caches_within_ttl():
    client = KaicCapabilitiesClient(kaic_url="http://k", api_key="x", ttl_seconds=60)
    client._http = _FakeHttp(payload=_CAPS_PAYLOAD)

    async def go():
        await client.refresh()
        await client.refresh()
        await client.refresh()

    asyncio.run(go())
    assert client._http.calls == 1          # served from cache after the first


def test_capabilities_client_refetches_after_ttl():
    client = KaicCapabilitiesClient(kaic_url="http://k", api_key="x", ttl_seconds=0)
    client._http = _FakeHttp(payload=_CAPS_PAYLOAD)

    async def go():
        await client.refresh()
        await client.refresh()

    asyncio.run(go())
    assert client._http.calls == 2


def test_capabilities_client_unreachable_yields_none_and_never_raises():
    client = KaicCapabilitiesClient(kaic_url="http://k", api_key="x")
    client._http = _FakeHttp(error=httpx.ConnectError("kai-c down"))
    tasks = asyncio.run(client.refresh())
    assert tasks is None
    assert client.tasks_advertised is None


def test_capabilities_client_negative_caches_failures():
    # A down KAI-C is retried at most once per TTL — the skills poll must
    # not pay a connect timeout on every request.
    client = KaicCapabilitiesClient(kaic_url="http://k", api_key="x", ttl_seconds=60)
    client._http = _FakeHttp(error=httpx.ConnectError("kai-c down"))

    async def go():
        await client.refresh()
        await client.refresh()

    asyncio.run(go())
    assert client._http.calls == 1
    assert client.tasks_advertised is None


def test_capabilities_client_malformed_payload_yields_none():
    client = KaicCapabilitiesClient(kaic_url="http://k", api_key="x")
    client._http = _FakeHttp(payload={"adapters": {"weird": {"tasks_advertised": None}}})
    assert asyncio.run(client.refresh()) == set()   # tolerated: no tasks listed
    client2 = KaicCapabilitiesClient(kaic_url="http://k", api_key="x")
    client2._http = _FakeHttp(payload={"adapters": "not-a-dict"})
    assert asyncio.run(client2.refresh()) is None   # structurally broken → unknown


# ── skills_payload: per-skill backing_tasks / tasks_available ──────────


def test_skills_payload_all_backing_tasks_available():
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps(
        {"object_detection", "image_captioning", "face_recognition"})
    skills = _by_id(rt)
    # backing_tasks is derived from the bundled tasks.yml and now folds in
    # each canonical task's aliases (so either spelling of a live adapter
    # matches). The canonical name leads each list.
    assert skills["see"]["backing_tasks"][:1] == ["image_captioning"]
    assert "scene_caption" in skills["see"]["backing_tasks"]   # the alias case
    assert "vqa" in skills["see"]["backing_tasks"]
    assert skills["see"]["tasks_available"] is True
    assert skills["count"]["backing_tasks"][0] == "object_detection"
    assert skills["count"]["tasks_available"] is True
    assert skills["faces"]["backing_tasks"] == ["face_recognition"]
    assert skills["faces"]["tasks_available"] is True
    # Converged watch monitors: object detection + their SDK rule library.
    assert skills["watch"]["backing_tasks"][0] == "object_detection"
    assert skills["watch"]["tasks_available"] is True
    assert skills["watch"]["rules"] == ["line_crossing", "occupancy"]


def test_skills_payload_missing_tasks_flagged():
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps({"image_captioning"})   # only captioning
    skills = _by_id(rt)
    assert skills["see"]["tasks_available"] is True          # captioning|vqa
    assert skills["count"]["tasks_available"] is False
    assert skills["faces"]["tasks_available"] is False
    assert skills["watch"]["tasks_available"] is False


def test_skills_payload_vqa_alone_backs_see():
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps({"vqa"})
    assert _by_id(rt)["see"]["tasks_available"] is True


def test_skills_payload_alias_and_canonical_both_back_see():
    # The canonical-alias case (contract §4): an adapter that advertises
    # the alias ``scene_caption`` satisfies ``see`` exactly like one that
    # advertises the canonical ``image_captioning`` — because backing_tasks
    # is derived from tasks.yml with aliases folded in.
    for advertised in ({"image_captioning"}, {"scene_caption"}):
        rt = _runtime()
        rt.kaic_capabilities = _StubCaps(advertised)
        assert _by_id(rt)["see"]["tasks_available"] is True, advertised


# ── greyed skills → suggested_adapters + enable_url (guidance on-ramp) ──


def test_greyed_skill_names_suggested_adapters():
    # count is greyed (no object_detection advertised): it must name the
    # suggested adapter(s) from tasks.yml so the operator knows what to enable.
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps({"image_captioning"})   # only captioning
    skills = _by_id(rt)
    assert skills["count"]["tasks_available"] is False
    assert skills["count"]["suggested_adapters"] == ["yolov8"]
    # see's backing tasks (image_captioning + vqa) union blip+moondream.
    assert skills["faces"]["suggested_adapters"] == ["insightface"]
    # watch inherits count's backing → count's suggested adapter.
    assert skills["watch"]["suggested_adapters"] == ["yolov8"]


def test_greyed_see_unions_deduped_adapters_across_backing_tasks():
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps(set())                  # nothing advertised
    see = _by_id(rt)["see"]
    assert see["tasks_available"] is False
    # image_captioning → [blip, moondream], vqa → [moondream]; union deduped,
    # order preserved.
    assert see["suggested_adapters"] == ["blip", "moondream"]


def test_enable_url_present_only_when_ui_url_set():
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        opennvr_ui_url="https://nvr.example/",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front door")],
    )
    rt = CameraAgentRuntime(cfg)
    rt.kaic_capabilities = _StubCaps(set())
    count = {s["id"]: s for s in rt.skills_payload()}["count"]
    # trailing slash on the base URL is normalized away.
    assert count["enable_url"] == "https://nvr.example/ai-adapters"


def test_enable_url_none_when_ui_url_unset():
    rt = _runtime()                                          # no opennvr_ui_url
    rt.kaic_capabilities = _StubCaps(set())
    count = {s["id"]: s for s in rt.skills_payload()}["count"]
    assert count["enable_url"] is None


def test_not_greyed_skill_omits_suggestion_fields():
    # When a skill is available (not greyed) the additive fields are absent —
    # the guidance only appears where there's something to guide.
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps({"object_detection"})   # count is served
    count = {s["id"]: s for s in rt.skills_payload()}["count"]
    assert count["tasks_available"] is True
    assert "suggested_adapters" not in count
    assert "enable_url" not in count


def test_derive_skill_suggested_adapters_from_bundled_registry():
    import camera_agent

    derived = camera_agent._derive_skill_suggested_adapters()
    assert derived["count"] == ["yolov8"]
    assert derived["faces"] == ["insightface"]
    assert derived["see"] == ["blip", "moondream"]
    assert derived["watch"] == ["yolov8"]                    # inherits count


# ── greyed skills → suggested_apps + app_enable_url (install on-ramp) ──


def test_derive_skill_suggested_apps_from_bundled_registry():
    import camera_agent

    derived = camera_agent._derive_skill_suggested_apps()
    # object_detection backs count → its packaged apps.
    assert derived["count"] == [
        "intrusion-detection", "loitering-detection", "occupancy-counting"
    ]
    assert derived["faces"] == ["smart-doorbell"]
    assert derived["watch"] == [                              # inherits count
        "intrusion-detection", "loitering-detection", "occupancy-counting"
    ]


def test_greyed_skill_includes_suggested_apps_and_app_enable_url():
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        opennvr_ui_url="https://nvr.example/",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front door")],
    )
    rt = CameraAgentRuntime(cfg)
    rt.kaic_capabilities = _StubCaps({"image_captioning"})   # count greyed
    count = {s["id"]: s for s in rt.skills_payload()}["count"]
    assert count["tasks_available"] is False
    assert count["suggested_apps"] == [
        "intrusion-detection", "loitering-detection", "occupancy-counting"
    ]
    # app deep-link points at the App Catalog (trailing slash normalized).
    assert count["app_enable_url"] == "https://nvr.example/app-catalog"


def test_app_enable_url_none_when_ui_url_unset():
    rt = _runtime()                                          # no opennvr_ui_url
    rt.kaic_capabilities = _StubCaps(set())
    count = {s["id"]: s for s in rt.skills_payload()}["count"]
    assert count["suggested_apps"] == [
        "intrusion-detection", "loitering-detection", "occupancy-counting"
    ]
    assert count["app_enable_url"] is None


def test_not_greyed_skill_omits_app_suggestion_fields():
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps({"object_detection"})   # count is served
    count = {s["id"]: s for s in rt.skills_payload()}["count"]
    assert count["tasks_available"] is True
    assert "suggested_apps" not in count
    assert "app_enable_url" not in count


# ── decoupling: a new agent_skill in tasks.yml needs no agent code edit ─


def test_new_agent_skill_mapping_needs_no_code_edit(tmp_path, monkeypatch):
    """The whole point of deriving from tasks.yml: dropping a new entry
    with an ``agent_skill`` into the registry makes it a skill backing
    with zero edits to camera_agent.py. We point the loader at a temp
    registry and assert the new mapping (+ its alias) appears."""
    import camera_agent

    registry = [
        {"task": "object_detection", "label": "Object Detection",
         "agent_skill": "count", "aliases": []},
        # A task nobody wired by hand — a brand-new adapter capability that
        # backs the ``see`` skill, declared purely in the taxonomy file.
        {"task": "thermal_scene", "label": "Thermal Scene",
         "agent_skill": "see", "aliases": ["thermal_caption"]},
    ]
    reg_file = tmp_path / "tasks.yml"
    import yaml as _yaml
    reg_file.write_text(_yaml.safe_dump(registry))
    monkeypatch.setattr(camera_agent, "TASKS_REGISTRY_PATH", reg_file)

    derived = camera_agent._derive_skill_backing_tasks()
    assert derived["count"] == ["object_detection"]
    # New mapping present, with BOTH the canonical name and its alias —
    # no line of camera_agent.py changed to make this happen.
    assert derived["see"] == ["thermal_scene", "thermal_caption"]


def test_derive_falls_back_when_bundled_file_missing(tmp_path, monkeypatch):
    """A missing/unreadable tasks.yml must never crash the agent — it
    falls back to the hardcoded last-known-good map."""
    import camera_agent

    monkeypatch.setattr(
        camera_agent, "TASKS_REGISTRY_PATH", tmp_path / "does-not-exist.yml"
    )
    derived = camera_agent._derive_skill_backing_tasks()
    assert derived == camera_agent._SKILL_BACKING_FALLBACK


def test_bundled_registry_derives_expected_skills():
    """Sanity check against the file actually shipped next to the agent."""
    import camera_agent

    derived = camera_agent._derive_skill_backing_tasks()
    assert derived["count"][0] == "object_detection"
    assert derived["faces"] == ["face_recognition"]
    assert "image_captioning" in derived["see"]
    assert "scene_caption" in derived["see"]     # alias folded in
    assert "vqa" in derived["see"]
    assert derived["watch"] == derived["count"]  # watch inherits count's tasks


def test_skills_payload_kaic_unreachable_falls_back_to_config_behavior():
    # None (= unreachable / never fetched) must read as available: the field
    # is advisory and a down KAI-C must not grey out working skills.
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps(None)
    skills = _by_id(rt)
    for sid in ("see", "count", "faces", "watch"):
        assert skills[sid]["tasks_available"] is True
    # ...and the pre-existing shape/gating is untouched.
    assert skills["see"]["enabled"] is True
    assert skills["count"]["enabled"] is True
    for key in ("id", "icon", "name", "example", "uses",
                "enabled", "available", "hint"):
        assert key in skills["see"]


def test_skills_without_kaic_backing_always_task_available():
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps(set())                  # KAI-C: no adapters
    skills = _by_id(rt)
    for sid in ("events", "footage", "alarm", "report", "task"):
        assert skills[sid]["backing_tasks"] == []
        assert skills[sid]["tasks_available"] is True


def test_missing_tasks_do_not_gate_enablement():
    # skill_requirement_met stays the enable gate: tasks_available=False is
    # display data, the tool stays advertised/enabled.
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps(set())                  # nothing advertised
    skills = _by_id(rt)
    assert skills["count"]["tasks_available"] is False
    assert skills["count"]["enabled"] is True
    assert "detect_objects" in {t["function"]["name"] for t in rt.tool_definitions}


# ── /skills endpoint: refresh (TTL'd) + fields on the wire ─────────────


def test_skills_endpoint_refreshes_and_reports_fields():
    rt = _runtime()
    rt.kaic_capabilities = _StubCaps({"object_detection"})
    client = TestClient(build_app(rt))
    skills = {s["id"]: s for s in client.get("/skills").json()["skills"]}
    assert rt.kaic_capabilities.refreshes == 1
    assert skills["count"]["tasks_available"] is True
    assert skills["see"]["tasks_available"] is False
    assert skills["see"]["backing_tasks"][:1] == ["image_captioning"]
    assert "vqa" in skills["see"]["backing_tasks"]
    assert skills["watch"]["rules"] == ["line_crossing", "occupancy"]


def test_skills_endpoint_survives_unreachable_kaic():
    class _BoomCaps(_StubCaps):
        async def refresh(self):                              # noqa: D401
            self.refreshes += 1
            self.tasks = None                                 # like a failed fetch
            return None

    rt = _runtime()
    rt.kaic_capabilities = _BoomCaps(None)
    client = TestClient(build_app(rt))
    resp = client.get("/skills")
    assert resp.status_code == 200
    assert all(s["tasks_available"] is True for s in resp.json()["skills"])


def test_bundled_tasks_registry_matches_server_canonical_copy():
    """The agent bundles a copy of server/config/tasks.yml (for offline
    startup). Guard against silent drift: the two must stay identical —
    if this fails, re-copy server/config/tasks.yml over
    examples/camera-agent/tasks.yml."""
    import pathlib
    import yaml

    here = pathlib.Path(__file__).resolve()
    bundled = here.parent.parent / "tasks.yml"
    server = here.parents[3] / "server" / "config" / "tasks.yml"
    if not server.exists():  # server tree not present in this checkout
        import pytest
        pytest.skip("server/config/tasks.yml not in this checkout")
    assert yaml.safe_load(bundled.read_text()) == yaml.safe_load(server.read_text()), (
        "agent tasks.yml has drifted from server/config/tasks.yml — re-sync them"
    )

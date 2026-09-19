"""Assist: the OpenNVR LLM API and its five tools (HA-501)."""

from __future__ import annotations

from unittest.mock import MagicMock

from pyopennvr import OpenNVRAuthError
import pytest

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component

from custom_components.opennvr.llm import API_ID

from . import create_mock_config_entry, load_fixture, setup_mock_config_entry
from .conftest import FakeStream

FRONT = "camera.front_door"


@pytest.fixture
async def api(hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    return entry


def _context() -> llm.LLMContext:
    return llm.LLMContext(platform="test", context=Context(), language="en",
                          assistant="conversation", device_id=None)


async def _tool(hass: HomeAssistant, name: str, **args):
    """Call a tool as an agent would. (``APIInstance.async_call_tool`` only
    adds a conversation trace, and conversation needs hassil, which the test
    image doesn't carry.)"""
    context = _context()
    instance = await llm.async_get_api(hass, API_ID, context)
    tool = next(t for t in instance.tools if t.name == name)
    return await tool.async_call(hass, llm.ToolInput(tool_name=name, tool_args=args), context)


async def test_registered_once_and_removed_with_the_last_site(
        hass: HomeAssistant, api) -> None:
    assert [a.id for a in llm.async_get_apis(hass)].count(API_ID) == 1
    instance = await llm.async_get_api(hass, API_ID, _context())
    assert {t.name for t in instance.tools} == {
        "opennvr_search_events", "opennvr_summarize_period", "opennvr_list_alerts",
        "opennvr_describe_camera", "opennvr_ptz_goto_preset"}
    assert "none exposed" in instance.api_prompt
    await hass.config_entries.async_unload(api.entry_id)
    assert API_ID not in [a.id for a in llm.async_get_apis(hass)]


async def test_only_exposed_cameras(hass: HomeAssistant, api, mock_client: MagicMock) -> None:
    out = await _tool(hass, "opennvr_search_events", query="van")
    assert "exposed" in out["error"]
    mock_client.search.assert_not_called()
    async_expose_entity(hass, "conversation", FRONT, True)
    instance = await llm.async_get_api(hass, API_ID, _context())
    assert "Front door" in instance.api_prompt and "Garage" not in instance.api_prompt
    out = await _tool(hass, "opennvr_describe_camera", camera="Garage")
    assert out["error"].startswith("No camera named 'Garage'")


async def test_search(hass: HomeAssistant, api, mock_client: MagicMock) -> None:
    async_expose_entity(hass, "conversation", FRONT, True)
    mock_client.search.return_value = {"semantic": True, "results": [
        {"kind": "footage", "at": "2026-09-18T08:31:00+00:00", "camera_id": 1,
         "labels": ["truck"], "caption": "a white van at the door"},
        {"kind": "event", "at": "2026-09-18T08:30:00+00:00", "camera_id": 3,
         "label": "car"},                                                  # not exposed
        {"kind": "event", "at": "2026-09-18T08:00:00+00:00", "camera_id": 1,
         "label": "car", "plate_text": "KA01AB1234", "evidence_url": "/api/v1/x"},
        {"kind": "alert", "at": "2026-09-18T07:00:00+00:00", "camera_id": None,
         "title": "Disk", "severity": "high", "source": "core", "acknowledged": False},
    ]}
    out = await _tool(hass, "opennvr_search_events", query="white van", camera="front door",
                      start="2026-09-18", end="2026-09-18T12:00")
    kwargs = mock_client.search.call_args.kwargs
    assert kwargs["q"] == "white van" and kwargs["camera_id"] == 1 and kwargs["limit"] == 10
    assert kwargs["from_"].endswith("+00:00") and kwargs["to"].endswith("+00:00")
    assert out["plain_language_search"] is True
    assert [(r["kind"], r["camera"]) for r in out["results"]] == [
        ("footage", "Front door"), ("event", "Front door"), ("alert", None)]
    assert out["results"][0]["description"] == "a white van at the door"
    assert out["results"][1]["plate_text"] == "KA01AB1234" and "evidence_url" not in \
        out["results"][1]
    bad = await _tool(hass, "opennvr_search_events", start="last tuesday")
    assert bad["error"].startswith("Not a time")
    assert "Invalid arguments" in (await _tool(hass, "opennvr_search_events", limit=500))["error"]


async def test_summarize_period(hass: HomeAssistant, api, mock_client: MagicMock) -> None:
    async_expose_entity(hass, "conversation", FRONT, True)
    summary = load_fixture("search_summary")
    summary["cameras"].append({**summary["cameras"][0], "camera_id": 3, "name": "Garage"})
    mock_client.search_summary.return_value = summary
    out = await _tool(hass, "opennvr_summarize_period", start="2026-09-18T00:00")
    assert [c["camera"] for c in out["cameras"]] == ["Front door"]       # 3 not exposed
    assert out["cameras"][0]["detections"] == {"car": 3, "person": 5}
    assert out["alerts_not_about_a_camera"] == {"medium": 1}
    assert mock_client.search_summary.call_args.kwargs == {"camera_id": None}


async def test_list_alerts_picks_the_camera_in_any_handle_form(
        hass: HomeAssistant, api, mock_client: MagicMock) -> None:
    async_expose_entity(hass, "conversation", FRONT, True)
    mock_client.get_alerts.return_value = {"alerts": [
        {"fired_at": "2026-09-18T09:00:00+00:00", "camera_id": "cam-1", "title": "Loiter",
         "severity": "high", "source_name": "loitering", "acknowledged_at": None},
        {"fired_at": "2026-09-18T08:00:00+00:00", "camera_id": "cam3", "title": "Hidden",
         "severity": "low", "source_name": "x", "acknowledged_at": None},
        {"fired_at": "2026-09-18T07:00:00+00:00", "camera_id": "1", "title": "Old",
         "severity": "low", "source_name": "x", "acknowledged_at": "2026-09-18T07:05:00"},
    ]}
    out = await _tool(hass, "opennvr_list_alerts", camera="Front door", unacknowledged_only=True)
    assert [a["title"] for a in out["alerts"]] == ["Loiter", "Old"]
    assert out["alerts"][1]["acknowledged"] is True
    assert mock_client.get_alerts.call_args.kwargs["unacked"] is True
    out = await _tool(hass, "opennvr_list_alerts")
    assert [a["title"] for a in out["alerts"]] == ["Loiter", "Old"]   # cam3 not exposed


async def test_describe_camera(hass: HomeAssistant, api, mock_client: MagicMock) -> None:
    async_expose_entity(hass, "conversation", FRONT, True)
    mock_client.describe_camera.return_value = load_fixture("camera_describe")
    out = await _tool(hass, "opennvr_describe_camera", camera="Front door",
                      question="is there a van?")
    assert out["description"].startswith("A white van") and out["camera"] == "Front door"
    assert mock_client.describe_camera.call_args.args == (1, "is there a van?")
    assert mock_client.describe_camera.call_args.kwargs["correlation_id"]
    mock_client.describe_camera.return_value = {"available": False, "description": None}
    out = await _tool(hass, "opennvr_describe_camera", camera=FRONT)
    assert out["description"] is None and "no image model" in out["note"]
    mock_client.describe_camera.side_effect = OpenNVRAuthError("needs live.view")
    out = await _tool(hass, "opennvr_describe_camera", camera=FRONT)
    assert out["error"] == "OpenNVR does not allow this: needs live.view"


async def test_ptz_goto_preset(hass: HomeAssistant, api, mock_client: MagicMock) -> None:
    async_expose_entity(hass, "conversation", FRONT, True)
    mock_client.ptz_presets.return_value = [{"token": "p1", "name": "Gate"}]
    out = await _tool(hass, "opennvr_ptz_goto_preset", camera="Front door", preset="gate")
    assert out == {"camera": "Front door", "preset": "gate", "moved": True}
    assert mock_client.ptz_goto_preset.call_args.args == (1, "p1")
    assert mock_client.ptz_goto_preset.call_args.kwargs["correlation_id"]
    out = await _tool(hass, "opennvr_ptz_goto_preset", camera="Front door", preset="moon")
    assert out["error"] == "Front door has no preset 'moon'. Presets: Gate"


async def test_hidden_cameras_widen_the_page(hass: HomeAssistant, api,
                                             mock_client: MagicMock) -> None:
    """Garage is shown but not exposed: its rows are dropped after the
    server's cut, so the site is asked for a full page."""
    async_expose_entity(hass, "conversation", FRONT, True)
    mock_client.search.return_value = {"results": []}
    await _tool(hass, "opennvr_search_events", query="van")
    assert mock_client.search.call_args.kwargs["limit"] == 100
    await _tool(hass, "opennvr_search_events", query="van", camera="Front door")
    assert mock_client.search.call_args.kwargs["limit"] == 10
    async_expose_entity(hass, "conversation", "camera.garage", True)
    await _tool(hass, "opennvr_search_events", query="van")
    assert mock_client.search.call_args.kwargs["limit"] == 10


def test_a_name_on_two_sites_is_ambiguous() -> None:
    from custom_components.opennvr.llm import _Cam, _find, _ToolError

    a = _Cam(entry=object(), camera_id=1, name="Gate", entity_id="camera.gate")
    b = _Cam(entry=object(), camera_id=4, name="Gate", entity_id="camera.gate_2")
    with pytest.raises(_ToolError, match="camera.gate, camera.gate_2"):
        _find([a, b], "gate")
    assert _find([a, b], "camera.gate_2") is b

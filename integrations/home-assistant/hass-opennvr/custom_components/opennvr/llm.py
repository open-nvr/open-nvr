"""Assist: OpenNVR's tools for Home Assistant's LLM agents (design §9, HA-501).

One LLM API, ``OpenNVR``, whatever the number of sites: an agent that has it
selected can search what happened, summarise a period, list alerts, ask
what a camera sees, and move a camera to a preset.

What it may reach: the cameras OpenNVR lets the token see, that the entry
shows, and that the user has **exposed to Assist** (Settings > Voice
assistants > Expose). Reading goes through the entry's viewer client
(OpenNVR enforces the shown cameras itself); describing and PTZ use the
entry's token, carry the conversation's context id as the correlation id,
and are audited by OpenNVR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pyopennvr import OpenNVRAuthError, OpenNVRError, OpenNVRNotFoundError
import voluptuous as vol

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er, llm
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from .const import DOMAIN

API_ID = DOMAIN
API_NAME = "OpenNVR"
#: The most rows a tool hands the model.
MAX_ROWS = 25

PROMPT = (
    "You can look into the OpenNVR video recorder with these tools. Cameras are "
    "named as listed below; pass a camera's name exactly. Times are ISO 8601; a "
    "time without a zone is local time. Say what the results show, including "
    "when nothing was found; never guess beyond them. Descriptions of a camera's "
    "view come from an AI model and can be wrong."
)


@dataclass(frozen=True)
class _Cam:
    entry: Any
    camera_id: int
    name: str
    entity_id: str


def _cameras(hass: HomeAssistant, assistant: str) -> list[_Cam]:
    """Every OpenNVR camera an agent may use: shown by its (loaded) entry and
    exposed to this assistant."""
    registry = er.async_get(hass)
    found: list[_Cam] = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        data = entry.runtime_data.coordinator.data
        if data is None:
            continue
        for cid, cam in data.cameras.items():
            entity_id = registry.async_get_entity_id(
                "camera", DOMAIN, f"{data.info.site_id}:camera.{cid}")
            if entity_id and async_should_expose(hass, assistant, entity_id):
                found.append(_Cam(entry, cid, cam.name, entity_id))
    return found


def _find(cams: list[_Cam], name: str | None) -> _Cam:
    wanted = (name or "").strip().casefold()
    found = [c for c in cams if wanted in (c.name.casefold(), c.entity_id.casefold())]
    if len(found) > 1:  # the same name on two sites: the entity id decides
        raise _ToolError(f"Several cameras are named {name!r}; use one of: "
                         + ", ".join(c.entity_id for c in found))
    if found:
        return found[0]
    raise _ToolError(f"No camera named {name!r}. Cameras: "
                     + (", ".join(c.name for c in cams) or "none exposed"))


class _ToolError(Exception):
    """A problem the model should read and can act on."""


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value)) or (
        dt_util.start_of_local_day(d) if (d := dt_util.parse_date(str(value))) else None)
    if parsed is None:
        raise _ToolError(f"Not a time: {value!r}; use ISO 8601, e.g. 2026-09-18T07:00")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(parsed)


def _local(iso: str | None) -> str | None:
    parsed = dt_util.parse_datetime(iso or "")
    return dt_util.as_local(parsed).isoformat(timespec="seconds") if parsed else iso


#: Rows asked of a site when some of its shown cameras aren't exposed (the
#: server's own maximum): hidden cameras' rows are dropped here, after the cut.
WIDE_PAGE = 100


def _page(entry: Any, cams: list[_Cam], cam: _Cam | None, limit: int) -> int:
    if cam is not None:
        return limit
    exposed = {c.camera_id for c in cams if c.entry.entry_id == entry.entry_id}
    shown = set(entry.runtime_data.coordinator.data.cameras)
    return limit if shown <= exposed else WIDE_PAGE


def _entries(cams: list[_Cam], cam: _Cam | None) -> list[Any]:
    if cam is not None:
        return [cam.entry]
    seen: dict[str, Any] = {}
    for c in cams:
        seen.setdefault(c.entry.entry_id, c.entry)
    return list(seen.values())


class _OpenNVRTool(llm.Tool):
    """Runs ``_call``; turns what goes wrong into words the model can use."""

    async def async_call(self, hass: HomeAssistant, tool_input: llm.ToolInput,
                         llm_context: llm.LLMContext) -> JsonObjectType:
        cams = _cameras(hass, llm_context.assistant)
        if not cams:
            return {"error": "No OpenNVR camera is exposed to this assistant. The user "
                             "can expose cameras under Settings > Voice assistants > Expose."}
        try:
            args = self.parameters(dict(tool_input.tool_args or {}))
        except vol.Invalid as err:
            return {"error": f"Invalid arguments: {err}"}
        try:
            return await self._call(hass, args, llm_context, cams)
        except _ToolError as err:
            return {"error": str(err)}
        except OpenNVRAuthError as err:
            return {"error": f"OpenNVR does not allow this: {err}"}
        except OpenNVRNotFoundError as err:
            return {"error": f"Not found in OpenNVR: {err}"}
        except OpenNVRError as err:
            return {"error": f"OpenNVR could not answer: {err}"}

    async def _call(self, hass: HomeAssistant, args: dict[str, Any],
                    llm_context: llm.LLMContext, cams: list[_Cam]) -> JsonObjectType:
        raise NotImplementedError


def _correlation(llm_context: llm.LLMContext) -> str | None:
    return llm_context.context.id if llm_context.context else None


class SearchEventsTool(_OpenNVRTool):
    name = "opennvr_search_events"
    description = (
        "Search what OpenNVR recorded: each result is one visit (an object seen on a "
        "camera, with its time, class, plate and caption). 'query' is plain language "
        "(e.g. 'white van', 'person with a red bag'); OpenNVR says how it understood "
        "it. Put the camera and the times in their own fields, not in 'query': they "
        "always win over how the words are read. Best matches first. For alerts use "
        "opennvr_list_alerts.")
    parameters = vol.Schema({
        vol.Optional("query"): cv.string,
        vol.Optional("camera"): cv.string,
        vol.Optional("label", description="an object class: person, car, dog..."): cv.string,
        vol.Optional("zone"): cv.string,
        vol.Optional("plate"): cv.string,
        vol.Optional("start"): cv.string,
        vol.Optional("end"): cv.string,
        vol.Optional("limit", default=10): vol.All(vol.Coerce(int), vol.Range(1, MAX_ROWS)),
    })

    async def _call(self, hass, args, llm_context, cams):
        cam = _find(cams, args["camera"]) if args.get("camera") else None
        start, end = _time(args.get("start")), _time(args.get("end"))
        names = {(c.entry.entry_id, c.camera_id): c.name for c in cams}
        rows: list[dict[str, Any]] = []
        understood: list[dict[str, Any]] = []
        for entry in _entries(cams, cam):
            client = await entry.runtime_data.coordinator.async_viewer_client()
            found = await client.search(
                q=args.get("query") or "", camera_id=cam.camera_id if cam else None,
                label=args.get("label"), zone=args.get("zone"), plate=args.get("plate"),
                from_=start.isoformat() if start else None,
                to=end.isoformat() if end else None,
                limit=_page(entry, cams, cam, args["limit"]))
            if isinstance(found.get("interpretation"), dict):
                i = found["interpretation"]
                understood.append({k: i.get(k) for k in ("labels", "text", "plate", "from",
                                                         "to") if i.get(k)})
            for r in found.get("results", []):
                camera = names.get((entry.entry_id, r.get("camera_id")))
                if r.get("camera_id") is not None and camera is None:
                    continue  # not exposed
                rows.append(_row(r, camera))
        return {"results": rows[:args["limit"]], "count": len(rows[:args["limit"]]),
                "understood_as": understood[0] if len(understood) == 1 else understood}


def _row(r: dict[str, Any], camera: str | None) -> dict[str, Any]:
    """A visit, in the words and times the model should use."""
    row: dict[str, Any] = {"at": _local(r.get("started_at")), "camera": camera,
                           "object": r.get("label")}
    if r.get("ended_at"):
        row["until"] = _local(r["ended_at"])
    if r.get("plate_text"):
        row["plate"] = r["plate_text"]
    if r.get("caption"):
        row["description"] = r["caption"]
    return row


class SummarizePeriodTool(_OpenNVRTool):
    name = "opennvr_summarize_period"
    description = (
        "Count what OpenNVR saw in a period (at most 31 days): per camera, "
        "detections by object class with the first and last, and alerts by "
        "severity. Use it for 'what happened overnight' or 'how busy was the gate'.")
    parameters = vol.Schema({
        vol.Required("start"): cv.string,
        vol.Optional("end"): cv.string,
        vol.Optional("camera"): cv.string,
    })

    async def _call(self, hass, args, llm_context, cams):
        cam = _find(cams, args["camera"]) if args.get("camera") else None
        start = _time(args["start"])
        end = _time(args.get("end")) or dt_util.utcnow()
        names = {(c.entry.entry_id, c.camera_id): c.name for c in cams}
        cameras: list[dict[str, Any]] = []
        site_alerts: dict[str, int] = {}
        entries = [e for e in _entries(cams, cam)
                   if e.runtime_data.coordinator.data.info.has("search_summary")]
        if not entries:
            raise _ToolError("This OpenNVR is too old to summarise; update it.")
        for entry in entries:  # a site too old to summarise is left out
            client = await entry.runtime_data.coordinator.async_viewer_client()
            out = await client.search_summary(start.isoformat(), end.isoformat(),
                                              camera_id=cam.camera_id if cam else None)
            for c in out.get("cameras", []):
                name = names.get((entry.entry_id, c.get("camera_id")))
                if name is None:
                    continue
                cameras.append({
                    "camera": name, "detections": c.get("events", {}),
                    "first_detection": _local(c.get("first_event")),
                    "last_detection": _local(c.get("last_event")),
                    "alerts": c.get("alerts", {})})
            for sev, n in (out.get("site_alerts") or {}).items():
                site_alerts[sev] = site_alerts.get(sev, 0) + n
        return {"from": _local(start.isoformat()), "to": _local(end.isoformat()),
                "cameras": cameras, "alerts_not_about_a_camera": site_alerts}


class ListAlertsTool(_OpenNVRTool):
    name = "opennvr_list_alerts"
    description = "List OpenNVR's alerts, newest first, optionally only unacknowledged ones."
    parameters = vol.Schema({
        vol.Optional("unacknowledged_only", default=False): cv.boolean,
        vol.Optional("severity"): vol.In(["low", "medium", "high", "critical"]),
        vol.Optional("camera"): cv.string,
        vol.Optional("limit", default=10): vol.All(vol.Coerce(int), vol.Range(1, MAX_ROWS)),
    })

    async def _call(self, hass, args, llm_context, cams):
        cam = _find(cams, args["camera"]) if args.get("camera") else None
        names = {(c.entry.entry_id, c.camera_id): c.name for c in cams}
        rows: list[dict[str, Any]] = []
        for entry in _entries(cams, cam):
            client = await entry.runtime_data.coordinator.async_viewer_client()
            # Alerts name cameras in several handle forms (cam3, cam-3, 3),
            # so one camera is picked here, from a wider page.
            out = await client.get_alerts(
                severity=args.get("severity"), unacked=args["unacknowledged_only"],
                limit=WIDE_PAGE if cam else _page(entry, cams, None, args["limit"]))
            for a in out.get("alerts", []):
                cid = _camera_num(a.get("camera_id"))
                if cam is not None and cid != cam.camera_id:
                    continue
                name = names.get((entry.entry_id, cid)) if cid is not None else None
                if cid is not None and name is None:
                    continue
                rows.append({"at": _local(a.get("fired_at")), "camera": name,
                             "title": a.get("title"), "severity": a.get("severity"),
                             "source": a.get("source_name"),
                             "acknowledged": a.get("acknowledged_at") is not None})
        rows.sort(key=lambda r: r.get("at") or "", reverse=True)
        return {"alerts": rows[:args["limit"]]}


def _camera_num(handle: Any) -> int | None:
    text = str(handle or "").strip().lower().removeprefix("cam").lstrip("-")
    return int(text) if text.isdigit() else None


class DescribeCameraTool(_OpenNVRTool):
    name = "opennvr_describe_camera"
    description = (
        "Describe what a camera sees right now, or answer a question about its "
        "current view (e.g. 'is the gate open?'), using OpenNVR's image model.")
    parameters = vol.Schema({
        vol.Required("camera"): cv.string,
        vol.Optional("question"): vol.All(cv.string, vol.Length(max=300)),
    })

    async def _call(self, hass, args, llm_context, cams):
        cam = _find(cams, args["camera"])
        coordinator = cam.entry.runtime_data.coordinator
        if not coordinator.data.info.has("camera_describe"):
            raise _ToolError("This OpenNVR cannot describe camera views; update it.")
        out = await cam.entry.runtime_data.client.describe_camera(
            cam.camera_id, args.get("question"), correlation_id=_correlation(llm_context))
        if not out.get("available"):
            return {"camera": cam.name, "description": None,
                    "note": "OpenNVR has no image model to describe views with, or it "
                            "declined. The camera entity's snapshot can still be viewed."}
        return {"camera": cam.name, "at": _local(out.get("at")),
                "description": out.get("description")}


class PTZGotoPresetTool(_OpenNVRTool):
    name = "opennvr_ptz_goto_preset"
    description = "Move a pan-tilt-zoom camera to one of its saved presets, by name."
    parameters = vol.Schema({
        vol.Required("camera"): cv.string,
        vol.Required("preset"): cv.string,
    })

    async def _call(self, hass, args, llm_context, cams):
        cam = _find(cams, args["camera"])
        client = cam.entry.runtime_data.client
        presets = await client.ptz_presets(cam.camera_id)
        wanted = args["preset"].strip().casefold()
        token = next((p.get("token") for p in presets
                      if wanted in (str(p.get("name", "")).casefold(),
                                    str(p.get("token", "")).casefold())), None)
        if token is None:
            raise _ToolError(f"{cam.name} has no preset {args['preset']!r}. Presets: "
                             + (", ".join(str(p.get("name")) for p in presets) or "none"))
        await client.ptz_goto_preset(cam.camera_id, token,
                                     correlation_id=_correlation(llm_context))
        return {"camera": cam.name, "preset": args["preset"], "moved": True}


TOOLS: tuple[type[_OpenNVRTool], ...] = (
    SearchEventsTool, SummarizePeriodTool, ListAlertsTool, DescribeCameraTool,
    PTZGotoPresetTool)


class OpenNVRAPI(llm.API):
    """The ``OpenNVR`` LLM API: the five tools over every loaded site."""

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        cams = _cameras(self.hass, llm_context.assistant)
        now = dt_util.now()
        prompt = (f"{PROMPT}\nNow: {now.isoformat(timespec='minutes')} "
                  f"({now.tzname()}). Cameras: "
                  + (", ".join(sorted(c.name for c in cams)) or "none exposed") + ".")
        return llm.APIInstance(api=self, api_prompt=prompt, llm_context=llm_context,
                               tools=[tool() for tool in TOOLS])


@callback
def async_setup_llm_api(hass: HomeAssistant, entry) -> None:
    """Register the API with the first loaded site; unregister it when the
    last one unloads."""
    holders: set[str] = hass.data.setdefault(f"{DOMAIN}_llm_entries", set())
    if not holders:
        hass.data[f"{DOMAIN}_llm_unregister"] = llm.async_register_api(
            hass, OpenNVRAPI(hass=hass, id=API_ID, name=API_NAME))
    holders.add(entry.entry_id)

    @callback
    def release() -> None:
        holders.discard(entry.entry_id)
        if not holders and (unregister := hass.data.pop(f"{DOMAIN}_llm_unregister", None)):
            unregister()

    entry.async_on_unload(release)


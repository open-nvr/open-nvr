"""Actions (design §7.5). Registered once in ``async_setup``; each finds its
OpenNVR site from the camera entity it targets, an explicit config entry, or
the only site there is. Every call carries Home Assistant's context id as
``X-Correlation-Id``, so OpenNVR's audit log ties it to the automation run.

Signed media (clip exports, search thumbnails) live for the entry's
"notification link lifetime" option.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
from typing import Any

import aiohttp
from pyopennvr import OpenNVRAuthError, OpenNVRError, OpenNVRNotFoundError
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import CONF_MEDIA_TTL, DEFAULT_MEDIA_TTL, DOMAIN
from .views import relay_path

ATTR_CAMERA = "camera"
#: Longest clip a signed export URL may cover (the server's own limit).
MAX_CLIP_S = 3600
#: PTZ directions as continuous-move vectors (pan, tilt, zoom).
PTZ_VECTORS = {
    "up": (0, 1, 0), "down": (0, -1, 0), "left": (-1, 0, 0), "right": (1, 0, 0),
    "in": (0, 0, 1), "out": (0, 0, -1),
}

_ENTRY = vol.Optional(ATTR_CONFIG_ENTRY_ID)
_CAMERA = vol.Required(ATTR_CAMERA)

SCHEMAS: dict[str, vol.Schema] = {
    "ptz": vol.Schema({
        _CAMERA: cv.entity_id,
        vol.Required("action"): vol.In(["move", "zoom", "stop", "preset"]),
        vol.Optional("argument"): cv.string,
        vol.Optional("speed", default=0.5): vol.All(vol.Coerce(float), vol.Range(0.1, 1)),
    }),
    "create_event": vol.Schema({
        _CAMERA: cv.entity_id,
        vol.Optional("label", default="manual"): vol.All(cv.string, vol.Length(1, 60)),
        vol.Optional("sub_label"): vol.All(cv.string, vol.Length(max=500)),
        vol.Optional("duration"): vol.All(vol.Coerce(float), vol.Range(1, 3600)),
    }),
    "end_event": vol.Schema({_ENTRY: cv.string,
                             vol.Required("event_id"): vol.All(vol.Coerce(int), vol.Range(1))}),
    "export_recording": vol.Schema({
        _CAMERA: cv.entity_id,
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
        vol.Optional("with_hash", default=False): cv.boolean,
    }),
    "protect_recording": vol.Schema({
        _ENTRY: cv.string,
        vol.Required("event_id"): vol.All(vol.Coerce(int), vol.Range(1)),
        vol.Optional("pre_s", default=10): vol.All(vol.Coerce(int), vol.Range(0, 300)),
        vol.Optional("post_s", default=10): vol.All(vol.Coerce(int), vol.Range(0, 300)),
    }),
    "ack_alerts": vol.Schema({
        _ENTRY: cv.string,
        vol.Optional("alert_ids"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        vol.Optional("source"): vol.All(cv.string, vol.Length(1, 100)),
        vol.Optional("severity"): vol.In(["low", "medium", "high", "critical"]),
    }),
    "search_events": vol.Schema({
        _ENTRY: cv.string,
        vol.Optional("query"): vol.All(cv.string, vol.Length(max=200)),
        vol.Optional(ATTR_CAMERA): cv.entity_id,
        vol.Optional("label"): vol.All(cv.string, vol.Length(max=60)),
        vol.Optional("zone"): vol.All(cv.string, vol.Length(max=60)),
        vol.Optional("plate"): vol.All(cv.string, vol.Length(max=32)),
        vol.Optional("start"): cv.datetime,
        vol.Optional("end"): cv.datetime,
        vol.Optional("limit", default=25): vol.All(vol.Coerce(int), vol.Range(1, 100)),
    }),
}


# ── finding the site and camera ──────────────────────────────────────────


def _invalid(key: str, **placeholders: Any) -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key,
                                  translation_placeholders={k: str(v) for k, v in
                                                            placeholders.items()} or None)


def _loaded(hass: HomeAssistant, entry_id: str):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise _invalid("unknown_entry", entry_id=entry_id)
    if entry.state is not ConfigEntryState.LOADED:
        raise _invalid("entry_not_loaded", name=entry.title)
    return entry


def _entry(hass: HomeAssistant, call: ServiceCall):
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        return _loaded(hass, entry_id)
    if entity_id := call.data.get(ATTR_CAMERA):
        return _camera(hass, entity_id)[0]
    loaded = hass.config_entries.async_loaded_entries(DOMAIN)
    if len(loaded) != 1:
        raise _invalid("entry_required")
    return loaded[0]


def _camera(hass: HomeAssistant, entity_id: str):
    """(entry, OpenNVR camera id) of one of our camera entities."""
    ent = er.async_get(hass).async_get(entity_id)
    if (ent is None or ent.platform != DOMAIN or ent.domain != "camera"
            or not ent.config_entry_id or ":camera." not in ent.unique_id):
        raise _invalid("not_an_opennvr_camera", entity_id=entity_id)
    entry = _loaded(hass, ent.config_entry_id)
    return entry, int(ent.unique_id.rsplit(":camera.", 1)[1])


async def _run(coro):
    """Await a pyopennvr call, turning its errors into HA's."""
    try:
        return await coro
    except OpenNVRNotFoundError as err:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_found",
                                 translation_placeholders={"detail": str(err)}) from err
    except OpenNVRAuthError as err:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_permitted",
                                 translation_placeholders={"detail": str(err)}) from err
    except OpenNVRError as err:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_failed",
                                 translation_placeholders={"detail": str(err)}) from err


def _ttl_s(entry) -> int:
    return int(entry.options.get(CONF_MEDIA_TTL, DEFAULT_MEDIA_TTL)) * 3600


def _utc_iso(value: datetime) -> str:
    """HA's datetime selector gives local wall-clock time without a zone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(value).isoformat()


# ── the actions ──────────────────────────────────────────────────────────


async def _ptz(hass: HomeAssistant, call: ServiceCall) -> None:
    entry, cid = _camera(hass, call.data[ATTR_CAMERA])
    client, ctx = entry.runtime_data.client, call.context.id
    action, arg = call.data["action"], (call.data.get("argument") or "").strip().lower()
    if action == "stop":
        await _run(client.ptz_stop(cid, correlation_id=ctx))
        return
    if action == "preset":
        presets = await _run(client.ptz_presets(cid))
        token = next((p.get("token") for p in presets
                      if arg in (str(p.get("token", "")).lower(),
                                 str(p.get("name", "")).lower())), None)
        if not arg or token is None:
            raise _invalid("unknown_preset", preset=arg,
                           presets=", ".join(str(p.get("name")) for p in presets) or "-")
        await _run(client.ptz_goto_preset(cid, token, correlation_id=ctx))
        return
    allowed = ("up", "down", "left", "right") if action == "move" else ("in", "out")
    if arg not in allowed:
        raise _invalid("bad_ptz_argument", action=action, allowed=", ".join(allowed))
    speed = call.data["speed"]
    x, y, z = (v * speed for v in PTZ_VECTORS[arg])
    await _run(client.ptz_move(cid, x, y, z, correlation_id=ctx))


async def _create_event(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry, cid = _camera(hass, call.data[ATTR_CAMERA])
    event = await _run(entry.runtime_data.client.create_event(
        cid, label=call.data["label"], note=call.data.get("sub_label"),
        duration_s=call.data.get("duration"), correlation_id=call.context.id))
    return {"event_id": event.get("id")}


async def _end_event(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _entry(hass, call)
    await _run(entry.runtime_data.client.end_event(call.data["event_id"],
                                                   correlation_id=call.context.id))


async def _export_recording(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry, cid = _camera(hass, call.data[ATTR_CAMERA])
    start, end = call.data["start"], call.data["end"]
    duration = (end - start).total_seconds()
    if not 0 < duration <= MAX_CLIP_S:
        raise _invalid("bad_clip_range", max=MAX_CLIP_S)
    client = entry.runtime_data.client
    media = await _run(client.sign_media("clip", camera_id=cid, start=_utc_iso(start),
                                         duration_s=duration, ttl_s=_ttl_s(entry)))
    # ``url`` is under Home Assistant's own address (reachable wherever HA
    # is, e.g. for a phone notification); ``direct_url`` is OpenNVR's.
    result: dict[str, Any] = {"url": relay_path(entry.unique_id, media.url) or media.url,
                              "direct_url": media.url, "expires_at": media.expires_at}
    if call.data["with_hash"]:
        result.update(await _hash(hass, client, media.url))
    return result


async def _hash(hass: HomeAssistant, client, url: str) -> dict[str, Any]:
    """sha256 of the clip exactly as the URL serves it (evidence handover)."""
    digest, size = hashlib.sha256(), 0
    session = async_get_clientsession(hass, client.ssl is not False)
    try:
        async with session.get(url, ssl=client.ssl,
                               timeout=aiohttp.ClientTimeout(total=600)) as resp:
            if resp.status != 200:
                raise HomeAssistantError(translation_domain=DOMAIN,
                                         translation_key="command_failed",
                                         translation_placeholders={
                                             "detail": f"clip download: HTTP {resp.status}"})
            async for chunk in resp.content.iter_chunked(1 << 16):
                digest.update(chunk)
                size += len(chunk)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_failed",
                                 translation_placeholders={"detail": str(err)}) from err
    return {"sha256": digest.hexdigest(), "bytes": size}


async def _protect_recording(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _entry(hass, call)
    await _run(entry.runtime_data.client.protect_event(
        call.data["event_id"], pre_s=call.data["pre_s"], post_s=call.data["post_s"],
        correlation_id=call.context.id))


async def _ack_alerts(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    ids = call.data.get("alert_ids")
    source, severity = call.data.get("source"), call.data.get("severity")
    if ids and (source or severity):
        # The server refuses both too: they are two different intentions.
        raise _invalid("ack_ids_or_filter")
    entry = _entry(hass, call)
    result = await _run(entry.runtime_data.client.ack_alerts(
        ids=ids or None, source=source, severity=severity, correlation_id=call.context.id))
    return {"count": int(result.get("acknowledged", 0))}


async def _search_events(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    data = call.data
    camera_id = None
    if data.get(ATTR_CAMERA):
        entry, camera_id = _camera(hass, data[ATTR_CAMERA])
    else:
        entry = _entry(hass, call)
    client = entry.runtime_data.client
    found = await _run(client.search(
        q=data.get("query"), camera_id=camera_id, label=data.get("label"),
        zone=data.get("zone"), plate=data.get("plate"),
        from_=_utc_iso(data["start"]) if data.get("start") else None,
        to=_utc_iso(data["end"]) if data.get("end") else None, limit=data["limit"]))
    results = list(found.get("results", []))
    limit = asyncio.Semaphore(8)

    async def thumbnail(row: dict[str, Any]) -> None:
        if row.get("kind") != "event" or not row.get("evidence_url"):
            return
        async with limit:
            try:
                media = await client.sign_media("event", id=int(row["id"]), name="evidence",
                                                ttl_s=_ttl_s(entry))
            except OpenNVRError:
                return
        row["thumbnail_url"] = relay_path(entry.unique_id, media.url) or media.url

    await asyncio.gather(*(thumbnail(r) for r in results))
    for row in results:
        row.pop("evidence_url", None)  # an API path needing auth; the signed URL replaces it
    return {"results": results}


HANDLERS = {
    "ptz": (_ptz, SupportsResponse.NONE),
    "create_event": (_create_event, SupportsResponse.OPTIONAL),
    "end_event": (_end_event, SupportsResponse.NONE),
    "export_recording": (_export_recording, SupportsResponse.OPTIONAL),
    "protect_recording": (_protect_recording, SupportsResponse.NONE),
    "ack_alerts": (_ack_alerts, SupportsResponse.OPTIONAL),
    "search_events": (_search_events, SupportsResponse.ONLY),
}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    for name, (handler, response) in HANDLERS.items():

        async def run(call: ServiceCall, handler=handler) -> ServiceResponse:
            return await handler(hass, call)

        hass.services.async_register(DOMAIN, name, run, schema=SCHEMAS[name],
                                     supports_response=response)

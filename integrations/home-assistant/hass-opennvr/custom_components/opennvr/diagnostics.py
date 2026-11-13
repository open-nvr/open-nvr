"""Diagnostics: what a bug report needs, with secrets and addresses removed."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant

from . import OpenNVRConfigEntry

TO_REDACT = {CONF_API_TOKEN, CONF_URL, "token", "url", "urls", "ticket", "rtsp_url",
             "ip_address", "username", "changed_by", "plate_text", "configuration_url"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OpenNVRConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    stream = coordinator.stream
    info = data.info
    caller = dict(info.caller)
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT),
                  "options": dict(entry.options)},
        "server": {
            "version": info.version,
            "contract_version": info.contract_version,
            "features": list(info.features),
            "recording_pause_enabled": info.recording_pause_enabled,
            "caller": async_redact_data(caller, TO_REDACT | {"name"}),
        },
        "stream": {"state": coordinator.stream_state,
                   "epoch": stream.epoch if stream else None,
                   "last_seq": stream.last_seq if stream else None},
        "cameras": {"visible": sorted(data.all_cameras), "shown": sorted(data.cameras)},
        "entities": {
            "catalog_etag": data.catalog.etag,
            "by_platform": dict(Counter(d.platform for d in coordinator.descriptors())),
            # Descriptors this integration version cannot render (design §6.10).
            "skipped": async_redact_data(list(data.catalog.skipped), TO_REDACT),
            "states": len(data.states),
        },
        "recent_frames": async_redact_data(list(coordinator.recent_frames), TO_REDACT),
    }

"""Diagnostics: useful, and without the token or addresses."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.opennvr.diagnostics import async_get_config_entry_diagnostics

from . import TOKEN, URL, create_mock_config_entry, load_fixture, setup_mock_config_entry
from .conftest import FakeStream


async def test_diagnostics_redact_secrets(hass: HomeAssistant, mock_client: MagicMock,
                                          mock_stream: type[FakeStream]) -> None:
    catalog = load_fixture("entity_list")
    catalog["entities"].append({"key": "x.lock", "platform": "lock", "name": "Future",
                                "device": {"kind": "site", "id": "site"}})
    from pyopennvr import EntityCatalog

    mock_client.get_entities.return_value = EntityCatalog.from_dict(catalog)
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    [stream] = mock_stream.instances
    stream.on_frame({"v": 2, "seq": 1, "event_type": "media_ready",
                     "payload": {"url": f"{URL}/api/v1/media/s/secret", "kind": "clip"}})

    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag, default=str)
    assert TOKEN not in text and URL not in text and "secret" not in text
    assert diag["server"]["contract_version"] == "1.1.0"
    assert diag["server"]["caller"]["scopes"]
    assert [s["key"] for s in diag["entities"]["skipped"]] == ["x.lock"]
    assert diag["entities"]["by_platform"] == {"sensor": 1, "switch": 1, "button": 1}
    assert diag["recent_frames"][0]["payload"]["kind"] == "clip"

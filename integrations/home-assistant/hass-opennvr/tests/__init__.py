"""Tests for the OpenNVR integration, and the helpers they share.

``create_mock_client`` answers from the server's contract fixtures (copies of
``server/contract/fixtures``, drift-checked by test_fixtures.py), so the
integration is tested against what OpenNVR actually sends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from pyopennvr import Camera, EntityCatalog, OpenNVRClient, SiteMode, SystemInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_API_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant

from custom_components.opennvr.const import DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"

URL = "https://nvr.local"
TOKEN = "onvr_abcdef12_0123456789abcdef0123456789abcdef"
SITE_ID = "0755a940-1ff5-4861-ac08-1f57bb29a180"

CAMERAS = [
    {"id": 1, "name": "Front door", "is_active": True, "detection_enabled": True},
    {"id": 3, "name": "Garage", "is_active": True, "detection_enabled": False},
]


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def system_info(**overrides: Any) -> SystemInfo:
    return SystemInfo.from_dict({**load_fixture("system_info"), **overrides})


def create_mock_client() -> MagicMock:
    """An OpenNVRClient whose async methods answer from the fixtures."""
    client = MagicMock(spec=OpenNVRClient)
    client.base_url = URL
    client.ssl = None
    client.get_system_info.return_value = system_info()
    client.get_cameras.return_value = [Camera.from_dict(c) for c in CAMERAS]
    client.get_entities.return_value = EntityCatalog.from_dict(load_fixture("entity_list"))
    client.get_entity_states.return_value = load_fixture("entity_states")["states"]
    client.get_site_mode.return_value = SiteMode.from_dict(load_fixture("site_mode"))
    return client


def create_mock_config_entry(**kwargs: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, title="OpenNVR", unique_id=kwargs.pop("unique_id", SITE_ID),
        data=kwargs.pop("data", {CONF_URL: URL, CONF_API_TOKEN: TOKEN,
                                 CONF_VERIFY_SSL: False}),
        options=kwargs.pop("options", {}), **kwargs)


async def setup_mock_config_entry(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    entry.add_to_hass(hass)
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result

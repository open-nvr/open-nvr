"""Scaffold smoke test: Home Assistant can load the integration."""

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component

from custom_components.opennvr.const import DOMAIN


async def test_integration_loads(hass: HomeAssistant) -> None:
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.domain == DOMAIN
    assert await async_setup_component(hass, DOMAIN, {})

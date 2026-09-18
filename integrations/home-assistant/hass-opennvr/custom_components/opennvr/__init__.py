"""The OpenNVR integration.

Scaffold only: the config flow, coordinator and platforms land in HA-202
onwards (see docs/design/home-assistant-integration-implementation-plan.md).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

# Set up from config entries only; no YAML configuration.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the OpenNVR integration."""
    return True

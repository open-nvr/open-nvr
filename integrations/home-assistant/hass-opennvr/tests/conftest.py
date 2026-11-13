"""Shared fixtures for the OpenNVR integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from . import create_mock_client, create_mock_config_entry

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/opennvr in every test."""
    yield


class FakeStream:
    """Stands in for pyopennvr's EventStream: runs until stopped; tests push
    frames and state changes through the callbacks the coordinator gave it."""

    instances: list[FakeStream] = []

    def __init__(self, client, session, on_frame, *, on_state=None, **kwargs: Any) -> None:
        self.on_frame = on_frame
        self.on_state = on_state
        self.types = kwargs.get("types")
        self.state = "disconnected"
        self.epoch: str | None = None
        self.last_seq: int | None = None
        self.stopped = False
        self._stop = asyncio.Event()
        FakeStream.instances.append(self)

    async def run(self) -> None:
        self.state = "connected"
        await self._stop.wait()
        self.state = "stopped"

    async def stop(self) -> None:
        self.stopped = True
        self._stop.set()


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    client = create_mock_client()
    with (
        patch("custom_components.opennvr.OpenNVRClient", return_value=client),
        patch("custom_components.opennvr.config_flow.OpenNVRClient", return_value=client),
        # The viewer client (a card-session token) is the same mock here.
        patch("custom_components.opennvr.coordinator.OpenNVRClient", return_value=client),
    ):
        yield client


@pytest.fixture
def mock_stream() -> Generator[type[FakeStream]]:
    FakeStream.instances = []
    with patch("custom_components.opennvr.coordinator.EventStream", FakeStream):
        yield FakeStream


@pytest.fixture
def mock_config_entry():
    return create_mock_config_entry()

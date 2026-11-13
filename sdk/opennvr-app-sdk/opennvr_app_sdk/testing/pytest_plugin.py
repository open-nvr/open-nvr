# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""pytest fixtures over ``opennvr_app_sdk.testing``. Enable with
``pytest_plugins = ["opennvr_app_sdk.testing.pytest_plugin"]`` in
``conftest.py``."""
from __future__ import annotations

import pytest

from . import FakeCore, RecorderChannel, app_config


@pytest.fixture
def recorder() -> RecorderChannel:
    """A fresh alert recorder; ``recorder.dispatcher()`` for the app."""
    return RecorderChannel()


@pytest.fixture
def app_config_factory():
    """``app_config_factory(watch_labels=[...])`` → a config object."""
    return app_config


@pytest.fixture
def fake_core():
    """A running ``FakeCore`` (``fake_core.url``), stopped after the test."""
    core = FakeCore().start()
    try:
        yield core
    finally:
        core.stop()

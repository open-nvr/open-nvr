# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Suite-wide fixtures. camera_agent's app state is module-global, so every
test that touches it must leave the globals as it found them."""
from __future__ import annotations

import pytest

import camera_agent as ca


@pytest.fixture(autouse=True)
def _restore_camera_agent_globals():
    saved = (ca._cfg, ca._auth, ca._brain, ca._stt, ca._tts)
    yield
    ca._cfg, ca._auth, ca._brain, ca._stt, ca._tts = saved

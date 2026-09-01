# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared kai-c test fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_kai_c_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """#371: the lifespan handler persists adapter registrations to
    ``KAI_C_STATE_DIR`` (default ``./kai-c-state``). Point every test at
    its own tmp dir so no test ever writes state into the repo, and no
    test can see another's persisted adapters."""
    monkeypatch.setenv("KAI_C_STATE_DIR", str(tmp_path / "kai-c-state"))

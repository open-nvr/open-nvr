# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Keep the suite off the network.

Any test that sets ``opennvr_url`` makes the alerter look up camera names
for its alert text. Unstubbed, that is a real GET against a host that
does not exist here. The default answer is core's "could not ask" (``[]``);
a test that wants names patches ``lpr.discover_cameras`` itself.
"""
from __future__ import annotations

import pytest

import license_plate_recognition as lpr


@pytest.fixture(autouse=True)
def _no_camera_roster(monkeypatch):
    monkeypatch.setattr(lpr, "discover_cameras", lambda *a, **k: [])

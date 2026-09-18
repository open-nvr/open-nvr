# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Page objects: one module per view.

A GUI test should read as a journey -- "open the cameras page, add a camera,
see it listed" -- and never as a pile of locators. The page objects own the
clicking, so a test says what a person does and the page object knows how.

That split is what makes GUI tests quick to write and survivable to maintain:

* Tests contain **no selectors**. Those live in ``harness/selectors.py``, one
  editable line each.
* Page objects contain **no assertions about product behaviour**. They expose
  state; the test decides what is correct.
* Every awkward thing the UI does -- a native ``window.confirm``, a duplicate
  prompt, a control that only exists on hover -- is absorbed here once, rather
  than being rediscovered by each new test.
"""

from .alerts import AlertsPage
from .base import BasePage
from .cameras import CamerasPage
from .live import LivePage
from .playback import PlaybackPage
from .settings import RecordingSettingsPage
from .shell import Shell
from .vehicles import VehiclesPage

__all__ = [
    "BasePage",
    "Shell",
    "CamerasPage",
    "LivePage",
    "PlaybackPage",
    "VehiclesPage",
    "AlertsPage",
    "RecordingSettingsPage",
]

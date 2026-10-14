# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Weighing-desk rules: no platform imports, testable on their own."""
from .core import (  # noqa: F401
    BarTracker, DeskMonitor, DeskSettings, Zones, draw, gold_mask,
)

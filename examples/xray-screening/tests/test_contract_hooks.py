# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The app overrides the SDK hooks the platform actually calls.

The SDK serves ``GET /state`` from ``state_snapshot()``. A method named
``state()`` is never called: the app page then shows nothing at all,
with no error anywhere. This app shipped that way once, copied from
another app that had the same mistake. Read from source so the test
needs no SDK.
"""
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "xray_screening.py").read_text(encoding="utf-8")


def test_state_is_served_by_the_hook_the_sdk_calls():
    assert "def state_snapshot(self)" in SOURCE
    assert "def state(self)" not in SOURCE


def test_it_says_when_it_cannot_work():
    assert "def not_ready_reason(self)" in SOURCE

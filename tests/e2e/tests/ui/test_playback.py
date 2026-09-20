# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recorded footage, opened and scrubbed by hand.

The API mirror proves segments are indexed and a clip can be exported. This
proves an operator can find the footage, open it, and move the playhead --
which runs through the timeline component, the HLS session lifecycle and the
player, none of which the API tier touches.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from harness import selectors as S
from pages.base import fatal_errors

pytestmark = [pytest.mark.ui, pytest.mark.media]


def test_the_recordings_page_lists_footage(playback_page, recorded_camera):
    """A camera with a completed segment shows up as playable footage.

    ``recorded_camera`` waits for a segment to actually close, which matters:
    a camera deleted seconds after it starts publishing never produces one, and
    the page would then correctly show an empty state that looked like a bug.
    """
    camera, _segment = recorded_camera

    errors = playback_page.console_errors()
    playback_page.open()

    expect(playback_page.page.get_by_text(camera["name"]).first).to_be_visible(
        timeout=60_000
    )
    assert playback_page.has_recordings(), "the recordings page shows an empty state"
    assert not fatal_errors(errors), f"the Recordings page threw: {errors[:3]}"


def test_a_recording_opens_and_the_timeline_can_be_scrubbed(
    playback_page, recorded_camera
):
    """Open the console and drag the playhead.

    The timeline is the hardest element in the app to drive -- bare divs, no
    role, no id, and pointer-capture rather than a click handler -- which is
    exactly why it is worth covering: nothing else in the suite would notice if
    it stopped responding, and no operator could use playback without it.

    Asserted on the HH:MM:SS readout rather than on pixel positions, because
    the readout is what a person actually uses to know where they are.
    """
    camera, _segment = recorded_camera
    playback_page.open()
    playback_page.open_first_recording(camera["name"])

    before = playback_page.playhead_text()
    playback_page.scrub_to(0.6)
    after = playback_page.playhead_text()

    # The readout is locale-formatted as "MM/DD, HH:MM:SS" (fmtFull in
    # PlaybackTimeline.tsx), so search for the clock rather than anchoring at
    # the start of the string.
    assert re.search(r"\d{2}:\d{2}:\d{2}", after or ""), (
        f"the playhead readout is not a time after scrubbing: {after!r}"
    )
    assert after != before or before == "", (
        f"scrubbing did not move the playhead (still {after!r}) -- the track "
        "may no longer respond to pointer events"
    )
    playback_page.close()


def test_the_playback_console_closes(playback_page, recorded_camera):
    """An overlay you cannot dismiss traps the operator on one clip."""
    camera, _segment = recorded_camera
    playback_page.open()
    playback_page.open_first_recording(camera["name"])

    playback_page.close()

    expect(playback_page.find(S.TIMELINE_TRACK)).to_have_count(0, timeout=30_000)

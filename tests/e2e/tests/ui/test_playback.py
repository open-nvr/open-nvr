# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recorded footage, found, scrubbed and exported by hand on the Recordings
page (/playback/sync).

The API mirror proves segments are indexed and a clip can be exported. This
proves an operator can find the footage, move the playhead and take a clip
home -- through the multi-camera timeline, the HLS tiles and the export
strip, none of which the API tier touches.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from pages.base import fatal_errors

pytestmark = [pytest.mark.ui, pytest.mark.media]


def test_the_recordings_page_lists_footage(playback_page, recorded_camera):
    """A camera with a completed segment shows up with footage on its day.

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


def test_the_timeline_can_be_scrubbed(playback_page, recorded_camera):
    """Drag the playhead on the multi-camera timeline.

    The timeline is pointer capture on bare divs, which is why it is worth
    covering: nothing else in the suite would notice if it stopped
    responding, and no operator could use playback without it. Asserted on
    the HH:MM:SS readout, which is what a person uses to know where they are.
    """
    camera, _segment = recorded_camera
    playback_page.open_camera(camera["id"])

    before = playback_page.playhead_text()
    playback_page.scrub_into_footage(0.6)
    after = playback_page.playhead_text()

    assert re.search(r"\d{2}:\d{2}:\d{2}", after or ""), (
        f"the playhead readout is not a time after scrubbing: {after!r}"
    )
    assert after != before or before == "", (
        f"scrubbing did not move the playhead (still {after!r}) -- the track "
        "may no longer respond to pointer events"
    )


def test_a_range_exports_as_an_mp4(playback_page, recorded_camera):
    """Export: mark a range on the timeline, Download, get a real MP4.

    The range starts inside footage on purpose; a start before the first
    sample is the server's job (and the API tier's), not the page's.
    """
    camera, _segment = recorded_camera
    playback_page.open_camera(camera["id"])

    playback_page.start_export()
    playback_page.mark_range_in_footage(0.2, 0.8)
    download = playback_page.download()

    assert download.suggested_filename.endswith(".mp4"), download.suggested_filename
    with open(download.path(), "rb") as fh:
        head = fh.read(12)
    assert b"ftyp" in head, f"the download is not an MP4: {head!r}"

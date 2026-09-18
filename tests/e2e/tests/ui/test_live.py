# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live video: does a picture actually arrive in the browser?

This is the single clearest example of something only a browser can answer.
The API can confirm a stream URL was issued, that MediaMTX has the path and
that bytes are flowing into it -- and every one of those can be true while the
operator stares at a black tile, because the player failed to negotiate WebRTC,
or hls.js never loaded, or the token was rejected at the edge.

"Playing" is asserted as a JavaScript question, never as pixels. A screenshot
comparison would fail on every legitimate style change while happily passing on
a frozen first frame.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from harness import selectors as S
from pages.base import fatal_errors

pytestmark = [pytest.mark.ui, pytest.mark.media]


def test_a_publishing_camera_plays_in_the_browser(live_page, publishing_camera):
    """Frames decode and the clock advances.

    ``publishing_camera`` guarantees bytes are reaching MediaMTX before the
    browser is involved, so a failure here is unambiguously the player or the
    edge -- not the pipeline. That separation is why the API tier is kept.
    """
    live_page.show_only(publishing_camera["id"])
    errors = live_page.console_errors()
    live_page.open()

    current_time = live_page.wait_until_playing()

    assert current_time > 0, "the video element never advanced past zero"
    assert not fatal_errors(errors), f"the Live View threw: {errors[:3]}"


def test_the_live_badge_appears_for_a_playing_camera(live_page, publishing_camera):
    """The operator's own signal that a tile is live.

    Distinct from the video element's state: the badge is what a person reads,
    and it is rendered from the player's own view of the stream. The two can
    disagree, and when they do the UI is lying.
    """
    live_page.show_only(publishing_camera["id"])
    live_page.open()
    live_page.wait_until_playing()

    expect(live_page.find(S.LIVE_BADGE).first).to_be_visible(timeout=30_000)


def test_an_empty_grid_says_so(live_page):
    """With no cameras assigned, the tile explains itself.

    An empty state that renders nothing is indistinguishable from a page that
    failed to load -- for an operator and for a test.
    """
    live_page.page.add_init_script(
        "(() => { try { localStorage.setItem("
        "'liveview-camera-display-order', '[0]') } catch (e) {} })()"
    )
    errors = live_page.console_errors()
    live_page.open()

    expect(live_page.find(S.NO_CAMERA_CHIP).first).to_be_visible(timeout=30_000)
    assert not fatal_errors(errors), f"the Live View threw: {errors[:3]}"

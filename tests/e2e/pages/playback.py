# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recordings and the playback console, including the timeline scrub."""

from __future__ import annotations

from harness import selectors as S
from harness.budgets import BUDGETS

from .base import DEFAULT_TIMEOUT_MS, BasePage


class PlaybackPage(BasePage):
    """The recordings list, and the console it opens."""

    #: The route is /playback but the heading reads "Recordings" -- the sidebar
    #: entry labelled "Recordings" actually points at /playback/sync.
    path = "/playback"
    ready = S.PLAYBACK_HEADING

    def has_recordings(self) -> bool:
        return self.find(S.PLAYBACK_EMPTY).count() == 0

    def open_first_recording(self, camera: str) -> None:
        """Expand this camera's group and play its first day of footage.

        The camera name is required, and the expansion is not optional. The
        Recordings list groups footage under one collapsible button per
        camera and auto-expands only when exactly ONE camera has any -- which
        stops being true the moment a second test leaves a recording behind.
        Past that point every Play button is out of the DOM until the right
        group is opened.

        This used to fall back to clicking ``get_by_role("button").first``,
        which is worth naming as a mistake rather than quietly deleting: on
        this page the first button is the sidebar toggle, so it opened
        navigation, left the group collapsed, and the test then timed out
        against a Play button that was never going to exist. A blind
        ``.first`` click is not a fallback -- it is a different action that
        happens not to raise.
        """
        play = self.find(S.PLAY_RECORDING)
        if play.count() == 0:
            self.find(S.PLAYBACK_CAMERA_GROUP, camera=camera).first.click()
            play = self.find(S.PLAY_RECORDING)
        play.first.click()
        self.find(S.TIMELINE_TRACK).first.wait_for(
            state="visible", timeout=int(BUDGETS.HLS_SESSION_READY * 1000)
        )

    # -- the timeline ----------------------------------------------------
    def scrub_to(self, fraction: float) -> None:
        """Drag the playhead to a fraction of the visible window.

        The timeline is the hardest element in the app to drive: bare ``<div>``
        elements with no role, no label and no id, positioned with inline
        percentages. There is no click handler either -- it is pointer-down,
        move, up with pointer capture -- so a plain ``.click()`` does nothing
        at all. Hence the explicit mouse gestures.

        This is the strongest argument for the ``data-testid`` on the track;
        until that lands the selector falls back to a Tailwind class chain,
        which is exactly the brittleness the testid exists to remove.
        """
        track = self.find(S.TIMELINE_TRACK).first
        track.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        box = track.bounding_box()
        if not box:
            raise AssertionError("the timeline track has no bounding box to scrub")

        x = box["x"] + box["width"] * fraction
        y = box["y"] + box["height"] / 2
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        # Several small moves rather than one jump: the component tracks
        # pointermove, and a single teleport can be coalesced away.
        self.page.mouse.move(x, y, steps=4)
        self.page.mouse.up()

        # Then get the pointer off the track, which is not tidying up -- it is
        # what makes the result readable. The playhead readout renders only
        # while `hoverMs == null`, so the component deliberately swaps it for
        # the hover time chip whenever the pointer is over the timeline. Leave
        # the mouse where the drag ended and the readout is simply not in the
        # DOM, and playhead_text() reports "" for a scrub that worked
        # perfectly.
        self.page.mouse.move(box["x"], box["y"] - 120)

    def playhead_text(self) -> str:
        """The playhead readout above the track, e.g. ``09/09, 14:23:59``.

        Empty when the readout is not rendered, which is a real state and not
        only a missing selector: the component hides it whenever the pointer
        is over the timeline, and whenever the playhead sits outside the
        visible window.
        """
        readout = self.find(S.TIMELINE_PLAYHEAD)
        if readout.count() == 0:
            return ""
        return (readout.first.inner_text() or "").strip()

    def close(self) -> None:
        self.find(S.PLAYBACK_CLOSE).first.click()

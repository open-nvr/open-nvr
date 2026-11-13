# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Recordings page (/playback/sync): the multi-camera timeline, its
scrub, and clip export."""

from __future__ import annotations

from harness import selectors as S
from harness.budgets import BUDGETS

from .base import DEFAULT_TIMEOUT_MS, BasePage


class PlaybackPage(BasePage):
    """The Recordings page every route into footage lands on.

    The old per-camera list at /playback is gone (it redirects here); its
    console and Close button went with it.
    """

    path = "/playback/sync"
    ready = S.PLAYBACK_HEADING

    def has_recordings(self) -> bool:
        return self.find(S.PLAYBACK_EMPTY).count() == 0

    def open_camera(self, camera_id: int) -> None:
        """Open on exactly this camera, through the page's deep link.

        Ticking it in the camera list instead would be a toggle: the page
        pre-selects the latest day's first few cameras, so a click can just
        as well UNtick the one under test. ``?camera=`` selects it and only
        it, which is also what search results and alarms use.
        """
        self.page.goto(f"{self.path}?camera={camera_id}")
        self.find(S.TIMELINE_TRACK).first.wait_for(
            state="visible", timeout=int(BUDGETS.HLS_SESSION_READY * 1000)
        )

    # -- the timeline ----------------------------------------------------
    def _footage_box(self) -> dict:
        """The last block of footage, zoomed to the 10-minute preset when it
        is too thin to aim inside (a minute of footage in the default 24h
        window is under a pixel wide)."""
        block = self.find(S.TIMELINE_FOOTAGE).last
        block.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        box = block.bounding_box()
        if box and box["width"] < 40:
            self.page.get_by_role("button", name="10m", exact=True).click()
            self.page.wait_for_timeout(300)
            box = block.bounding_box()
        if not box:
            raise AssertionError("no footage block on the timeline")
        return box

    def _drag(self, x0: float, x1: float, y: float) -> None:
        """Pointer down, move, up: the timeline has no click handler (it is
        pointer capture), so ``.click()`` does nothing. Several small moves,
        because one teleport can be coalesced away."""
        self.page.mouse.move(x0, y)
        self.page.mouse.down()
        self.page.mouse.move(x1, y, steps=6)
        self.page.mouse.up()

    def _pointer_off_track(self) -> None:
        # The playhead readout renders only while nothing hovers the track;
        # leave the pointer there and playhead_text() reads "" for a scrub
        # that worked.
        track = self.find(S.TIMELINE_TRACK).first.bounding_box()
        if track:
            self.page.mouse.move(track["x"], track["y"] - 120)

    def scrub_into_footage(self, fraction: float) -> None:
        """Drag the playhead to a point INSIDE the last block of footage.

        A drag that lands in a gap snaps to the nearest footage edge by
        design, so 60% of a wide window is no probe at all.
        """
        box = self._footage_box()
        x = box["x"] + max(1, box["width"] * fraction)
        y = box["y"] + box["height"] / 2
        self._drag(x, x, y)
        self._pointer_off_track()

    def playhead_text(self) -> str:
        """The playhead readout above the tracks, e.g. ``10/26, 14:23:59``;
        empty while hovering or when the playhead is outside the window."""
        readout = self.find(S.TIMELINE_PLAYHEAD)
        if readout.count() == 0:
            return ""
        return (readout.first.inner_text() or "").strip()

    # -- export ----------------------------------------------------------
    def start_export(self) -> None:
        self.find(S.EXPORT_TOGGLE).first.click()
        self.find(S.EXPORT_DOWNLOAD).first.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)

    def mark_range_in_footage(self, start: float, end: float) -> None:
        """In export mode a drag marks a range instead of seeking."""
        box = self._footage_box()
        y = box["y"] + box["height"] / 2
        self._drag(box["x"] + box["width"] * start, box["x"] + box["width"] * end, y)
        self._pointer_off_track()

    def download(self):
        """Click Download and return the Playwright Download it starts."""
        with self.page.expect_download(timeout=int(BUDGETS.HLS_SESSION_READY * 1000)) as dl:
            self.find(S.EXPORT_DOWNLOAD).first.click()
        return dl.value

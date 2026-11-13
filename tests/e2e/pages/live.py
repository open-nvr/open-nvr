# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live view -- the one place where "does it play?" is the whole question."""

from __future__ import annotations

import json

from harness import selectors as S
from harness.budgets import BUDGETS
from harness.waiting import eventually

from .base import BasePage

#: Live View fills its tiles from this key, NOT from a query parameter. The
#: Cameras page links to /live?camera=5 and the param is simply ignored, so
#: seeding this is the only deterministic way to put a known camera on screen.
DISPLAY_ORDER_KEY = "liveview-camera-display-order"
GRID_MODE_KEY = "liveview-grid-mode"

#: HTMLMediaElement.HAVE_CURRENT_DATA -- there is a decoded frame at the
#: current position. The app itself uses this threshold before taking a
#: snapshot, so it is the product's own definition of "there is a picture".
HAVE_CURRENT_DATA = 2


class LivePage(BasePage):
    """The live grid."""

    path = "/live"
    ready = S.LIVE_HEADING

    def show_only(self, camera_id: int) -> "LivePage":
        """Pin one camera into the first tile before the page loads.

        Must run as an init script: the display order is read during the first
        render, so writing it afterwards has no effect until a reload.
        """
        order = json.dumps([camera_id])
        self.page.add_init_script(
            "(() => { try {"
            f" localStorage.setItem('{DISPLAY_ORDER_KEY}', '{order}');"
            f" localStorage.setItem('{GRID_MODE_KEY}', 'fit');"
            " } catch (e) {} })()"
        )
        return self

    def video(self):
        return self.find(S.VIDEO).first

    def wait_until_playing(self, budget: float | None = None) -> float:
        """Wait until the video has real decoded frames advancing.

        Deliberately a JavaScript question rather than a DOM one. There is no
        class or attribute that means "playing", and a screenshot comparison
        would be worse than useless -- it would fail on every legitimate style
        change while passing on a frozen first frame.

        ``readyState >= 2`` says a frame is decoded; ``currentTime > 0`` says
        the clock is moving. Both are needed: a stalled stream can satisfy the
        first indefinitely.
        """
        video = self.video()
        video.wait_for(state="visible", timeout=int(BUDGETS.QUICK * 1000))

        eventually(
            lambda: video.evaluate("v => v.readyState"),
            until=lambda state: state >= HAVE_CURRENT_DATA,
            budget=budget or BUDGETS.STREAM_RECEIVING,
            describe="the video element to have a decoded frame (readyState >= 2)",
        )
        return eventually(
            lambda: video.evaluate("v => v.currentTime"),
            until=lambda t: t > 0,
            budget=budget or BUDGETS.STREAM_RECEIVING,
            describe="the video clock to start advancing (currentTime > 0)",
        )

    def transport(self) -> str:
        """Which transport the player settled on: ``webrtc`` or ``hls``."""
        chip = self.find(S.TRANSPORT_CHIP)
        if chip.count() == 0:
            return ""
        return (chip.first.inner_text() or "").strip().lower()

    def switch_transport(self) -> None:
        """Flip between WebRTC and HLS.

        Only a button when both transports are available; otherwise it is a
        plain span and this is a no-op the caller should skip on.
        """
        self.find(S.TRANSPORT_CHIP).first.click()

    def has_empty_tile(self) -> bool:
        return self.find(S.NO_CAMERA_CHIP).count() > 0

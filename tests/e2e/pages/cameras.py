# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Cameras list and its Add-Camera dialog."""

from __future__ import annotations

from harness import selectors as S
from harness.budgets import BUDGETS

from .base import DEFAULT_TIMEOUT_MS, BasePage


class CamerasPage(BasePage):
    """Add, find and remove cameras the way an operator does."""

    path = "/cameras"
    ready = S.CAMERAS_HEADING

    # -- reading ---------------------------------------------------------
    def row(self, name: str):
        """The table row for a camera, found by its name.

        The table is a real ``<table>`` and is **not** virtualised, so every
        row is in the DOM and a plain filter is safe here. (The Network/IDS
        alert table is the one exception in this app -- see AlertsPage.)
        """
        return self.find(S.CAMERA_ROW).filter(has_text=name)

    def is_listed(self, name: str) -> bool:
        return self.row(name).count() > 0

    def wait_until_listed(self, name: str, budget: float | None = None) -> None:
        self.wait_for_count(
            S.CAMERA_ROW,
            until=lambda _n: self.is_listed(name),
            budget=budget or BUDGETS.STREAM_READY,
        )

    # -- the add-camera journey ------------------------------------------
    def open_add_dialog(self) -> None:
        """Open the Add Camera dialog and wait for it to be on screen.

        ``Add Camera`` is genuinely ambiguous -- the page header, the empty
        state and the dialog's own submit button all carry that name. Clicking
        the first is right for *opening*, since both page-level buttons open
        the same dialog.

        An earlier version derived a scope from the heading
        (``locator("div").filter(has=heading).last``) and passed it to every
        subsequent lookup. That was too clever: the modal has no role and no
        id, so "the container" resolved to whichever nested div happened to
        match last, which did not always contain the tabs -- the Manual tab
        then timed out inside a scope that could never hold it.

        Everything inside the dialog is unique on the page anyway once it is
        open (the Discover tab's Username/Password are mutually exclusive with
        Manual's), so only the submit button needs disambiguating, and ``.last``
        does that: the dialog footer comes after the page content in DOM order.
        """
        self.find(S.ADD_CAMERA_OPEN).first.click()
        self.find(S.ADD_CAMERA_DIALOG).wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )

    def add_camera(self, *, name: str, ip: str, rtsp: str) -> None:
        """Fill the Manual tab and submit, handling the duplicate prompt.

        Two details are load-bearing:

        * The dialog opens on **Discover**, which immediately starts an ONVIF
          network scan. Switching to Manual cancels it, so doing that first
          keeps the test deterministic instead of racing a scan.
        * The RTSP field is filled **last**: blurring any other field runs
          ``syncIdentity``, which rewrites it. Fill it earlier and the value
          silently changes underneath you.
        """
        self.open_add_dialog()

        self.find(S.TAB_MANUAL).click()
        self.find(S.FIELD_CAMERA_NAME).fill(name)
        self.find(S.FIELD_IP).fill(ip)
        self.find(S.FIELD_RTSP).fill(rtsp)

        self.find(S.ADD_CAMERA_SUBMIT).last.click()
        self._confirm_duplicate_if_prompted()

    def _confirm_duplicate_if_prompted(self) -> None:
        """Click through the "already added" prompt when it appears.

        Every fake camera is served from one IP, so the duplicate guard fires
        on all but the first -- the prompt is the normal path here, not an edge
        case. While it is open the ordinary footer row is hidden, so there is
        exactly one Cancel on screen and no stray Add Camera.
        """
        prompt = self.find(S.DUPLICATE_PROMPT)
        try:
            prompt.first.wait_for(state="visible", timeout=3000)
        except Exception:
            return  # no duplicate: the create already succeeded
        self.find(S.ADD_ANYWAY).click()

    # -- destructive -----------------------------------------------------
    def delete_camera(self, name: str) -> None:
        """Delete via the row action, accepting the native confirm.

        Without the dialog handler Playwright dismisses the confirm, the delete
        quietly does not happen, and the test fails later on a row that is
        still present with nothing pointing at the cause.
        """
        self.accept_native_dialogs()
        self.find(S.CAMERA_ROW_DELETE, scope=self.row(name), name=name).first.click()

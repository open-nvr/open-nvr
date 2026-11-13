# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A camera tour running on a display board (/live/tour/:id)."""

from __future__ import annotations

from harness import selectors as S

from .base import BasePage


class TourPage(BasePage):
    """The tour wall. Build one per tour: the path carries its id."""

    ready = S.TOUR_WALL

    def __init__(self, page, tour_id: int) -> None:
        super().__init__(page)
        self.path = f"/live/tour/{tour_id}"

    def tile_names(self) -> list[str]:
        """Camera names on the page now showing, in tile order."""
        return [n.strip() for n in self.find(S.TOUR_TILE_NAME).all_inner_texts()]

    def page_label(self) -> str:
        return self.find(S.TOUR_PAGE_LABEL).first.inner_text().strip()

    def press(self, selector) -> None:
        """Click a button on the wall's bar.

        The bar fades a few seconds after the mouse last moved and takes no
        clicks while hidden, as on a real board, and Playwright checks what
        is under the pointer before it moves the mouse, so a bare click on a
        faded bar lands on a tile every retry. Move the mouse over the wall
        first, as a person would."""
        box = self.find(S.TOUR_WALL).bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.page.mouse.move(x, y)
        self.page.mouse.move(x + 8, y + 8)
        self.find(selector).first.click()

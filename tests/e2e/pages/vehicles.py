# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Vehicles page -- where plate reads become something an operator sees."""

from __future__ import annotations

from harness import selectors as S
from harness.budgets import BUDGETS

from .base import BasePage


class VehiclesPage(BasePage):
    """Plate reads, their evidence photos and the vehicle history modal."""

    path = "/vehicles"
    ready = S.VEHICLES_HEADING

    def has_reads(self) -> bool:
        return self.find(S.VEHICLES_EMPTY).count() == 0

    def plate(self, plate: str):
        """The cell for one plate.

        It is a ``<button>`` (it opens the vehicle-history modal), so it has a
        role and does not need a text-content search.
        """
        return self.find(S.PLATE_CELL, plate=plate)

    def wait_for_plate(self, plate: str, budget: float | None = None) -> int:
        return self.wait_for_count(
            S.PLATE_CELL,
            until=lambda _n: self.plate(plate).count() > 0,
            budget=budget or BUDGETS.PLATE_ENRICHED,
            plate=plate,
        )

    def open_history(self, plate: str) -> None:
        self.plate(plate).first.click()

    def search(self, text: str) -> None:
        self.find(S.PLATE_SEARCH).fill(text)

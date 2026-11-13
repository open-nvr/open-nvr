# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Offset paging helpers shared by the paginated read endpoints.

One function, because one rule is easy to get subtly wrong and it is
worth having in a single tested place rather than inlined per router.
"""
from __future__ import annotations

from typing import Callable


def resolve_total(page_len: int, skip: int, limit: int,
                  count_fn: Callable[[], int]) -> int:
    """How many rows match — without a COUNT when the page proves it.

    A page SHORTER than the limit means the result set ended inside it,
    so the total is arithmetic: ``skip + page_len``. That short-circuit
    is what keeps a filtered total affordable on an endpoint the UI
    polls every ten seconds — the common request (a list smaller than
    one page) pays nothing at all.

    The trap, and the reason this is a function rather than an inline
    conditional: an EMPTY page at ``skip > 0`` proves only that the
    total is <= skip, NOT that it IS skip. A client that jumped past
    the end would otherwise be told "of 500" for a twelve-row table —
    a wrong number that looks entirely plausible. That case must fall
    through to the real count.
    """
    if page_len < limit and (skip == 0 or page_len > 0):
        return skip + page_len
    return count_fn()

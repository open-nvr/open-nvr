# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Every file of the SDK says Apache-2.0, the licence it ships under.

LICENSING.md promises an app developer that nothing in this package is
AGPL. Nine files once said otherwise, a header copied from core. The
app template is left out: what it writes into a new app (``__LICENSE__``,
AGPL-3.0 by default) is that developer's choice, not the SDK's.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "opennvr_app_sdk" / "templates"
SOURCES = sorted(
    p for d in ("opennvr_app_sdk", "tests")
    for p in (ROOT / d).rglob("*.py")
    if TEMPLATES not in p.parents
)


def test_there_are_sources_to_check():
    assert len(SOURCES) > 50


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_the_header_is_apache(path):
    head = path.read_text(encoding="utf-8").splitlines()[:5]
    assert "# SPDX-License-Identifier: Apache-2.0" in head
    assert not any("AGPL" in line or "Affero" in line for line in head)

"""The fixture copies here must match the server's (the source of truth in the
monorepo); after the repo split this test skips."""

from __future__ import annotations

from pathlib import Path

import pytest

from . import FIXTURES


def test_fixtures_match_the_servers_copy() -> None:
    server = Path(__file__).resolve().parents[4] / "server" / "contract" / "fixtures"
    if not server.is_dir():
        pytest.skip("not in the OpenNVR monorepo")
    for f in server.glob("*.json"):
        assert (FIXTURES / f.name).read_text(encoding="utf-8") == f.read_text(
            encoding="utf-8"), f.name

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""/health names the build: the updater tells two builds of one version apart by it."""

from __future__ import annotations

import asyncio


def test_health_reports_the_build_commit(monkeypatch):
    import main

    monkeypatch.setenv("OPENNVR_BUILD_COMMIT", "0123abcd" * 5)
    body = asyncio.run(main.health_check())
    assert body["version"] == main.__version__
    assert body["commit"] == "0123abcd" * 5


def test_a_local_build_reports_no_commit(monkeypatch):
    import main

    monkeypatch.setenv("OPENNVR_BUILD_COMMIT", "")
    assert asyncio.run(main.health_check())["commit"] is None
    monkeypatch.delenv("OPENNVR_BUILD_COMMIT")
    assert asyncio.run(main.health_check())["commit"] is None

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Every uv.lock that takes the app SDK from the repo (an editable path
dependency) pins the SDK's current version.

The agent depends on the SDK that way and its image build runs
`uv export --locked`, which refuses when the lock's SDK version differs
from the SDK's own. The 0.12.0 bump (2026-11-13) was pushed without
refreshing the agent's lock, and `camera-agent:main` went red until it
was. This fails the PR instead. Apps that pin a released SDK from PyPI
are not this test's business: a pin to 0.7.0 there is a choice."""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _sdk_version() -> str:
    text = (ROOT / "sdk/opennvr-app-sdk/opennvr_app_sdk/_version.py").read_text()
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def test_every_editable_lock_of_the_app_sdk_matches_its_version():
    want = _sdk_version()
    stale = []
    for lock in ROOT.rglob("uv.lock"):
        if ".venv" in lock.parts or "node_modules" in lock.parts:
            continue
        text = lock.read_text()
        m = re.search(r'name = "opennvr-app-sdk"\nversion = "([^"]+)"\nsource = \{ editable = ', text)
        if m and m.group(1) != want:
            stale.append(f"{lock.relative_to(ROOT)} pins {m.group(1)}")
    assert not stale, (
        f"the app SDK is {want} but these lockfiles take it from the repo at another version — run `uv lock` there: "
        + "; ".join(stale))

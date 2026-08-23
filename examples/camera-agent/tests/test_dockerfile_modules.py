# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Dockerfile copies agent modules by EXPLICIT list — safe against
shipping stray files, but a newly added module silently missing from the
image imports fine in dev and 500s in production (field case: /system-check
raised ModuleNotFoundError because system_check.py wasn't COPYed). This
test pins the invariant: every top-level agent module is in the Dockerfile."""
from __future__ import annotations

from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]


def test_every_agent_module_is_copied_into_the_image():
    dockerfile = (AGENT_DIR / "Dockerfile").read_text()
    missing = [
        py.name
        for py in sorted(AGENT_DIR.glob("*.py"))
        if f"/{py.name} " not in dockerfile and f"/{py.name}\n" not in dockerfile
        and f"{py.name} " not in dockerfile
    ]
    assert not missing, (
        f"module(s) not COPYed into the camera-agent image: {missing} — "
        f"add COPY lines to examples/camera-agent/Dockerfile"
    )

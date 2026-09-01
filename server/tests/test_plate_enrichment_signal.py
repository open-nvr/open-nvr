# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Issue #371, the "why it's silent" half: a plate read that 404s/403s
at KAI-C must produce an operator-visible WARNING — rate-limited so a
busy gate camera doesn't turn one dead adapter into a log flood.

Before this, a missing OCR adapter was a per-event DEBUG line: a stack
whose restart had unregistered ``fast_plate_ocr`` was indistinguishable
from a stack with no plates in frame.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

import services.plate_enrichment as pe  # noqa: E402


def _reset():
    pe._last_missing_adapter_warn = None


def test_missing_adapter_warns_once(caplog):
    _reset()
    with caplog.at_level(logging.WARNING, logger="plate_enrichment"):
        pe._warn_adapter_missing(404)
        pe._warn_adapter_missing(404)   # inside the rate window — silent
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "fast_plate_ocr" in msg
    assert "404" in msg
    assert "FAILING" in msg


def test_first_warn_fires_even_on_a_freshly_booted_host(caplog, monkeypatch):
    """Regression: the limiter compared against an initial 0.0, and
    time.monotonic() counts from HOST BOOT — on a machine up for less
    than the interval (every fresh CI VM, any rebooted NVR host) the
    FIRST warning was silently swallowed. The sentinel must be None."""
    _reset()
    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: 5.0)  # "booted 5s ago"
    with caplog.at_level(logging.WARNING, logger="plate_enrichment"):
        pe._warn_adapter_missing(404)
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


def test_warn_fires_again_after_the_window(caplog, monkeypatch):
    _reset()
    with caplog.at_level(logging.WARNING, logger="plate_enrichment"):
        pe._warn_adapter_missing(404)
        # Simulate the window elapsing.
        pe._last_missing_adapter_warn -= (
            pe._MISSING_ADAPTER_WARN_INTERVAL_SECONDS + 1
        )
        pe._warn_adapter_missing(403)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "403" in warnings[1].getMessage()


def test_enrichment_calls_the_warn_on_non_200():
    """Lockstep: the warn helper must actually be wired into the non-200
    branch of the shared OCR client (_ocr_jpeg — used by both the
    enrichment sweep and the early-attempt path) — a helper nothing
    calls is the silence bug back again. String-level (the function
    needs a live DB + httpx stack to execute; the call-site is what
    we're pinning)."""
    src = (REPO_ROOT / "server" / "services" / "plate_enrichment.py").read_text()
    body = src.split("async def _ocr_jpeg", 1)[1]
    assert "_warn_adapter_missing(resp.status_code)" in body

# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenNVR Tier-0 detection pipeline (compute-gated inference).

See ``docs/design/compute-gated-inference.md``. This package holds the
always-on, low-risk detection path: pull the detect substream from MediaMTX,
hardware-decode it, and (in later commits) run motion → region → cheap detector
→ tracker. Nothing here gates the expensive tier — that lands in PR B behind
its safety rails.
"""

__all__ = ["__version__"]

__version__ = "0.0.1"

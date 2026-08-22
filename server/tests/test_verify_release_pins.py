# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the release-pin verifier's variable substitution.

The failure this guards (PR #289's red gate): compose supports NESTED
defaults — ``${CAPTION_ADAPTER_TAG:-${ADAPTER_TAG:-latest}}``, used by the
camera-agent caption image — but the verifier's original regex stopped a
default at the first ``}``, resolving the ref to ``…-adapter:latest}`` with
a stray trailing brace. GHCR then 404s on the mangled tag and the gate
fails even though every image exists. Pure-function tests; no network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from verify_release_pins import substitute  # noqa: E402


CAPTION_REF = (
    "ghcr.io/open-nvr/${CAPTION_ADAPTER:-moondream}-adapter:"
    "${CAPTION_ADAPTER_TAG:-${ADAPTER_TAG:-latest}}"
)


def test_nested_default_pinned_outer_var_wins():
    pins = {"CAPTION_ADAPTER": "ollamavlm",
            "CAPTION_ADAPTER_TAG": "latest", "ADAPTER_TAG": "0.1.3"}
    assert substitute(CAPTION_REF, pins) == \
        "ghcr.io/open-nvr/ollamavlm-adapter:latest"


def test_nested_default_blank_outer_falls_through_to_inner_pin():
    # CAPTION_ADAPTER_TAG= (blank) means "follow ADAPTER_TAG" — the
    # documented way to re-pin the caption image once a release ships it.
    pins = {"CAPTION_ADAPTER": "ollamavlm",
            "CAPTION_ADAPTER_TAG": "", "ADAPTER_TAG": "0.1.3"}
    assert substitute(CAPTION_REF, pins) == \
        "ghcr.io/open-nvr/ollamavlm-adapter:0.1.3"


def test_nested_default_nothing_pinned_uses_innermost_default():
    assert substitute(CAPTION_REF, {"CAPTION_ADAPTER": "ollamavlm"}) == \
        "ghcr.io/open-nvr/ollamavlm-adapter:latest"


def test_flat_default_unchanged():
    assert substitute("ghcr.io/open-nvr/core:${CORE_TAG:-latest}",
                      {"CORE_TAG": "0.1.3"}) == "ghcr.io/open-nvr/core:0.1.3"
    assert substitute("ghcr.io/open-nvr/core:${CORE_TAG:-latest}", {}) == \
        "ghcr.io/open-nvr/core:latest"


def test_no_stray_braces_survive():
    resolved = substitute(CAPTION_REF, {})
    assert "}" not in resolved and "${" not in resolved

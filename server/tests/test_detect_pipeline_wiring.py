# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Every Tier-0 knob the code reads must reach the container that runs it.

``docker-compose.yml`` has no ``env_file:`` — each service enumerates its
environment by hand, so a variable added to the code and to
``.env.example`` is still inert in a compose install until someone
remembers the third list. Nothing checked that, and it silently ate a
shipped knob: ``DETECT_MOTION_MAX_FORCED_EXITS`` was added to the code
and documented at length in ``.env.example`` while never being passed to
the container, so the motion-gate latch could not be tuned or disabled
by any operator on a compose install.

The failure is invisible from the outside — the code falls back to its
own default, the service starts clean, and the log says nothing — which
is exactly why it needs a test rather than a review habit.

Deliberately string-level (no yaml dependency in this suite; same style
as test_lpr_adapter_wiring and test_tier0_metrics' compose guard).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG = REPO_ROOT / "detect-pipeline" / "detect_pipeline"
_COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
_ENV_EXAMPLE = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

#: Read by the code but intentionally NOT passed to the container.
#: Every entry needs a reason — "we forgot" is the bug this test exists
#: to catch, so an unexplained addition here defeats the whole guard.
DELIBERATELY_UNPLUMBED = {
    # Dead on the shipped DETECT_DETECTOR=onnx path: run.py's
    # `model_size = cfg.onnx_input if cfg.detector in ("onnx", "rfdetr")`
    # overrides it before use. Plumbing it would ship a knob that
    # silently does nothing — the same defect in a new place.
    "DETECT_MODEL_SIZE",
    # Metrics label only, derived from the model path when unset; no
    # operator reason to override it in a compose install.
    "DETECT_MODEL_ID",
}


def _read_by_code() -> set[str]:
    """Every DETECT_* env name the pipeline package looks up."""
    names: set[str] = set()
    for path in sorted(_PKG.glob("*.py")):
        names |= set(re.findall(r'["\'](DETECT_[A-Z0-9_]+)["\']',
                                path.read_text(encoding="utf-8")))
    return names


def _passed_to_container() -> set[str]:
    """Env names enumerated on the detect-pipeline service."""
    lines = _COMPOSE.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith("  detect-pipeline:"))
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^  [a-z0-9_-]+:", lines[i])), len(lines))
    block = "\n".join(lines[start:end])
    return set(re.findall(r"^\s+- ([A-Z0-9_]+)=", block, re.M))


def test_the_service_block_is_actually_found():
    """Guard the guard: a compose refactor that renames the service must
    fail loudly here, not silently reduce every assertion below to a
    comparison of two empty sets."""
    passed = _passed_to_container()
    assert len(passed) > 20, (
        "found only %d env vars on the detect-pipeline service — the "
        "block parser is no longer matching docker-compose.yml, so the "
        "wiring assertions below are vacuous" % len(passed))
    assert "DETECT_FPS" in passed


def test_every_knob_the_code_reads_reaches_the_container():
    missing = sorted(_read_by_code() - _passed_to_container()
                     - DELIBERATELY_UNPLUMBED)
    assert not missing, (
        "read by detect_pipeline but never passed to the container — "
        "setting these in .env does nothing, and the code silently uses "
        "its own default instead: " + ", ".join(missing) + ". Add them to "
        "the detect-pipeline service's environment: block in "
        "docker-compose.yml, or to DELIBERATELY_UNPLUMBED with a reason.")


def test_every_documented_knob_reaches_the_container():
    """The direction that would have caught the shipped regression.

    A variable can be plumbed but undocumented (harmless), never the
    other way round: documenting a knob in .env.example promises an
    operator it works.
    """
    documented = set(re.findall(r"(?m)^(DETECT_[A-Z0-9_]+)=", _ENV_EXAMPLE))
    broken = sorted(documented - _passed_to_container()
                    - DELIBERATELY_UNPLUMBED)
    assert not broken, (
        ".env.example documents these knobs but docker-compose.yml never "
        "passes them to detect-pipeline, so following the documentation "
        "has no effect: " + ", ".join(broken))


def test_the_motion_gate_latch_is_tunable():
    """The specific regression, pinned by name.

    The latch decides when Tier-0 gives up gating a scene with no static
    background. Its default is sane, so a broken knob looks like nothing
    is wrong until an operator tries to change it on a camera where the
    default is wrong.
    """
    assert "DETECT_MOTION_MAX_FORCED_EXITS" in _passed_to_container(), (
        "the motion-gate latch threshold is not passed to the container; "
        "operators cannot tune or disable the latch on a compose install")

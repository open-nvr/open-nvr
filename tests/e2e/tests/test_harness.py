# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guards on the harness itself.

A test suite is also software, and the parts of this one that parse or sequence
things can be wrong in ways that look like product bugs. When the harness
breaks, it does not fail honestly — it reports something misleading about
OpenNVR, which is worse than not running at all.

Everything here is pure Python: no stack, no network, milliseconds. They run in
the ``smoke`` tier so a broken harness is caught before the tests that depend on
it start producing nonsense.

Each case below pins a mistake that was actually made, not a hypothetical one.
"""

from __future__ import annotations

import pytest

from harness.clips import (
    QUIET_LEAD_SECONDS,
    SYNTHETIC_CLIPS,
    ffmpeg_command,
    real_clips,
    synthetic_names,
)
from harness.compose import parse_setup_token
from harness.guards import _adapter_names
from harness.sandbox import E2E_PREFIX, new_sandbox
from harness.waiting import WaitTimeout, eventually

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# The setup-token parser
# ---------------------------------------------------------------------------
BANNER = """\
2026-09-07 05:22:47,118 INFO success: kai-c entered RUNNING state
================================================================
 OpenNVR first-time setup token (one-time use)
----------------------------------------------------------------
  RW0my12O9RNN5s8ZkSrB3GWk46E2T7paBtC19Kzpfo0
----------------------------------------------------------------
 Pass this token in the `setup_token` field of
 POST /auth/first-time-setup. It is consumed on first
================================================================
"""


def test_the_setup_token_is_read_from_the_banner():
    assert parse_setup_token(BANNER) == "RW0my12O9RNN5s8ZkSrB3GWk46E2T7paBtC19Kzpfo0"


def test_the_banner_rule_is_not_mistaken_for_the_token():
    """The regression this file exists for.

    The banner draws its rules from 64 '-' characters, which satisfy a naive
    ``[A-Za-z0-9_-]{20,}`` exactly as well as a real token does. The first
    version of the parser returned the dashes, bootstrap sent them as the
    setup token, and the server answered 403 — a failure that pointed at
    authentication rather than at a string match, and cost a full stack boot
    to track down.
    """
    token = parse_setup_token(BANNER)

    assert token is not None
    assert set(token) != {"-"}, "the parser returned the banner's rule line"
    assert sum(char.isalnum() for char in token) >= 8


def test_no_banner_means_no_token():
    """Absence is normal — it means the admin account is already claimed —
    so it must be reported as None rather than raising."""
    assert parse_setup_token("nothing to see here\njust logs\n") is None
    assert parse_setup_token("") is None


# ---------------------------------------------------------------------------
# Sandbox teardown
# ---------------------------------------------------------------------------
def test_cleanups_run_in_reverse_order():
    """LIFO, because later entities reference earlier ones.

    A camera permission references a user and a camera, so it has to be
    removed before either. Creation order reversed gets that right for free;
    a fixed teardown sequence would have to be maintained forever.
    """
    order: list[str] = []
    box = new_sandbox("test::ordering")
    box.track("camera", lambda: order.append("camera"))
    box.track("user", lambda: order.append("user"))
    box.track("permission", lambda: order.append("permission"))

    assert box.close() == []
    assert order == ["permission", "user", "camera"]


def test_one_failing_cleanup_does_not_block_the_rest():
    """Teardown is best-effort and reported.

    If the first undo could abort the rest, a single stale entity would leak
    everything created before it, and the tests that broke would be later ones
    that never touched any of it.
    """
    survived: list[str] = []
    box = new_sandbox("test::resilience")
    box.track("first", lambda: survived.append("first"))
    box.track("explodes", _raise)
    box.track("last", lambda: survived.append("last"))

    failures = box.close()

    assert survived == ["last", "first"], "a failing cleanup stopped the others"
    assert len(failures) == 1
    assert "explodes" in failures[0], f"the failure is not identifiable: {failures}"


def _raise() -> None:
    raise RuntimeError("boom")


def test_namespaces_are_unique_and_greppable():
    """The namespace is the correlation token.

    It appears in the User-Agent of every request and in every entity name, so
    it has to be unique per test and carry the shared prefix the end-of-run
    isolation check looks for.
    """
    first = new_sandbox("test::a")
    second = new_sandbox("test::b")

    assert first.namespace != second.namespace
    assert first.namespace.startswith(E2E_PREFIX)
    assert first.name("gate").startswith(first.namespace)
    assert len(first.name("gate")) <= 100, "camera names are capped at 100 chars"


# ---------------------------------------------------------------------------
# eventually()
# ---------------------------------------------------------------------------
def test_eventually_returns_as_soon_as_the_condition_holds():
    calls = {"n": 0}

    def probe() -> int:
        calls["n"] += 1
        return calls["n"]

    assert eventually(
        probe, until=lambda n: n >= 3, budget=5, describe="three calls", interval=0.01
    ) == 3
    assert calls["n"] == 3, "it kept polling after the condition was met"


def test_a_timeout_reports_what_it_last_saw():
    """"Timed out" is a shrug; the last value is a diagnosis."""
    with pytest.raises(WaitTimeout) as caught:
        eventually(
            lambda: {"items": []},
            until=lambda payload: bool(payload["items"]),
            budget=0.2,
            describe="a visit row to appear",
            interval=0.05,
        )

    message = str(caught.value)
    assert "a visit row to appear" in message
    assert "'items': []" in message, "the last observed value is missing"
    assert "E2E_BUDGET_" in message, "the message does not say how to raise the budget"


def test_an_exception_in_the_probe_is_treated_as_not_yet():
    """A 404 while a resource is still provisioning is a legitimate 'not yet'.

    Failing on the first one would make every wait racy against startup.
    """
    calls = {"n": 0}

    def probe() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("not provisioned yet")
        return "ready"

    assert eventually(
        probe, budget=5, describe="the resource to provision", interval=0.01
    ) == "ready"


def test_a_probe_that_never_stops_raising_reports_the_error():
    with pytest.raises(WaitTimeout) as caught:
        eventually(
            _raise,
            budget=0.2,
            describe="something that never works",
            interval=0.05,
        )

    assert "RuntimeError: boom" in str(caught.value)


# ---------------------------------------------------------------------------
# Clip specs
# ---------------------------------------------------------------------------
def test_generated_clips_open_with_a_quiet_lead():
    """The shape Tier-0's motion gate requires.

    The gate must calibrate on a comparatively quiet frame before it will ever
    run the detector. A clip that moves from frame one never calibrates, the
    detector never runs, and every downstream assertion fails for a reason
    that has nothing to do with the code under test. The lead is encoded in
    the filter expression, so this pins it there.
    """
    moving = [spec for spec in SYNTHETIC_CLIPS if spec.filters]
    assert moving, "no moving clip is specified"

    for spec in moving:
        assert f"lt(t,{QUIET_LEAD_SECONDS})" in spec.filters, (
            f"{spec.name} does not hold still for the first "
            f"{QUIET_LEAD_SECONDS}s: {spec.filters}"
        )
        assert spec.seconds > QUIET_LEAD_SECONDS, (
            f"{spec.name} ends before it ever moves"
        )


def test_the_ffmpeg_command_renders_a_playable_file():
    """Encoding choices the rest of the stack depends on.

    yuv420p and libx264 are what lets the rig stream-copy the clip and what
    every decoder downstream accepts; the output path is what the rig then
    finds. Cheap to assert, and each has broken a pipeline before.
    """
    spec = SYNTHETIC_CLIPS[0]
    argv = ffmpeg_command(spec, out_dir="/out")

    assert argv[0] == "ffmpeg"
    assert "-pix_fmt" in argv and argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    assert "libx264" in argv
    assert argv[-1] == f"/out/{spec.name}.mp4"


def test_real_and_generated_clips_are_distinguishable(tmp_path):
    """The detection tier depends on telling them apart.

    Asking for real footage and silently getting a generated clip would mean
    a detection test that waits out its whole budget and then blames OpenNVR
    for finding nothing in a picture that never contained anything.
    """
    (tmp_path / "e2e-motion.mp4").write_bytes(b"x")
    (tmp_path / "somebody-real.mp4").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")

    found = {path.name for path in real_clips(tmp_path)}

    assert found == {"e2e-motion.mp4", "somebody-real.mp4"}, "non-video files leaked in"
    assert "e2e-motion" in synthetic_names()
    assert "somebody-real" not in synthetic_names()


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def test_adapter_names_are_read_from_every_shape_kai_c_uses():
    """KAI-C's adapter listing has more than one envelope in the wild.

    Reading the wrong one returns an empty set, which makes the guard claim
    the adapter is missing and skip a test that would have passed — a false
    negative that is much harder to notice than a false alarm.
    """
    expected = {"fast_plate_ocr", "yolov8"}

    assert _adapter_names({"adapters": [{"name": "fast_plate_ocr"}, {"name": "yolov8"}]}) == expected
    assert _adapter_names({"items": [{"name": "fast_plate_ocr"}, {"name": "yolov8"}]}) == expected
    assert _adapter_names([{"name": "fast_plate_ocr"}, {"name": "yolov8"}]) == expected
    assert _adapter_names(["fast_plate_ocr", "yolov8"]) == expected
    assert _adapter_names({"fast_plate_ocr": {}, "yolov8": {}}) == expected


def test_no_adapters_reads_as_empty_not_as_an_error():
    assert _adapter_names({"adapters": []}) == set()
    assert _adapter_names([]) == set()

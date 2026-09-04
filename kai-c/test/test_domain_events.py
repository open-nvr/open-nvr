# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""RFC-0002 Phase 0 normaliser: completions → domain events.

The contract under test is docs/EVENT_CONTRACTS.md: subject shape
(camera last), the full envelope every time, accepted-reads-only for
plate.recognized.v1, and normalise-never-raises. The publisher-side
guarantee (best-effort, counted, never raises upward) is covered by
test_nats_publisher.py; here we add only the domain-publish method's
happy path and disabled short-circuit.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kai_c.domain_events import NORMALISERS, normalise_completion
from kai_c.nats_publisher import NatsPublisher

PLATE_RESULT = {"accepted": True, "plate_text": "abc 1234", "confidence": 0.91}


# ── normalise_completion: mapping + skip cases ─────────────────────


def test_fast_plate_ocr_maps_to_plate_recognized_v1():
    out = normalise_completion(
        "fast_plate_ocr", PLATE_RESULT,
        camera_id="cam-front", correlation_id="corr-1", event_id=42,
    )
    assert out is not None
    subject, env = out
    assert subject == "opennvr.events.plate.recognized.v1.cam-front"
    # The envelope: every field, every time (EVENT_CONTRACTS.md).
    assert env["schema"] == "plate.recognized.v1"
    assert env["correlation_id"] == "corr-1"
    assert env["camera_id"] == "cam-front"
    assert env["producer"] == "kai-c"
    assert env["id"].startswith("evt_") and len(env["id"]) == 16
    assert env["ts"].endswith("+00:00")
    assert env["payload"] == {
        "plate_text": "ABC1234",       # normalised: upper, no spaces
        "confidence": 0.91,
        "vehicle_label": None,
        "event_id": 42,
    }


def test_unmapped_adapter_produces_no_domain_event():
    assert normalise_completion(
        "yolov8", {"detections": []}, camera_id="cam-1",
    ) is None


def test_no_camera_id_skips_a_domain_event_needs_its_camera():
    assert normalise_completion(
        "fast_plate_ocr", PLATE_RESULT, camera_id=None,
    ) is None
    assert normalise_completion(
        "fast_plate_ocr", PLATE_RESULT, camera_id="",
    ) is None


@pytest.mark.parametrize("result", [
    {"accepted": False, "plate_text": "ABC1234"},   # adapter's own verdict
    {"plate_text": ""},                              # empty read
    {"plate_text": "   "},                           # whitespace-only
    {"plate_text": 1234},                            # wrong type
    {},                                              # nothing at all
    "not-a-dict",                                    # malformed body
    None,
])
def test_only_accepted_reads_fire(result):
    assert normalise_completion(
        "fast_plate_ocr", result, camera_id="cam-1",
    ) is None


def test_confidence_only_when_numeric():
    for raw, expected in [(0.5, 0.5), (1, 1), ("high", None), (True, None), (None, None)]:
        out = normalise_completion(
            "fast_plate_ocr",
            {"plate_text": "XYZ", "confidence": raw},
            camera_id="cam-1",
        )
        assert out is not None
        assert out[1]["payload"]["confidence"] == expected


def test_normalise_never_raises():
    # A normaliser bug must not hurt the infer path. Break the mapped
    # normaliser and confirm the wrapper swallows it.
    def boom(*a, **k):
        raise RuntimeError("normaliser bug")
    original = NORMALISERS["fast_plate_ocr"]
    NORMALISERS["fast_plate_ocr"] = boom
    try:
        assert normalise_completion(
            "fast_plate_ocr", PLATE_RESULT, camera_id="cam-1",
        ) is None
    finally:
        NORMALISERS["fast_plate_ocr"] = original


# ── publish_domain_event: same best-effort semantics ───────────────


def _publisher_with_fake_client() -> tuple[NatsPublisher, AsyncMock]:
    pub = NatsPublisher(
        url="nats://127.0.0.1:4222", token=None, sovereignty_mode="local_only")
    client = MagicMock()
    client.publish = AsyncMock()
    pub._client = client
    return pub, client


@pytest.mark.asyncio
async def test_publish_domain_event_publishes_json_envelope():
    pub, client = _publisher_with_fake_client()
    subject, env = normalise_completion(
        "fast_plate_ocr", PLATE_RESULT, camera_id="cam-1",
    )
    assert await pub.publish_domain_event(subject, env) is True
    (sent_subject, sent_payload), _ = client.publish.call_args
    assert sent_subject == subject
    import json
    assert json.loads(sent_payload.decode()) == env
    assert pub.published_count == 1


@pytest.mark.asyncio
async def test_publish_domain_event_disabled_short_circuits():
    pub = NatsPublisher(url=None, token=None, sovereignty_mode="local_only")
    assert await pub.publish_domain_event("opennvr.events.x.y.v1.c", {}) is False


@pytest.mark.asyncio
async def test_publish_domain_event_failure_counts_never_raises():
    pub, client = _publisher_with_fake_client()
    client.publish = AsyncMock(side_effect=RuntimeError("bus down"))
    assert await pub.publish_domain_event(
        "opennvr.events.plate.recognized.v1.cam-1", {"correlation_id": "c"},
    ) is False
    assert pub.failed_count == 1
    assert pub._client is None  # forced rebuild on next call


def test_plate_box_is_forwarded_when_the_adapter_localised_the_plate():
    """The consumer that stores the plate needs the geometry to reject
    PARTIAL reads — a crop whose edge cuts the plate still OCRs the
    surviving characters at high confidence. KAI-C forwards, never judges:
    it does not hold the crop the box is measured against."""
    result = dict(PLATE_RESULT, plate_detection={
        "found": True, "confidence": 0.87, "box": [847, 463, 1076, 550],
    })
    out = normalise_completion("fast_plate_ocr", result, camera_id="cam1",
                               correlation_id=None, event_id=7)
    assert out is not None
    _, env = out
    assert env["payload"]["plate_box"] == [847, 463, 1076, 550]


def test_plate_box_is_omitted_when_absent_or_malformed():
    """Additive-only: the field simply does not appear, so a consumer on
    the old contract sees exactly what it saw before."""
    _, env = normalise_completion("fast_plate_ocr", PLATE_RESULT,
                                  camera_id="cam1", correlation_id=None,
                                  event_id=7)
    assert "plate_box" not in env["payload"]
    for bad in ({"found": False}, {"box": None}, {"box": [1, 2, 3]}, {"box": "x"}):
        _, env = normalise_completion(
            "fast_plate_ocr", dict(PLATE_RESULT, plate_detection=bad),
            camera_id="cam1", correlation_id=None, event_id=7)
        assert "plate_box" not in env["payload"]


def test_plate_box_confidence_is_forwarded_for_false_localisations(monkeypatch):
    """#386: the localiser's own doubt is the only signal that separates
    a plate from a manufacturer badge. The badge OCRs into plausible
    characters, from a box nowhere near a crop edge, so the consumer
    cannot reconstruct this from anything else we send. With the
    publish-side floor disabled the doubt is forwarded for the consumer
    to judge; with it on (the default) the read is not published at all."""
    result = dict(PLATE_RESULT, plate_detection={
        "attempted": True, "found": True, "confidence": 0.3756,
        "box": [121, 229, 233, 267],
    })
    monkeypatch.setenv("KAI_C_PLATE_MIN_DETECTION_CONFIDENCE", "0")
    _, env = normalise_completion("fast_plate_ocr", result, camera_id="cam1",
                                  correlation_id=None, event_id=7)
    assert env["payload"]["plate_box_confidence"] == 0.3756
    assert env["payload"]["plate_text"] == "ABC1234"
    monkeypatch.delenv("KAI_C_PLATE_MIN_DETECTION_CONFIDENCE")
    assert normalise_completion("fast_plate_ocr", result, camera_id="cam1",
                                correlation_id=None, event_id=7) is None


def test_junk_reads_are_never_published():
    """A subscriber acting on plate.recognized.v1 (the LPR app's
    "Unknown vehicle" alarm, a barrier) must never see a fragment, a
    badge, or a read off the car body. Each was previously published and
    filtered by ONE consumer while the others alerted on it."""
    def _out(detection):
        return normalise_completion(
            "fast_plate_ocr", dict(PLATE_RESULT, plate_detection=detection),
            camera_id="cam1", correlation_id=None, event_id=7)

    # clipped: box abuts the crop edge (x1 == 0)
    assert _out({"attempted": True, "found": True, "confidence": 0.9,
                 "box": [0, 40, 120, 70], "image_size": [400, 300]}) is None
    # clipped on the far edge (x2 == width)
    assert _out({"attempted": True, "found": True, "confidence": 0.9,
                 "box": [300, 40, 400, 70], "image_size": [400, 300]}) is None
    # weak localisation: a badge
    assert _out({"attempted": True, "found": True, "confidence": 0.37,
                 "box": [100, 100, 200, 130], "image_size": [400, 300]}) is None
    # not localised: the localiser looked and found nothing
    assert _out({"attempted": True, "found": False, "confidence": None,
                 "box": None, "image_size": [400, 300]}) is None
    # a whole, well-localised plate still flows
    out = _out({"attempted": True, "found": True, "confidence": 0.9,
                "box": [100, 100, 200, 130], "image_size": [400, 300]})
    assert out is not None and out[1]["payload"]["plate_text"] == "ABC1234"
    # no opinion at all (OCR-only adapter) still flows, exactly as before
    assert _out({"attempted": False, "found": False}) is not None


def test_require_localisation_is_operator_tunable(monkeypatch):
    monkeypatch.setenv("KAI_C_PLATE_REQUIRE_LOCALISATION", "0")
    out = normalise_completion(
        "fast_plate_ocr", dict(PLATE_RESULT, plate_detection={
            "attempted": True, "found": False}),
        camera_id="cam1", correlation_id=None, event_id=7)
    assert out is not None


def test_plate_box_confidence_is_omitted_when_absent_or_malformed():
    """Additive-only, like plate_box: an OCR-only adapter that never
    localises must not start looking like a low-confidence one."""
    _, env = normalise_completion("fast_plate_ocr", PLATE_RESULT,
                                  camera_id="cam1", correlation_id=None,
                                  event_id=7)
    assert "plate_box_confidence" not in env["payload"]
    for bad in ({"found": False}, {"confidence": None},
                {"confidence": "0.9"}, {"confidence": True}):
        _, env = normalise_completion(
            "fast_plate_ocr", dict(PLATE_RESULT, plate_detection=bad),
            camera_id="cam1", correlation_id=None, event_id=7)
        assert "plate_box_confidence" not in env["payload"], bad

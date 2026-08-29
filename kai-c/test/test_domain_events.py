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

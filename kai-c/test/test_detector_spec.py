# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract v1.1 detector-spec parsing (KAI-C consumer side).

The detector block is optional and backward-compatible: existing adapters that
never send it must keep parsing (detector -> None), and an adapter that does send
it must round-trip the accelerator + input spec.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from kai_c.contract_types import CapabilitiesResponse, DetectorSpec


def _base() -> dict:
    return {
        "adapter": {
            "name": "yolo",
            "version": "1.0.0",
            "vendor": "opennvr",
            "license": "AGPL-3.0-or-later",
            "supported_contract_versions": ["1"],
        },
        "model": {"name": "yolov8n", "version": "8.0", "framework": "ultralytics"},
        "endpoints": {
            "infer": {"supported": True},
            "infer_stream": {"supported": False},
        },
        "scheduling": {"max_inflight": 1},
    }


def test_capabilities_without_detector_parses_and_defaults_none():
    caps = CapabilitiesResponse.model_validate(_base())
    assert caps.detector is None            # non-detector adapters unaffected


def test_capabilities_with_detector_round_trips():
    payload = _base()
    payload["detector"] = {
        "input": {"width": 320, "height": 320, "pixel_format": "rgb"},
        "accelerator": {"backend": "openvino", "device": "GPU"},
        "labels": ["person", "car"],
        "max_detections": 20,
    }
    caps = CapabilitiesResponse.model_validate(payload)
    assert caps.detector is not None
    assert caps.detector.input.width == 320 and caps.detector.input.height == 320
    assert caps.detector.accelerator.backend == "openvino"
    assert caps.detector.labels == ["person", "car"]


def test_unknown_accelerator_backend_still_parses():
    # forward-compat: a backend KAI-C doesn't know about must not fail to parse
    spec = DetectorSpec.model_validate(
        {"input": {"width": 640, "height": 640}, "accelerator": {"backend": "some-new-npu"}}
    )
    assert spec.accelerator.backend == "some-new-npu"
    assert spec.accelerator.device is None
    assert spec.max_detections == 20        # default


def test_detector_requires_input_dimensions():
    with pytest.raises(ValidationError):
        DetectorSpec.model_validate({"accelerator": {"backend": "cpu"}})  # no input

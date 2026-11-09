# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Hardware panel tells the truth about accelerators: a box whose
detector, captioner and language model run on the Hexagon used to read
"Running on CPU only — an NVIDIA (CUDA) GPU would speed up…" (Ventuno Q,
2026-11-09)."""
from __future__ import annotations

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, accelerator_from_tier0
from context import CameraSpec

QNN = ('tier0_detector_backend{backend="ort",requested="QNNExecutionProvider(htp)",'
       'effective="QNNExecutionProvider, CPUExecutionProvider"} 1\n')


def test_accelerator_from_the_detectors_gauge():
    assert accelerator_from_tier0(QNN) == "Qualcomm Hexagon NPU"
    assert accelerator_from_tier0('tier0_detector_backend{effective="OpenVINOExecutionProvider:NPU"} 1') == "Intel NPU"
    assert accelerator_from_tier0('tier0_detector_backend{effective="CUDAExecutionProvider"} 1') == "NVIDIA GPU"
    assert accelerator_from_tier0('tier0_detector_backend{effective="CPUExecutionProvider"} 1') is None
    assert accelerator_from_tier0('tier0_detector_backend{effective="QNNExecutionProvider"} 0\n') is None
    assert accelerator_from_tier0("") is None


def _runtime(**over):
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t", text_mode=True,
                    synthetic_detection=True, bestframe_base_url="http://tier0:9109",
                    cameras=[CameraSpec(camera_id="c", frame_url="synth:people=0", role="r")], **over)
    return CameraAgentRuntime(cfg)


def test_panel_names_the_npu_and_what_runs_on_it(monkeypatch):
    rt = _runtime(caption_adapter="litertlm", llm_provider="openai",
                  llm_base_url="http://caption-adapter:9006", llm_model="gemma-4-e2b-npu")
    monkeypatch.setattr(rt, "detector_accelerator", lambda: "Qualcomm Hexagon NPU")
    hw = rt.hardware_recommendation()
    assert hw["running_on"] == "npu" and hw["accelerator"] == "Qualcomm Hexagon NPU"
    assert hw["summary"].startswith("Running on the Qualcomm Hexagon NPU:")
    assert "CUDA" not in hw["summary"]
    llm = [r for r in hw["rows"] if r["task"] == "llm"][0]
    assert llm["accelerated"] and llm["recommended"] == "" and "caption-adapter" in llm["adapters"][0]
    det = [r for r in hw["rows"] if r["task"] in ("object_detection", "detection")]
    assert det and all(r["accelerated"] for r in det)
    assert not any("smaller model" in t for t in hw["tips"])


def test_panel_on_a_cpu_box_reads_as_before(monkeypatch):
    rt = _runtime()
    monkeypatch.setattr(rt, "detector_accelerator", lambda: None)
    hw = rt.hardware_recommendation()
    assert hw["running_on"] in ("cpu", "unknown") and hw["accelerator"] is None
    assert not any(r.get("accelerated") for r in hw["rows"])
    assert any("smaller model" in t for t in hw["tips"])

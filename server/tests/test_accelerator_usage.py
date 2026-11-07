# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The NPU busy share the dashboard shows: inference seconds over wall seconds,
from the detector's latency sum and KAI-C's per-adapter latency sums."""
from services.accelerator_usage import AcceleratorUsage


def _fill(template: str, **values) -> str:
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template

DETECT = '''
tier0_detector_backend{backend="ort",requested="QNNExecutionProvider:htp",effective="QNNExecutionProvider:htp"} 1
tier0_detector_latency_seconds_sum{camera="cam23",model="yolo26n"} {det}
tier0_detector_latency_seconds_count{camera="cam23",model="yolo26n"} 100
'''
KAIC = '''
kaic_proxy_infer_latency_seconds_sum{adapter="litertlm"} {cap}
kaic_proxy_infer_latency_seconds_sum{adapter="whisper"} {stt}
'''


def test_busy_share_is_inference_seconds_over_wall_seconds():
    u = AcceleratorUsage()
    first = u.update(_fill(DETECT, det=10.0), _fill(KAIC, cap=5.0, stt=2.0), at=1000.0)
    assert first == [{"kind": "npu", "name": "Qualcomm Hexagon NPU", "busy_percent": None,
                      "users": [], "source": "inference-time"}], "no window yet"
    # 15 s later: the detector computed 1.5 s, the captioner 3 s, whisper (CPU) 4 s
    out = u.update(_fill(DETECT, det=11.5), _fill(KAIC, cap=8.0, stt=6.0), at=1015.0)
    assert len(out) == 1 and out[0]["kind"] == "npu"
    assert out[0]["busy_percent"] == 30.0
    assert out[0]["users"] == [{"name": "detector", "percent": 10.0}, {"name": "litertlm", "percent": 20.0}]
    assert all(u_["name"] != "whisper" for u_ in out[0]["users"]), "a CPU adapter is not charged to the NPU"


def test_nothing_on_an_accelerator_means_no_entry():
    u = AcceleratorUsage()
    cpu = DETECT.replace('effective="QNNExecutionProvider:htp"', 'effective="CPUExecutionProvider"')
    assert u.update(_fill(cpu, det=1.0), None, at=1.0) == []
    assert u.update(None, None, at=2.0) == []


def test_intel_openvino_devices_are_told_apart():
    u = AcceleratorUsage()
    gpu = DETECT.replace("QNNExecutionProvider:htp", "OpenVINOExecutionProvider:GPU")
    u.update(_fill(gpu, det=0.0), None, at=0.0)
    out = u.update(_fill(gpu, det=3.0), None, at=10.0)
    assert out[0]["kind"] == "gpu" and out[0]["name"] == "Intel GPU" and out[0]["busy_percent"] == 30.0

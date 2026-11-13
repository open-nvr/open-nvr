# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""How much one AI step may spend, by hardware class.

RFC #620's presets: on a GPU 6 tool calls in 45 s, on CPU 4 in 120 s.
Measured on 4 vCPU (#617, #649): qwen3:1.7b takes 9-12 s a step, so the
CPU preset fits Look with room. No CPU model yet qualifies for
Investigate; granite4:micro, the default since October 2026, is the
closest (44/60, examples/camera-agent/MODELS_AND_LATENCY.md).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    tool_calls: int
    seconds: float

    def __post_init__(self) -> None:
        if self.tool_calls < 0 or self.seconds <= 0:
            raise ValueError("limits must be positive")

    @classmethod
    def gpu(cls) -> Limits:
        return cls(tool_calls=6, seconds=45.0)

    @classmethod
    def cpu(cls) -> Limits:
        return cls(tool_calls=4, seconds=120.0)

    @classmethod
    def for_hardware(cls, hardware: str) -> Limits:
        """``"gpu"`` or ``"cpu"``; anything else is CPU, the safe side."""
        return cls.gpu() if hardware.strip().lower() == "gpu" else cls.cpu()

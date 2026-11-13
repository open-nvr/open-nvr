# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Core depends on the in-repo SDK.

The Agentic Workflows engine (RFC #620) runs its AI step on
``opennvr_app_sdk.agent``. ``server/pyproject.toml`` takes the SDK from
``../sdk/opennvr-app-sdk``, and the Dockerfile installs it into the image
(``uv sync --no-editable``). If either drifts, this is the test that
says so — not a workflow failing at runtime.
"""
from __future__ import annotations


def test_core_can_import_the_agent_library():
    from opennvr_app_sdk.agent import LLMClient, OllamaChat, OpenAIChat

    assert isinstance(OllamaChat("http://ollama:11434", model="m"), LLMClient)
    assert isinstance(OpenAIChat("http://llm/v1", model="m"), LLMClient)

# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The Agentic Workflows engine (RFC #620, §8).

This first piece runs an AI step inside a run and records it:
:mod:`.ai_steps`. The registry, the validator over a whole workflow file,
triggers, the other step kinds and the routers come next; the AI step's
schema, checks and executor are the SDK's (``opennvr_app_sdk.workflows``),
so the engine, the builder and outside tools agree on them.
"""

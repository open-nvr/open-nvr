# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The lite agent must not invent past events.

QA: with a person sitting live, lite referenced "old events" — but lite has NO
event store or history at all. The cause is a small LLM confabulating from the
rolling chat history. These lock in the grounding guardrail (system prompt) and
the trimmed history window that keep it answering only about the current view.
"""
from __future__ import annotations

import services


def test_system_prompt_forbids_inventing_past_events():
    p = services.SYSTEM_PROMPT.lower()
    # The guardrail phrases must be present — a silent removal reopens the bug.
    assert "no memory of past events" in p
    assert "no history" in p
    assert "current view" in p
    assert "never reference" in p or "never recall" in p


def test_history_window_is_short():
    # Long history is what a 3B model confabulates from; keep it to 3 exchanges.
    import re
    m = re.search(r"del self\._history\[:-(\d+)\]", open("services.py").read())
    assert m and int(m.group(1)) <= 6


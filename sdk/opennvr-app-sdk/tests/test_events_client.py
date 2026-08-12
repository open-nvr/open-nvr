# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""EventsClient — the SDK's read primitive for the platform's memory."""

import asyncio
import json

from opennvr_app_sdk import EventsClient


def _client(responses):
    calls = []

    async def http_get(url, headers):
        calls.append((url, headers))
        return responses.pop(0)

    c = EventsClient("http://core:8000", "sekret", http_get=http_get)
    return c, calls


def test_search_builds_url_and_parses():
    body = json.dumps({"events": [
        {"id": 42, "camera_id": 3, "label": "person", "score": 0.91,
         "started_at": "2026-08-12T15:12:04+00:00",
         "ended_at": "2026-08-12T15:14:11+00:00",
         "stationary": False, "has_evidence": True},
        {"junk": "row is skipped, not fatal"},
    ]}).encode()
    c, calls = _client([(200, body)])
    rows = asyncio.run(c.search(label="person", start="2026-08-12T15:00",
                                end="2026-08-12T16:00"))
    url, headers = calls[0]
    assert "/api/v1/internal/camera-agent/events?" in url
    assert "label=person" in url and "from=2026-08-12T15%3A00" in url
    assert headers == {"X-Internal-Api-Key": "sekret"}
    assert len(rows) == 1 and rows[0].id == 42 and rows[0].has_evidence


def test_search_returns_none_on_failure_not_empty():
    # None = "couldn't check"; [] = "nothing came". Different answers.
    async def boom(url, headers):
        raise OSError("core down")
    c = EventsClient("http://core:8000", None, http_get=boom)
    assert asyncio.run(c.search(label="car")) is None
    c2, _calls = _client([(422, b"bad range")])
    assert asyncio.run(c2.search(label="car")) is None
    c3, _calls = _client([(200, b'{"events": []}')])
    assert asyncio.run(c3.search(label="car")) == []


def test_evidence_roundtrip_and_miss():
    c, _ = _client([(200, b"\xff\xd8jpg"), (404, b"")])
    assert asyncio.run(c.evidence(42)) == b"\xff\xd8jpg"
    assert asyncio.run(c.evidence(43)) is None
